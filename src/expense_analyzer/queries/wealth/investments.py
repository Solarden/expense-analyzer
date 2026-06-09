"""Investment-position queries — the DB side of portfolio snapshots (design §7.3).

Positions are imported as dated snapshots (see
:mod:`expense_analyzer.importers.positions`); the dashboard cares about the
*latest* snapshot per account, so these helpers resolve "as of the most recent
snapshot date" rather than summing history.
"""

from datetime import date

from sqlmodel import Session, col, select

from expense_analyzer.models import Account, AccountType, InvestmentPosition


def portfolio_accounts(session: Session) -> list[Account]:
    """Accounts of type ``portfolio`` — where investment snapshots are imported."""
    return list(
        session.exec(
            select(Account).where(Account.type == AccountType.portfolio).order_by(col(Account.name))
        ).all()
    )


def latest_snapshot_date(session: Session, account_id: int) -> date | None:
    """Most recent snapshot date for an account, or None if it has no positions."""
    return session.exec(
        select(col(InvestmentPosition.snapshot_date))
        .where(InvestmentPosition.account_id == account_id)
        .order_by(col(InvestmentPosition.snapshot_date).desc())
    ).first()


def latest_positions(session: Session, account_id: int) -> list[InvestmentPosition]:
    """Holdings of an account as of its most recent snapshot, largest value first."""
    snapshot = latest_snapshot_date(session, account_id)
    if snapshot is None:
        return []
    rows = session.exec(
        select(InvestmentPosition).where(
            InvestmentPosition.account_id == account_id,
            InvestmentPosition.snapshot_date == snapshot,
        )
    ).all()

    return sorted(rows, key=lambda p: p.value, reverse=True)


def portfolio_value(session: Session, account_id: int) -> int:
    """Total value (minor units) of an account's latest snapshot — 0 if empty."""
    return sum(p.value for p in latest_positions(session, account_id))
