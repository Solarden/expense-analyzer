"""Login / logout routes (unauthenticated)."""

from typing import Annotated

from fastapi import APIRouter, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from expense_analyzer.api.deps import DbSession
from expense_analyzer.api.forms import LoginForm
from expense_analyzer.auth import login_session, logout_session, verify_password
from expense_analyzer.queries import users
from expense_analyzer.templating import templates

router = APIRouter(tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {})


@router.post("/login")
def login(
    request: Request,
    form: Annotated[LoginForm, Form()],
    session: DbSession,
) -> Response:
    user = users.get_by_username(session, form.username.strip())
    if (
        user is None
        or not user.is_active
        or not verify_password(form.password.get_secret_value(), user.password_hash)
    ):
        # Same message whether the user exists or not — don't leak which.
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid username or password."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    login_session(request, user)

    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    logout_session(request)

    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
