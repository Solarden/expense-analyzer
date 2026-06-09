"""Loan queries — the DB side of loans and their repayment schedules.

The schedule *math* is pure and lives in :mod:`expense_analyzer.loans`; this
module is the only place that touches the DB for loans: it stores the loan and
its variable-rate history, recomputes the schedule on demand (never persisted —
a variable schedule changes when a base-rate observation is added), and links
real installment transactions to the plan for the plan-vs-reality view.

Suggesting which transaction pays which installment mirrors the transfers
suggest/confirm pattern (see :mod:`expense_analyzer.queries.transfers`): a
candidate is an outflow on a non-loan account near a due date and close to the
planned amount; an unambiguous match auto-links, the rest are manual suggestions.
"""

from dataclasses import dataclass
from datetime import date

from sqlmodel import Session, col, select

from expense_analyzer.loans import (
    Reconciliation,
    Schedule,
    expand_monthly_rates,
    generate_schedule,
    reconcile,
)
from expense_analyzer.models import (
    Account,
    AccountType,
    Loan,
    LoanCreate,
    LoanRateChange,
    RateType,
    Transaction,
)


def list_loans(session: Session) -> list[Loan]:
    return list(session.exec(select(Loan).order_by(col(Loan.start_date).desc())).all())


def get_loan(session: Session, loan_id: int) -> Loan | None:
    return session.get(Loan, loan_id)


def create_loan(session: Session, data: LoanCreate) -> Loan:
    """Create a loan from a :class:`LoanCreate` input. For a variable rate,
    ``data.initial_base_rate_bp`` seeds a rate change effective on the start date
    so the schedule has a rate from month 1."""
    # initial_base_rate_bp is not a Loan column — it seeds a rate change below.
    loan = Loan(**data.model_dump(exclude={"initial_base_rate_bp"}))
    session.add(loan)
    session.commit()
    session.refresh(loan)

    if data.rate_type is RateType.variable and data.initial_base_rate_bp is not None:
        add_rate_change(
            session,
            loan_id=loan.id,
            effective_date=data.start_date,
            base_rate_bp=data.initial_base_rate_bp,
        )

    return loan


def update_loan(session: Session, loan_id: int, data: LoanCreate) -> Loan | None:
    """Update a loan's definition; the schedule recomputes from it on demand.

    Nothing about the amortization plan is stored (see :func:`loan_schedule`), so
    rewriting these fields *is* the recalculation — there's no cached schedule to
    invalidate. For a variable rate, the **start-date observation** (the one
    :func:`create_loan` seeded on the original start date) tracks the loan's start
    date and initial base rate, so it's moved/updated to match ``data.start_date``
    / ``data.initial_base_rate_bp`` (later observations the user added on the detail
    page are left intact — matching on the old start date avoids mistargeting one of
    them when the start date moves). A loan with no observation yet — e.g. one just
    switched from fixed to variable — gets one seeded; a loan switched to fixed keeps
    any old observations, harmless since a fixed schedule ignores them. Returns None
    if the loan doesn't exist.
    """
    loan = session.get(Loan, loan_id)
    if loan is None:
        return None

    old_start = loan.start_date  # the start-date observation sits here (pre-edit)
    # initial_base_rate_bp is not a Loan column — it seeds/updates a rate change.
    for field, value in data.model_dump(exclude={"initial_base_rate_bp"}).items():
        setattr(loan, field, value)
    session.add(loan)

    if data.rate_type is RateType.variable and data.initial_base_rate_bp is not None:
        changes = list_rate_changes(session, loan_id)  # ordered by effective_date
        # The seed is the observation on the old start date; fall back to the
        # earliest if it's gone (e.g. hand-edited history).
        seed = next((c for c in changes if c.effective_date == old_start), None)
        seed = seed or (changes[0] if changes else None)
        if seed is not None:
            seed.effective_date = data.start_date
            seed.base_rate_bp = data.initial_base_rate_bp
            session.add(seed)
        else:
            session.add(
                LoanRateChange(
                    loan_id=loan_id,
                    effective_date=data.start_date,
                    base_rate_bp=data.initial_base_rate_bp,
                )
            )
    session.commit()
    session.refresh(loan)

    return loan


def delete_loan(session: Session, loan_id: int) -> bool:
    """Delete a loan, its rate-change history, and unlink any payments.

    Loans aren't financial records to preserve (unlike transactions, which are
    soft-deleted) — a wrong definition is just re-entered. Linked transactions are
    kept but un-pinned (``loan_id``/``loan_installment_index`` cleared)."""
    loan = session.get(Loan, loan_id)
    if loan is None:
        return False

    for change in list_rate_changes(session, loan_id):
        session.delete(change)
    # Clear the link on *every* referencing transaction, including soft-deleted
    # ones — a payment can be linked and then soft-deleted by a batch rollback,
    # and with foreign_keys=ON a lingering loan_id would block the loan delete.
    referencing = session.exec(select(Transaction).where(Transaction.loan_id == loan_id)).all()
    for tx in referencing:
        tx.loan_id = None
        tx.loan_installment_index = None
    session.add_all(referencing)
    session.delete(loan)
    session.commit()

    return True


def add_rate_change(
    session: Session, *, loan_id: int, effective_date: date, base_rate_bp: int
) -> LoanRateChange:
    change = LoanRateChange(
        loan_id=loan_id, effective_date=effective_date, base_rate_bp=base_rate_bp
    )
    session.add(change)
    session.commit()
    session.refresh(change)

    return change


def list_rate_changes(session: Session, loan_id: int) -> list[LoanRateChange]:
    return list(
        session.exec(
            select(LoanRateChange)
            .where(LoanRateChange.loan_id == loan_id)
            .order_by(col(LoanRateChange.effective_date))
        ).all()
    )


