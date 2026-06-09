"""Users page: list login identities, add new ones, and (admins only) manage
them — delete or toggle active (design §10, Phase 15).

No public registration. Data stays a single shared household view with no roles
for *viewing*; ``is_admin`` is a soft management role. The first user created
bootstraps as admin (see :func:`expense_analyzer.queries.users.create_user`);
everyone after is a plain member. Two guards keep an admin from locking the
household out: you cannot act on your own account, and you cannot strip the last
active admin.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from expense_analyzer.api.deps import AdminUser, CurrentUser, DbSession
from expense_analyzer.api.forms import UserForm
from expense_analyzer.auth import require_user
from expense_analyzer.models import Owner
from expense_analyzer.queries import users as user_queries
from expense_analyzer.templating import templates

router = APIRouter(prefix="/dashboard/users", tags=["users"], dependencies=[Depends(require_user)])


def _users_context(session: Session, user: Owner, **extra) -> dict:
    return {
        "user": user,
        "users": user_queries.list_users(session),
        **extra,
    }


def _last_active_admin(session: Session, target: Owner) -> bool:
    """True if removing/deactivating ``target`` would leave no active admin."""
    return target.is_admin and target.is_active and user_queries.active_admin_count(session) <= 1


@router.get("", response_class=HTMLResponse)
def users_page(request: Request, user: CurrentUser, session: DbSession) -> HTMLResponse:
    return templates.TemplateResponse(request, "users.html", _users_context(session, user))


@router.post("", response_class=HTMLResponse)
def add_user(
    request: Request,
    form: Annotated[UserForm, Form()],
    user: CurrentUser,
    session: DbSession,
) -> Response:
    if user_queries.get_by_username(session, form.username.strip()) is not None:
        return templates.TemplateResponse(
            request,
            "users.html",
            _users_context(
                session, user, error=f"Username {form.username.strip()!r} is already taken."
            ),
            status_code=status.HTTP_409_CONFLICT,
        )

    user_queries.create_user(
        session, username=form.username, name=form.name, password=form.password.get_secret_value()
    )

    return RedirectResponse("/dashboard/users", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{user_id}/toggle-active", response_class=HTMLResponse)
def toggle_active(
    request: Request,
    user_id: int,
    admin: AdminUser,
    session: DbSession,
) -> Response:
    target = user_queries.get(session, user_id)
    if target is None:
        return templates.TemplateResponse(
            request,
            "users.html",
            _users_context(session, admin, error="That user no longer exists."),
            status_code=status.HTTP_404_NOT_FOUND,
        )

    error: str | None = None
    if target.id == admin.id:
        error = "You can't deactivate your own account."
    elif target.is_active and _last_active_admin(session, target):
        error = "Can't deactivate the last active admin."
    if error is not None:
        return templates.TemplateResponse(
            request,
            "users.html",
            _users_context(session, admin, error=error),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    user_queries.set_active(session, target, is_active=not target.is_active)

    return RedirectResponse("/dashboard/users", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{user_id}/delete", response_class=HTMLResponse)
def delete_user(
    request: Request,
    user_id: int,
    admin: AdminUser,
    session: DbSession,
) -> Response:
    target = user_queries.get(session, user_id)
    if target is None:
        return templates.TemplateResponse(
            request,
            "users.html",
            _users_context(session, admin, error="That user no longer exists."),
            status_code=status.HTTP_404_NOT_FOUND,
        )

    error: str | None = None
    if target.id == admin.id:
        error = "You can't delete your own account."
    elif _last_active_admin(session, target):
        error = "Can't delete the last active admin."
    if error is not None:
        return templates.TemplateResponse(
            request,
            "users.html",
            _users_context(session, admin, error=error),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    user_queries.delete_user(session, target)

    return RedirectResponse("/dashboard/users", status_code=status.HTTP_303_SEE_OTHER)
