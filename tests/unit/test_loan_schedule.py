"""Pure amortization logic — no DB (see src/expense_analyzer/loans.py)."""

from datetime import date

import pytest

from expense_analyzer.loans import (
    LoanScheduleError,
    _add_months,
    expand_monthly_rates,
    generate_schedule,
    reconcile,
)
from expense_analyzer.models import InstallmentType, RateType, Transaction

START = date(2026, 1, 15)


def _tx(amount: int, day: int, *, installment_index: int | None = None) -> Transaction:
    """An in-memory transaction for reconcile tests (never touches the DB)."""
    return Transaction(
        account_id=1,
        import_batch_id=1,
        amount=amount,
        booked_date=date(2026, 2, day),
        raw_description="loan payment",
        fingerprint=f"fp-{amount}-{day}",
        loan_installment_index=installment_index,
    )


# --- _add_months -----------------------------------------------------------


def test_add_months_basic() -> None:
    assert _add_months(date(2026, 1, 15), 1) == date(2026, 2, 15)
    assert _add_months(date(2026, 1, 15), 12) == date(2027, 1, 15)


def test_add_months_clamps_to_shorter_month() -> None:
    # Jan 31 + 1 month has no Feb 31 -> clamp to end of February.
    assert _add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    # Leap year February has 29 days.
    assert _add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)


# --- generate_schedule: invariants -----------------------------------------


def test_equal_fully_amortizes_and_payments_are_level() -> None:
    principal = 30_000_000  # 300,000 PLN
    schedule = generate_schedule(
        principal=principal,
        monthly_rates_bp=[720] * 360,  # 7.2% annual, fixed
        installment_type=InstallmentType.equal,
        start_date=START,
        term_months=360,
    )

    assert len(schedule.rows) == 360
    assert schedule.rows[-1].balance_after == 0
    # Principal portions repay exactly the borrowed amount.
    assert sum(r.principal_paid for r in schedule.rows) == principal
    # Annuity: every installment but the last is the same total amount.
    levels = {r.payment for r in schedule.rows[:-1]}
    assert len(levels) == 1
    # Real interest is paid on a positive rate.
    assert schedule.total_interest > 0
    assert schedule.total_paid == principal + schedule.total_interest


def test_decreasing_fully_amortizes_and_payments_shrink() -> None:
    principal = 12_000_000
    schedule = generate_schedule(
        principal=principal,
        monthly_rates_bp=[600] * 120,
        installment_type=InstallmentType.decreasing,
        start_date=START,
        term_months=120,
    )

    assert schedule.rows[-1].balance_after == 0
    assert sum(r.principal_paid for r in schedule.rows) == principal
    # Decreasing: interest falls with the balance, so each payment is <= the prior.
    payments = [r.payment for r in schedule.rows]
    assert all(b <= a for a, b in zip(payments, payments[1:], strict=False))
    assert payments[0] > payments[-1]


def test_zero_rate_has_no_interest_and_amortizes() -> None:
    for kind in (InstallmentType.equal, InstallmentType.decreasing):
        schedule = generate_schedule(
            principal=100_000,
            monthly_rates_bp=[0] * 3,
            installment_type=kind,
            start_date=START,
            term_months=3,
        )
        assert schedule.total_interest == 0
        assert sum(r.principal_paid for r in schedule.rows) == 100_000
        assert schedule.rows[-1].balance_after == 0


def test_single_month_loan_pays_principal_plus_one_period_interest() -> None:
    schedule = generate_schedule(
        principal=1_000_000,
        monthly_rates_bp=[1200],  # 12% annual -> 1% monthly
        installment_type=InstallmentType.equal,
        start_date=START,
        term_months=1,
    )
    [row] = schedule.rows
    assert row.principal_paid == 1_000_000
    assert row.interest == 10_000  # 1% of 1,000,000
    assert row.payment == 1_010_000
    assert row.balance_after == 0


def test_first_due_date_is_one_month_after_start() -> None:
    schedule = generate_schedule(
        principal=100_000,
        monthly_rates_bp=[500] * 2,
        installment_type=InstallmentType.equal,
        start_date=date(2026, 1, 15),
        term_months=2,
    )
    assert schedule.rows[0].due_date == date(2026, 2, 15)
    assert schedule.rows[1].due_date == date(2026, 3, 15)


