"""Home dashboard page: account & category setup and import-batch rollback.

Part of the dashboard — the working surface (design §8). Handlers stay thin: all
DB access goes through ``expense_analyzer.queries``. Every route requires a
logged-in user; the household view is shared (no per-user data isolation).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session

from expense_analyzer.api.deps import CurrentUser, DbSession
from expense_analyzer.api.forms import AccountForm, CategoryColorForm, CategoryForm
from expense_analyzer.auth import require_user
from expense_analyzer.importers.pipeline import rollback_batch
from expense_analyzer.models import AccountType, CategoryKind, ImportBatch, Owner
from expense_analyzer.queries import accounts, batches, categories
from expense_analyzer.queries.categories import HEX_COLOR_RE
from expense_analyzer.queries.transactions import MANUAL_BATCH_SOURCE
from expense_analyzer.templating import templates

router = APIRouter(prefix="/dashboard", tags=["home"], dependencies=[Depends(require_user)])


def _index_context(session: Session, user: Owner, **extra) -> dict:
    return {
        "user": user,
        "accounts": accounts.list_accounts(session),
        "categories": categories.list_categories(session),
        "batches": batches.recent_batches(session),
        "account_types": [t.value for t in AccountType],
        "category_kinds": [k.value for k in CategoryKind],
        **extra,
    }


def _parse_color(raw: str) -> tuple[str | None, str | None]:
    """Validate an "#rrggbb" string from the colour picker. Returns
    ``(colour, error)``: blank input is a valid "no colour" (``None``); anything
    that isn't a 6-digit hex colour is rejected so it never reaches the markup."""
    value = raw.strip().lower()
    if not value:
        return None, None
    if not HEX_COLOR_RE.match(value):
        return None, "Colour must be a hex value like #4f8cff."

    return value, None


@router.get("", response_class=HTMLResponse)
def index(request: Request, user: CurrentUser, session: DbSession) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", _index_context(session, user))


@router.post("/accounts")
def create_account(form: Annotated[AccountForm, Form()], session: DbSession) -> RedirectResponse:
    accounts.create_account(session, name=form.name, type=form.type)

    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/categories")
def create_category(
    request: Request, form: Annotated[CategoryForm, Form()], user: CurrentUser, session: DbSession
) -> Response:
    color, error = _parse_color(form.color)
    if error is not None:
        return templates.TemplateResponse(
            request,
            "index.html",
            _index_context(session, user, error=error),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    categories.create_category(session, name=form.name, kind=form.kind, color=color)

    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/categories/{category_id}/color")
def set_category_color(
    request: Request,
    category_id: int,
    form: Annotated[CategoryColorForm, Form()],
    user: CurrentUser,
    session: DbSession,
) -> Response:
    # "Clear" wins over whatever the picker holds; otherwise validate the hex.
    if form.clear:
        color, error = None, None
    else:
        color, error = _parse_color(form.color)

    if error is not None:
        return templates.TemplateResponse(
            request,
            "index.html",
            _index_context(session, user, error=error),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if categories.set_category_color(session, category_id, color) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="category not found")

    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/batches/{batch_id}/rollback")
def rollback(batch_id: int, session: DbSession) -> RedirectResponse:
    # The Manual batch is a container for hand-entered rows, not a real import —
    # rolling it back would wipe every cash entry at once. Those are deleted one at
    # a time from the transactions list instead. Refuse it (defence in depth; the UI
    # also hides the button for this batch).
    batch = session.get(ImportBatch, batch_id)
    if batch is not None and batch.source == MANUAL_BATCH_SOURCE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="manual entries are deleted individually, not rolled back as a batch",
        )
    rollback_batch(session, batch_id)

    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
