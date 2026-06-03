"""Loan amortization — the schedule math, kept pure (no DB).

A loan (design §5, §7.4) is repaid over ``term_months`` installments. Two styles:

- **equal** (annuity): the total installment is constant; each month interest is
  taken on the outstanding balance and the rest pays down principal.
- **decreasing**: the principal portion is constant; interest (and so the total
  installment) shrinks as the balance falls.

For a **variable** rate the annual rate changes over the life of the loan (e.g. a
WIBOR fix moves), so the schedule is recomputed for the *remaining* term on the
*remaining* balance from the month the rate changes — standard Polish mortgage
behaviour. To keep this module DB-free and trivially testable, callers pass a
pre-expanded per-month rate list (:func:`expand_monthly_rates` builds it from the
rate history); the math here never needs to know fixed from variable.

Money is integer **minor units** throughout (never float; see :mod:`money`).
:class:`~decimal.Decimal` is used only for the rate and the annuity term, and the
result is rounded half-up to minor units. The balance is carried as an exact int
the whole way, and the **last installment pays off whatever is left** so the
schedule always ends at a balance of exactly zero regardless of rounding.

Linking real payments to the plan (plan vs reality) lives in :func:`reconcile`,
which is also pure over a preloaded list of transactions — the DB side is in
:mod:`expense_analyzer.queries.loans`.
"""

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from expense_analyzer.models import InstallmentType, RateType, Transaction

# Basis points per unit (100% == 10000 bp) and months per year, as Decimal so the
# monthly rate is computed without ever touching a float.
_BP_PER_UNIT = Decimal(10000)
_MONTHS_PER_YEAR = Decimal(12)


class LoanScheduleError(ValueError):
    """Raised when a schedule cannot be generated (bad inputs / missing rate)."""


@dataclass(frozen=True, slots=True)
class ScheduleRow:
    """One installment in the amortization plan. Amounts are minor units."""

    index: int  # 1-based installment number
    due_date: date
    payment: int  # total installment (interest + principal_paid)
    interest: int
    principal_paid: int
    balance_after: int  # outstanding balance after this installment


@dataclass(frozen=True, slots=True)
class Schedule:
    rows: list[ScheduleRow]

    @property
    def total_interest(self) -> int:
        return sum(r.interest for r in self.rows)

    @property
    def total_paid(self) -> int:
        return sum(r.payment for r in self.rows)


@dataclass(frozen=True, slots=True)
class ReconciledRow:
    """A scheduled installment paired with the real payment linked to it (if any)."""

    scheduled: ScheduleRow
    payment: Transaction | None  # None == no payment linked yet (missing)
    amount_diff: int | None  # actual |amount| - planned payment (minor units)
    date_gap_days: int | None  # |booked_date - due_date|


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """Outcome of :func:`reconcile`: the plan with reality attached.

    ``rows`` is one entry per scheduled installment (``payment is None`` ==
    unpaid/missing). ``unmatched_payments`` are transactions linked to the loan
    that don't pin to a real schedule row (no index, or an index past the term) —
    overpayments, prepayments or stale links.
    """

    rows: list[ReconciledRow]
    unmatched_payments: list[Transaction]


def _add_months(start: date, k: int) -> date:
    """``start`` plus ``k`` months, clamping the day to the target month length.

    Jan 31 + 1 month -> Feb 28 (or 29 in a leap year). Stdlib only."""
    month_index = start.month - 1 + k
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])

    return date(year, month, day)


def _monthly_rate(annual_bp: int) -> Decimal:
    """Monthly interest rate as a Decimal fraction, from an annual rate in bp."""
    return Decimal(annual_bp) / _BP_PER_UNIT / _MONTHS_PER_YEAR


