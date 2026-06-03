"""Net-worth aggregation and outstanding-principal queries."""

from collections.abc import Callable
from datetime import date

from sqlmodel import Session

from expense_analyzer.models import Account, AccountType, InvestmentPosition, Loan, Transaction
from expense_analyzer.queries import investments, net_worth
from expense_analyzer.queries import loans as loan_queries


def test_cash_balance_sums_live_transactions(
    db_session: Session,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
) -> None:
    acc = make_account(name="PKO", type=AccountType.bank)
    make_transaction(account_id=acc.id, amount=500_00)
    make_transaction(account_id=acc.id, amount=-120_00)

    balances = {b.account_id: b for b in net_worth.account_balances(db_session)}
    assert balances[acc.id].balance == 380_00


def test_portfolio_uses_only_latest_snapshot(
    db_session: Session,
    make_account: Callable[..., Account],
    make_investment: Callable[..., InvestmentPosition],
) -> None:
    acc = make_account(name="IKE XTB", type=AccountType.portfolio)
    # Older snapshot — must be ignored once a newer one exists.
    make_investment(
        account_id=acc.id, ticker="SNT.PL", value=100_00, snapshot_date=date(2026, 3, 15)
    )
    # Latest snapshot: two holdings.
    make_investment(
        account_id=acc.id, ticker="SNT.PL", value=150_00, snapshot_date=date(2026, 4, 15)
    )
    make_investment(
        account_id=acc.id, ticker="SXR8.DE", value=250_00, snapshot_date=date(2026, 4, 15)
    )

    assert investments.portfolio_value(db_session, acc.id) == 400_00
    balances = {b.account_id: b for b in net_worth.account_balances(db_session)}
    assert balances[acc.id].balance == 400_00


def test_outstanding_principal_bounds(
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
) -> None:
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(
        account_id=acc.id, principal=120_000_00, start_date=date(2026, 1, 15), term_months=12
    )

    # Before the first installment is due: nothing repaid yet.
    assert (
        loan_queries.outstanding_principal(db_session, loan.id, as_of=date(2026, 1, 15))
        == 120_000_00
    )
    # After the final installment: fully amortized.
    assert loan_queries.outstanding_principal(db_session, loan.id, as_of=date(2030, 1, 1)) == 0


def test_loan_balance_is_negative_outstanding(
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
) -> None:
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(
        account_id=acc.id, principal=120_000_00, start_date=date(2026, 1, 15), term_months=12
    )

    outstanding = loan_queries.outstanding_principal(db_session, loan.id)
    balances = {b.account_id: b for b in net_worth.account_balances(db_session)}
    assert balances[acc.id].balance == -(outstanding or 0)


def test_loan_account_without_loan_notes_zero(
    db_session: Session,
    make_account: Callable[..., Account],
) -> None:
    acc = make_account(name="Empty loan acct", type=AccountType.loan)

    balances = {b.account_id: b for b in net_worth.account_balances(db_session)}
    assert balances[acc.id].balance == 0
    assert balances[acc.id].note is not None


def test_current_net_worth_sums_all(
    db_session: Session,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
    make_investment: Callable[..., InvestmentPosition],
    make_loan: Callable[..., Loan],
) -> None:
    bank = make_account(name="PKO", type=AccountType.bank)
    make_transaction(account_id=bank.id, amount=1_000_00)
    portfolio = make_account(name="IKE", type=AccountType.portfolio)
    make_investment(account_id=portfolio.id, value=500_00, snapshot_date=date(2026, 4, 15))
    loan_acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(
        account_id=loan_acc.id, principal=120_000_00, start_date=date(2026, 1, 15), term_months=12
    )

    outstanding = loan_queries.outstanding_principal(db_session, loan.id) or 0
    expected = 1_000_00 + 500_00 - outstanding
    assert net_worth.current_net_worth(db_session) == expected
