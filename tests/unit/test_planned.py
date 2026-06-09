"""Planned-item query layer (Phase 19a): the derived monthly cashflow view.

The view is computed live from the active items and their per-month paid status —
income/charge totals, the FOR LIVING remainder, unestimated variable items and the
overdue flag. ``plan_overview`` takes an injectable ``today`` so overdue is
deterministic without touching the clock.
"""

from collections.abc import Callable
from datetime import date

from sqlmodel import Session, select

from expense_analyzer.models import PlannedItem, PlannedItemPayment
from expense_analyzer.queries.planning import planned as pq


def _payments(session: Session) -> list[PlannedItemPayment]:
    return list(session.exec(select(PlannedItemPayment)).all())


def test_overview_empty(db_session: Session) -> None:
    overview = pq.plan_overview(db_session, "2026-06")

    assert overview.rows == []
    assert overview.income_total == 0
    assert overview.charges_total == 0
    assert overview.for_living == 0
    assert overview.left_to_pay == 0
    assert overview.unestimated_count == 0


def test_overview_totals_and_for_living(
    db_session: Session, make_planned_item: Callable[..., PlannedItem]
) -> None:
    make_planned_item(name="Salary", expected_amount=8000_00)  # income
    make_planned_item(name="Rent", expected_amount=-3000_00)  # charge
    make_planned_item(name="Netflix", expected_amount=-50_00)  # charge

    overview = pq.plan_overview(db_session, "2026-06")

    assert overview.income_total == 8000_00
    assert overview.charges_total == 3050_00
    assert overview.for_living == 8000_00 - 3050_00
    assert overview.left_to_pay == 3050_00  # nothing paid yet
    assert overview.unestimated_count == 0


def test_variable_item_is_unestimated_not_zero(
    db_session: Session, make_planned_item: Callable[..., PlannedItem]
) -> None:
    make_planned_item(name="Salary", expected_amount=8000_00)
    make_planned_item(name="ZUS", expected_amount=None)  # variable

    overview = pq.plan_overview(db_session, "2026-06")

    assert overview.unestimated_count == 1
    # The variable charge is excluded from the sums (never a silent zero).
    assert overview.charges_total == 0
    assert overview.for_living == 8000_00
    [_, zus] = overview.rows
    assert zus.expected_amount is None
    assert zus.is_expense  # an unestimated item reads as a charge


def test_mark_paid_then_overview(
    db_session: Session, make_planned_item: Callable[..., PlannedItem]
) -> None:
    rent = make_planned_item(name="Rent", expected_amount=-3000_00)

    assert pq.mark_paid(db_session, planned_item_id=rent.id, month="2026-06") is True
    overview = pq.plan_overview(db_session, "2026-06")

    [row] = overview.rows
    assert row.paid is True
    assert row.paid_date is not None
    assert overview.left_to_pay == 0  # paid charges drop out of "left to pay"
    assert overview.charges_total == 3000_00  # but still count toward total charges


def test_paid_status_is_per_month(
    db_session: Session, make_planned_item: Callable[..., PlannedItem]
) -> None:
    rent = make_planned_item(name="Rent", expected_amount=-3000_00)
    pq.mark_paid(db_session, planned_item_id=rent.id, month="2026-06")

    assert pq.plan_overview(db_session, "2026-06").rows[0].paid is True
    assert pq.plan_overview(db_session, "2026-07").rows[0].paid is False


def test_mark_paid_is_idempotent(
    db_session: Session, make_planned_item: Callable[..., PlannedItem]
) -> None:
    rent = make_planned_item(name="Rent", expected_amount=-3000_00)
    pq.mark_paid(db_session, planned_item_id=rent.id, month="2026-06")
    pq.mark_paid(db_session, planned_item_id=rent.id, month="2026-06")

    assert len(_payments(db_session)) == 1


