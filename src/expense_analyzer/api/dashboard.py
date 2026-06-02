"""Dashboard — the minimal Phase 1 working surface.

Plain server-rendered forms (POST -> redirect). HTMX/Chart.js richer
interactivity is deferred to the full dashboard (roadmap §11, Phase 4); Phase 1
only needs CSV upload and manual categorization. See design §8.

Handlers stay thin: all DB access goes through ``expense_analyzer.queries``.
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from expense_analyzer.db import get_session
from expense_analyzer.importers import ImporterError, run_import
from expense_analyzer.importers.pipeline import rollback_batch
from expense_analyzer.importers.registry import available, get_importer
from expense_analyzer.models import AccountType, CategoryKind, Scope
from expense_analyzer.queries import accounts, batches, categories, transactions
from expense_analyzer.queries.transactions import DEFAULT_LIMIT
from expense_analyzer.templating import templates

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "accounts": accounts.list_accounts(session),
            "categories": categories.list_categories(session),
            "batches": batches.recent_batches(session),
            "account_types": [t.value for t in AccountType],
            "category_kinds": [k.value for k in CategoryKind],
        },
    )


@router.post("/accounts")
def create_account(
    name: str = Form(...),
    type: AccountType = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    accounts.create_account(session, name=name, type=type)

    return RedirectResponse("/dashboard", status_code=303)


@router.post("/categories")
def create_category(
    name: str = Form(...),
    kind: CategoryKind = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    categories.create_category(session, name=name, kind=kind)

    return RedirectResponse("/dashboard", status_code=303)


@router.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "upload.html",
        {"accounts": accounts.list_accounts(session), "importers": available()},
    )


@router.post("/upload", response_class=HTMLResponse)
async def upload(
    request: Request,
    account_id: int = Form(...),
    importer: str = Form(...),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    context: dict = {"accounts": accounts.list_accounts(session), "importers": available()}

    if importer not in available():
        context["error"] = f"Unknown importer: {importer!r}."
    elif accounts.get_account(session, account_id) is None:
        context["error"] = f"Unknown account #{account_id}."
    else:
        data = await file.read()
        try:
            summary = run_import(
                session,
                account_id=account_id,
                importer=get_importer(importer),
                filename=file.filename or "upload.csv",
                data=data,
            )
        except ImporterError as exc:
            # Wrong bank/format or a malformed row — a normal user mistake, not a crash.
            context["error"] = f"Could not parse the file: {exc}"
        else:
            context["summary"] = summary
            context["account_id"] = account_id
            context["flash"] = f"Imported: {summary.new} new, {summary.skipped} skipped."

    return templates.TemplateResponse(request, "upload.html", context)


@router.get("/transactions", response_class=HTMLResponse)
def list_transactions(
    request: Request,
    account_id: int | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "transactions.html",
        {
            "transactions": transactions.list_transactions(session, account_id),
            "accounts": accounts.list_accounts(session),
            "categories": categories.list_categories(session),
            "account_id": account_id,
            "scopes": [s.value for s in Scope],
            "limit": DEFAULT_LIMIT,
        },
    )


@router.post("/transactions/{tx_id}/categorize")
def categorize(
    tx_id: int,
    category_id: str = Form(""),
    scope: Scope = Form(...),
    account_id: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    parsed_category_id: int | None = None
    if category_id:
        if not category_id.isdigit():
            raise HTTPException(status_code=400, detail=f"invalid category id: {category_id!r}")
        parsed_category_id = int(category_id)
        if categories.get_category(session, parsed_category_id) is None:
            raise HTTPException(status_code=404, detail=f"category {parsed_category_id} not found")

    if (
        transactions.set_category(session, tx_id=tx_id, category_id=parsed_category_id, scope=scope)
        is None
    ):
        raise HTTPException(status_code=404, detail=f"transaction {tx_id} not found")

    dest = "/dashboard/transactions"
    if account_id:
        dest += f"?account_id={account_id}"

    return RedirectResponse(dest, status_code=303)


@router.post("/batches/{batch_id}/rollback")
def rollback(batch_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    rollback_batch(session, batch_id)

    return RedirectResponse("/dashboard", status_code=303)
