"""Planned-item queries — the DB side of the monthly cashflow checklist (Phase 19).

A :class:`~expense_analyzer.models.PlannedItem` is a recurring obligation the user
defines once (salary, rent, ZUS, a subscription); the per-month view is **derived**
— there is no "generate the month" step. :func:`plan_overview` walks the active
items and their :class:`~expense_analyzer.models.PlannedItemPayment` rows for the
selected month to produce the list, the income/charge totals and the "FOR LIVING"
remainder.

Phase 19a covers manual status only (``mark_paid``/``mark_unpaid`` set/clear
``paid_at``). Linking a real transaction and loan-backed derivation arrive in 19b;
the columns already exist so that work needs no migration.
"""

import calendar
from dataclasses import dataclass
from datetime import date

from sqlmodel import Session, col, delete, select

from expense_analyzer.clock import local_today, utc_now
from expense_analyzer.models import PlannedItem, PlannedItemPayment


def list_planned_items(session: Session, *, active_only: bool = False) -> list[PlannedItem]:
    """All planned items in display order (``sort_order`` then id).

    ``active_only`` drops retired items — used by the month view; the management
    list shows everything so a retired item can be reactivated.
    """
    stmt = select(PlannedItem)
    if active_only:
        stmt = stmt.where(col(PlannedItem.active).is_(True))

    return list(session.exec(stmt.order_by(col(PlannedItem.sort_order), col(PlannedItem.id))).all())


def get_planned_item(session: Session, item_id: int) -> PlannedItem | None:
    return session.get(PlannedItem, item_id)


def create_planned_item(
    session: Session,
    *,
    name: str,
    expected_amount: int | None,
    category_id: int | None = None,
    payee_account: str | None = None,
    due_day: int | None = None,
    note: str | None = None,
) -> PlannedItem:
    """Create a planned item. New items go to the bottom of the list (``sort_order``
    one past the current max) so define-order is preserved until reordered."""
    next_order = session.exec(
        select(col(PlannedItem.sort_order)).order_by(col(PlannedItem.sort_order).desc())
    ).first()
    item = PlannedItem(
        name=name,
        expected_amount=expected_amount,
        category_id=category_id,
        payee_account=payee_account,
        due_day=due_day,
        note=note,
        sort_order=(next_order + 1) if next_order is not None else 0,
    )
    session.add(item)
    session.commit()
    session.refresh(item)

    return item


def update_planned_item(
    session: Session,
    item_id: int,
    *,
    name: str,
    expected_amount: int | None,
    category_id: int | None = None,
    payee_account: str | None = None,
    due_day: int | None = None,
    note: str | None = None,
) -> PlannedItem | None:
    """Update a planned item's definition (not its order or active flag — those have
    their own actions). Returns None if it doesn't exist."""
    item = session.get(PlannedItem, item_id)
    if item is None:
        return None

    item.name = name
    item.expected_amount = expected_amount
    item.category_id = category_id
    item.payee_account = payee_account
    item.due_day = due_day
    item.note = note
    session.add(item)
    session.commit()
    session.refresh(item)

    return item


def delete_planned_item(session: Session, item_id: int) -> bool:
    """Delete a planned item and its payment-status rows. Returns False if missing.

    Planned items are config, not financial records (like budgets/loans) — a wrong
    line is just re-entered, so this is a hard delete. The ``paid_at`` ticks go with
    it; the real transactions a 19b link points at are untouched (the link lives on
    the payment row, which is what's deleted)."""
    item = session.get(PlannedItem, item_id)
    if item is None:
        return False

    session.exec(delete(PlannedItemPayment).where(PlannedItemPayment.planned_item_id == item_id))
    session.delete(item)
    session.commit()

    return True


def set_active(session: Session, item_id: int, active: bool) -> bool:
    """Retire (``active=False``) or reactivate a planned item. Returns False if missing.

    Retiring keeps the item and its history but drops it from the month view — the
    soft alternative to deleting a line that's simply no longer due."""
    item = session.get(PlannedItem, item_id)
    if item is None:
        return False

    item.active = active
    session.add(item)
    session.commit()

    return True


def move_item(session: Session, item_id: int, *, up: bool) -> bool:
    """Swap an item's ``sort_order`` with its neighbour in display order.

    Returns False if the item is missing or already at the end it's moving toward
    (nothing to swap with). Reorders the management list; the month view follows it.
    """
    items = list_planned_items(session)
    index = next((i for i, it in enumerate(items) if it.id == item_id), None)
    if index is None:
        return False

    swap_with = index - 1 if up else index + 1
    if swap_with < 0 or swap_with >= len(items):
        return False

    a, b = items[index], items[swap_with]
    a.sort_order, b.sort_order = b.sort_order, a.sort_order
    session.add_all([a, b])
    session.commit()

    return True


