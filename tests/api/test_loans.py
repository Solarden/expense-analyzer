"""Tests for loan queries and the Loans dashboard pages.

Query-layer tests run on ``db_session``; HTTP tests use ``auth_client`` (both
share the same temp engine). Model builders (``make_account``, ``make_loan``,
``make_transaction``) come from conftest.
"""

from collections.abc import Callable
from datetime import date

from fastapi import status
from fastapi.testclient import TestClient
from sqlmodel import Session

from expense_analyzer.models import (
    Account,
    AccountType,
    InstallmentType,
    Loan,
    LoanCreate,
    RateType,
    Transaction,
)
from expense_analyzer.queries.planning import loans as lq

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


def test_update_loan_changes_fields_and_reseeds_earliest_rate(
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(
        account_id=acc.id,
        rate_type=RateType.variable,
        rate_bp=150,
        term_months=12,
        start_date=date(2026, 1, 15),
    )
    lq.add_rate_change(
        db_session, loan_id=loan.id, effective_date=date(2026, 1, 15), base_rate_bp=400
    )
    later = lq.add_rate_change(
        db_session, loan_id=loan.id, effective_date=date(2026, 7, 1), base_rate_bp=600
    )

    updated = lq.update_loan(
        db_session,
        loan.id,
        LoanCreate(
            account_id=acc.id,
            principal=20_000_000,
            rate_type=RateType.variable,
            rate_bp=200,
            installment_type=InstallmentType.equal,
            start_date=date(2026, 3, 10),
            term_months=24,
            base_rate_ref="WIBOR 3M",
            initial_base_rate_bp=450,
        ),
    )

    assert updated is not None
    assert updated.principal == 20_000_000
    assert updated.term_months == 24
    changes = lq.list_rate_changes(db_session, loan.id)
    # Earliest observation moved to the new start date + new base rate; the later
    # one the user added on the detail page is untouched.
    assert (changes[0].effective_date, changes[0].base_rate_bp) == (date(2026, 3, 10), 450)
    assert (changes[1].id, changes[1].base_rate_bp) == (later.id, 600)


def test_update_loan_reseeds_start_observation_when_moved_past_a_later_one(
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    # The start-date observation is matched by the *old* start date, so moving the
    # start past a later observation still updates the right one (not whatever is
    # earliest now), and the later observation is preserved untouched.
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(
        account_id=acc.id,
        rate_type=RateType.variable,
        rate_bp=150,
        term_months=24,
        start_date=date(2026, 1, 15),
    )
    lq.add_rate_change(
        db_session, loan_id=loan.id, effective_date=date(2026, 1, 15), base_rate_bp=400
    )
    later = lq.add_rate_change(
        db_session, loan_id=loan.id, effective_date=date(2026, 7, 1), base_rate_bp=600
    )

    lq.update_loan(
        db_session,
        loan.id,
        LoanCreate(
            account_id=acc.id,
            principal=loan.principal,
            rate_type=RateType.variable,
            rate_bp=150,
            installment_type=InstallmentType.equal,
            start_date=date(2026, 8, 1),  # moved *after* the later observation
            term_months=24,
            initial_base_rate_bp=420,
        ),
    )

    changes = {c.effective_date: c.base_rate_bp for c in lq.list_rate_changes(db_session, loan.id)}
    assert changes == {date(2026, 7, 1): 600, date(2026, 8, 1): 420}  # later kept, seed moved
    # The user's later observation row is the same one, untouched.
    db_session.refresh(later)
    assert later.base_rate_bp == 600


def test_update_loan_seeds_rate_when_switching_to_variable(
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id, rate_type=RateType.fixed, rate_bp=700)
    assert lq.list_rate_changes(db_session, loan.id) == []

    lq.update_loan(
        db_session,
        loan.id,
        LoanCreate(
            account_id=acc.id,
            principal=loan.principal,
            rate_type=RateType.variable,
            rate_bp=150,
            installment_type=loan.installment_type,
            start_date=loan.start_date,
            term_months=loan.term_months,
            initial_base_rate_bp=500,
        ),
    )

    [seed] = lq.list_rate_changes(db_session, loan.id)
    assert (seed.effective_date, seed.base_rate_bp) == (loan.start_date, 500)


def test_update_loan_missing_returns_none(
    db_session: Session, make_account: Callable[..., Account]
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    data = LoanCreate(
        account_id=acc.id,
        principal=1_000_00,
        rate_type=RateType.fixed,
        rate_bp=500,
        installment_type=InstallmentType.equal,
        start_date=date(2026, 1, 15),
        term_months=12,
    )
    assert lq.update_loan(db_session, 9999, data) is None


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
    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert resp.headers["location"].startswith("/dashboard/loans/")

    loan = lq.list_loans(db_session)[0]
    assert loan.principal == 30_000_000
    assert loan.rate_bp == 725  # 7.25% -> basis points


def test_create_loan_with_contract_number(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
):
    """Phase 19a: the contract number is stored and surfaced on the detail page."""
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
            "contract_number": "BLP0068094260",
        },
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER

    loan = lq.list_loans(db_session)[0]
    assert loan.contract_number == "BLP0068094260"
    detail = auth_client.get(f"/dashboard/loans/{loan.id}")
    assert "BLP0068094260" in detail.text


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
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
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
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "loan account" in resp.text.lower()


def test_loan_detail_renders_schedule(
    auth_client: TestClient,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id, term_months=12)
    resp = auth_client.get(f"/dashboard/loans/{loan.id}")
    assert resp.status_code == status.HTTP_200_OK
    assert "plan vs reality" in resp.text.lower()


def test_loan_detail_404_for_missing_loan(auth_client: TestClient):
    assert auth_client.get("/dashboard/loans/9999").status_code == status.HTTP_404_NOT_FOUND


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
    assert resp.status_code == status.HTTP_303_SEE_OTHER
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
    assert resp.status_code == status.HTTP_303_SEE_OTHER
    db_session.refresh(tx)
    assert tx.loan_id == loan.id

    resp = auth_client.post(
        f"/dashboard/loans/{loan.id}/payments/{tx.id}/unlink", follow_redirects=False
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER
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
    assert resp.status_code == status.HTTP_303_SEE_OTHER
    db_session.expire_all()  # drop the cached instance so get reloads from the DB
    assert lq.get_loan(db_session, loan_id) is None


def test_loan_edit_form_prefills_current_values(
    auth_client: TestClient,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id, principal=30_000_000, rate_bp=725)

    resp = auth_client.get(f"/dashboard/loans/{loan.id}/edit")

    assert resp.status_code == status.HTTP_200_OK
    assert "Edit loan" in resp.text
    assert 'value="300000.00"' in resp.text  # principal round-trips to PLN
    assert 'value="7.25"' in resp.text  # rate_bp 725 -> "7.25"%


def test_edit_fixed_loan_updates_and_redirects(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id, principal=30_000_000, rate_bp=725, term_months=360)

    resp = auth_client.post(
        f"/dashboard/loans/{loan.id}/edit",
        data={
            "account_id": acc.id,
            "principal": "250000",
            "rate_type": "fixed",
            "rate_percent": "6.50",
            "installment_type": "equal",
            "start_date": "2026-02-01",
            "term_months": "300",
        },
        follow_redirects=False,
    )

    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert resp.headers["location"] == f"/dashboard/loans/{loan.id}"
    db_session.expire_all()
    updated = lq.get_loan(db_session, loan.id)
    assert (updated.principal, updated.rate_bp, updated.term_months) == (25_000_000, 650, 300)


def test_edit_variable_loan_missing_base_rate_flashes_not_500(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id, rate_type=RateType.fixed, rate_bp=700)

    resp = auth_client.post(
        f"/dashboard/loans/{loan.id}/edit",
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

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "base rate" in resp.text.lower()
    db_session.expire_all()
    unchanged = lq.get_loan(db_session, loan.id)
    assert unchanged.rate_type is RateType.fixed  # the bad edit didn't persist


def test_edit_loan_404_for_missing_loan(auth_client: TestClient):
    assert auth_client.get("/dashboard/loans/9999/edit").status_code == status.HTTP_404_NOT_FOUND
