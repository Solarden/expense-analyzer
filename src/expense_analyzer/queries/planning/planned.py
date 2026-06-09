"""Planned-item queries — the DB side of the monthly cashflow checklist (Phase 19).

A :class:`~expense_analyzer.models.PlannedItem` is a recurring obligation the user
defines once (salary, rent, ZUS, a subscription); the per-month view is **derived**
— there is no "generate the month" step. :func:`plan_overview` walks the active
items and their :class:`~expense_analyzer.models.PlannedItemPayment` rows for the
selected month to produce the list, the income/charge totals and the "FOR LIVING"
remainder.

Phase 19a covered manual status (``mark_paid``/``mark_unpaid`` set/clear ``paid_at``).
Phase 19b adds, on top of the same model (no migration):

- **Real-transaction linking** for non-loan items (``link_transaction``/
  ``unlink_transaction`` set/clear ``PlannedItemPayment.transaction_id``) plus
  amount+date auto-suggestions (``suggest_links``), mirroring the loan payment
  suggest/confirm pattern.
- **Loan-backed items** (``PlannedItem.loan_id`` set): name, installment counter and
  amount are derived live from the loan's amortization schedule for the month, and
  the paid status comes from loan reconciliation — the Loans module stays the single
  source of truth (no ``PlannedItemPayment`` row). An item whose schedule has no
  installment due in the month (before the first or past the term) simply doesn't
  appear that month — auto-expiry, purely derived.
- **Last-month hint** for variable items: the most recent real linked amount, shown
  as a guide for an item with no fixed ``expected_amount``.
"""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from sqlmodel import Session, col, delete, select

