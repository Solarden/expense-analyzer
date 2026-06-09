"""Planned-item Phase 19b query logic: transaction linking, auto-suggestions,
loan-backed derivation and the last-month hint.

All over the same model 19a created (no migration). ``plan_overview`` takes an
injectable ``today`` so overdue stays deterministic.
"""

from collections.abc import Callable
from datetime import date

from sqlmodel import Session

from expense_analyzer.models import Account, AccountType, Loan, PlannedItem, Transaction
from expense_analyzer.queries import loans as loan_queries
from expense_analyzer.queries import planned as pq

# --- transaction linking (non-loan items) ----------------------------------


def test_link_transaction_marks_paid_with_real_amount(
    db_session: Session,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
    make_planned_item: Callable[..., PlannedItem],
) -> None:
    acc = make_account()
    tx = make_transaction(account_id=acc.id, amount=-120_00, booked_date=date(2026, 6, 10))
    item = make_planned_item(name="Internet", expected_amount=None)  # variable

    assert pq.link_transaction(db_session, planned_item_id=item.id, month="2026-06", tx_id=tx.id)
    row = pq.plan_overview(db_session, "2026-06").rows[0]
    assert row.paid is True
    assert row.real_amount == -120_00
    assert row.effective_amount == -120_00  # a variable line counts at its real amount
    assert pq.plan_overview(db_session, "2026-06").charges_total == 120_00


def test_link_rejects_tx_already_linked_elsewhere(
    db_session: Session,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
    make_planned_item: Callable[..., PlannedItem],
) -> None:
    acc = make_account()
    tx = make_transaction(account_id=acc.id, amount=-100_00, booked_date=date(2026, 6, 10))
    a = make_planned_item(name="A", expected_amount=-100_00)
    b = make_planned_item(name="B", expected_amount=-100_00)

    assert pq.link_transaction(db_session, planned_item_id=a.id, month="2026-06", tx_id=tx.id)
    # Same transaction can't pay a second line.
    assert not pq.link_transaction(db_session, planned_item_id=b.id, month="2026-06", tx_id=tx.id)
    # Re-linking the same (item, month) is fine (idempotent).
    assert pq.link_transaction(db_session, planned_item_id=a.id, month="2026-06", tx_id=tx.id)


def test_link_rejects_loan_backed_item(
    db_session: Session,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
    make_planned_item: Callable[..., PlannedItem],
    make_loan: Callable[..., Loan],
) -> None:
    loan_acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=loan_acc.id)
    bank = make_account()
    tx = make_transaction(account_id=bank.id, amount=-100_00, booked_date=date(2026, 6, 10))
    item = make_planned_item(name="Mortgage", expected_amount=None, loan_id=loan.id)

    # Loan-backed payments are managed in the Loans module, not here.
    assert not pq.link_transaction(
        db_session, planned_item_id=item.id, month="2026-06", tx_id=tx.id
    )


def test_unlink_transaction(
    db_session: Session,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
    make_planned_item: Callable[..., PlannedItem],
) -> None:
    acc = make_account()
    tx = make_transaction(account_id=acc.id, amount=-100_00, booked_date=date(2026, 6, 10))
    item = make_planned_item(name="Rent", expected_amount=-100_00)
    pq.link_transaction(db_session, planned_item_id=item.id, month="2026-06", tx_id=tx.id)

    assert pq.unlink_transaction(db_session, planned_item_id=item.id, month="2026-06")
    assert pq.plan_overview(db_session, "2026-06").rows[0].paid is False
    assert not pq.unlink_transaction(db_session, planned_item_id=item.id, month="2026-06")


# --- auto-suggestions -------------------------------------------------------


def test_suggest_links_matches_by_amount_and_date(
    db_session: Session,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
    make_planned_item: Callable[..., PlannedItem],
) -> None:
    acc = make_account()
    item = make_planned_item(name="Rent", expected_amount=-2000_00, due_day=10)
    near = make_transaction(account_id=acc.id, amount=-1980_00, booked_date=date(2026, 6, 9))
    far_amount = make_transaction(account_id=acc.id, amount=-500_00, booked_date=date(2026, 6, 9))

    overview = pq.plan_overview(db_session, "2026-06")
    suggestions = pq.suggest_links(db_session, overview, window_days=5, tolerance_pct=5)

    ids = [tx.id for tx in suggestions[item.id]]
    assert near.id in ids  # within 5% of 2000
    assert far_amount.id not in ids  # way off amount


