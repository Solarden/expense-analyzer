"""Plan dashboard page (Phase 19a): define items, the monthly view, mark paid.

HTTP tests use ``auth_client`` (logged in) and share the temp engine with
``db_session`` so a row created over HTTP is visible to a query-layer assertion.
Bad input must come back as a 400 re-render with a flash, never a 500.
"""

from collections.abc import Callable

from fastapi import status
from fastapi.testclient import TestClient
from sqlmodel import Session

from expense_analyzer.models import Category, CategoryKind, PlannedItem
from expense_analyzer.queries import planned as pq


def test_plan_page_renders(auth_client: TestClient) -> None:
    resp = auth_client.get("/dashboard/plan")
    assert resp.status_code == status.HTTP_200_OK
    assert "Monthly plan" in resp.text


def test_create_expense_item(auth_client: TestClient, db_session: Session) -> None:
    resp = auth_client.post(
        "/dashboard/plan",
        data={"name": "Rent", "amount": "3000", "direction": "expense", "month": "2026-06"},
        follow_redirects=False,
    )

    assert resp.status_code == status.HTTP_303_SEE_OTHER
    [item] = pq.list_planned_items(db_session)
    assert item.name == "Rent"
    assert item.expected_amount == -3000_00  # expense -> negative


def test_create_income_item(auth_client: TestClient, db_session: Session) -> None:
    auth_client.post(
        "/dashboard/plan",
        data={"name": "Salary", "amount": "8000", "direction": "income"},
        follow_redirects=False,
    )

    [item] = pq.list_planned_items(db_session)
    assert item.expected_amount == 8000_00  # income -> positive


def test_create_variable_item_has_no_amount(auth_client: TestClient, db_session: Session) -> None:
    auth_client.post(
        "/dashboard/plan",
        data={"name": "ZUS", "amount": "", "direction": "expense"},
        follow_redirects=False,
    )

    [item] = pq.list_planned_items(db_session)
    assert item.expected_amount is None  # blank amount -> unestimated


def test_create_with_due_day_and_payee(auth_client: TestClient, db_session: Session) -> None:
    auth_client.post(
        "/dashboard/plan",
        data={
            "name": "Rent",
            "amount": "3000",
            "direction": "expense",
            "due_day": "10",
            "payee_account": "PL61109010140000071219812874",
            "note": "landlord",
        },
        follow_redirects=False,
    )

    [item] = pq.list_planned_items(db_session)
    assert item.due_day == 10
    assert item.payee_account == "PL61109010140000071219812874"
    assert item.note == "landlord"


def test_create_rejects_blank_name(auth_client: TestClient) -> None:
    resp = auth_client.post("/dashboard/plan", data={"name": "  ", "amount": "100"})

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "Name is required" in resp.text


def test_create_rejects_unparseable_amount(auth_client: TestClient) -> None:
    resp = auth_client.post("/dashboard/plan", data={"name": "Rent", "amount": "abc"})

    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_create_rejects_bad_due_day(auth_client: TestClient) -> None:
    resp = auth_client.post(
        "/dashboard/plan",
        data={"name": "Rent", "amount": "100", "due_day": "40"},
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "Due day" in resp.text


def test_edit_prefills_and_updates(
    auth_client: TestClient,
    db_session: Session,
    make_planned_item: Callable[..., PlannedItem],
) -> None:
    item = make_planned_item(name="Rent", expected_amount=-3000_00)

    prefilled = auth_client.get(f"/dashboard/plan?edit={item.id}")
    assert prefilled.status_code == status.HTTP_200_OK
    assert "Edit plan item" in prefilled.text
    assert 'value="3000.00"' in prefilled.text  # amount round-trips

    resp = auth_client.post(
        f"/dashboard/plan/{item.id}/edit",
        data={"name": "Rent", "amount": "3200", "direction": "expense"},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER
    db_session.expire_all()  # the app committed in its own session
    [updated] = pq.list_planned_items(db_session)
    assert updated.expected_amount == -3200_00


def test_mark_paid_and_unpaid_over_http(
    auth_client: TestClient,
    db_session: Session,
    make_planned_item: Callable[..., PlannedItem],
) -> None:
    item = make_planned_item(name="Rent", expected_amount=-3000_00)

    auth_client.post(
        f"/dashboard/plan/{item.id}/mark-paid",
        data={"month": "2026-06"},
        follow_redirects=False,
    )
    assert pq.plan_overview(db_session, "2026-06").rows[0].paid is True

    auth_client.post(
        f"/dashboard/plan/{item.id}/mark-unpaid",
        data={"month": "2026-06"},
        follow_redirects=False,
    )
    db_session.expire_all()
    assert pq.plan_overview(db_session, "2026-06").rows[0].paid is False


def test_toggle_active_over_http(
    auth_client: TestClient,
    db_session: Session,
    make_planned_item: Callable[..., PlannedItem],
) -> None:
    item = make_planned_item(name="Gym", expected_amount=-100_00)

    auth_client.post(f"/dashboard/plan/{item.id}/toggle-active", data={"month": "2026-06"})
    db_session.expire_all()
    assert pq.list_planned_items(db_session)[0].active is False


def test_delete_over_http(
    auth_client: TestClient,
    db_session: Session,
    make_planned_item: Callable[..., PlannedItem],
) -> None:
    item = make_planned_item(name="Rent", expected_amount=-3000_00)

    resp = auth_client.post(
        f"/dashboard/plan/{item.id}/delete", data={"month": "2026-06"}, follow_redirects=False
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert pq.list_planned_items(db_session) == []


def test_for_living_shown_on_page(
    auth_client: TestClient,
    db_session: Session,
    make_planned_item: Callable[..., PlannedItem],
) -> None:
    make_planned_item(name="Salary", expected_amount=8000_00)
    make_planned_item(name="Rent", expected_amount=-3000_00)

    resp = auth_client.get("/dashboard/plan")

    assert "FOR LIVING" in resp.text
    assert "Salary" in resp.text and "Rent" in resp.text


def test_malformed_month_does_not_500(
    auth_client: TestClient, make_planned_item: Callable[..., PlannedItem]
) -> None:
    # A hand-crafted ?month= must not crash the due-date math (it parses the month).
    make_planned_item(name="Rent", expected_amount=-3000_00, due_day=10)

    resp = auth_client.get("/dashboard/plan?month=not-a-month")
    assert resp.status_code == status.HTTP_200_OK


def test_edit_missing_item_is_404(auth_client: TestClient) -> None:
    resp = auth_client.post("/dashboard/plan/9999/edit", data={"name": "X", "amount": "1"})
    assert resp.status_code == status.HTTP_404_NOT_FOUND


def test_create_with_category(
    auth_client: TestClient,
    db_session: Session,
    make_category: Callable[..., Category],
) -> None:
    cat = make_category(name="Housing", kind=CategoryKind.expense)
    auth_client.post(
        "/dashboard/plan",
        data={"name": "Rent", "amount": "3000", "direction": "expense", "category_id": str(cat.id)},
        follow_redirects=False,
    )

    [item] = pq.list_planned_items(db_session)
    assert item.category_id == cat.id