from expense_analyzer.clock import local_today, utc_now
from expense_analyzer.loans import LoanScheduleError, Schedule, ScheduleRow
from expense_analyzer.models import PlannedItem, PlannedItemPayment, Transaction
from expense_analyzer.queries.planning import loans as loan_queries


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
    loan_id: int | None = None,
    payee_account: str | None = None,
    due_day: int | None = None,
    note: str | None = None,
) -> PlannedItem:
    """Create a planned item. New items go to the bottom of the list (``sort_order``
    one past the current max) so define-order is preserved until reordered.

    A ``loan_id`` makes it loan-backed: the month view then derives its name, amount
    and paid status from that loan's schedule (``expected_amount`` is ignored)."""
    next_order = session.exec(
        select(col(PlannedItem.sort_order)).order_by(col(PlannedItem.sort_order).desc())
    ).first()
    item = PlannedItem(
        name=name,
        expected_amount=expected_amount,
        category_id=category_id,
        loan_id=loan_id,
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
    loan_id: int | None = None,
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
    item.loan_id = loan_id
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


def link_transaction(session: Session, *, planned_item_id: int, month: str, tx_id: int) -> bool:
    """Link a real transaction to a non-loan planned item for ``month``.

    Upserts the ``(item, month)`` status row with ``transaction_id`` set (this *is*
    the paid status — a linked payment, with its real amount and date). Refuses if
    the item or transaction is missing/deleted, if the transaction is already a loan
    payment (``loan_id`` set — that belongs to the Loans module), or if it's already
    linked to another planned item/month. Loan-backed items don't link here — their
    payment is managed in Loans (see module docstring)."""
    item = session.get(PlannedItem, planned_item_id)
    tx = session.get(Transaction, tx_id)
    if item is None or item.loan_id is not None:
        return False
    if tx is None or tx.deleted_at is not None or tx.loan_id is not None:
        return False
    if _is_tx_linked(session, tx_id, exclude=(planned_item_id, month)):
        return False

    payment = _payment_for(session, planned_item_id, month)
    if payment is None:
        payment = PlannedItemPayment(
            planned_item_id=planned_item_id, month=month, transaction_id=tx_id
        )
    else:
        payment.transaction_id = tx_id
        payment.paid_at = None  # a real link supersedes a manual tick
    session.add(payment)
    session.commit()

    return True


def unlink_transaction(session: Session, *, planned_item_id: int, month: str) -> bool:
    """Drop the linked transaction for ``month`` (the status reverts to unpaid).

    Removes only a transaction link — a manual tick is cleared via :func:`mark_unpaid`.
    Returns False if there was no linked transaction."""
    payment = _payment_for(session, planned_item_id, month)
    if payment is None or payment.transaction_id is None:
        return False

    session.delete(payment)
    session.commit()

    return True


def _is_tx_linked(session: Session, tx_id: int, *, exclude: tuple[int, str] | None = None) -> bool:
    """Whether ``tx_id`` is already linked to some planned item/month (other than
    ``exclude``, the (item, month) currently being (re)linked)."""
    for p in session.exec(
        select(PlannedItemPayment).where(PlannedItemPayment.transaction_id == tx_id)
    ).all():
        if exclude is None or (p.planned_item_id, p.month) != exclude:
            return True

    return False


def last_linked_amount(session: Session, planned_item_id: int, *, before_month: str) -> int | None:
    """The magnitude (positive minor units) of the most recent real transaction
    linked to this item in a month earlier than ``before_month``.

    Drives the "last: …" hint for a variable item with no fixed ``expected_amount``.
    Returns None when nothing has ever been linked before this month."""
    payment = session.exec(
        select(PlannedItemPayment)
        .where(
            PlannedItemPayment.planned_item_id == planned_item_id,
            col(PlannedItemPayment.transaction_id).is_not(None),
            PlannedItemPayment.month < before_month,
        )
        .order_by(col(PlannedItemPayment.month).desc())
    ).first()
    if payment is None or payment.transaction_id is None:
        return None

    tx = session.get(Transaction, payment.transaction_id)

    return abs(tx.amount) if tx is not None and tx.deleted_at is None else None


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
    display_name: str  # item name, or "<name> rata n/term" for a loan-backed line
    expected_amount: int | None  # signed minor units; None = unestimated (variable)
    real_amount: int | None  # signed; the linked transaction's amount, if linked
    paid: bool
    paid_date: date | None  # tick date, or the linked transaction's booked date
    overdue: bool  # unpaid and past its due day
    is_loan_backed: bool
    loan_id: int | None  # for the "manage in Loans" deep link
    installment_index: int | None  # loan-backed: the installment due this month
    transfer_title: str  # the payment-card title (loan-backed adds "umowa nr …")
    payee_account: str | None
    last_hint: int | None  # variable items: last linked real magnitude (positive)
    linked: bool  # a real transaction is linked (enables the unlink action)

    @property
    def effective_amount(self) -> int | None:
        """The figure that counts toward the totals: the expected amount when set,
        else the real linked amount. None = unestimated (neither known)."""
        return self.expected_amount if self.expected_amount is not None else self.real_amount

    @property
    def is_income(self) -> bool:
        amount = self.effective_amount
        return amount is not None and amount > 0

    @property
    def is_expense(self) -> bool:
        amount = self.effective_amount
        return amount is None or amount < 0


@dataclass(frozen=True, slots=True)
class PlanOverview:
    """The whole month: every active item plus the cashflow totals."""

    month: str
    rows: list[PlannedRow]
    income_total: int  # sum of positive effective amounts
    charges_total: int  # magnitude of negative effective amounts
    for_living: int  # income_total - charges_total (from known amounts)
    left_to_pay: int  # magnitude of unpaid charges (known amounts)
    unestimated_count: int  # items with no effective amount (FOR LIVING is approximate)


def _installment_for_month(schedule: Schedule, month: str) -> ScheduleRow | None:
    """The schedule row whose installment falls due in ``month`` (YYYY-MM), or None.

    None means the loan has no installment that month — before the first or past the
    last — which is exactly how a loan-backed line auto-expires from the view."""
    for row in schedule.rows:
        if row.due_date.strftime("%Y-%m") == month:
            return row

    return None


def _resolve_schedule(
    session: Session, loan_id: int, cache: dict[int, Schedule | None]
) -> Schedule | None:
    """Loan schedule for ``loan_id``, memoised per call. None if the loan is gone or
    its schedule can't be built (e.g. a misconfigured variable rate) — the caller
    then falls back to the item's own ``expected_amount`` so the line still shows."""
    if loan_id not in cache:
        try:
            cache[loan_id] = loan_queries.loan_schedule(session, loan_id)
        except LoanScheduleError:
            cache[loan_id] = None

    return cache[loan_id]


def plan_overview(session: Session, month: str, *, today: date | None = None) -> PlanOverview:
    """Derive the cashflow checklist for ``month`` from the active planned items.

    Each line's figure is its **effective amount** — the fixed ``expected_amount``
    when set, otherwise the real amount of a linked transaction. Income (positive)
    and charges (negative) are summed from the lines that *have* a figure; "FOR
    LIVING" is income minus charges. A variable line with neither an expected nor a
    linked amount is counted in ``unestimated_count`` and left out of the sums (never
    a silent zero), so the totals are flagged approximate rather than quietly wrong.

    A **loan-backed** line (``item.loan_id`` set) derives its name (``"<name> rata
    n/term"``), amount and paid status from the loan's schedule and reconciliation
    for ``month`` — the Loans module is the single source of truth, so it carries no
    ``PlannedItemPayment`` row. Such a line only appears in months with an installment
    due (auto-expiry past the term). If the loan or its schedule is unavailable it
    degrades to the item's own ``expected_amount``.

    ``overdue`` marks an unpaid charge whose ``due_day`` (or, for a loan-backed line
    with no ``due_day``, the installment's due date) has already passed (``today``
    defaults to the local date; injectable for tests).
    """
    today = today or local_today()
    items = list_planned_items(session, active_only=True)

    payments = {
        p.planned_item_id: p
        for p in session.exec(
            select(PlannedItemPayment).where(PlannedItemPayment.month == month)
        ).all()
    }
    linked_tx_ids = {p.transaction_id for p in payments.values() if p.transaction_id is not None}
    linked_txs = (
        {
            tx.id: tx
            for tx in session.exec(
                select(Transaction).where(col(Transaction.id).in_(linked_tx_ids))
            ).all()
        }
        if linked_tx_ids
        else {}
    )

    schedules: dict[int, Schedule | None] = {}
    rows: list[PlannedRow] = []
    for item in items:
        schedule = _resolve_schedule(session, item.loan_id, schedules) if item.loan_id else None

        if schedule is not None:
            installment = _installment_for_month(schedule, month)
            if installment is None:
                continue  # no installment due this month -> auto-expire from the view
            row = _loan_backed_row(session, item, month, installment, schedule, today)
        else:
            row = _manual_row(session, item, month, payments.get(item.id), linked_txs, today)

        rows.append(row)

    income_total = charges_total = left_to_pay = unestimated_count = 0
    for row in rows:
        amount = row.effective_amount
        if amount is None:
            unestimated_count += 1
        elif amount > 0:
            income_total += amount
        else:
            charges_total += -amount
            if not row.paid:
                left_to_pay += -amount

    return PlanOverview(
        month=month,
        rows=rows,
        income_total=income_total,
        charges_total=charges_total,
        for_living=income_total - charges_total,
        left_to_pay=left_to_pay,
        unestimated_count=unestimated_count,
    )


def _loan_backed_row(
    session: Session,
    item: PlannedItem,
    month: str,
    installment: ScheduleRow,
    schedule: Schedule,
    today: date,
) -> PlannedRow:
    """Build a loan-backed row: name/amount from the schedule, paid from reconciliation."""
    loan = loan_queries.get_loan(session, item.loan_id)
    recon = loan_queries.loan_reconciliation(session, item.loan_id, schedule)
    reconciled = recon.rows[installment.index - 1] if recon is not None else None
    payment_tx = reconciled.payment if reconciled is not None else None

    paid = payment_tx is not None
    expected = -installment.payment  # an installment is an outflow
    name = f"{item.name} rata {installment.index}/{loan.term_months}"
    title = name
    if loan.contract_number:
        title = f"{name} umowa nr {loan.contract_number}"

    due = _due_date(month, item.due_day) if item.due_day is not None else installment.due_date
    overdue = not paid and due < today

    return PlannedRow(
        item=item,
        display_name=name,
        expected_amount=expected,
        real_amount=payment_tx.amount if payment_tx is not None else None,
        paid=paid,
        paid_date=payment_tx.booked_date if payment_tx is not None else None,
        overdue=overdue,
        is_loan_backed=True,
        loan_id=item.loan_id,
        installment_index=installment.index,
        transfer_title=title,
        payee_account=item.payee_account,
        last_hint=None,
        linked=False,
    )


def _manual_row(
    session: Session,
    item: PlannedItem,
    month: str,
    payment: PlannedItemPayment | None,
    linked_txs: dict[int, Transaction],
    today: date,
) -> PlannedRow:
    """Build a non-loan row: amount from ``expected_amount`` (or a linked transaction),
    paid from the item's ``PlannedItemPayment`` status."""
    linked_tx = (
        linked_txs.get(payment.transaction_id) if payment and payment.transaction_id else None
    )
    paid = payment is not None and (payment.paid_at is not None or linked_tx is not None)
    paid_date = (
        linked_tx.booked_date
        if linked_tx is not None
        else (payment.paid_at.date() if payment and payment.paid_at else None)
    )
    overdue = not paid and item.due_day is not None and _due_date(month, item.due_day) < today

    expected = item.expected_amount
    real = linked_tx.amount if linked_tx is not None else None
    # A variable, still-unestimated line gets a "last time it was …" guide.
    hint = (
        last_linked_amount(session, item.id, before_month=month)
        if expected is None and real is None
        else None
    )

    return PlannedRow(
        item=item,
        display_name=item.name,
        expected_amount=expected,
        real_amount=real,
        paid=paid,
        paid_date=paid_date,
        overdue=overdue,
        is_loan_backed=False,
        loan_id=None,
        installment_index=None,
        transfer_title=item.name,
        payee_account=item.payee_account,
        last_hint=hint,
        linked=linked_tx is not None,
    )


def _month_window(month: str, window_days: int) -> tuple[date, date]:
    """The ``[start, end)`` date range covering ``month`` widened by ``window_days``
    on each side — the search window for transactions that could pay a line."""
    year, mon = (int(part) for part in month.split("-"))
    start = date(year, mon, 1)
    end = date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1)

    return start - timedelta(days=window_days), end + timedelta(days=window_days)