def _payment_for(session: Session, item_id: int, month: str) -> PlannedItemPayment | None:
    return session.exec(
        select(PlannedItemPayment).where(
            PlannedItemPayment.planned_item_id == item_id,
            PlannedItemPayment.month == month,
        )
    ).first()


def mark_paid(session: Session, *, planned_item_id: int, month: str) -> bool:
    """Manually tick a planned item as paid for ``month`` (cash / untracked).

    Upserts the ``(item, month)`` status row with ``paid_at`` set. Idempotent — a
    second tick on an already-paid month is a no-op. Returns False if the item is
    missing. (Linking a real transaction instead of a manual tick is Phase 19b.)
    """
    if session.get(PlannedItem, planned_item_id) is None:
        return False

    payment = _payment_for(session, planned_item_id, month)
    if payment is None:
        payment = PlannedItemPayment(
            planned_item_id=planned_item_id, month=month, paid_at=utc_now()
        )
        session.add(payment)
        session.commit()
    elif payment.paid_at is None and payment.transaction_id is None:
        payment.paid_at = utc_now()
        session.add(payment)
        session.commit()

    return True


def mark_unpaid(session: Session, *, planned_item_id: int, month: str) -> bool:
    """Clear a manual paid tick for ``month``. Returns False if there was none.

    Only removes a manual tick (a row with no linked transaction); a 19b
    transaction link is unlinked through its own action, not here."""
    payment = _payment_for(session, planned_item_id, month)
    if payment is None or payment.transaction_id is not None:
        return False

    session.delete(payment)
    session.commit()

    return True


def _due_date(month: str, due_day: int) -> date:
    """The due date for ``due_day`` in ``month`` (YYYY-MM), clamping past month end
    (a ``due_day`` of 31 in February lands on the 28th/29th)."""
    year, mon = (int(part) for part in month.split("-"))
    day = min(due_day, calendar.monthrange(year, mon)[1])

    return date(year, mon, day)


@dataclass(frozen=True, slots=True)
class PlannedRow:
    """One planned item resolved for a given month."""

    item: PlannedItem
    expected_amount: int | None  # signed minor units; None = unestimated
    paid: bool
    paid_date: date | None  # when it was ticked/paid (manual tick date), if known
    overdue: bool  # unpaid and past its due day

    @property
    def is_income(self) -> bool:
        return self.expected_amount is not None and self.expected_amount > 0

    @property
    def is_expense(self) -> bool:
        return self.expected_amount is None or self.expected_amount < 0


@dataclass(frozen=True, slots=True)
class PlanOverview:
    """The whole month: every active item plus the cashflow totals."""

    month: str
    rows: list[PlannedRow]
    income_total: int  # sum of positive expected amounts
    charges_total: int  # magnitude of negative expected amounts
    for_living: int  # income_total - charges_total (from known amounts)
    left_to_pay: int  # magnitude of unpaid charges (known amounts)
    unestimated_count: int  # items with no expected amount (FOR LIVING is approximate)


def plan_overview(session: Session, month: str, *, today: date | None = None) -> PlanOverview:
    """Derive the cashflow checklist for ``month`` from the active planned items.

    Income (positive ``expected_amount``) and charges (negative) are summed from the
    items that *have* a figure; "FOR LIVING" is income minus charges. A variable
    item with no expected amount is counted in ``unestimated_count`` and excluded
    from the sums (never a silent zero — see the model docstring), so the totals are
    flagged approximate rather than quietly wrong.

    ``overdue`` marks an unpaid charge whose ``due_day`` has already passed (``today``
    defaults to the local date; injectable for tests). Loan-backed derivation and
    real-transaction status are Phase 19b — here every item reads ``expected_amount``
    and a manual paid tick.
    """
    today = today or local_today()
    items = list_planned_items(session, active_only=True)

    payments = {
        p.planned_item_id: p
        for p in session.exec(
            select(PlannedItemPayment).where(PlannedItemPayment.month == month)
        ).all()
    }

    rows: list[PlannedRow] = []
    income_total = charges_total = left_to_pay = unestimated_count = 0
    for item in items:
        payment = payments.get(item.id)
        paid = payment is not None and (
            payment.paid_at is not None or payment.transaction_id is not None
        )
        paid_date = payment.paid_at.date() if payment and payment.paid_at else None

        overdue = not paid and item.due_day is not None and _due_date(month, item.due_day) < today

        amount = item.expected_amount
        if amount is None:
            unestimated_count += 1
        elif amount > 0:
            income_total += amount
        else:
            charges_total += -amount
            if not paid:
                left_to_pay += -amount

        rows.append(
            PlannedRow(
                item=item,
                expected_amount=amount,
                paid=paid,
                paid_date=paid_date,
                overdue=overdue,
            )
        )

    return PlanOverview(
        month=month,
        rows=rows,
        income_total=income_total,
        charges_total=charges_total,
        for_living=income_total - charges_total,
        left_to_pay=left_to_pay,
        unestimated_count=unestimated_count,
    )