def loan_schedule(session: Session, loan_id: int) -> Schedule | None:
    """Recompute the amortization schedule for a loan, or None if it's missing."""
    loan = session.get(Loan, loan_id)
    if loan is None:
        return None

    changes = [(c.effective_date, c.base_rate_bp) for c in list_rate_changes(session, loan_id)]
    monthly_rates = expand_monthly_rates(
        rate_type=loan.rate_type,
        rate_bp=loan.rate_bp,
        term_months=loan.term_months,
        start_date=loan.start_date,
        base_rate_changes=changes,
    )

    return generate_schedule(
        principal=loan.principal,
        monthly_rates_bp=monthly_rates,
        installment_type=loan.installment_type,
        start_date=loan.start_date,
        term_months=loan.term_months,
    )


def outstanding_principal(
    session: Session, loan_id: int, *, as_of: date | None = None
) -> int | None:
    """Remaining principal (minor units) as of ``as_of`` (default: today, local).

    Uses the *planned* schedule: principal still owed is the initial principal
    minus the principal portion of every installment due on or before the date.
    This is the plan's view of the debt (it ignores missed/extra payments), which
    is what the net-worth view wants — a clean snapshot from the amortization plan.
    Returns None if the loan is missing or its schedule can't be built.
    """
    from expense_analyzer.clock import local_today

    schedule = loan_schedule(session, loan_id)
    loan = session.get(Loan, loan_id)
    if schedule is None or loan is None:
        return None

    cutoff = as_of or local_today()
    paid = sum(row.principal_paid for row in schedule.rows if row.due_date <= cutoff)

    return loan.principal - paid


def linked_payments(session: Session, loan_id: int) -> list[Transaction]:
    """Non-deleted transactions linked to this loan as installment payments."""
    return list(
        session.exec(
            select(Transaction).where(
                Transaction.loan_id == loan_id,
                col(Transaction.deleted_at).is_(None),
            )
        ).all()
    )


def loan_reconciliation(
    session: Session, loan_id: int, schedule: Schedule | None = None
) -> Reconciliation | None:
    """Plan vs reality: the schedule with linked payments attached.

    Pass an already-computed ``schedule`` to avoid recomputing it (the detail
    page also needs it for payment suggestions); omit it and it's loaded here."""
    if schedule is None:
        schedule = loan_schedule(session, loan_id)
    if schedule is None:
        return None

    return reconcile(schedule, linked_payments(session, loan_id))


def link_payment(session: Session, *, loan_id: int, tx_id: int, installment_index: int) -> bool:
    """Pin a transaction to a loan installment. Returns False if either is missing
    or the transaction is already linked to a loan."""
    loan = session.get(Loan, loan_id)
    tx = session.get(Transaction, tx_id)
    if loan is None or tx is None or tx.deleted_at is not None:
        return False
    if tx.loan_id is not None:
        return False

    tx.loan_id = loan_id
    tx.loan_installment_index = installment_index
    session.add(tx)
    session.commit()

    return True


def unlink_payment(session: Session, tx_id: int) -> bool:
    """Unpin a transaction from its loan installment."""
    tx = session.get(Transaction, tx_id)
    if tx is None or tx.loan_id is None:
        return False

    tx.loan_id = None
    tx.loan_installment_index = None
    session.add(tx)
    session.commit()

    return True


@dataclass(frozen=True, slots=True)
class PaymentSuggestion:
    """A candidate transaction that may pay a given installment."""

    installment_index: int
    transaction: Transaction
    amount_diff: int  # |amount| - planned payment
    date_gap_days: int


def suggest_payments(
    session: Session,
    loan: Loan,
    schedule: Schedule,
    *,
    window_days: int,
    tolerance_pct: int,
) -> list[PaymentSuggestion]:
    """Candidate outflows that could pay still-unpaid installments.

    A candidate is a non-deleted outflow (``amount < 0``) not already linked to a
    loan, on an account other than the loan's own account, booked within
    ``window_days`` of an unpaid installment's due date, and within
    ``tolerance_pct`` of that installment's planned payment. Unlike transfers,
    nothing is auto-linked: a loan payment is higher-stakes and ambiguity is
    common (a level annuity makes every installment the same amount), so this
    returns every candidate edge sorted by closeness and confirmation is always
    manual. Results are ordered by ``(installment_index, date_gap, |amount_diff|)``.
    """
    paid_indexes = {
        tx.loan_installment_index
        for tx in linked_payments(session, loan.id)
        if tx.loan_installment_index is not None
    }
    candidates = session.exec(
        select(Transaction).where(
            col(Transaction.deleted_at).is_(None),
            col(Transaction.loan_id).is_(None),
            Transaction.amount < 0,
            Transaction.account_id != loan.account_id,
        )
    ).all()

    suggestions: list[PaymentSuggestion] = []
    for row in schedule.rows:
        if row.index in paid_indexes:
            continue
        tolerance = row.payment * tolerance_pct // 100
        for tx in candidates:
            gap = abs((tx.booked_date - row.due_date).days)
            if gap > window_days:
                continue
            amount_diff = abs(tx.amount) - row.payment
            if abs(amount_diff) > tolerance:
                continue
            suggestions.append(
                PaymentSuggestion(
                    installment_index=row.index,
                    transaction=tx,
                    amount_diff=amount_diff,
                    date_gap_days=gap,
                )
            )

    suggestions.sort(key=lambda s: (s.installment_index, s.date_gap_days, abs(s.amount_diff)))

    return suggestions


def loan_accounts(session: Session) -> list[Account]:
    """Accounts of type ``loan`` — the candidates a loan can attach to."""
    return list(
        session.exec(
            select(Account).where(Account.type == AccountType.loan).order_by(col(Account.name))
        ).all()
    )
