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

from expense_analyzer.models import Account, Category, CategoryKind, Transaction
from expense_analyzer.queries import budgets as bq


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
