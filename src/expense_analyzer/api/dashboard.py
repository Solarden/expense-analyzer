"""Dashboard — the minimal Phase 1 working surface.

Plain server-rendered forms (POST -> redirect). HTMX/Chart.js richer
interactivity is deferred to the full dashboard (roadmap §11, Phase 4); Phase 1
only needs CSV upload and manual categorization. See design §8.
"""

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, col, select

from expense_analyzer.db import get_session
from expense_analyzer.importers import run_import
from expense_analyzer.importers.pipeline import rollback_batch
from expense_analyzer.importers.registry import available, get_importer
from expense_analyzer.models import (
    Account,
    AccountType,
    Category,
    CategoryKind,
    ImportBatch,
    Scope,
    Transaction,
    TxSource,
)
from expense_analyzer.templating import templates

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _accounts(session: Session) -> list[Account]:
    return list(session.exec(select(Account).order_by(col(Account.name))).all())


def _categories(session: Session) -> list[Category]:
    return list(session.exec(select(Category).order_by(col(Category.name))).all())


@router.get("", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    batches = session.exec(select(ImportBatch).order_by(col(ImportBatch.imported_at).desc())).all()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "accounts": _accounts(session),
            "categories": _categories(session),
            "batches": batches,
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
    session.add(Account(name=name.strip(), type=type))
    session.commit()
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/categories")
def create_category(
    name: str = Form(...),
    kind: CategoryKind = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    session.add(Category(name=name.strip(), kind=kind))
    session.commit()
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "upload.html",
        {"accounts": _accounts(session), "importers": available()},
    )


@router.post("/upload", response_class=HTMLResponse)
async def upload(
    request: Request,
    account_id: int = Form(...),
    importer: str = Form(...),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    data = await file.read()
    summary = run_import(
        session,
        account_id=account_id,
        importer=get_importer(importer),
        filename=file.filename or "upload.csv",
        data=data,
    )
    return templates.TemplateResponse(
        request,
        "upload.html",
        {
            "accounts": _accounts(session),
            "importers": available(),
            "summary": summary,
            "account_id": account_id,
            "flash": f"Imported: {summary.new} new, {summary.skipped} skipped.",
        },
    )


@router.get("/transactions", response_class=HTMLResponse)
def transactions(
    request: Request,
    account_id: int | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    query = select(Transaction).where(col(Transaction.deleted_at).is_(None))
    if account_id is not None:
        query = query.where(Transaction.account_id == account_id)
    query = query.order_by(col(Transaction.booked_date).desc(), col(Transaction.id).desc())
    rows = session.exec(query).all()
    return templates.TemplateResponse(
        request,
        "transactions.html",
        {
            "transactions": rows,
            "accounts": _accounts(session),
            "categories": _categories(session),
            "account_id": account_id,
            "scopes": [s.value for s in Scope],
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
    tx = session.get(Transaction, tx_id)
    if tx is not None:
        tx.category_id = int(category_id) if category_id else None
        tx.scope = scope
        tx.source = TxSource.manual  # a human touched it
        session.add(tx)
        session.commit()
    dest = "/dashboard/transactions"
    if account_id:
        dest += f"?account_id={account_id}"
    return RedirectResponse(dest, status_code=303)


@router.post("/batches/{batch_id}/rollback")
def rollback(batch_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    rollback_batch(session, batch_id)
    return RedirectResponse("/dashboard", status_code=303)
