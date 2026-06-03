"""Home dashboard page: account & category setup and import-batch rollback.

Part of the dashboard — the working surface (design §8). Handlers stay thin: all
DB access goes through ``expense_analyzer.queries``. Every route requires a
logged-in user; the household view is shared (no per-user data isolation).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from expense_analyzer.api.deps import CurrentUser, DbSession
from expense_analyzer.api.forms import AccountForm, CategoryForm
from expense_analyzer.auth import require_user
from expense_analyzer.importers.pipeline import rollback_batch
from expense_analyzer.models import AccountType, CategoryKind
from expense_analyzer.queries import accounts, batches, categories
from expense_analyzer.templating import templates

router = APIRouter(prefix="/dashboard", tags=["home"], dependencies=[Depends(require_user)])


@router.get("", response_class=HTMLResponse)
def index(request: Request, user: CurrentUser, session: DbSession) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "user": user,
            "accounts": accounts.list_accounts(session),
            "categories": categories.list_categories(session),
            "batches": batches.recent_batches(session),
            "account_types": [t.value for t in AccountType],
            "category_kinds": [k.value for k in CategoryKind],
        },
    )


@router.post("/accounts")
def create_account(form: Annotated[AccountForm, Form()], session: DbSession) -> RedirectResponse:
    accounts.create_account(session, name=form.name, type=form.type)

    return RedirectResponse("/dashboard", status_code=303)


@router.post("/categories")
def create_category(form: Annotated[CategoryForm, Form()], session: DbSession) -> RedirectResponse:
    categories.create_category(session, name=form.name, kind=form.kind)

    return RedirectResponse("/dashboard", status_code=303)


@router.post("/batches/{batch_id}/rollback")
def rollback(batch_id: int, session: DbSession) -> RedirectResponse:
    rollback_batch(session, batch_id)

    return RedirectResponse("/dashboard", status_code=303)