def test_mark_unpaid_removes_tick(
    db_session: Session, make_planned_item: Callable[..., PlannedItem]
) -> None:
    rent = make_planned_item(name="Rent", expected_amount=-3000_00)
    pq.mark_paid(db_session, planned_item_id=rent.id, month="2026-06")

    assert pq.mark_unpaid(db_session, planned_item_id=rent.id, month="2026-06") is True
    assert pq.plan_overview(db_session, "2026-06").rows[0].paid is False
    # Nothing to clear the second time.
    assert pq.mark_unpaid(db_session, planned_item_id=rent.id, month="2026-06") is False


def test_mark_paid_missing_item(db_session: Session) -> None:
    assert pq.mark_paid(db_session, planned_item_id=999, month="2026-06") is False


def test_overdue_flag(db_session: Session, make_planned_item: Callable[..., PlannedItem]) -> None:
    rent = make_planned_item(name="Rent", expected_amount=-3000_00, due_day=10)

    # Today is the 15th — the 10th has passed and it's unpaid -> overdue.
    overdue = pq.plan_overview(db_session, "2026-06", today=date(2026, 6, 15))
    assert overdue.rows[0].overdue is True

    # On the 5th the due date hasn't arrived -> not overdue.
    early = pq.plan_overview(db_session, "2026-06", today=date(2026, 6, 5))
    assert early.rows[0].overdue is False

    # Paid -> never overdue, even past the due day.
    pq.mark_paid(db_session, planned_item_id=rent.id, month="2026-06")
    paid = pq.plan_overview(db_session, "2026-06", today=date(2026, 6, 15))
    assert paid.rows[0].overdue is False


def test_no_due_day_is_never_overdue(
    db_session: Session, make_planned_item: Callable[..., PlannedItem]
) -> None:
    make_planned_item(name="Rent", expected_amount=-3000_00, due_day=None)

    overview = pq.plan_overview(db_session, "2026-06", today=date(2026, 12, 31))
    assert overview.rows[0].overdue is False


def test_retired_item_excluded_from_view_but_listed(
    db_session: Session, make_planned_item: Callable[..., PlannedItem]
) -> None:
    item = make_planned_item(name="Old gym", expected_amount=-100_00)
    pq.set_active(db_session, item.id, False)

    assert pq.plan_overview(db_session, "2026-06").rows == []  # not in the month view
    assert len(pq.list_planned_items(db_session)) == 1  # but still manageable
    assert pq.list_planned_items(db_session, active_only=True) == []


def test_move_item_reorders(
    db_session: Session, make_planned_item: Callable[..., PlannedItem]
) -> None:
    make_planned_item(name="A", expected_amount=-100_00, sort_order=0)
    b = make_planned_item(name="B", expected_amount=-200_00, sort_order=1)

    assert [i.name for i in pq.list_planned_items(db_session)] == ["A", "B"]
    assert pq.move_item(db_session, b.id, up=True) is True
    assert [i.name for i in pq.list_planned_items(db_session)] == ["B", "A"]
    # B is already first — can't move up further.
    assert pq.move_item(db_session, b.id, up=True) is False


def test_delete_item_removes_its_payments(
    db_session: Session, make_planned_item: Callable[..., PlannedItem]
) -> None:
    rent = make_planned_item(name="Rent", expected_amount=-3000_00)
    pq.mark_paid(db_session, planned_item_id=rent.id, month="2026-06")

    assert pq.delete_planned_item(db_session, rent.id) is True
    assert pq.list_planned_items(db_session) == []
    assert _payments(db_session) == []


def test_create_appends_to_end(
    db_session: Session, make_planned_item: Callable[..., PlannedItem]
) -> None:
    make_planned_item(name="First", expected_amount=-100_00, sort_order=5)
    created = pq.create_planned_item(db_session, name="Second", expected_amount=-200_00)

    assert created.sort_order == 6  # one past the current max
    assert [i.name for i in pq.list_planned_items(db_session)] == ["First", "Second"]
