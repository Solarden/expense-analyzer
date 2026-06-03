"""Tests for loan queries and the Loans dashboard pages.

Query-layer tests run on ``db_session``; HTTP tests use ``auth_client`` (both
share the same temp engine). Model builders (``make_account``, ``make_loan``,
``make_transaction``) come from conftest.
"""

from collections.abc import Callable
from datetime import date

from fastapi.testclient import TestClient
from sqlmodel import Session

from expense_analyzer.models import (
    Account,
    AccountType,
    Loan,
    RateType,
    Transaction,
)
from expense_analyzer.queries import loans as lq

# --- query layer -----------------------------------------------------------


def test_loan_schedule_recomputes_for_variable_rate(
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(
        account_id=acc.id,
        rate_type=RateType.variable,
        rate_bp=150,  # margin
        term_months=12,
        start_date=date(2026, 1, 15),
    )
    lq.add_rate_change(
        db_session, loan_id=loan.id, effective_date=date(2026, 1, 15), base_rate_bp=400
    )
    lq.add_rate_change(
        db_session, loan_id=loan.id, effective_date=date(2026, 7, 1), base_rate_bp=600
    )

    schedule = lq.loan_schedule(db_session, loan.id)
    assert schedule is not None
    assert schedule.rows[-1].balance_after == 0
    # The installment after the base-rate hike is larger than the first one.
    assert schedule.rows[-1].payment > schedule.rows[0].payment


def test_link_and_unlink_payment_round_trips(
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
    make_transaction: Callable[..., Transaction],
):
    loan_acc = make_account(name="Mortgage", type=AccountType.loan)
    checking = make_account(name="PKO", type=AccountType.bank)
    loan = make_loan(account_id=loan_acc.id)
    tx = make_transaction(account_id=checking.id, amount=-250000, day=15)

    assert lq.link_payment(db_session, loan_id=loan.id, tx_id=tx.id, installment_index=1) is True
    db_session.refresh(tx)
    assert tx.loan_id == loan.id
    assert tx.loan_installment_index == 1
    # Already linked -> can't relink elsewhere.
    assert lq.link_payment(db_session, loan_id=loan.id, tx_id=tx.id, installment_index=2) is False

    assert lq.unlink_payment(db_session, tx.id) is True
    db_session.refresh(tx)
    assert tx.loan_id is None
    assert tx.loan_installment_index is None


def test_suggest_payments_finds_near_match_on_other_account(
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
    make_transaction: Callable[..., Transaction],
):
    loan_acc = make_account(name="Mortgage", type=AccountType.loan)
    checking = make_account(name="PKO", type=AccountType.bank)
    loan = make_loan(account_id=loan_acc.id, term_months=12, start_date=date(2026, 1, 15))
    schedule = lq.loan_schedule(db_session, loan.id)
    planned = schedule.rows[0].payment  # first installment, due 2026-02-15

    # An outflow on the checking account, close to the due date and amount.
    good = make_transaction(account_id=checking.id, amount=-planned, booked_date=date(2026, 2, 16))
    # An outflow on the loan account itself -> never suggested.
    make_transaction(account_id=loan_acc.id, amount=-planned, booked_date=date(2026, 2, 16))

    suggestions = lq.suggest_payments(db_session, loan, schedule, window_days=5, tolerance_pct=5)
    assert any(s.transaction.id == good.id and s.installment_index == 1 for s in suggestions)
    assert all(s.transaction.account_id == checking.id for s in suggestions)


def test_delete_loan_unlinks_payments_and_removes_rate_changes(
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
    make_transaction: Callable[..., Transaction],
):
    loan_acc = make_account(name="Mortgage", type=AccountType.loan)
    checking = make_account(name="PKO", type=AccountType.bank)
    loan = make_loan(account_id=loan_acc.id, rate_type=RateType.variable, rate_bp=150)
    lq.add_rate_change(
        db_session, loan_id=loan.id, effective_date=date(2026, 1, 15), base_rate_bp=400
    )
    tx = make_transaction(account_id=checking.id, amount=-250000, day=15)
    lq.link_payment(db_session, loan_id=loan.id, tx_id=tx.id, installment_index=1)

    assert lq.delete_loan(db_session, loan.id) is True
    assert lq.get_loan(db_session, loan.id) is None
    assert lq.list_rate_changes(db_session, loan.id) == []
    db_session.refresh(tx)
    assert tx.loan_id is None  # payment kept, just unlinked


def test_delete_loan_clears_link_on_soft_deleted_payment(
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
    make_transaction: Callable[..., Transaction],
):
    # A payment linked to a loan and then soft-deleted (e.g. by a batch rollback)
    # still carries loan_id. With foreign_keys=ON, deleting the loan must not trip
    # an integrity error — delete_loan clears the link on soft-deleted rows too.
    from expense_analyzer.clock import utc_now

    loan_acc = make_account(name="Mortgage", type=AccountType.loan)
    checking = make_account(name="PKO", type=AccountType.bank)
    loan = make_loan(account_id=loan_acc.id)
    tx = make_transaction(account_id=checking.id, amount=-250000, day=15)
    lq.link_payment(db_session, loan_id=loan.id, tx_id=tx.id, installment_index=1)
    tx.deleted_at = utc_now()  # soft-delete after linking
    db_session.add(tx)
    db_session.commit()

    assert lq.delete_loan(db_session, loan.id) is True
    db_session.refresh(tx)
    assert tx.loan_id is None
    assert tx.loan_installment_index is None


# --- HTTP layer ------------------------------------------------------------


def test_create_fixed_loan_redirects_to_detail(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    resp = auth_client.post(
        "/dashboard/loans",
        data={
            "account_id": acc.id,
            "principal": "300000",
            "rate_type": "fixed",
            "rate_percent": "7.25",
            "installment_type": "equal",
            "start_date": "2026-01-15",
            "term_months": "360",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/dashboard/loans/")

    loan = lq.list_loans(db_session)[0]
    assert loan.principal == 30_000_000
    assert loan.rate_bp == 725  # 7.25% -> basis points


def test_create_variable_loan_requires_initial_base_rate(
    auth_client: TestClient,
    make_account: Callable[..., Account],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    resp = auth_client.post(
        "/dashboard/loans",
        data={
            "account_id": acc.id,
            "principal": "300000",
            "rate_type": "variable",
            "rate_percent": "1.50",
            "installment_type": "equal",
            "start_date": "2026-01-15",
            "term_months": "360",
            # no base_rate_percent
        },
    )
    assert resp.status_code == 400
    assert "base rate" in resp.text.lower()


def test_create_loan_rejects_non_loan_account(
    auth_client: TestClient,
    make_account: Callable[..., Account],
):
    acc = make_account(name="PKO", type=AccountType.bank)
    resp = auth_client.post(
        "/dashboard/loans",
        data={
            "account_id": acc.id,
            "principal": "300000",
            "rate_type": "fixed",
            "rate_percent": "7.25",
            "installment_type": "equal",
            "start_date": "2026-01-15",
            "term_months": "360",
        },
    )
    assert resp.status_code == 400
    assert "loan account" in resp.text.lower()


def test_loan_detail_renders_schedule(
    auth_client: TestClient,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id, term_months=12)
    resp = auth_client.get(f"/dashboard/loans/{loan.id}")
    assert resp.status_code == 200
    assert "plan vs reality" in resp.text.lower()


def test_loan_detail_404_for_missing_loan(auth_client: TestClient):
    assert auth_client.get("/dashboard/loans/9999").status_code == 404


def test_add_rate_change_then_appears_on_detail(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id, rate_type=RateType.variable, rate_bp=150)
    lq.add_rate_change(
        db_session, loan_id=loan.id, effective_date=date(2026, 1, 15), base_rate_bp=400
    )

    resp = auth_client.post(
        f"/dashboard/loans/{loan.id}/rate-changes",
        data={"effective_date": "2026-07-01", "base_rate_percent": "6.00"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert len(lq.list_rate_changes(db_session, loan.id)) == 2


def test_link_payment_over_http_then_unlink(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
    make_transaction: Callable[..., Transaction],
):
    loan_acc = make_account(name="Mortgage", type=AccountType.loan)
    checking = make_account(name="PKO", type=AccountType.bank)
    loan = make_loan(account_id=loan_acc.id)
    tx = make_transaction(account_id=checking.id, amount=-250000, day=15)

    resp = auth_client.post(
        f"/dashboard/loans/{loan.id}/payments/link",
        data={"tx_id": tx.id, "installment_index": 1},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.refresh(tx)
    assert tx.loan_id == loan.id

    resp = auth_client.post(
        f"/dashboard/loans/{loan.id}/payments/{tx.id}/unlink", follow_redirects=False
    )
    assert resp.status_code == 303
    db_session.refresh(tx)
    assert tx.loan_id is None


def test_delete_loan_over_http(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id)
    loan_id = loan.id
    resp = auth_client.post(f"/dashboard/loans/{loan_id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    db_session.expire_all()  # drop the cached instance so get reloads from the DB
    assert lq.get_loan(db_session, loan_id) is None
