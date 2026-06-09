"""Net worth (design §7.3, §9): one number across accounts, assets minus debt.

    net worth = Σ bank/cash balances + Σ portfolio values − Σ outstanding loan debt

Balances are derived, not stored:
- **bank / cash**: the sum of (non-deleted) transaction amounts on the account.
- **portfolio**: the value of the account's latest investment snapshot.
- **loan**: the negative of the outstanding planned principal.

Everything is integer minor units. **Scope limit (consistent with prior phases):
all figures are treated as PLN** — a portfolio denominated in another currency is
summed at face value (FX conversion is deliberately out of scope, like the
PLN-only assumption on transfers and loans).
"""

from dataclasses import dataclass

from sqlmodel import Session, col, func, select

from expense_analyzer.loans import LoanScheduleError
from expense_analyzer.models import AccountType, Transaction
from expense_analyzer.queries.core.accounts import list_accounts
from expense_analyzer.queries.planning import loans as loan_queries
from expense_analyzer.queries.wealth import investments


@dataclass(frozen=True)
class AccountBalance:
    account_id: int
    name: str
    type: AccountType
    balance: int  # minor units; negative for loan debt
    note: str | None = None  # e.g. why a loan balance couldn't be computed


def _cash_balance(session: Session, account_id: int) -> int:
    """Sum of live transaction amounts on a bank/cash account (minor units)."""
    total = session.exec(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.account_id == account_id,
            col(Transaction.deleted_at).is_(None),
        )
    ).one()

    return int(total)


def _loan_for_account(session: Session, account_id: int) -> int | None:
    """A loan account holds one loan; return its id (or None)."""
    return session.exec(
        select(col(loan_queries.Loan.id)).where(loan_queries.Loan.account_id == account_id)
    ).first()


def account_balances(session: Session) -> list[AccountBalance]:
    """Current balance per account, in declaration order from :func:`list_accounts`."""
    balances: list[AccountBalance] = []
    for account in list_accounts(session):
        note: str | None = None
        if account.type in (AccountType.bank, AccountType.cash):
            balance = _cash_balance(session, account.id)
        elif account.type == AccountType.portfolio:
            balance = investments.portfolio_value(session, account.id)
        elif account.type == AccountType.loan:
            loan_id = _loan_for_account(session, account.id)
            if loan_id is None:
                balance, note = 0, "No loan defined for this account yet."
            else:
                try:
                    remaining = loan_queries.outstanding_principal(session, loan_id)
                except LoanScheduleError:
                    balance, note = 0, "Loan schedule unavailable (check the rate setup)."
                else:
                    balance = -(remaining or 0)
        else:
            balance = 0

        balances.append(
            AccountBalance(
                account_id=account.id,
                name=account.name,
                type=account.type,
                balance=balance,
                note=note,
            )
        )

    return balances


def current_net_worth(session: Session) -> int:
    """Sum of every account balance (assets positive, loan debt negative)."""
    return sum(b.balance for b in account_balances(session))
