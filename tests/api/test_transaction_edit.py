"""HTTP layer for the single-row edit layer (Phase 13): manual entry, notes,
edit and delete. Query-layer behaviour is covered in tests/unit/test_manual_transactions.py.

``auth_client`` and ``db_session`` share the same temp engine, so a row created
over HTTP is then asserted against ``db_session``.
"""

from collections.abc import Callable
from datetime import date

from fastapi import status
from fastapi.testclient import TestClient
from sqlmodel import Session

from expense_analyzer.models import Account, Category, CategoryKind, Scope, Transaction, TxSource
from expense_analyzer.queries import transactions
from expense_analyzer.queries.transactions import TransactionFilters


def _only_row(db_session: Session) -> Transaction:
    page = transactions.list_transactions(db_session, TransactionFilters(), page=1, page_size=10)
    assert page.total == 1

    return page.rows[0]


def test_add_manual_expense_creates_negative_row(
    auth_client: TestClient, db_session: Session, account: Account
):
    resp = auth_client.post(
        "/dashboard/transactions/add",
        data={
            "account_id": account.id,
            "booked_date": "2026-05-10",
            "amount": "19,99",
            "direction": "expense",
            "description": "Lunch",
            "scope": "private",
        },
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER

    tx = _only_row(db_session)
    assert tx.amount == -1999  # expense -> negative
    assert tx.source is TxSource.manual
    assert tx.raw_description == "Lunch"
    assert transactions.is_manual_entry(db_session, tx) is True


def test_add_manual_income_is_positive(
    auth_client: TestClient, db_session: Session, account: Account
):
    auth_client.post(
        "/dashboard/transactions/add",
        data={
            "account_id": account.id,
            "booked_date": "2026-05-10",
            "amount": "1000",
            "direction": "income",
            "description": "Cash gift",
            "scope": "private",
        },
    )
    assert _only_row(db_session).amount == 100000  # income -> positive


def test_add_manual_bad_amount_flashes_error_and_creates_nothing(
    auth_client: TestClient, db_session: Session, account: Account
):
    resp = auth_client.post(
        "/dashboard/transactions/add",
        data={
            "account_id": account.id,
            "booked_date": "2026-05-10",
            "amount": "not money",
            "direction": "expense",
            "description": "Lunch",
            "scope": "private",
        },
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "could not read the amount" in resp.text.lower()

    page = transactions.list_transactions(db_session, TransactionFilters(), page=1, page_size=10)
    assert page.total == 0  # nothing persisted


def test_add_manual_with_note_and_category(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
):
    food = make_category(name="Food", kind=CategoryKind.expense)
    auth_client.post(
        "/dashboard/transactions/add",
        data={
            "account_id": account.id,
            "booked_date": "2026-05-10",
            "amount": "12",
            "direction": "expense",
            "description": "Snack",
            "category_id": str(food.id),
            "scope": "household",
            "note": "shared",
        },
    )
    tx = _only_row(db_session)
    assert tx.category_id == food.id
    assert tx.scope is Scope.household
    assert tx.note == "shared"


def test_edit_form_manual_shows_money_fields(
    auth_client: TestClient, db_session: Session, account: Account
):
    tx = transactions.create_manual_transaction(
        db_session,
        account_id=account.id,
        booked_date=date(2026, 5, 10),
        amount=-2000,
        description="Coffee",
        category_id=None,
        scope=Scope.private,
        note=None,
        owner_id=None,
    )
    resp = auth_client.get(f"/dashboard/transactions/{tx.id}/edit")
    assert resp.status_code == status.HTTP_200_OK
    assert 'name="amount"' in resp.text  # money fields editable for a manual entry
    assert 'name="description"' in resp.text


def test_edit_form_imported_hides_money_fields(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    tx = make_transaction(account_id=account.id, amount=-777, raw_description="BIEDRONKA")
    resp = auth_client.get(f"/dashboard/transactions/{tx.id}/edit")
    assert resp.status_code == status.HTTP_200_OK
    assert 'name="amount"' not in resp.text  # bank fields are read-only
    assert 'name="note"' in resp.text  # but the note is still editable
    assert "read-only" in resp.text.lower()


def test_edit_manual_updates_money_fields(
    auth_client: TestClient, db_session: Session, account: Account
):
    tx = transactions.create_manual_transaction(
        db_session,
        account_id=account.id,
        booked_date=date(2026, 5, 10),
        amount=-2000,
        description="Coffe",
        category_id=None,
        scope=Scope.private,
        note=None,
        owner_id=None,
    )
    resp = auth_client.post(
        f"/dashboard/transactions/{tx.id}/edit",
        data={
            "account_id": account.id,
            "booked_date": "2026-06-01",
            "amount": "25",
            "direction": "expense",
            "description": "Coffee",
            "scope": "private",
            "note": "fixed",
        },
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER

    db_session.expire_all()  # the mutation committed in the app's session
    fresh = transactions.get_transaction(db_session, tx.id)
    assert fresh.amount == -2500
    assert fresh.raw_description == "Coffee"
    assert fresh.booked_date == date(2026, 6, 1)
    assert fresh.note == "fixed"


def test_edit_imported_changes_category_not_amount(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
):
    food = make_category(name="Food", kind=CategoryKind.expense)
    tx = make_transaction(account_id=account.id, amount=-777, raw_description="BIEDRONKA")

    auth_client.post(
        f"/dashboard/transactions/{tx.id}/edit",
        data={
            "category_id": str(food.id),
            "scope": "household",
            "note": "groceries",
            # an imported row's money fields are ignored even if submitted
            "amount": "9999",
            "direction": "income",
        },
    )
    db_session.expire_all()  # the mutation committed in the app's session
    fresh = transactions.get_transaction(db_session, tx.id)
    assert fresh.amount == -777  # untouched despite the posted amount
    assert fresh.category_id == food.id
    assert fresh.note == "groceries"


def test_delete_manual_soft_deletes(auth_client: TestClient, db_session: Session, account: Account):
    tx = transactions.create_manual_transaction(
        db_session,
        account_id=account.id,
        booked_date=date(2026, 5, 10),
        amount=-2000,
        description="Coffee",
        category_id=None,
        scope=Scope.private,
        note=None,
        owner_id=None,
    )
    resp = auth_client.post(f"/dashboard/transactions/{tx.id}/delete", follow_redirects=False)
    assert resp.status_code == status.HTTP_303_SEE_OTHER

    db_session.expire_all()  # the soft-delete committed in the app's session
    assert transactions.get_transaction(db_session, tx.id) is None
    page = transactions.list_transactions(db_session, TransactionFilters(), page=1, page_size=10)
    assert page.total == 0


def test_delete_imported_is_rejected(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    tx = make_transaction(account_id=account.id, amount=-777)
    resp = auth_client.post(f"/dashboard/transactions/{tx.id}/delete")

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert transactions.get_transaction(db_session, tx.id) is not None  # still there


def test_edit_and_delete_missing_transaction_404(auth_client: TestClient):
    assert (
        auth_client.get("/dashboard/transactions/9999/edit").status_code
        == status.HTTP_404_NOT_FOUND
    )
    assert (
        auth_client.post("/dashboard/transactions/9999/delete").status_code
        == status.HTTP_404_NOT_FOUND
    )


def test_note_modal_renders_on_list(
    auth_client: TestClient,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    tx = make_transaction(account_id=account.id, amount=-500, note="reimburse me")
    resp = auth_client.get("/dashboard/transactions")
    # The per-row note modal and its trigger are present, and the note text shows
    # in the trigger's title (the at-a-glance peek).
    assert f'id="note-{tx.id}"' in resp.text
    assert 'href="#note-' in resp.text
    assert "reimburse me" in resp.text


def test_set_note_via_endpoint(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    tx = make_transaction(account_id=account.id, amount=-500)
    resp = auth_client.post(
        f"/dashboard/transactions/{tx.id}/note",
        data={"note": "  paid back  ", "return_to": "/dashboard/transactions"},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER

    db_session.expire_all()
    fresh = transactions.get_transaction(db_session, tx.id)
    assert fresh.note == "paid back"  # trimmed
    assert fresh.source is TxSource.import_csv  # a note doesn't change source


def test_clear_note_via_endpoint(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    tx = make_transaction(account_id=account.id, amount=-500, note="old")
    auth_client.post(f"/dashboard/transactions/{tx.id}/note", data={"note": "   "})

    db_session.expire_all()
    assert transactions.get_transaction(db_session, tx.id).note is None  # blank clears it


def test_note_endpoint_missing_404(auth_client: TestClient):
    assert (
        auth_client.post("/dashboard/transactions/9999/note", data={"note": "x"}).status_code
        == status.HTTP_404_NOT_FOUND
    )


def test_nav_uses_icon_sprite_and_groups(auth_client: TestClient):
    """The redesigned nav renders icon + label from the inline sprite, collapsed
    into labelled dropdown groups."""
    resp = auth_client.get("/dashboard/transactions")
    assert 'id="i-transactions"' in resp.text  # sprite present
    assert 'class="nav-ico"' in resp.text  # icon links rendered
    # Grouped into hover dropdowns (Money / Planning / …) instead of 14 flat links.
    for group in ("Money", "Planning", "Categorize", "Wealth", "System"):
        assert f'class="nav-trigger" tabindex="0">{group}</span>' in resp.text
    assert "Transactions" in resp.text  # the items still live inside the menus
