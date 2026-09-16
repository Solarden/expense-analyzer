"""Net worth: one number across accounts, assets minus debt.

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
from expense_analyzer.queries.visibility import visible_to
from expense_analyzer.queries.wealth import investments

# Only these two derive their balance from scoped transactions; a portfolio or a
# loan is computed from positions and schedules, which carry no scope.
_SCOPED_TYPES = (AccountType.bank, AccountType.cash)


@dataclass(frozen=True)
class AccountBalance:
    account_id: int
    name: str
    type: AccountType
    balance: int  # minor units; negative for loan debt
    note: str | None = None  # e.g. why a loan balance couldn't be computed


def _cash_balance(session: Session, account_id: int, *, viewer_id: int | None) -> int:
    """Sum of live transaction amounts on a bank/cash account (minor units).

    Viewer-scoped like every other total: a balance that summed another member's
    private rows would leak their history as an aggregate.
    """
    query = select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        Transaction.account_id == account_id,
        col(Transaction.deleted_at).is_(None),
    )
    total = session.exec(visible_to(query, viewer_id=viewer_id)).one()

    return int(total)


def _has_visible_rows(session: Session, account_id: int, *, viewer_id: int | None) -> bool:
    """Whether the viewer can see any live transaction on this account."""
    query = select(Transaction.id).where(
        Transaction.account_id == account_id,
        col(Transaction.deleted_at).is_(None),
    )

    return session.exec(visible_to(query, viewer_id=viewer_id).limit(1)).first() is not None


def _loan_for_account(session: Session, account_id: int) -> int | None:
    """A loan account holds one loan; return its id (or None)."""
    return session.exec(
        select(col(loan_queries.Loan.id)).where(loan_queries.Loan.account_id == account_id)
    ).first()


def account_balances(session: Session, *, viewer_id: int | None) -> list[AccountBalance]:
    """Current balance per account, in declaration order from :func:`list_accounts`.

    Cash balances are viewer-scoped. Portfolio and loan figures are not: positions
    and loans carry no scope or owner, they are shared household reference data.

    Another member's **bank or cash** account is dropped when the viewer can see
    nothing on it: every row is private, so a scoped sum reads ``0.00`` —
    indistinguishable from an account nobody has used, and it drags net worth down
    with a number that looks real. A background job (``viewer_id=None``) sees the
    shared ones only, for the same reason.

    The test is what the viewer can *see*, not who owns the account, so a household
    row booked against a member's own account still counts for everyone — otherwise
    net worth would omit money that this month's spending figure includes, and the
    two would stop reconciling.

    Bank and cash only: a portfolio's value and a loan's debt come from positions and
    schedules, which carry no scope at all, so there is no zero to avoid and nothing
    private to withhold. Skipping those because someone labelled the account would
    delete a real asset — or a mortgage — from every other member's net worth.
    """
    balances: list[AccountBalance] = []

    for account in list_accounts(session):
        if account.type in _SCOPED_TYPES and account.owner_id not in (None, viewer_id):
            if not _has_visible_rows(session, account.id, viewer_id=viewer_id):
                continue

        note: str | None = None

        if account.type in (AccountType.bank, AccountType.cash):
            balance = _cash_balance(session, account.id, viewer_id=viewer_id)
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


def current_net_worth(session: Session, *, viewer_id: int | None) -> int:
    """Sum of every account balance (assets positive, loan debt negative)."""
    return sum(b.balance for b in account_balances(session, viewer_id=viewer_id))
