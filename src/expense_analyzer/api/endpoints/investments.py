"""Investments page (design §7.3): portfolio holdings, allocation, and import.

Two sources feed it (both upsert a dated snapshot via ``import_positions``):
- **Fetch from myFund** — pulls the configured portfolio over the network. Hidden
  unless myFund is configured (``EA_MYFUND_API_KEY`` + ``EA_MYFUND_PORTFOLIO``);
  with no key the app makes no outbound calls at all.
- **Upload XTB export** — parses an offline ``.xlsx`` the user downloaded.

Handlers stay thin: parsing lives in the importers, DB access in queries. A bad
file / failed fetch becomes a red flash, never a 500.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from expense_analyzer.api.deps import CurrentUser, DbSession
from expense_analyzer.api.forms import FetchPositionsForm
from expense_analyzer.auth import require_user
from expense_analyzer.clock import utc_now
from expense_analyzer.config import get_settings
from expense_analyzer.importers.base import ImporterError
from expense_analyzer.importers.myfund import MyFundClient, MyFundError
from expense_analyzer.importers.positions import import_positions
from expense_analyzer.importers.xtb import MAX_XLSX_BYTES, XTBImporter
from expense_analyzer.models import Account, AccountType, Owner
from expense_analyzer.queries import accounts
from expense_analyzer.queries import investments as investment_queries
from expense_analyzer.templating import templates

router = APIRouter(
    prefix="/dashboard/investments", tags=["investments"], dependencies=[Depends(require_user)]
)


def _context(session: Session, user: Owner, **extra) -> dict:
    """Shared context: portfolio accounts and each one's latest holdings."""
    portfolios = investment_queries.portfolio_accounts(session)
    holdings = [
        {
            "account": acc,
            "snapshot_date": investment_queries.latest_snapshot_date(session, acc.id),
            "positions": (positions := investment_queries.latest_positions(session, acc.id)),
            "total": sum(p.value for p in positions),
        }
        for acc in portfolios
    ]
    # Allocation chart over the first portfolio with holdings (the common single-
    # portfolio case); amounts stay minor units, the template divides by 100.
    chart = next(
        (
            {
                "labels": [p.ticker for p in h["positions"]],
                "data": [p.value for p in h["positions"]],
            }
            for h in holdings
            if h["positions"]
        ),
        {"labels": [], "data": []},
    )
    return {
        "user": user,
        "portfolios": portfolios,
        "holdings": holdings,
        "allocation_chart": chart,
        "myfund_configured": get_settings().myfund_configured,
        **extra,
    }


@router.get("", response_class=HTMLResponse)
def investments_page(request: Request, user: CurrentUser, session: DbSession) -> HTMLResponse:
    return templates.TemplateResponse(request, "investments.html", _context(session, user))


def _require_portfolio(session: Session, account_id: int) -> Account | None:
    account = accounts.get_account(session, account_id)
    if account is None or account.type != AccountType.portfolio:
        return None

    return account


@router.post("/fetch", response_class=HTMLResponse)
def fetch_from_myfund(
    request: Request,
    form: Annotated[FetchPositionsForm, Form()],
    user: CurrentUser,
    session: DbSession,
) -> HTMLResponse:
    settings = get_settings()
    error: str | None = None
    flash: str | None = None

    if not settings.myfund_configured:
        error = "myFund is not configured. Set EA_MYFUND_API_KEY and EA_MYFUND_PORTFOLIO."
    elif _require_portfolio(session, form.account_id) is None:
        error = "Pick a portfolio account (create one of type 'portfolio' first)."
    else:
        try:
            result = MyFundClient.from_settings(settings).fetch()
            summary = import_positions(
                session,
                account_id=form.account_id,
                result=result,
                source="myfund_api",
                fetched_at=utc_now(),
            )
        except MyFundError as exc:
            error = f"Could not fetch from myFund: {exc}"
        else:
            flash = (
                f"Fetched {summary.imported} positions from myFund "
                f"({summary.inserted} new, {summary.updated} updated)."
            )

    return templates.TemplateResponse(
        request,
        "investments.html",
        _context(session, user, error=error, flash=flash),
        status_code=status.HTTP_200_OK if error is None else status.HTTP_400_BAD_REQUEST,
    )


@router.post("/upload", response_class=HTMLResponse)
async def upload_xtb(
    request: Request,
    user: CurrentUser,
    session: DbSession,
    account_id: int = Form(...),
    file: UploadFile = File(...),
) -> HTMLResponse:
    error: str | None = None
    flash: str | None = None

    if _require_portfolio(session, account_id) is None:
        error = "Pick a portfolio account (create one of type 'portfolio' first)."
    else:
        # Read at most one byte past the cap so an oversized upload is rejected
        # without ever loading the whole (potentially huge) file into memory.
        data = await file.read(MAX_XLSX_BYTES + 1)
        try:
            if len(data) > MAX_XLSX_BYTES:
                raise ImporterError(
                    f"file is too large; an XTB export is well under "
                    f"{MAX_XLSX_BYTES // (1024 * 1024)} MiB"
                )
            result = XTBImporter().parse(data)
            summary = import_positions(
                session,
                account_id=account_id,
                result=result,
                source="xtb",
                fetched_at=utc_now(),
            )
        except ImporterError as exc:
            error = f"Could not parse the XTB export: {exc}"
        else:
            flash = (
                f"Imported {summary.imported} positions from XTB "
                f"({summary.inserted} new, {summary.updated} updated). "
                f"Reconciliation: {summary.reconciliation.label}."
            )

    return templates.TemplateResponse(
        request,
        "investments.html",
        _context(session, user, error=error, flash=flash),
        status_code=status.HTTP_200_OK if error is None else status.HTTP_400_BAD_REQUEST,
    )
