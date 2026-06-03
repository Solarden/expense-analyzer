"""HTTP tests for the Phase 4 dashboard: overview page, list filters/pagination,
the vendored Chart.js asset, and the categorize return-to round trip.

Rows are created directly via the model factories (same temp engine the app
uses) so each test controls exactly what the page should show.
"""

from collections.abc import Callable
from datetime import date

from fastapi.testclient import TestClient
from sqlmodel import Session

from expense_analyzer.models import Account, Category, CategoryKind, Transaction


def test_static_chart_js_served(auth_client: TestClient):
    resp = auth_client.get("/static/chart.min.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


def test_stats_page_renders_with_charts(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    make_transaction(account_id=account.id, amount=-7500, booked_date=date(2026, 5, 3))
    make_transaction(account_id=account.id, amount=12000, booked_date=date(2026, 5, 1))

    resp = auth_client.get("/dashboard/stats?month=2026-05")
    assert resp.status_code == 200
    assert "Overview" in resp.text
    assert 'id="categoryChart"' in resp.text
    assert 'id="trendChart"' in resp.text
    assert "/static/chart.min.js" in resp.text
    assert "75,00 zł" in resp.text  # spending magnitude, formatted


def test_stats_page_excludes_transfers(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
):
    a = make_account(name="PKO")
    b = make_account(name="mBank")
    make_transaction(
        account_id=a.id, amount=-200000, booked_date=date(2026, 5, 5), transfer_group_id="g1"
    )
    make_transaction(
        account_id=b.id, amount=200000, booked_date=date(2026, 5, 5), transfer_group_id="g1"
    )
    make_transaction(account_id=a.id, amount=-3000, booked_date=date(2026, 5, 6))

    resp = auth_client.get("/dashboard/stats?month=2026-05")
    assert resp.status_code == 200
    assert "30,00 zł" in resp.text  # only the real expense
    assert "2000,00 zł" not in resp.text  # the transfer leg is gone


def test_stats_page_empty_db(auth_client: TestClient, db_session: Session):
    resp = auth_client.get("/dashboard/stats")
    assert resp.status_code == 200
    assert "No spending recorded" in resp.text


def test_transactions_pagination(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    for d in range(1, 61):  # 60 rows, default page_size 50 -> 2 pages
        make_transaction(account_id=account.id, amount=-100 * d, booked_date=date(2026, 5, 1))

    page1 = auth_client.get("/dashboard/transactions").text
    assert "Page 1 of 2" in page1
    assert "60 transactions" in page1

    page2 = auth_client.get("/dashboard/transactions?page=2").text
    assert "Page 2 of 2" in page2


def test_transactions_filter_by_month(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    make_transaction(
        account_id=account.id, amount=-100, booked_date=date(2026, 5, 1), raw_description="MAY ROW"
    )
    make_transaction(
        account_id=account.id,
        amount=-200,
        booked_date=date(2026, 4, 1),
        raw_description="APRIL ROW",
    )

    resp = auth_client.get("/dashboard/transactions?month=2026-05")
    assert "MAY ROW" in resp.text
    assert "APRIL ROW" not in resp.text


def test_transactions_filter_uncategorized(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
):
    food = make_category(name="Food", kind=CategoryKind.expense)
    make_transaction(
        account_id=account.id, amount=-100, day=1, category_id=food.id, raw_description="HAS CAT"
    )
    make_transaction(account_id=account.id, amount=-200, day=2, raw_description="NO CAT")

    resp = auth_client.get("/dashboard/transactions?category=none")
    assert "NO CAT" in resp.text
    assert "HAS CAT" not in resp.text


def test_categorize_returns_to_filtered_view(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
):
    food = make_category(name="Food", kind=CategoryKind.expense)
    tx = make_transaction(account_id=account.id, amount=-100, day=1)

    resp = auth_client.post(
        f"/dashboard/transactions/{tx.id}/categorize",
        data={
            "category_id": str(food.id),
            "scope": "household",
            "return_to": "/dashboard/transactions?month=2026-05&page=2",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/transactions?month=2026-05&page=2"


def test_categorize_rejects_offsite_return_to(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    tx = make_transaction(account_id=account.id, amount=-100, day=1)

    resp = auth_client.post(
        f"/dashboard/transactions/{tx.id}/categorize",
        data={"category_id": "", "scope": "private", "return_to": "https://evil.example/phish"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/transactions"  # open redirect refused


def test_categorize_rejects_sibling_path_return_to(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    tx = make_transaction(account_id=account.id, amount=-100, day=1)

    resp = auth_client.post(
        f"/dashboard/transactions/{tx.id}/categorize",
        data={
            "category_id": "",
            "scope": "private",
            # Shares the list-path prefix but is a different route — must be refused.
            "return_to": "/dashboard/transactionsX/evil",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/transactions"


def test_transactions_invalid_month_does_not_500(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    make_transaction(
        account_id=account.id, amount=-100, booked_date=date(2026, 5, 1), raw_description="A ROW"
    )

    resp = auth_client.get("/dashboard/transactions?month=not-a-month")

    assert resp.status_code == 200  # malformed filter ignored, not a crash
    assert "A ROW" in resp.text