def suggest_links(
    session: Session,
    overview: PlanOverview,
    *,
    window_days: int,
    tolerance_pct: int,
) -> dict[int, list[Transaction]]:
    """Candidate transactions to link, per **unpaid non-loan** item id.

    Mirrors the loan payment suggester: an item with a fixed ``expected_amount``
    matches transactions of the same sign within ``tolerance_pct`` of that amount,
    near the due date; a variable item (no fixed amount) falls back to outflows in
    the window, nearest the due date first. Loan-backed and already-paid rows are
    skipped (their payments live in Loans, or are settled). A transaction already
    linked to a loan or another planned line is never offered."""
    lo, hi = _month_window(overview.month, window_days)
    linked = {
        p.transaction_id
        for p in session.exec(
            select(PlannedItemPayment).where(col(PlannedItemPayment.transaction_id).is_not(None))
        ).all()
    }
    candidates = [
        tx
        for tx in session.exec(
            select(Transaction).where(
                col(Transaction.deleted_at).is_(None),
                col(Transaction.loan_id).is_(None),
                Transaction.booked_date >= lo,
                Transaction.booked_date < hi,
            )
        ).all()
        if tx.id not in linked
    ]

    out: dict[int, list[Transaction]] = {}
    for row in overview.rows:
        if row.paid or row.is_loan_backed:
            continue
        due = _due_date(overview.month, row.item.due_day or 15)
        out[row.item.id] = _match_candidates(
            row.expected_amount,
            due,
            candidates,
            tolerance_pct=tolerance_pct,
            category_id=row.item.category_id,
        )

    return out


