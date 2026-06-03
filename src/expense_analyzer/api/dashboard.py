"""Dashboard — the working surface (design §8).

Plain server-rendered forms (POST -> redirect); the only client-side richness is
Chart.js on the overview, served from vendored static assets so the Pi stays
offline. See roadmap §11 (Phase 4: transaction list with filters + pagination,
overview charts).

Handlers stay thin: all DB access goes through ``expense_analyzer.queries``.
Every route requires a logged-in user (``require_user``); the household view is
shared (no per-user data isolation).
"""

from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from expense_analyzer.auth import require_user
from expense_analyzer.clock import local_month, utc_now
from expense_analyzer.config import get_settings
from expense_analyzer.db import get_session
from expense_analyzer.importers import ImporterError, run_import
from expense_analyzer.importers.pipeline import rollback_batch
from expense_analyzer.importers.registry import available, get_importer
from expense_analyzer.loans import LoanScheduleError
from expense_analyzer.models import (
    AccountType,
    CategoryKind,
    InstallmentType,
    Owner,
    RateType,
    Scope,
)
from expense_analyzer.money import MoneyParseError, parse_pln
from expense_analyzer.queries import accounts, batches, categories, stats, transactions
from expense_analyzer.queries import loans as loan_queries
from expense_analyzer.queries import transfers as transfer_queries
from expense_analyzer.queries import users as user_queries
from expense_analyzer.queries.transactions import UNCATEGORIZED, TransactionFilters
from expense_analyzer.templating import templates
from expense_analyzer.transfers import find_transfer_pairs

# Months of history on the overview trend chart.
TREND_MONTHS = 12

# Every route here requires login; handlers only re-declare `user` when they
# need the object (FastAPI caches the dependency within a request).
router = APIRouter(prefix="/dashboard", tags=["dashboard"], dependencies=[Depends(require_user)])


@router.get("", response_class=HTMLResponse)
def index(
    request: Request,
    user: Owner = Depends(require_user),
    session: Session = Depends(get_session),
) -> HTMLResponse:
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


