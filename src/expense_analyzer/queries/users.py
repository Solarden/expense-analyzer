"""User (login identity) queries."""

from sqlmodel import Session, col, select

from expense_analyzer.models import Owner


def get(session: Session, user_id: int) -> Owner | None:
    return session.get(Owner, user_id)


def get_by_username(session: Session, username: str) -> Owner | None:
    return session.exec(select(Owner).where(Owner.username == username)).first()


def list_users(session: Session) -> list[Owner]:
    return list(session.exec(select(Owner).order_by(col(Owner.username))).all())


def create_user(session: Session, *, username: str, name: str, password: str) -> Owner:
    # Imported lazily: auth.py depends on this module, so a top-level import
    # would create a cycle.
    from expense_analyzer.auth import hash_password

    user = Owner(
        username=username.strip(),
        name=name.strip(),
        password_hash=hash_password(password),
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    return user