def test_suggest_links_excludes_already_linked_and_loan_payments(
    db_session: Session,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
    make_planned_item: Callable[..., PlannedItem],
    make_loan: Callable[..., Loan],
) -> None:
    loan_acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=loan_acc.id)
    acc = make_account()
    item = make_planned_item(name="Rent", expected_amount=-100_00, due_day=10)
    loan_tx = make_transaction(
        account_id=acc.id, amount=-100_00, booked_date=date(2026, 6, 9), loan_id=loan.id
    )
    linked_tx = make_transaction(account_id=acc.id, amount=-100_00, booked_date=date(2026, 6, 9))
    other = make_planned_item(name="Phone", expected_amount=-100_00)
    pq.link_transaction(db_session, planned_item_id=other.id, month="2026-06", tx_id=linked_tx.id)

    overview = pq.plan_overview(db_session, "2026-06")
    suggestions = pq.suggest_links(db_session, overview, window_days=5, tolerance_pct=5)

    ids = [tx.id for tx in suggestions[item.id]]
    assert loan_tx.id not in ids  # already a loan payment
    assert linked_tx.id not in ids  # already linked to another line


def test_last_month_hint(
    db_session: Session,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
    make_planned_item: Callable[..., PlannedItem],
) -> None:
    acc = make_account()
    item = make_planned_item(name="Electricity", expected_amount=None)  # variable
    may_tx = make_transaction(account_id=acc.id, amount=-243_18, booked_date=date(2026, 5, 12))
    pq.link_transaction(db_session, planned_item_id=item.id, month="2026-05", tx_id=may_tx.id)

    # In June it's not yet paid, so we show what it was last time.
    row = pq.plan_overview(db_session, "2026-06").rows[0]
    assert row.paid is False
    assert row.last_hint == 243_18


# --- loan-backed derivation -------------------------------------------------


def test_loan_backed_row_derives_from_schedule(
    db_session: Session,
    make_account: Callable[..., Account],
    make_planned_item: Callable[..., PlannedItem],
    make_loan: Callable[..., Loan],
) -> None:
    loan_acc = make_account(name="Mortgage", type=AccountType.loan)
    # Fixed loan, starts 2026-01-15, 12 months -> installment 1 due 2026-02-15.
    loan = make_loan(account_id=loan_acc.id, term_months=12, contract_number="BLP0068094260")
    make_planned_item(name="Mortgage", expected_amount=None, loan_id=loan.id)

    row = pq.plan_overview(db_session, "2026-02").rows[0]
    assert row.is_loan_backed is True
    assert row.display_name == "Mortgage rata 1/12"
    assert row.installment_index == 1
    assert row.expected_amount is not None and row.expected_amount < 0  # an outflow
    assert "umowa nr BLP0068094260" in row.transfer_title


def test_loan_backed_auto_expires_past_term(
    db_session: Session,
    make_account: Callable[..., Account],
    make_planned_item: Callable[..., PlannedItem],
    make_loan: Callable[..., Loan],
) -> None:
    loan_acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=loan_acc.id, term_months=12)  # last installment 2027-01
    make_planned_item(name="Mortgage", expected_amount=None, loan_id=loan.id)

    assert pq.plan_overview(db_session, "2026-02").rows  # installment due -> present
    assert pq.plan_overview(db_session, "2027-06").rows == []  # past term -> gone


def test_loan_backed_paid_from_reconciliation(
    db_session: Session,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
    make_planned_item: Callable[..., PlannedItem],
    make_loan: Callable[..., Loan],
) -> None:
    loan_acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=loan_acc.id, term_months=12)
    bank = make_account()
    tx = make_transaction(account_id=bank.id, amount=-2600_00, booked_date=date(2026, 2, 15))
    make_planned_item(name="Mortgage", expected_amount=None, loan_id=loan.id)

    # Before linking the loan payment, the installment reads unpaid.
    assert pq.plan_overview(db_session, "2026-02").rows[0].paid is False
    # Linking it in the Loans module flips the plan's derived status (single source).
    loan_queries.link_payment(db_session, loan_id=loan.id, tx_id=tx.id, installment_index=1)
    db_session.expire_all()
    row = pq.plan_overview(db_session, "2026-02").rows[0]
    assert row.paid is True
    assert row.real_amount == -2600_00
    assert row.paid_date == date(2026, 2, 15)


# --- FOR LIVING trend (Phase 19c) -------------------------------------------


def test_for_living_trend(
    db_session: Session, make_planned_item: Callable[..., PlannedItem]
) -> None:
    make_planned_item(name="Salary", expected_amount=8000_00)
    make_planned_item(name="Rent", expected_amount=-3000_00)

    trend = pq.for_living_trend(db_session, months=3, today=date(2026, 6, 15))

    # Last 3 months ending at the current one, oldest first.
    assert [m for m, _ in trend] == ["2026-04", "2026-05", "2026-06"]
    # Recurring items -> the same remainder every month.
    assert [v for _, v in trend] == [5000_00, 5000_00, 5000_00]


def test_for_living_trend_crosses_year_boundary(
    db_session: Session, make_planned_item: Callable[..., PlannedItem]
) -> None:
    make_planned_item(name="Salary", expected_amount=8000_00)

    trend = pq.for_living_trend(db_session, months=3, today=date(2026, 1, 10))

    assert [m for m, _ in trend] == ["2025-11", "2025-12", "2026-01"]
