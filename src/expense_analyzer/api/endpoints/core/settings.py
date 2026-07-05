"""Setup page (/dashboard/settings): account & category setup and import-batch
rollback. The dashboard landing page (/dashboard) is the overview — see overview.py.

Part of the dashboard — the working surface (design §8). Handlers stay thin: all
DB access goes through ``expense_analyzer.queries``. Every route requires a
logged-in user; the household view is shared (no per-user data isolation).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session

from expense_analyzer import iban
from expense_analyzer.api.deps import CurrentUser, DbSession
from expense_analyzer.api.forms import AccountForm, CategoryEditForm, CategoryForm
from expense_analyzer.auth import require_user
from expense_analyzer.importers.pipeline import rollback_batch
from expense_analyzer.models import AccountType, CategoryKind, ImportBatch, Owner
from expense_analyzer.queries.categorize import categories
from expense_analyzer.queries.categorize.categories import HEX_COLOR_RE
from expense_analyzer.queries.core import accounts
from expense_analyzer.queries.money import batches
from expense_analyzer.queries.money.transactions import MANUAL_BATCH_SOURCE
from expense_analyzer.templating import templates

router = APIRouter(prefix="/dashboard", tags=["settings"], dependencies=[Depends(require_user)])


def _settings_context(session: Session, user: Owner, **extra) -> dict:
    return {
        "user": user,
        "accounts": accounts.list_accounts(session),
        "categories": categories.list_categories(session),
        "batches": batches.recent_batches(session),
        "account_types": [t.value for t in AccountType],
        "category_kinds": [k.value for k in CategoryKind],
        # Re-render helpers: an error path passes the submitted AccountForm back so
        # the form keeps what the user typed; edit_id marks which row it belongs to.
        "account_form": None,
        "edit_id": None,
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


def _parse_number(raw: str) -> tuple[str | None, str | None]:
    """Parse the optional account number / IBAN from the form. Returns
    ``(number, error)``: blank is a valid "no number" (``None``). A value shaped like
    an IBAN is stored canonically (upper, no spaces) and must pass the mod-97
    checksum, so a mistyped payment reference is caught. Anything else (cash box,
    brokerage id) is kept exactly as typed (only trimmed) — its case and separators
    can be meaningful, so we don't mangle it."""
    if not raw.strip():
        return None, None
    compact = iban.normalize(raw)
    if iban.looks_like_iban(compact):
        if not iban.is_valid(compact):
            return None, "That IBAN doesn't look valid — check the digits."

        return compact, None

    return raw.strip(), None


# Setup page (accounts, categories, recent imports). Lives at /dashboard/settings
# now that /dashboard itself is the overview — see overview.py.
@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: CurrentUser, session: DbSession) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "core/settings.html", _settings_context(session, user)
    )


@router.post("/accounts")
def create_account(
    request: Request, form: Annotated[AccountForm, Form()], user: CurrentUser, session: DbSession
) -> Response:
    # Name is the required field, so check it first — its error shouldn't be masked
    # by a number problem (mirrors create/edit category).
    number = None
    if not form.name.strip():
        error = "Account name can't be empty."
    else:
        number, error = _parse_number(form.number)

    if error is not None:
        return templates.TemplateResponse(
            request,
            "core/settings.html",
            _settings_context(session, user, error=error, account_form=form),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    accounts.create_account(session, name=form.name, type=form.type, number=number)

    return RedirectResponse("/dashboard/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/accounts/{account_id}/edit")
def edit_account(
    request: Request,
    account_id: int,
    form: Annotated[AccountForm, Form()],
    user: CurrentUser,
    session: DbSession,
) -> Response:
    number = None
    if not form.name.strip():
        error = "Account name can't be empty."
    else:
        number, error = _parse_number(form.number)

    if error is not None:
        return templates.TemplateResponse(
            request,
            "core/settings.html",
            _settings_context(session, user, error=error, account_form=form, edit_id=account_id),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if (
        accounts.update_account(session, account_id, name=form.name, type=form.type, number=number)
        is None
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")

    return RedirectResponse("/dashboard/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/categories")
def create_category(
    request: Request, form: Annotated[CategoryForm, Form()], user: CurrentUser, session: DbSession
) -> Response:
    color, error = _parse_color(form.color)
    if error is not None:
        return templates.TemplateResponse(
            request,
            "core/settings.html",
            _settings_context(session, user, error=error),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    categories.create_category(session, name=form.name, kind=form.kind, color=color)

    return RedirectResponse("/dashboard/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/categories/{category_id}/edit")
def edit_category(
    request: Request,
    category_id: int,
    form: Annotated[CategoryEditForm, Form()],
    user: CurrentUser,
    session: DbSession,
) -> Response:
    # Name is the required field, so check it first — its error shouldn't be
    # masked by a colour problem. "Clear" wins over whatever the picker holds;
    # otherwise validate the hex. Normalisation (strip) lives in the query layer,
    # consistent with create_category.
    color, error = None, None
    if not form.name.strip():
        error = "Category name can't be empty."
    elif not form.clear:
        color, error = _parse_color(form.color)

    if error is not None:
        return templates.TemplateResponse(
            request,
            "core/settings.html",
            _settings_context(session, user, error=error),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if (
        categories.update_category(
            session, category_id, name=form.name, kind=form.kind, color=color
        )
        is None
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="category not found")

    return RedirectResponse("/dashboard/settings", status_code=status.HTTP_303_SEE_OTHER)


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

    return RedirectResponse("/dashboard/settings", status_code=status.HTTP_303_SEE_OTHER)
