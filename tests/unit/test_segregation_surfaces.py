"""Privacy boundary on the ancillary link/suggest surfaces (PR6 regression).

The core boundary (list / dashboard / budgets / review-queue rows) is covered in
test_visibility.py + test_auth.py. These guard the transfer, plan, loan, and
embeddings-neighbour paths — each links, suggests, or lists transactions and must
never surface or accept another member's private row.
"""

from collections.abc import Callable

from sqlmodel import Session

from expense_analyzer.models import (
    Account,
    Category,
    Loan,
    PlannedItem,
    Scope,
    Transaction,
    TxSource,
)
from expense_analyzer.queries.categorize.classifier import confirmed_label_texts
from expense_analyzer.queries.core import users
from expense_analyzer.queries.money import transfers as transfer_q
from expense_analyzer.queries.planning import budgets as budget_q
from expense_analyzer.queries.planning import loans as loan_q
from expense_analyzer.queries.planning import planned as plan_q


def test_transfers_never_expose_or_link_another_members_private(
    db_session: Session,
    account: Account,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
):
    alice = users.create_user(db_session, username="alice", name="A", password="pw")
    bob = users.create_user(db_session, username="bob", name="B", password="pw")
    other = make_account(name="mBank")
    out = make_transaction(
        account_id=account.id, amount=-500, day=1, owner_id=alice.id, scope=Scope.private
    )
    inc = make_transaction(
        account_id=other.id, amount=500, day=2, owner_id=alice.id, scope=Scope.private
    )

    # Bob never sees Alice's private legs as candidates, and can't link them (IDOR).
    bob_candidates = {t.id for t in transfer_q.unmatched_candidates(db_session, viewer_id=bob.id)}
    assert out.id not in bob_candidates and inc.id not in bob_candidates
    assert (
        transfer_q.link_transfer(db_session, tx_a_id=out.id, tx_b_id=inc.id, viewer_id=bob.id)
        is None
    )
    # Alice can link her own private transfer; Bob still sees no groups.
    group = transfer_q.link_transfer(db_session, tx_a_id=out.id, tx_b_id=inc.id, viewer_id=alice.id)
    assert group is not None
    assert not transfer_q.list_transfer_groups(db_session, viewer_id=bob.id)


def test_plan_link_viewer_scoped(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
    make_planned_item: Callable[..., PlannedItem],
):
    alice = users.create_user(db_session, username="alice", name="A", password="pw")
    bob = users.create_user(db_session, username="bob", name="B", password="pw")
    item = make_planned_item(name="Rent")
    alices_private = make_transaction(
        account_id=account.id, amount=-200_000, day=3, owner_id=alice.id, scope=Scope.private
    )
    bobs_private = make_transaction(
        account_id=account.id, amount=-200_000, day=4, owner_id=bob.id, scope=Scope.private
    )
    shared = make_transaction(account_id=account.id, amount=-200_000, day=5)  # household default

    # Distinct months so each attempt is an independent (item, month) upsert.
    def _link(tx_id: int, month: str, viewer_id: int) -> bool:
        return plan_q.link_transaction(
            db_session, planned_item_id=item.id, month=month, tx_id=tx_id, viewer_id=viewer_id
        )

    # Viewer-scoped: Alice links her own private tx; Bob's private is invisible to her (IDOR).
    assert _link(alices_private.id, "2026-05", alice.id) is True
    assert _link(bobs_private.id, "2026-06", alice.id) is False
    # A household tx is linkable by anyone.
    assert _link(shared.id, "2026-07", bob.id) is True


def test_loan_link_payment_viewer_scoped(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
    make_loan: Callable[..., Loan],
):
    alice = users.create_user(db_session, username="alice", name="A", password="pw")
    bob = users.create_user(db_session, username="bob", name="B", password="pw")
    loan = make_loan(account_id=account.id)
    private = make_transaction(
        account_id=account.id, amount=-100_000, day=5, owner_id=alice.id, scope=Scope.private
    )

    # Bob can't pin Alice's private tx (IDOR); Alice can pin her own.
    assert (
        loan_q.link_payment(
            db_session, loan_id=loan.id, tx_id=private.id, installment_index=1, viewer_id=bob.id
        )
        is False
    )
    assert (
        loan_q.link_payment(
            db_session, loan_id=loan.id, tx_id=private.id, installment_index=1, viewer_id=alice.id
        )
        is True
    )


