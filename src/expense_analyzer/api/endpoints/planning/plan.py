"""Plan page: the monthly cashflow checklist (design §11, Phase 19).

Replaces the old Google-Sheet — income at the top, every monthly obligation
below, "FOR LIVING" as the remainder. The user defines each repeating line once
(:class:`~expense_analyzer.models.PlannedItem`); the month view is derived per
``?month=YYYY-MM`` from the active items and their paid status, with no "generate"
step. Phase 19a is expected amounts + a manual paid tick + the FOR LIVING / overdue
read-out; linking real transactions and loan-backed lines arrive in 19b.

Handlers stay thin: all DB access goes through
:mod:`expense_analyzer.queries.planning.planned`. Bad input (blank name, unparseable
amount, out-of-range due day) becomes a red flash, not a 500.
"""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from expense_analyzer.api.deps import CurrentUser, DbSession
from expense_analyzer.api.forms import PlannedItemForm, PlannedLinkForm, TxDirection
from expense_analyzer.auth import require_user
from expense_analyzer.clock import local_month, utc_now
from expense_analyzer.config import get_settings
from expense_analyzer.models import Owner, PlannedItem
from expense_analyzer.money import MoneyParseError, from_minor_units, parse_pln
from expense_analyzer.queries.categorize import categories as category_queries
from expense_analyzer.queries.core import accounts as account_queries
from expense_analyzer.queries.money import stats
from expense_analyzer.queries.planning import loans as loan_queries
from expense_analyzer.queries.planning import planned as planned_queries
from expense_analyzer.templating import templates

router = APIRouter(prefix="/dashboard/plan", tags=["plan"], dependencies=[Depends(require_user)])

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")  # YYYY-MM, month 01-12


def _months(session: Session) -> list[str]:
    """Month-picker values: every month with transactions plus the current local
    month (the plan is useful before any data exists), newest first."""
    current = local_month(utc_now())

    return sorted({current, *stats.available_months(session)}, reverse=True)


def _safe_month(month: str | None) -> str:
    """A valid ``YYYY-MM`` to work with: the requested month if well-formed, else
    the current local month. Guards the derivation (``plan_overview`` parses the
    month for due-date math) against a hand-crafted ``?month=`` that would otherwise
    500 — the picker only ever submits valid values, but a bare URL might not."""
    if month and _MONTH_RE.match(month):
        return month

    return local_month(utc_now())


def _context(
    session: Session, user: Owner, selected_month: str, months: list[str], **extra
) -> dict:
    """Shared context: the month's derived overview, per-item link suggestions, the
    management list and the category options for the define/edit form."""
    all_categories = category_queries.list_categories(session)
    settings = get_settings()
    overview = planned_queries.plan_overview(session, selected_month, viewer_id=user.id)
    suggestions = planned_queries.suggest_links(
        session,
        overview,
        window_days=settings.loan_match_window_days,
        tolerance_pct=settings.loan_match_amount_tolerance_pct,
        viewer_id=user.id,
    )
    return {
        "user": user,
        "months": months,
        "month": selected_month,
        "overview": overview,
        "suggestions": suggestions,
        "items": planned_queries.list_planned_items(session),
        "categories": all_categories,
        "category_names": {c.id: c.name for c in all_categories if c.id is not None},
        "category_colors": {c.id: c.color for c in all_categories if c.id is not None},
        "accounts": {a.id: a.name for a in account_queries.list_accounts(session)},
        "loans": loan_queries.list_loans(session),
        "directions": [d.value for d in TxDirection],
        "for_living_chart": _for_living_chart(session, viewer_id=user.id),
        **extra,
    }


def _for_living_chart(session: Session, *, viewer_id: int) -> dict:
    """Labels + FOR LIVING values (minor units) for the trend chart, last 6 months."""
    trend = planned_queries.for_living_trend(session, months=6, viewer_id=viewer_id)

    return {"labels": [m for m, _ in trend], "data": [v for _, v in trend]}


def _parse_item_form(form: PlannedItemForm) -> tuple[str | None, dict | None]:
    """Validate a submitted item form into create/update kwargs, or an error string.

    Shared by create and edit so both apply the same rules. ``amount`` is a positive
    PLN magnitude that ``direction`` signs (income +, expense −); blank means a
    variable item with no fixed figure (``expected_amount`` stays None)."""
    name = form.name.strip()
    if not name:
        return "Name is required.", None

    expected_amount: int | None = None
    if form.amount.strip():
        try:
            magnitude = parse_pln(form.amount)
        except MoneyParseError as exc:
            return f"Could not read the amount: {exc}", None
        if magnitude <= 0:
            return "Amount must be positive (or leave it blank for a variable item).", None
        expected_amount = magnitude if form.direction is TxDirection.income else -magnitude

    due_day: int | None = None
    if form.due_day.strip():
        if not form.due_day.strip().isdigit() or not 1 <= int(form.due_day) <= 31:
            return "Due day must be a day of the month (1–31), or left blank.", None
        due_day = int(form.due_day)

    category_id = int(form.category_id) if form.category_id.isdigit() else None
    loan_id = int(form.loan_id) if form.loan_id.isdigit() else None

    return None, {
        "name": name,
        "expected_amount": expected_amount,
        "category_id": category_id,
        "loan_id": loan_id,
        "payee_account": form.payee_account.strip() or None,
        "due_day": due_day,
        "note": form.note.strip() or None,
    }


