"""User (login identity) queries."""

from sqlmodel import Session, col, func, select, update

from expense_analyzer.models import Account, Owner, Transaction


def get(session: Session, user_id: int) -> Owner | None:
    return session.get(Owner, user_id)


def get_by_username(session: Session, username: str) -> Owner | None:
    return session.exec(select(Owner).where(Owner.username == username)).first()


def list_users(session: Session) -> list[Owner]:
    return list(session.exec(select(Owner).order_by(col(Owner.username))).all())


def count_users(session: Session) -> int:
    return session.exec(select(func.count()).select_from(Owner)).one()


def active_admin_count(session: Session) -> int:
    """How many active admins exist. Drives the "don't strip the last admin" guard."""
    return session.exec(
        select(func.count())
        .select_from(Owner)
        .where(col(Owner.is_admin).is_(True), col(Owner.is_active).is_(True))
    ).one()


def create_user(session: Session, *, username: str, name: str, password: str) -> Owner:
    # Imported lazily: auth.py depends on this module, so a top-level import
    # would create a cycle.
    from expense_analyzer.auth import hash_password

    # The very first user (table empty) bootstraps the household as admin; every
    # user added afterwards is a plain member. Roles can only be widened by
    # editing the DB directly — intentional for a small self-hosted household.
    is_admin = count_users(session) == 0

    user = Owner(
        username=username.strip(),
        name=name.strip(),
        password_hash=hash_password(password),
        is_admin=is_admin,
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    return user


def set_active(session: Session, user: Owner, *, is_active: bool) -> Owner:
    user.is_active = is_active
    session.add(user)
    session.commit()
    session.refresh(user)

    return user


def set_admin(session: Session, user: Owner, *, is_admin: bool) -> Owner:
    user.is_admin = is_admin
    session.add(user)
    session.commit()
    session.refresh(user)

    return user


def set_password(session: Session, user: Owner, *, password: str) -> Owner:
    """Replace a user's password (admin reset from the UI / CLI bootstrap)."""
    # Imported lazily: auth.py depends on this module, so a top-level import
    # would create a cycle (same reason as create_user).
    from expense_analyzer.auth import hash_password

    user.password_hash = hash_password(password)
    session.add(user)
    session.commit()
    session.refresh(user)

    return user


def delete_user(session: Session, user: Owner) -> None:
    """Delete a login identity, keeping the household data they imported.

    ``owner_id`` on accounts/transactions is just a "who imported" tag, so it is
    nulled out (one bulk UPDATE each) rather than cascade-deleting real financial
    rows — and to satisfy the foreign key under SQLite's ``foreign_keys=ON``."""
    session.exec(update(Account).where(col(Account.owner_id) == user.id).values(owner_id=None))
    session.exec(
        update(Transaction).where(col(Transaction.owner_id) == user.id).values(owner_id=None)
    )
    session.delete(user)
    session.commit()