def test_loan_reconciliation_viewer_sees_own_private_payment(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
    make_loan: Callable[..., Loan],
):
    """The upgrade regression: a legacy payment backfilled as private/admin-owned must
    reconcile as paid for its owner — household-only scoping would hide it and the
    installment would wrongly render unpaid."""
    admin = users.create_user(db_session, username="admin", name="Admin", password="pw")
    loan = make_loan(account_id=account.id)
    payment = make_transaction(
        account_id=account.id, amount=-2_600_000, day=15, owner_id=admin.id, scope=Scope.private
    )
    assert (
        loan_q.link_payment(
            db_session, loan_id=loan.id, tx_id=payment.id, installment_index=1, viewer_id=admin.id
        )
        is True
    )

    # The owner sees the payment -> installment 1 reconciles as paid.
    owner_recon = loan_q.loan_reconciliation(db_session, loan.id, viewer_id=admin.id)
    assert owner_recon is not None
    assert owner_recon.rows[0].payment is not None
    # Household-only (no viewer) can't see the private row -> still unpaid.
    household_recon = loan_q.loan_reconciliation(db_session, loan.id)
    assert household_recon is not None
    assert household_recon.rows[0].payment is None


def test_private_budgets_are_owner_isolated(
    db_session: Session,
    make_category: Callable[..., Category],
):
    """A private budget belongs to one member: another member never sees it in the
    list, can't fetch or delete it (IDOR), and each member holds their own private
    limit for the same category/month."""
    alice = users.create_user(db_session, username="alice", name="A", password="pw")
    bob = users.create_user(db_session, username="bob", name="B", password="pw")
    food = make_category(name="Food")

    shared = budget_q.set_budget(
        db_session, category_id=food.id, month=None, limit_amount=500_00, scope=Scope.household
    )
    alices = budget_q.set_budget(
        db_session,
        category_id=food.id,
        month=None,
        limit_amount=200_00,
        scope=Scope.private,
        viewer_id=alice.id,
    )

    # The list is viewer-scoped: Bob sees the household budget, never Alice's private.
    bob_ids = {b.id for b in budget_q.list_budgets(db_session, viewer_id=bob.id)}
    assert shared.id in bob_ids and alices.id not in bob_ids
    assert {b.id for b in budget_q.list_budgets(db_session, viewer_id=alice.id)} == {
        shared.id,
        alices.id,
    }

    # IDOR gate: Bob can't fetch or delete Alice's private budget; Alice can fetch it.
    assert budget_q.get_budget(db_session, alices.id, viewer_id=bob.id) is None
    assert budget_q.delete_budget(db_session, alices.id, viewer_id=bob.id) is False
    assert budget_q.get_budget(db_session, alices.id, viewer_id=alice.id) is not None

    # Two members each hold their own private limit for the same category/month.
    bobs = budget_q.set_budget(
        db_session,
        category_id=food.id,
        month=None,
        limit_amount=300_00,
        scope=Scope.private,
        viewer_id=bob.id,
    )
    assert bobs.id != alices.id
    assert bobs.owner_id == bob.id and alices.owner_id == alice.id


def test_confirmed_labels_scoped_for_neighbours_not_the_classifier(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
):
    alice = users.create_user(db_session, username="alice", name="A", password="pw")
    bob = users.create_user(db_session, username="bob", name="B", password="pw")
    food = make_category(name="Food")
    make_transaction(
        account_id=account.id,
        amount=-500,
        day=6,
        category_id=food.id,
        owner_id=alice.id,
        scope=Scope.private,
        source=TxSource.manual,
        merchant_normalized="ALICE-PRIVATE-SHOP",
    )

    # Bob's neighbour index (viewer-scoped) never contains Alice's private label...
    bob_texts = [t for t, _ in confirmed_label_texts(db_session, viewer_id=bob.id)]
    assert not any("ALICE-PRIVATE-SHOP" in t for t in bob_texts)
    # ...but the classifier's model (unscoped) still trains on it — it exposes no text
    # to a user, only a category on that user's own row.
    all_texts = [t for t, _ in confirmed_label_texts(db_session)]
    assert any("ALICE-PRIVATE-SHOP" in t for t in all_texts)
