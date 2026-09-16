"""Net-worth aggregation and outstanding-principal queries."""

from collections.abc import Callable
from datetime import date

from sqlmodel import Session

from expense_analyzer.models import (
    Account,
    AccountType,
    InvestmentPosition,
    Loan,
    Scope,
    Transaction,
)
from expense_analyzer.queries.core import users
from expense_analyzer.queries.planning import loans as loan_queries
from expense_analyzer.queries.wealth import investments, net_worth


def test_cash_balance_sums_live_transactions(
    db_session: Session,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
) -> None:
    acc = make_account(name="PKO", type=AccountType.bank)
    make_transaction(account_id=acc.id, amount=500_00)
    make_transaction(account_id=acc.id, amount=-120_00)

    balances = {b.account_id: b for b in net_worth.account_balances(db_session, viewer_id=None)}

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
    balances = {b.account_id: b for b in net_worth.account_balances(db_session, viewer_id=None)}

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
    balances = {b.account_id: b for b in net_worth.account_balances(db_session, viewer_id=None)}

    assert balances[acc.id].balance == -(outstanding or 0)


def test_loan_account_without_loan_notes_zero(
    db_session: Session,
    make_account: Callable[..., Account],
) -> None:
    acc = make_account(name="Empty loan acct", type=AccountType.loan)

    balances = {b.account_id: b for b in net_worth.account_balances(db_session, viewer_id=None)}
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

    assert net_worth.current_net_worth(db_session, viewer_id=None) == expected


def test_another_members_account_is_absent_not_zero(
    db_session: Session,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
) -> None:
    """A scoped sum over someone else's account reads 0.00, which is indistinguishable
    from an unused account and quietly drags net worth down. Leave it out instead."""
    alice = users.create_user(db_session, username="alice", name="Alice", password="secret123")
    bob = users.create_user(db_session, username="bob", name="Bob", password="secret123")
    shared = make_account(name="Joint", type=AccountType.bank)
    hers = make_account(name="Alice mBank", type=AccountType.bank, owner_id=alice.id)
    make_transaction(account_id=shared.id, amount=1_000_00, scope=Scope.household)
    make_transaction(account_id=hers.id, amount=700_00, owner_id=alice.id, scope=Scope.private)

    bob_sees = {b.account_id for b in net_worth.account_balances(db_session, viewer_id=bob.id)}
    alice_sees = {b.account_id for b in net_worth.account_balances(db_session, viewer_id=alice.id)}
    exported = {b.account_id for b in net_worth.account_balances(db_session, viewer_id=None)}

    assert bob_sees == {shared.id}
    assert alice_sees == {shared.id, hers.id}
    assert exported == {shared.id}
    assert net_worth.current_net_worth(db_session, viewer_id=bob.id) == 1_000_00
    assert net_worth.current_net_worth(db_session, viewer_id=alice.id) == 1_700_00


def test_owned_portfolio_account_still_counts_for_everyone(
    db_session: Session,
    make_account: Callable[..., Account],
    make_investment: Callable[..., InvestmentPosition],
) -> None:
    """A labelled portfolio or loan account still counts for every viewer."""
    alice = users.create_user(db_session, username="alice", name="Alice", password="secret123")
    bob = users.create_user(db_session, username="bob", name="Bob", password="secret123")
    ike = make_account(name="IKE", type=AccountType.portfolio, owner_id=alice.id)
    make_investment(account_id=ike.id, value=500_00, snapshot_date=date(2026, 4, 15))

    bob_sees = {b.account_id for b in net_worth.account_balances(db_session, viewer_id=bob.id)}

    assert ike.id in bob_sees
    assert net_worth.current_net_worth(db_session, viewer_id=bob.id) == 500_00
    assert net_worth.current_net_worth(db_session, viewer_id=None) == 500_00


def test_household_row_on_a_personal_account_still_counts(
    db_session: Session,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
) -> None:
    """A household row on a personal account counts for everyone who can see it."""
    alice = users.create_user(db_session, username="alice", name="Alice", password="secret123")
    bob = users.create_user(db_session, username="bob", name="Bob", password="secret123")
    hers = make_account(name="Alice mBank", type=AccountType.bank, owner_id=alice.id)
    make_transaction(account_id=hers.id, amount=700_00, owner_id=alice.id, scope=Scope.private)
    make_transaction(account_id=hers.id, amount=-100_00, scope=Scope.household)

    bob_sees = {
        b.account_id: b.balance for b in net_worth.account_balances(db_session, viewer_id=bob.id)
    }

    # Bob sees the account, holding only the shared row — never Alice's 700.
    assert bob_sees[hers.id] == -100_00
    assert net_worth.current_net_worth(db_session, viewer_id=alice.id) == 600_00