def _match_candidates(
    expected_amount: int | None,
    due: date,
    candidates: list[Transaction],
    *,
    tolerance_pct: int,
    category_id: int | None = None,
    limit: int = 6,
) -> list[Transaction]:
    """Rank candidate transactions for a line. With a fixed amount: same sign and
    within tolerance, closest amount then nearest date. Variable: outflows nearest
    the due date — narrowed to the line's own category when it has one (a utilities
    line shouldn't offer the week's groceries), falling back to all outflows only if
    that category has none in the window. Pure over a preloaded candidate list."""
    if expected_amount is not None and expected_amount != 0:
        target = abs(expected_amount)
        want_income = expected_amount > 0
        tolerance = target * tolerance_pct // 100
        matches = [
            tx
            for tx in candidates
            if (tx.amount > 0) == want_income and abs(abs(tx.amount) - target) <= tolerance
        ]
        matches.sort(
            key=lambda tx: (abs(abs(tx.amount) - target), abs((tx.booked_date - due).days))
        )
    else:
        matches = [tx for tx in candidates if tx.amount < 0]  # variable -> treat as a charge
        if category_id is not None:
            same_category = [tx for tx in matches if tx.category_id == category_id]
            if same_category:
                matches = same_category
        matches.sort(key=lambda tx: abs((tx.booked_date - due).days))

    return matches[:limit]


def for_living_trend(
    session: Session, *, months: int, today: date | None = None
) -> list[tuple[str, int]]:
    """ "FOR LIVING" (income − charges) per month for the last ``months`` months,
    ending at the current local month, oldest first (left-to-right on a chart).

    Each month is a full :func:`plan_overview` derivation, so the trend reflects the
    same figures the checklist shows (loan-backed installments included, variable
    items only when estimated). Cheap at household scale; ``today`` is injectable so
    the month window is deterministic in tests."""
    today = today or local_today()
    year, month = today.year, today.month
    keys: list[str] = []
    for _ in range(max(1, months)):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    keys.reverse()

    return [(key, plan_overview(session, key, today=today).for_living) for key in keys]
