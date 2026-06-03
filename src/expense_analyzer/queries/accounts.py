"""Account queries."""

from sqlmodel import Session, col, select

from expense_analyzer.models import Account, AccountType


def list_accounts(session: Session) -> list[Account]:
    return list(session.exec(select(Account).order_by(col(Account.name))).all())


def get_account(session: Session, account_id: int) -> Account | None:
    return session.get(Account, account_id)


def create_account(session: Session, *, name: str, type: AccountType) -> Account:
    account = Account(name=name.strip(), type=type)
    session.add(account)
    session.commit()
    session.refresh(account)

    return account
