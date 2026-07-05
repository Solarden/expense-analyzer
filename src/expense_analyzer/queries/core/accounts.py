"""Account queries."""

from sqlmodel import Session, col, select

from expense_analyzer.models import Account, AccountType


def list_accounts(session: Session) -> list[Account]:
    return list(session.exec(select(Account).order_by(col(Account.name))).all())


def get_account(session: Session, account_id: int) -> Account | None:
    return session.get(Account, account_id)


def create_account(
    session: Session, *, name: str, type: AccountType, number: str | None = None
) -> Account:
    account = Account(name=name.strip(), type=type, number=number)
    session.add(account)
    session.commit()
    session.refresh(account)

    return account


def update_account(
    session: Session,
    account_id: int,
    *,
    name: str,
    type: AccountType,
    number: str | None,
) -> Account | None:
    """Edit an account in place — rename, change type, set/clear the account number.
    Returns the updated account, or ``None`` if the id doesn't exist (the handler
    404s). The ``number`` arrives already normalised from the handler."""
    account = session.get(Account, account_id)
    if account is None:
        return None

    account.name = name.strip()
    account.type = type
    account.number = number
    session.add(account)
    session.commit()
    session.refresh(account)

    return account
