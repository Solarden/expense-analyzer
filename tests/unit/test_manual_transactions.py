"""Manual (cash) entry, edit and delete — the single-row edit layer (Phase 13).

These exercise the query layer directly; the endpoint wiring is covered in
tests/api/test_transaction_edit.py.
"""

from collections.abc import Callable
from datetime import date

from sqlmodel import Session

from expense_analyzer.models import Account, Category, CategoryKind, Scope, Transaction, TxSource
from expense_analyzer.queries import transactions
from expense_analyzer.queries.transactions import (
    MANUAL_BATCH_SOURCE,
    TransactionFilters,
)


def _make_manual(db_session, account, **kw) -> Transaction:
    defaults = dict(
        account_id=account.id,
        booked_date=date(2026, 5, 10),
        amount=-2000,
        description="Coffee",
        category_id=None,
        scope=Scope.private,
        note=None,
        owner_id=None,
    )
    defaults.update(kw)

    return transactions.create_manual_transaction(db_session, **defaults)


def test_create_manual_transaction_sets_manual_source_and_uuid_fingerprint(
    db_session: Session, account: Account
):
    tx = _make_manual(db_session, account, amount=-1999, description="Lunch", note="with Anna")

    assert tx.source is TxSource.manual
    assert tx.amount == -1999
    assert tx.note == "with Anna"
    assert tx.merchant_normalized == "LUNCH"  # manual descriptions are normalized too
    # fingerprint is a uuid hex (32 chars), not a content hash — see query docstring
    assert len(tx.fingerprint) == 32
    assert transactions.is_manual_entry(db_session, tx) is True


def test_two_identical_cash_entries_both_persist(db_session: Session, account: Account):
    """The whole point of the uuid fingerprint: two genuinely-distinct identical
    cash operations must NOT be deduped against each other."""
    a = _make_manual(db_session, account, amount=-2000, description="Coffee")
    b = _make_manual(db_session, account, amount=-2000, description="Coffee")

    assert a.id != b.id
    assert a.fingerprint != b.fingerprint
    page = transactions.list_transactions(db_session, TransactionFilters(), page=1, page_size=10)
    assert page.total == 2


def test_manual_batch_is_reused_and_counts(db_session: Session, account: Account):
    _make_manual(db_session, account)
    _make_manual(db_session, account)

    batch_a = transactions.ensure_manual_batch(db_session)
    # Both entries share the one manual batch.
    assert batch_a.source == MANUAL_BATCH_SOURCE
    assert batch_a.record_count == 2


def test_is_manual_entry_false_for_imported(
    db_session: Session, account: Account, make_transaction: Callable[..., Transaction]
):
    imported = make_transaction(account_id=account.id, amount=-500)  # default "test" batch

    assert transactions.is_manual_entry(db_session, imported) is False


def test_update_manual_rewrites_money_fields(
    db_session: Session,
    account: Account,
    make_account: Callable[..., Account],
    make_category: Callable[..., Category],
):
    other = make_account(name="Cash 2")
    food = make_category(name="Food", kind=CategoryKind.expense)
    tx = _make_manual(db_session, account, amount=-2000, description="Coffe")  # typo

    updated = transactions.update_transaction(
        db_session,
        tx_id=tx.id,
        category_id=food.id,
        scope=Scope.household,
        note="fixed typo",
        account_id=other.id,
        booked_date=date(2026, 6, 1),
        amount=-2500,
        description="Coffee",
    )

    assert updated is not None
    assert updated.account_id == other.id
    assert updated.amount == -2500
    assert updated.booked_date == date(2026, 6, 1)
    assert updated.raw_description == "Coffee"
    assert updated.merchant_normalized == "COFFEE"  # re-normalized from new description
    assert updated.category_id == food.id
    assert updated.scope is Scope.household
    assert updated.note == "fixed typo"


def test_update_imported_leaves_bank_fields_untouched(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
):
    """Editing an imported row passes no money fields — amount/date/description are
    the bank's source of truth and must stay put; only category/scope/note change."""
    food = make_category(name="Food", kind=CategoryKind.expense)
    tx = make_transaction(
        account_id=account.id,
        amount=-777,
        booked_date=date(2026, 5, 3),
        raw_description="BIEDRONKA",
    )

    updated = transactions.update_transaction(
        db_session, tx_id=tx.id, category_id=food.id, scope=Scope.household, note="groceries"
    )

    assert updated is not None
    assert updated.amount == -777  # untouched
    assert updated.raw_description == "BIEDRONKA"  # untouched
    assert updated.booked_date == date(2026, 5, 3)  # untouched
    assert updated.category_id == food.id
    assert updated.note == "groceries"
    assert updated.source is TxSource.manual  # a human touched it


def test_soft_delete_hides_row_and_is_idempotent(db_session: Session, account: Account):
    tx = _make_manual(db_session, account)

    deleted = transactions.soft_delete_transaction(db_session, tx_id=tx.id)
    assert deleted is not None
    assert deleted.deleted_at is not None

    # Gone from the list, and no longer loadable as a live row.
    page = transactions.list_transactions(db_session, TransactionFilters(), page=1, page_size=10)
    assert page.total == 0
    assert transactions.get_transaction(db_session, tx.id) is None
    # Deleting again is a no-op (returns None, not a crash).
    assert transactions.soft_delete_transaction(db_session, tx_id=tx.id) is None


def test_get_transaction_missing_returns_none(db_session: Session):
    assert transactions.get_transaction(db_session, 9999) is None


def test_set_note_sets_clears_and_leaves_source_untouched(
    db_session: Session, account: Account, make_transaction: Callable[..., Transaction]
):
    """A note is an annotation, not categorization — set_note must NOT flip the
    row's source (unlike set_category / update_transaction)."""
    tx = make_transaction(account_id=account.id, amount=-500)  # source defaults to import_csv
    original_source = tx.source

    noted = transactions.set_note(db_session, tx_id=tx.id, note="check this")
    assert noted is not None
    assert noted.note == "check this"
    assert noted.source is original_source  # untouched

    cleared = transactions.set_note(db_session, tx_id=tx.id, note=None)
    assert cleared.note is None


def test_set_note_missing_returns_none(db_session: Session):
    assert transactions.set_note(db_session, tx_id=9999, note="x") is None


def test_update_note_only_does_not_flip_source(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
):
    """Editing an imported row's note (category/scope unchanged) must NOT flip
    source to manual — that would wrongly shield it from rule re-categorization."""
    food = make_category(name="Food", kind=CategoryKind.expense)
    tx = make_transaction(account_id=account.id, amount=-500, category_id=food.id)
    assert tx.source is TxSource.import_csv

    # Same category and scope, only a new note.
    updated = transactions.update_transaction(
        db_session, tx_id=tx.id, category_id=food.id, scope=tx.scope, note="just a note"
    )
    assert updated.note == "just a note"
    assert updated.source is TxSource.import_csv  # untouched

    # Now actually change the category → it counts as a human categorization.
    changed = transactions.update_transaction(
        db_session, tx_id=tx.id, category_id=None, scope=tx.scope, note="just a note"
    )
    assert changed.source is TxSource.manual


def test_soft_delete_decrements_batch_record_count(db_session: Session, account: Account):
    a = _make_manual(db_session, account)
    _make_manual(db_session, account)
    batch = transactions.ensure_manual_batch(db_session)
    assert batch.record_count == 2

    transactions.soft_delete_transaction(db_session, tx_id=a.id)
    db_session.refresh(batch)
    assert batch.record_count == 1  # kept in step with the delete
