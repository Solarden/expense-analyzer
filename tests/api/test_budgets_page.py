"""Budgets dashboard page (Phase 8): set/override/delete and the month overview.

HTTP tests use ``auth_client`` (logged in); they share the temp engine with
``db_session`` so a budget set over HTTP is visible to a query-layer assertion.
Bad input must come back as a 400 re-render with a flash, never a 500.
"""

from collections.abc import Callable
from datetime import date

from fastapi import status
from fastapi.testclient import TestClient
from sqlmodel import Session

from expense_analyzer.models import Account, Category, CategoryKind, Scope, Transaction
from expense_analyzer.queries.core import users
from expense_analyzer.queries.planning import budgets as bq


def test_budgets_page_renders(auth_client: TestClient) -> None:
    resp = auth_client.get("/dashboard/budgets")
    assert resp.status_code == status.HTTP_200_OK
    assert "Budgets" in resp.text


def test_set_recurring_budget(
    auth_client: TestClient, db_session: Session, make_category: Callable[..., Category]
) -> None:
    cat = make_category(name="Food", kind=CategoryKind.expense)

    resp = auth_client.post(
        "/dashboard/budgets",
        data={"category_id": cat.id, "month": "", "limit_amount": "2000"},
        follow_redirects=False,
    )

    assert resp.status_code == status.HTTP_303_SEE_OTHER
    [budget] = bq.list_budgets(db_session)
    assert budget.month is None  # "" -> recurring
    assert budget.limit_amount == 2000_00


def test_set_month_override(
    auth_client: TestClient, db_session: Session, make_category: Callable[..., Category]
) -> None:
    cat = make_category(name="Food", kind=CategoryKind.expense)

    auth_client.post(
        "/dashboard/budgets",
        data={"category_id": cat.id, "month": "2026-06", "limit_amount": "2500"},
        follow_redirects=False,
    )

    [budget] = bq.list_budgets(db_session)
    assert budget.month == "2026-06"
    assert budget.limit_amount == 2500_00


def test_set_budget_rejects_non_expense_category(
    auth_client: TestClient, make_category: Callable[..., Category]
) -> None:
    income = make_category(name="Salary", kind=CategoryKind.income)

    resp = auth_client.post(
        "/dashboard/budgets",
        data={"category_id": income.id, "month": "", "limit_amount": "2000"},
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "expense category" in resp.text


def test_set_budget_rejects_malformed_month(
    auth_client: TestClient, make_category: Callable[..., Category]
) -> None:
    cat = make_category(kind=CategoryKind.expense)

    resp = auth_client.post(
        "/dashboard/budgets",
        data={"category_id": cat.id, "month": "2026-13", "limit_amount": "2000"},
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "YYYY-MM" in resp.text


def test_set_budget_rejects_non_positive_limit(
    auth_client: TestClient, make_category: Callable[..., Category]
) -> None:
    cat = make_category(kind=CategoryKind.expense)

    resp = auth_client.post(
        "/dashboard/budgets",
        data={"category_id": cat.id, "month": "", "limit_amount": "0"},
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "positive" in resp.text


def test_set_budget_rejects_unparseable_amount(
    auth_client: TestClient, make_category: Callable[..., Category]
) -> None:
    cat = make_category(kind=CategoryKind.expense)

    resp = auth_client.post(
        "/dashboard/budgets",
        data={"category_id": cat.id, "month": "", "limit_amount": "abc"},
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_delete_budget_over_http(
    auth_client: TestClient, db_session: Session, make_category: Callable[..., Category]
) -> None:
    cat = make_category(kind=CategoryKind.expense)
    budget = bq.set_budget(db_session, category_id=cat.id, month=None, limit_amount=100_00)

    resp = auth_client.post(f"/dashboard/budgets/{budget.id}/delete", follow_redirects=False)

    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert bq.list_budgets(db_session) == []


def test_budget_edit_prefills_form(
    auth_client: TestClient, db_session: Session, make_category: Callable[..., Category]
) -> None:
    cat = make_category(name="Food", kind=CategoryKind.expense)
    budget = bq.set_budget(db_session, category_id=cat.id, month=None, limit_amount=2000_00)

    resp = auth_client.get(f"/dashboard/budgets?edit={budget.id}")

    assert resp.status_code == status.HTTP_200_OK
    assert "Edit budget" in resp.text
    assert 'value="2000.00"' in resp.text  # limit prefilled, parser round-trips
    # Category + month are the identity (upsert key) — carried as hidden fields.
    assert 'name="category_id"' in resp.text
    assert f'value="{cat.id}"' in resp.text


def test_budget_edit_updates_same_row_via_upsert(
    auth_client: TestClient, db_session: Session, make_category: Callable[..., Category]
) -> None:
    cat = make_category(name="Food", kind=CategoryKind.expense)
    budget = bq.set_budget(db_session, category_id=cat.id, month=None, limit_amount=2000_00)

    # Posting back the locked category+month with a new limit upserts the same row.
    resp = auth_client.post(
        "/dashboard/budgets",
        data={"category_id": cat.id, "month": "", "limit_amount": "2500"},
        follow_redirects=False,
    )

    assert resp.status_code == status.HTTP_303_SEE_OTHER
    db_session.expire_all()  # the app committed in its own session; drop cached rows
    [only] = bq.list_budgets(db_session)  # still one row, not a duplicate
    assert only.id == budget.id
    assert only.limit_amount == 2500_00


def test_budget_edit_stale_id_falls_back_to_create_form(auth_client: TestClient) -> None:
    resp = auth_client.get("/dashboard/budgets?edit=9999")

    assert resp.status_code == status.HTTP_200_OK
    assert "Set a budget" in resp.text  # no edit_budget -> the create form


def test_budget_edit_malformed_id_falls_back_not_422(auth_client: TestClient) -> None:
    # A non-numeric ?edit= degrades to the create form rather than 422-ing the page.
    resp = auth_client.get("/dashboard/budgets?edit=abc")

    assert resp.status_code == status.HTTP_200_OK
    assert "Set a budget" in resp.text


def test_home_lens_shows_spending_by_member(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
) -> None:
    """Under the home lens the budgets page breaks shared spend down by the member
    who added each row."""
    tester = users.get_by_username(db_session, "tester")
    account = make_account()
    make_transaction(
        account_id=account.id,
        amount=-120_00,
        booked_date=date(2026, 6, 5),
        owner_id=tester.id,
        scope=Scope.household,
    )

    resp = auth_client.get("/dashboard/budgets?lens=home&month=2026-06")

    assert resp.status_code == status.HTTP_200_OK
    assert "Spending by member" in resp.text
    assert "120,00" in resp.text  # tester's shared spend, formatted PLN


def test_overview_shows_remaining_for_over_budget_category(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    account = make_account()
    food = make_category(name="Food", kind=CategoryKind.expense)
    bq.set_budget(db_session, category_id=food.id, month=None, limit_amount=100_00)
    make_transaction(
        account_id=account.id, amount=-150_00, booked_date=date(2026, 6, 5), category_id=food.id
    )

    resp = auth_client.get("/dashboard/budgets?month=2026-06")

    assert resp.status_code == status.HTTP_200_OK
    assert "Food" in resp.text
    assert "-50,00" in resp.text  # remaining = 100 - 150, formatted PLN
