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


def test_plan_link_refuses_private_but_accepts_household(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
    make_planned_item: Callable[..., PlannedItem],
):
    alice = users.create_user(db_session, username="alice", name="A", password="pw")
    item = make_planned_item(name="Rent")
    private = make_transaction(
        account_id=account.id, amount=-200_000, day=3, owner_id=alice.id, scope=Scope.private
    )
    shared = make_transaction(account_id=account.id, amount=-200_000, day=4)  # household default

    # The plan is household-shared: a private tx can't be linked; a household one can.
    def _link(tx_id: int) -> bool:
        return plan_q.link_transaction(
            db_session, planned_item_id=item.id, month="2026-05", tx_id=tx_id
        )

    assert _link(private.id) is False
    assert _link(shared.id) is True


def test_loan_link_payment_refuses_another_members_private(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
    make_loan: Callable[..., Loan],
):
    alice = users.create_user(db_session, username="alice", name="A", password="pw")
    loan = make_loan(account_id=account.id)
    private = make_transaction(
        account_id=account.id, amount=-100_000, day=5, owner_id=alice.id, scope=Scope.private
    )
    linked = loan_q.link_payment(db_session, loan_id=loan.id, tx_id=private.id, installment_index=1)
    assert linked is False


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
