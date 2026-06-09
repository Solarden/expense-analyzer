"""Authentication: password hashing and the login-session dependency.

Single shared household view — any active user, once logged in, sees the same
data (no per-user isolation, no roles). The session stores only the user id in a
signed cookie (Starlette ``SessionMiddleware``); ``SameSite=Lax`` gives baseline
CSRF protection on the state-changing POST routes.
"""

import bcrypt
from fastapi import Depends, Request
from sqlmodel import Session

from expense_analyzer.db import get_session
from expense_analyzer.models import Owner
from expense_analyzer.queries.core import users

_SESSION_USER_KEY = "user_id"


class NotAuthenticatedError(Exception):
    """Raised by :func:`require_user` when no valid session is present.

    Handled in ``main.py`` by redirecting the browser to the login page.
    """


class NotAuthorizedError(Exception):
    """Raised by :func:`require_admin` when a logged-in user lacks the admin role.

    Handled in ``main.py`` by rendering a 403 page (the user *is* logged in, so
    redirecting to login would be wrong).
    """


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, password_hash: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("utf-8"))


def login_session(request: Request, user: Owner) -> None:
    request.session[_SESSION_USER_KEY] = user.id


def logout_session(request: Request) -> None:
    request.session.pop(_SESSION_USER_KEY, None)


def current_user(request: Request, session: Session = Depends(get_session)) -> Owner | None:
    """The logged-in user, or None. Inactive/unknown users count as logged out."""
    user_id = request.session.get(_SESSION_USER_KEY)
    if user_id is None:
        return None
    user = users.get(session, user_id)
    if user is None or not user.is_active:
        return None

    return user


def require_user(user: Owner | None = Depends(current_user)) -> Owner:
    """Dependency that enforces login on protected routes."""
    if user is None:
        raise NotAuthenticatedError

    return user


def require_admin(user: Owner = Depends(require_user)) -> Owner:
    """Dependency that gates user-management actions to admins.

    Builds on :func:`require_user`, so an anonymous request still redirects to
    login; a logged-in non-admin gets a 403 instead."""
    if not user.is_admin:
        raise NotAuthorizedError

    return user
