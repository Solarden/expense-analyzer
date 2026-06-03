"""Loans page: define loans, view amortization schedules, and link real
installment payments to the plan (design §7.4).

Handlers stay thin: the schedule math is pure (:mod:`expense_analyzer.loans`) and
all DB access goes through :mod:`expense_analyzer.queries.loans`. Bad form input
(wrong numbers/date, missing base rate) becomes a red flash, not a 500.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from expense_analyzer.api.deps import CurrentUser, DbSession
from expense_analyzer.api.forms import LoanForm, PaymentLinkForm, RateChangeForm
from expense_analyzer.auth import require_user
from expense_analyzer.config import get_settings
from expense_analyzer.loans import LoanScheduleError
from expense_analyzer.models import AccountType, InstallmentType, LoanCreate, Owner, RateType
from expense_analyzer.money import MoneyParseError, parse_pln
from expense_analyzer.queries import accounts
from expense_analyzer.queries import loans as loan_queries
from expense_analyzer.templating import templates

router = APIRouter(prefix="/dashboard/loans", tags=["loans"], dependencies=[Depends(require_user)])


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


@router.get("", response_class=HTMLResponse)
def loans_page(request: Request, user: CurrentUser, session: DbSession) -> HTMLResponse:
    return templates.TemplateResponse(request, "loans.html", _loans_context(session, user))


@router.post("", response_class=HTMLResponse)
def create_loan(
    request: Request,
    form: Annotated[LoanForm, Form()],
    user: CurrentUser,
    session: DbSession,
) -> Response:
    account = accounts.get_account(session, form.account_id)
    error: str | None = None
    if account is None or account.type != AccountType.loan:
        error = "Pick a loan account (create one of type 'loan' first)."
    elif form.term_months < 1:
        error = "Term must be at least one month."
    else:
        try:
            principal_minor = parse_pln(form.principal)
            rate_bp = parse_pln(form.rate_percent)  # "7,25" -> 725 basis points
            initial_base_rate_bp = (
                parse_pln(form.base_rate_percent) if form.base_rate_percent.strip() else None
            )
        except MoneyParseError as exc:
            error = f"Could not read the amounts/rate: {exc}"
        else:
            if principal_minor <= 0:
                error = "Principal must be a positive amount."
            elif form.rate_type is RateType.variable and initial_base_rate_bp is None:
                error = "A variable-rate loan needs an initial base rate (e.g. current WIBOR)."

    if error is not None:
        return templates.TemplateResponse(
            request,
            "loans.html",
            _loans_context(session, user, error=error),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    loan = loan_queries.create_loan(
        session,
        LoanCreate(
            account_id=form.account_id,
            principal=principal_minor,
            rate_type=form.rate_type,
            rate_bp=rate_bp,
            installment_type=form.installment_type,
            start_date=form.start_date,
            term_months=form.term_months,
            base_rate_ref=form.base_rate_ref.strip() or None,
            initial_base_rate_bp=initial_base_rate_bp,
        ),
    )

    return RedirectResponse(f"/dashboard/loans/{loan.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/{loan_id}", response_class=HTMLResponse)
def loan_detail(
    request: Request,
    loan_id: int,
    user: CurrentUser,
    session: DbSession,
    flash: str | None = None,
) -> HTMLResponse:
    loan = loan_queries.get_loan(session, loan_id)
    if loan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"loan {loan_id} not found"
        )

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


@router.post("/{loan_id}/rate-changes")
def add_rate_change(
    loan_id: int,
    form: Annotated[RateChangeForm, Form()],
    session: DbSession,
) -> RedirectResponse:
    loan = loan_queries.get_loan(session, loan_id)
    if loan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"loan {loan_id} not found"
        )
    try:
        base_rate_bp = parse_pln(form.base_rate_percent)
    except MoneyParseError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"invalid base rate: {exc}"
        ) from exc

    loan_queries.add_rate_change(
        session, loan_id=loan_id, effective_date=form.effective_date, base_rate_bp=base_rate_bp
    )

    return RedirectResponse(f"/dashboard/loans/{loan_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{loan_id}/payments/link")
def link_loan_payment(
    loan_id: int,
    form: Annotated[PaymentLinkForm, Form()],
    session: DbSession,
) -> RedirectResponse:
    if not loan_queries.link_payment(
        session, loan_id=loan_id, tx_id=form.tx_id, installment_index=form.installment_index
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="could not link payment")

    return RedirectResponse(f"/dashboard/loans/{loan_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{loan_id}/payments/{tx_id}/unlink")
def unlink_loan_payment(loan_id: int, tx_id: int, session: DbSession) -> RedirectResponse:
    loan_queries.unlink_payment(session, tx_id)

    return RedirectResponse(f"/dashboard/loans/{loan_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{loan_id}/delete")
def delete_loan(loan_id: int, session: DbSession) -> RedirectResponse:
    loan_queries.delete_loan(session, loan_id)

    return RedirectResponse("/dashboard/loans", status_code=status.HTTP_303_SEE_OTHER)