def _item_form_from(item: PlannedItem) -> PlannedItemForm:
    """Build a prefilled form from a stored item (the inverse of :func:`_parse_item_form`)."""
    if item.expected_amount is None:
        amount, direction = "", TxDirection.expense
    elif item.expected_amount > 0:
        amount, direction = str(from_minor_units(item.expected_amount)), TxDirection.income
    else:
        amount, direction = str(from_minor_units(-item.expected_amount)), TxDirection.expense

    return PlannedItemForm(
        name=item.name,
        amount=amount,
        direction=direction,
        category_id=str(item.category_id) if item.category_id is not None else "",
        loan_id=str(item.loan_id) if item.loan_id is not None else "",
        payee_account=item.payee_account or "",
        due_day=str(item.due_day) if item.due_day is not None else "",
        note=item.note or "",
    )


def _redirect(month: str) -> RedirectResponse:
    return RedirectResponse(f"/dashboard/plan?month={month}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("", response_class=HTMLResponse)
def plan_page(
    request: Request,
    user: CurrentUser,
    session: DbSession,
    month: str | None = None,
    edit: str | None = None,
) -> HTMLResponse:
    months = _months(session)
    selected = _safe_month(month)

    # ``?edit=<id>`` prefills the form to change one item. A non-numeric or stale id
    # falls back to the create form (taken as a string so it degrades, not a 422).
    edit_id = int(edit) if edit is not None and edit.isdigit() else None
    edit_item = planned_queries.get_planned_item(session, edit_id) if edit_id is not None else None
    extra: dict = {}
    if edit_item is not None:
        extra = {"edit_item": edit_item, "form": _item_form_from(edit_item)}

    return templates.TemplateResponse(
        request, "planning/plan.html", _context(session, user, selected, months, **extra)
    )


@router.post("", response_class=HTMLResponse)
def create_item(
    request: Request,
    form: Annotated[PlannedItemForm, Form()],
    user: CurrentUser,
    session: DbSession,
    month: str = "",
) -> Response:
    months = _months(session)
    selected = _safe_month(month)

    error, kwargs = _parse_item_form(form)
    if error is not None:
        return templates.TemplateResponse(
            request,
            "planning/plan.html",
            _context(session, user, selected, months, error=error, form=form),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    planned_queries.create_planned_item(session, **kwargs)

    return _redirect(selected)


@router.post("/{item_id}/edit", response_class=HTMLResponse)
def edit_item(
    request: Request,
    item_id: int,
    form: Annotated[PlannedItemForm, Form()],
    user: CurrentUser,
    session: DbSession,
    month: str = "",
) -> Response:
    item = planned_queries.get_planned_item(session, item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"item {item_id} not found"
        )

    months = _months(session)
    selected = _safe_month(month)

    error, kwargs = _parse_item_form(form)
    if error is not None:
        return templates.TemplateResponse(
            request,
            "planning/plan.html",
            _context(session, user, selected, months, error=error, edit_item=item, form=form),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    planned_queries.update_planned_item(session, item_id, **kwargs)

    return _redirect(selected)


@router.post("/{item_id}/delete")
def delete_item(item_id: int, session: DbSession, month: str = Form("")) -> RedirectResponse:
    planned_queries.delete_planned_item(session, item_id)

    return _redirect(_safe_month(month))


@router.post("/{item_id}/toggle-active")
def toggle_active(item_id: int, session: DbSession, month: str = Form("")) -> RedirectResponse:
    item = planned_queries.get_planned_item(session, item_id)
    if item is not None:
        planned_queries.set_active(session, item_id, not item.active)

    return _redirect(_safe_month(month))


@router.post("/{item_id}/move")
def move_item(
    item_id: int, session: DbSession, dir: str = Form("up"), month: str = Form("")
) -> RedirectResponse:
    planned_queries.move_item(session, item_id, up=(dir != "down"))

    return _redirect(_safe_month(month))


@router.post("/{item_id}/mark-paid")
def mark_paid(item_id: int, session: DbSession, month: str = Form("")) -> RedirectResponse:
    safe = _safe_month(month)
    planned_queries.mark_paid(session, planned_item_id=item_id, month=safe)

    return _redirect(safe)


@router.post("/{item_id}/mark-unpaid")
def mark_unpaid(item_id: int, session: DbSession, month: str = Form("")) -> RedirectResponse:
    safe = _safe_month(month)
    planned_queries.mark_unpaid(session, planned_item_id=item_id, month=safe)

    return _redirect(safe)


@router.post("/{item_id}/link")
def link_transaction(
    item_id: int,
    form: Annotated[PlannedLinkForm, Form()],
    user: CurrentUser,
    session: DbSession,
) -> RedirectResponse:
    safe = _safe_month(form.month)
    planned_queries.link_transaction(
        session, planned_item_id=item_id, month=safe, tx_id=form.tx_id, viewer_id=user.id
    )

    return _redirect(safe)


@router.post("/{item_id}/unlink")
def unlink_transaction(item_id: int, session: DbSession, month: str = Form("")) -> RedirectResponse:
    safe = _safe_month(month)
    planned_queries.unlink_transaction(session, planned_item_id=item_id, month=safe)

    return _redirect(safe)