def test_variable_rate_jump_recomputes_upward_without_negative_amortization() -> None:
    # Rate doubles halfway; the equal installment must rise to keep amortizing.
    rates = [400] * 6 + [1600] * 6
    schedule = generate_schedule(
        principal=6_000_000,
        monthly_rates_bp=rates,
        installment_type=InstallmentType.equal,
        start_date=START,
        term_months=12,
    )
    assert all(r.principal_paid >= 0 for r in schedule.rows)
    assert schedule.rows[-1].balance_after == 0
    # Payment after the rate jump is higher than before it.
    assert schedule.rows[6].payment > schedule.rows[0].payment


def test_term_must_match_rate_list_length() -> None:
    with pytest.raises(LoanScheduleError, match="term_months"):
        generate_schedule(
            principal=100_000,
            monthly_rates_bp=[500] * 3,
            installment_type=InstallmentType.equal,
            start_date=START,
            term_months=4,
        )


def test_rejects_nonpositive_principal_and_term() -> None:
    with pytest.raises(LoanScheduleError):
        generate_schedule(
            principal=0,
            monthly_rates_bp=[500],
            installment_type=InstallmentType.equal,
            start_date=START,
            term_months=1,
        )


# --- expand_monthly_rates --------------------------------------------------


def test_expand_fixed_is_flat() -> None:
    rates = expand_monthly_rates(
        rate_type=RateType.fixed,
        rate_bp=725,
        term_months=4,
        start_date=START,
        base_rate_changes=[],
    )
    assert rates == [725, 725, 725, 725]


def test_expand_variable_steps_with_base_rate_plus_margin() -> None:
    # Base 500 from start, jumps to 700 effective in the 3rd installment month.
    # Installments due 2026-02-15, 03-15, 04-15, 05-15; margin 150.
    rates = expand_monthly_rates(
        rate_type=RateType.variable,
        rate_bp=150,
        term_months=4,
        start_date=START,
        base_rate_changes=[(date(2026, 1, 15), 500), (date(2026, 4, 1), 700)],
    )
    assert rates == [650, 650, 850, 850]


def test_expand_variable_raises_without_initial_base_rate() -> None:
    with pytest.raises(LoanScheduleError, match="no base rate"):
        expand_monthly_rates(
            rate_type=RateType.variable,
            rate_bp=150,
            term_months=2,
            start_date=START,
            base_rate_changes=[(date(2026, 4, 1), 700)],  # only effective later
        )


# --- reconcile -------------------------------------------------------------


def _schedule(term: int):
    return generate_schedule(
        principal=300_000,
        monthly_rates_bp=[0] * term,
        installment_type=InstallmentType.equal,
        start_date=START,
        term_months=term,
    )


def test_reconcile_matches_pinned_payments() -> None:
    schedule = _schedule(3)
    planned = schedule.rows[0].payment
    payment = _tx(-planned, day=16, installment_index=1)
    result = reconcile(schedule, [payment])

    assert len(result.rows) == 3
    assert result.rows[0].payment is payment
    assert result.rows[0].amount_diff == 0
    assert result.rows[0].date_gap_days == 1  # due 02-15, paid 02-16
    assert result.unmatched_payments == []


def test_reconcile_missing_middle_payment_does_not_shift_tail() -> None:
    schedule = _schedule(3)
    # Pay installments 1 and 3, skip 2.
    p1 = _tx(-schedule.rows[0].payment, day=15, installment_index=1)
    p3 = _tx(-schedule.rows[2].payment, day=15, installment_index=3)
    result = reconcile(schedule, [p1, p3])

    assert result.rows[0].payment is p1
    assert result.rows[1].payment is None  # missing, not back-filled
    assert result.rows[2].payment is p3


def test_reconcile_extra_payment_goes_to_unmatched() -> None:
    schedule = _schedule(2)
    extra = _tx(-50_000, day=20, installment_index=None)  # linked but unassigned
    beyond = _tx(-50_000, day=21, installment_index=99)  # past the term
    result = reconcile(schedule, [extra, beyond])

    assert all(r.payment is None for r in result.rows)
    assert result.unmatched_payments == [extra, beyond]
