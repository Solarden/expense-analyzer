"""Users page: list login identities and add new ones (design §10).

No public registration and no roles — every active user shares the same
household view; this page just bootstraps additional logins.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from expense_analyzer.api.deps import CurrentUser, DbSession
from expense_analyzer.api.forms import UserForm
from expense_analyzer.auth import require_user
from expense_analyzer.queries import users as user_queries
from expense_analyzer.templating import templates

router = APIRouter(prefix="/dashboard/users", tags=["users"], dependencies=[Depends(require_user)])


@router.get("", response_class=HTMLResponse)
def users_page(request: Request, user: CurrentUser, session: DbSession) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "users.html",
        {"user": user, "users": user_queries.list_users(session)},
    )


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
            {
                "user": user,
                "users": user_queries.list_users(session),
                "error": f"Username {form.username.strip()!r} is already taken.",
            },
            status_code=409,
        )

    user_queries.create_user(
        session, username=form.username, name=form.name, password=form.password
    )

    return RedirectResponse("/dashboard/users", status_code=303)
