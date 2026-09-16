"""User (login identity) queries."""

from sqlmodel import Session, col, delete, func, select, update

from expense_analyzer.clock import utc_now
from expense_analyzer.models import Account, Budget, ImportBatch, Owner, Scope, Transaction


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
    """Delete a login identity, keeping the shared household data they imported.

    Split by scope so no row is left broken (``owner_id`` is a real FK, enforced
    on both dialects — SQLite via the per-connection ``foreign_keys=ON`` in db.py):

    - **Household** transactions and accounts are shared, so they stay; only the
      "who imported" ``owner_id`` tag is nulled.
    - **Private** transactions belong to this user alone — left owner-less they
      would be invisible to everyone and desync balances, so they are soft-deleted
      (``deleted_at``) and their ``owner_id`` cleared: they leave with the user.
    - **Import batches** keep their rows and lose their owner, which makes them the
      shared un-rollbackable kind (``ImportBatch.owner_id``): a departed member's
      batch must not become anyone else's to roll back.
    - **Private budgets** are the only owned ones — :func:`set_budget` nulls the
      owner on every household budget — so they simply go with the member.

    Every table with an ``owner_id`` FK has to appear here, or the ``DELETE`` fails
    the constraint and the Users page 500s instead of removing the member."""
    session.exec(
        update(Transaction)
        .where(col(Transaction.owner_id) == user.id, col(Transaction.scope) == Scope.private)
        .values(deleted_at=utc_now(), owner_id=None)
    )
    session.exec(
        update(Transaction)
        .where(col(Transaction.owner_id) == user.id, col(Transaction.scope) == Scope.household)
        .values(owner_id=None)
    )
    session.exec(update(Account).where(col(Account.owner_id) == user.id).values(owner_id=None))
    session.exec(
        update(ImportBatch).where(col(ImportBatch.owner_id) == user.id).values(owner_id=None)
    )
    session.exec(delete(Budget).where(col(Budget.owner_id) == user.id))
    session.delete(user)
    session.commit()