def _round(value: Decimal) -> int:
    """Round a Decimal amount half-up to integer minor units."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _annuity_payment(balance: int, monthly_rate: Decimal, remaining: int) -> Decimal:
    """The level annuity installment for ``balance`` over ``remaining`` months.

    ``A = B*r / (1 - (1+r)^-n)``; for a zero rate it degenerates to ``B / n``.
    Returned as a Decimal — rounding happens once, at the row."""
    if monthly_rate == 0:
        return Decimal(balance) / Decimal(remaining)

    factor = 1 - (1 + monthly_rate) ** -remaining

    return Decimal(balance) * monthly_rate / factor


def generate_schedule(
    *,
    principal: int,
    monthly_rates_bp: list[int],
    installment_type: InstallmentType,
    start_date: date,
    term_months: int,
) -> Schedule:
    """Build the full amortization schedule.

    ``monthly_rates_bp[i]`` is the annual rate (basis points) applied to
    installment ``i+1`` — for a fixed loan every entry is the same; for a variable
    loan it steps when the base rate changes. The first installment falls due one
    month after ``start_date`` (disbursement), so ``due_date(i) = start + i``.

    The balance is carried as an exact int and the final installment repays the
    residual outright, so ``balance_after`` lands on exactly zero. Raises
    :class:`LoanScheduleError` on inconsistent inputs.
    """
    if term_months != len(monthly_rates_bp):
        raise LoanScheduleError(
            f"term_months ({term_months}) != number of monthly rates ({len(monthly_rates_bp)})"
        )
    if term_months <= 0:
        raise LoanScheduleError(f"term_months must be positive, got {term_months}")
    if principal <= 0:
        raise LoanScheduleError(f"principal must be positive, got {principal}")

    rows: list[ScheduleRow] = []
    balance = principal
    # For equal installments we hold the annuity payment and only recompute it when
    # the rate changes (or on the first month). Unused for decreasing.
    annuity = Decimal(0)
    prev_rate_bp: int | None = None
    # Constant nominal principal portion for decreasing installments.
    flat_principal = _round(Decimal(principal) / Decimal(term_months))

    for i, annual_bp in enumerate(monthly_rates_bp, start=1):
        remaining = term_months - i + 1
        monthly_rate = _monthly_rate(annual_bp)
        interest = _round(Decimal(balance) * monthly_rate)

        if i == term_months:
            # Last row: pay off the residual balance exactly, whatever rounding left.
            principal_paid = balance
        elif installment_type is InstallmentType.equal:
            if annual_bp != prev_rate_bp:
                annuity = _annuity_payment(balance, monthly_rate, remaining)
            principal_paid = _round(annuity) - interest
        else:  # decreasing
            principal_paid = flat_principal

        prev_rate_bp = annual_bp

        if principal_paid < 0:
            raise LoanScheduleError(
                f"installment {i}: negative amortization (interest {interest} exceeds "
                f"payment) — check the rate inputs"
            )

        balance -= principal_paid
        rows.append(
            ScheduleRow(
                index=i,
                due_date=_add_months(start_date, i),
                payment=principal_paid + interest,
                interest=interest,
                principal_paid=principal_paid,
                balance_after=balance,
            )
        )

    if balance != 0:
        # Defensive: the last-row residual rule should guarantee this.
        raise LoanScheduleError(f"schedule did not fully amortize (balance {balance} left)")

    return Schedule(rows=rows)


def expand_monthly_rates(
    *,
    rate_type: RateType,
    rate_bp: int,
    term_months: int,
    start_date: date,
    base_rate_changes: list[tuple[date, int]],
) -> list[int]:
    """Per-installment annual rate (bp) over the loan's life.

    Fixed: every month is ``rate_bp``. Variable: each installment's rate is the
    latest base rate effective on its due date plus the margin (``rate_bp``).
    ``base_rate_changes`` is ``(effective_date, base_rate_bp)`` pairs; resolution
    is at month granularity (no daily proration — a deliberate household-tool
    simplification). Raises :class:`LoanScheduleError` if a variable loan has no
    base rate effective by the first installment.
    """
    if rate_type is RateType.fixed:
        return [rate_bp] * term_months

    changes = sorted(base_rate_changes)
    rates: list[int] = []
    for i in range(1, term_months + 1):
        due = _add_months(start_date, i)
        base = _latest_base_rate(changes, due)
        if base is None:
            raise LoanScheduleError(
                f"variable loan has no base rate effective by installment {i} "
                f"(due {due.isoformat()}); add a rate change on or before the start date"
            )
        rates.append(base + rate_bp)

    return rates


def _latest_base_rate(changes: list[tuple[date, int]], on: date) -> int | None:
    """Base rate (bp) in effect on ``on``, from sorted ``changes``, or None."""
    base: int | None = None
    for effective_date, base_rate_bp in changes:
        if effective_date <= on:
            base = base_rate_bp
        else:
            break

    return base


def reconcile(schedule: Schedule, payments: list[Transaction]) -> Reconciliation:
    """Attach real payments to scheduled installments (plan vs reality).

    Each payment is pinned to a schedule row by ``loan_installment_index`` (set
    when it was linked), so a missed month leaves its row ``payment=None`` without
    shifting the rest, and an overpayment/prepayment with no valid index falls
    into ``unmatched_payments``. Amounts compare magnitudes (a payment is an
    outflow, ``amount < 0``) against the planned installment.
    """
    by_index: dict[int, Transaction] = {}
    unmatched: list[Transaction] = []
    valid_indexes = {row.index for row in schedule.rows}
    for payment in payments:
        idx = payment.loan_installment_index
        if idx is not None and idx in valid_indexes and idx not in by_index:
            by_index[idx] = payment
        else:
            unmatched.append(payment)

    rows: list[ReconciledRow] = []
    for row in schedule.rows:
        payment = by_index.get(row.index)
        if payment is None:
            rows.append(
                ReconciledRow(scheduled=row, payment=None, amount_diff=None, date_gap_days=None)
            )
        else:
            rows.append(
                ReconciledRow(
                    scheduled=row,
                    payment=payment,
                    amount_diff=abs(payment.amount) - row.payment,
                    date_gap_days=abs((payment.booked_date - row.due_date).days),
                )
            )

    return Reconciliation(rows=rows, unmatched_payments=unmatched)