@router.get("/stats", response_class=HTMLResponse)
def stats_page(
    request: Request,
    month: str | None = None,
    user: Owner = Depends(require_user),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    months = stats.available_months(session)
    # Default to the most recent month with data, falling back to the current
    # local month so an empty DB still renders a sensible (zeroed) summary.
    selected = month or (months[0] if months else local_month(utc_now()))

    category_names = {c.id: c.name for c in categories.list_categories(session) if c.id is not None}
    # One transfer-excluded scan feeds both the month summary and the trend.
    spendable = stats.spendable_transactions(session)
    summary = stats.month_summary(spendable, selected, category_names)
    trend = stats.spending_trend(spendable, months=TREND_MONTHS)

    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "user": user,
            "months": months,
            "month": selected,
            "summary": summary,
            "trend": trend,
            # Chart.js datasets (amounts are minor units; the template divides by
            # 100 for display so money never round-trips as a float).
            "category_chart": {
                "labels": [c.name for c in summary.by_category],
                "data": [c.total for c in summary.by_category],
            },
            "trend_chart": {
                "labels": [m.month for m in trend],
                "spending": [m.spending for m in trend],
                "income": [m.income for m in trend],
            },
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
def upload_form(
    request: Request,
    user: Owner = Depends(require_user),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "upload.html",
        {"user": user, "accounts": accounts.list_accounts(session), "importers": available()},
    )


@router.post("/upload", response_class=HTMLResponse)
async def upload(
    request: Request,
    account_id: int = Form(...),
    importer: str = Form(...),
    file: UploadFile = File(...),
    user: Owner = Depends(require_user),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    context: dict = {
        "user": user,
        "accounts": accounts.list_accounts(session),
        "importers": available(),
    }

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
    month: str | None = None,
    category: str | None = None,  # "none" = uncategorized, a digit = that category
    scope: Scope | None = None,
    q: str | None = None,
    page: int = 1,
    user: Owner = Depends(require_user),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    uncategorized = category == UNCATEGORIZED
    category_id = int(category) if category and category.isdigit() else None

    filters = TransactionFilters(
        account_id=account_id,
        month=month or None,
        category_id=category_id,
        uncategorized=uncategorized,
        scope=scope,
        search=q or None,
    )
    result = transactions.list_transactions(
        session, filters, page=page, page_size=get_settings().page_size
    )

    def page_query(target_page: int) -> str:
        """Querystring for a pager link — keeps the active filters, swaps page."""
        params: list[tuple[str, str]] = []
        if account_id is not None:
            params.append(("account_id", str(account_id)))
        if month:
            params.append(("month", month))
        if category:
            params.append(("category", category))
        if scope:
            params.append(("scope", scope.value))
        if q:
            params.append(("q", q))
        params.append(("page", str(max(1, target_page))))

        return urlencode(params)

    # Where the categorize form returns to — the current filtered/paged view.
    return_to = "/dashboard/transactions"
    if request.url.query:
        return_to += f"?{request.url.query}"

    return templates.TemplateResponse(
        request,
        "transactions.html",
        {
            "user": user,
            "page": result,
            "accounts": accounts.list_accounts(session),
            "categories": categories.list_categories(session),
            "months": stats.available_months(session),
            "scopes": [s.value for s in Scope],
            "page_query": page_query,
            "return_to": return_to,
            # Echo the active filters back so the form stays sticky and the pager
            # carries them across pages.
            "f_account_id": account_id,
            "f_month": month or "",
            "f_category": category or "",
            "f_scope": scope.value if scope else "",
            "f_q": q or "",
        },
    )


@router.post("/transactions/{tx_id}/categorize")
def categorize(
    tx_id: int,
    category_id: str = Form(""),
    scope: Scope = Form(...),
    return_to: str = Form(""),
    user: Owner = Depends(require_user),
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

    # Return to the filtered/paged view the user came from. Only accept the list
    # path itself or the list path with a query string — no open redirect, and no
    # sibling path like "/dashboard/transactionsX" (defense in depth, per review).
    list_path = "/dashboard/transactions"
    allowed = return_to == list_path or return_to.startswith(f"{list_path}?")
    dest = return_to if allowed else list_path

    return RedirectResponse(dest, status_code=303)


@router.get("/transfers", response_class=HTMLResponse)
def transfers_page(
    request: Request,
    flash: str | None = None,
    user: Owner = Depends(require_user),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    # Suggestions are recomputed live (read-only) so a GET never mutates state;
    # auto-linking happens only on import or an explicit rescan.
    result = find_transfer_pairs(
        transfer_queries.unmatched_candidates(session),
        window_days=get_settings().transfer_window_days,
    )

    return templates.TemplateResponse(
        request,
        "transfers.html",
        {
            "user": user,
            "suggestions": result.ambiguous,
            "groups": transfer_queries.list_transfer_groups(session),
            "accounts": {a.id: a.name for a in accounts.list_accounts(session)},
            "flash": flash,
        },
    )


@router.post("/transfers/confirm")
def confirm_transfer(
    tx_a_id: int = Form(...),
    tx_b_id: int = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    if transfer_queries.link_transfer(session, tx_a_id=tx_a_id, tx_b_id=tx_b_id) is None:
        raise HTTPException(status_code=404, detail="not a valid transfer pair")

    return RedirectResponse("/dashboard/transfers", status_code=303)


@router.post("/transfers/rescan")
def rescan_transfers(
    session: Session = Depends(get_session),
) -> RedirectResponse:
    linked, _ = transfer_queries.detect_and_autolink(
        session, window_days=get_settings().transfer_window_days
    )

    return RedirectResponse(
        f"/dashboard/transfers?flash=Auto-linked+{linked}+transfer(s).", status_code=303
    )


@router.post("/transfers/{group_id}/unlink")
def unlink_transfer(
    group_id: str,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    transfer_queries.unlink_transfer(session, group_id)

    return RedirectResponse("/dashboard/transfers", status_code=303)


@router.post("/batches/{batch_id}/rollback")
def rollback(
    batch_id: int,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    rollback_batch(session, batch_id)

    return RedirectResponse("/dashboard", status_code=303)


def _loans_context(session: Session, user: Owner, **extra) -> dict:
    """Shared context for the loans list page (create form + existing loans)."""
    return {
        "user": user,
        "loans": loan_queries.list_loans(session),
        "loan_accounts": loan_queries.loan_accounts(session),
        "accounts": {a.id: a.name for a in accounts.list_accounts(session)},
        "rate_types": [t.value for t in RateType],
        "installment_types": [t.value for t in InstallmentType],
        **extra,
    }


@router.get("/loans", response_class=HTMLResponse)
def loans_page(
    request: Request,
    user: Owner = Depends(require_user),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    return templates.TemplateResponse(request, "loans.html", _loans_context(session, user))


@router.post("/loans", response_class=HTMLResponse)
def create_loan(
    request: Request,
    account_id: int = Form(...),
    principal: str = Form(...),
    rate_type: RateType = Form(...),
    rate_percent: str = Form(...),  # fixed: the rate; variable: the margin
    installment_type: InstallmentType = Form(...),
    start_date: str = Form(...),
    term_months: int = Form(...),
    base_rate_ref: str = Form(""),
    base_rate_percent: str = Form(""),  # variable only: initial base rate
    user: Owner = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    account = accounts.get_account(session, account_id)
    error: str | None = None
    if account is None or account.type != AccountType.loan:
        error = "Pick a loan account (create one of type 'loan' first)."
    elif term_months < 1:
        error = "Term must be at least one month."
    else:
        try:
            principal_minor = parse_pln(principal)
            rate_bp = parse_pln(rate_percent)  # "7,25" -> 725 basis points
            initial_base_rate_bp = (
                parse_pln(base_rate_percent) if base_rate_percent.strip() else None
            )
            start = date.fromisoformat(start_date)
        except (MoneyParseError, ValueError) as exc:
            error = f"Could not read the numbers/date: {exc}"
        else:
            if principal_minor <= 0:
                error = "Principal must be a positive amount."
            elif rate_type is RateType.variable and initial_base_rate_bp is None:
                error = "A variable-rate loan needs an initial base rate (e.g. current WIBOR)."

    if error is not None:
        return templates.TemplateResponse(
            request, "loans.html", _loans_context(session, user, error=error), status_code=400
        )

    loan = loan_queries.create_loan(
        session,
        account_id=account_id,
        principal=principal_minor,
        rate_type=rate_type,
        rate_bp=rate_bp,
        installment_type=installment_type,
        start_date=start,
        term_months=term_months,
        base_rate_ref=base_rate_ref.strip() or None,
        initial_base_rate_bp=initial_base_rate_bp,
    )

    return RedirectResponse(f"/dashboard/loans/{loan.id}", status_code=303)


@router.get("/loans/{loan_id}", response_class=HTMLResponse)
def loan_detail(
    request: Request,
    loan_id: int,
    flash: str | None = None,
    user: Owner = Depends(require_user),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    loan = loan_queries.get_loan(session, loan_id)
    if loan is None:
        raise HTTPException(status_code=404, detail=f"loan {loan_id} not found")

    # A misconfigured variable loan (no base rate by month 1) can't produce a
    # schedule — show the reason instead of a 500.
    schedule_error: str | None = None
    reconciliation = None
    suggestions: list = []
    try:
        # Compute the schedule once and feed both the reconciliation and the
        # payment suggestions (it's the expensive bit for a long-term loan).
        schedule = loan_queries.loan_schedule(session, loan_id)
        reconciliation = loan_queries.loan_reconciliation(session, loan_id, schedule)
        settings = get_settings()
        suggestions = loan_queries.suggest_payments(
            session,
            loan,
            schedule,
            window_days=settings.loan_match_window_days,
            tolerance_pct=settings.loan_match_amount_tolerance_pct,
        )
    except LoanScheduleError as exc:
        schedule_error = str(exc)

    return templates.TemplateResponse(
        request,
        "loan_detail.html",
        {
            "user": user,
            "loan": loan,
            "account_name": (acc := accounts.get_account(session, loan.account_id)) and acc.name,
            "reconciliation": reconciliation,
            "schedule_error": schedule_error,
            "rate_changes": loan_queries.list_rate_changes(session, loan_id),
            "suggestions": suggestions,
            "accounts": {a.id: a.name for a in accounts.list_accounts(session)},
            "flash": flash,
        },
    )


@router.post("/loans/{loan_id}/rate-changes")
def add_rate_change(
    loan_id: int,
    effective_date: str = Form(...),
    base_rate_percent: str = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    loan = loan_queries.get_loan(session, loan_id)
    if loan is None:
        raise HTTPException(status_code=404, detail=f"loan {loan_id} not found")
    try:
        effective = date.fromisoformat(effective_date)
        base_rate_bp = parse_pln(base_rate_percent)
    except (MoneyParseError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid rate change: {exc}") from exc

    loan_queries.add_rate_change(
        session, loan_id=loan_id, effective_date=effective, base_rate_bp=base_rate_bp
    )

    return RedirectResponse(f"/dashboard/loans/{loan_id}", status_code=303)


@router.post("/loans/{loan_id}/payments/link")
def link_loan_payment(
    loan_id: int,
    tx_id: int = Form(...),
    installment_index: int = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    if not loan_queries.link_payment(
        session, loan_id=loan_id, tx_id=tx_id, installment_index=installment_index
    ):
        raise HTTPException(status_code=404, detail="could not link payment")

    return RedirectResponse(f"/dashboard/loans/{loan_id}", status_code=303)


@router.post("/loans/{loan_id}/payments/{tx_id}/unlink")
def unlink_loan_payment(
    loan_id: int,
    tx_id: int,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    loan_queries.unlink_payment(session, tx_id)

    return RedirectResponse(f"/dashboard/loans/{loan_id}", status_code=303)


@router.post("/loans/{loan_id}/delete")
def delete_loan(
    loan_id: int,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    loan_queries.delete_loan(session, loan_id)

    return RedirectResponse("/dashboard/loans", status_code=303)


@router.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    user: Owner = Depends(require_user),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "users.html",
        {"user": user, "users": user_queries.list_users(session)},
    )


@router.post("/users", response_class=HTMLResponse)
def add_user(
    request: Request,
    username: str = Form(...),
    name: str = Form(...),
    password: str = Form(...),
    user: Owner = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    if user_queries.get_by_username(session, username.strip()) is not None:
        return templates.TemplateResponse(
            request,
            "users.html",
            {
                "user": user,
                "users": user_queries.list_users(session),
                "error": f"Username {username.strip()!r} is already taken.",
            },
            status_code=409,
        )

    user_queries.create_user(session, username=username, name=name, password=password)

    return RedirectResponse("/dashboard/users", status_code=303)
