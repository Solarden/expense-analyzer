"""Budgets page: per-category monthly limits and how spending tracks against them
(design §7.6).

A recurring default applies every month; a ``"YYYY-MM"`` override replaces it for
one month. The overview compares each effective limit to the month's actual
spending (transfers and loan installments already excluded, via the stats layer).

Handlers stay thin — all DB access goes through
:mod:`expense_analyzer.queries.planning.budgets`. Bad input (unparseable amount, malformed
month, non-expense category) becomes a red flash, not a 500.
"""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from expense_analyzer.api.deps import CurrentUser, DbSession
from expense_analyzer.api.forms import BudgetForm
from expense_analyzer.auth import require_user
from expense_analyzer.models import CategoryKind, Owner
from expense_analyzer.money import MoneyParseError, from_minor_units, parse_pln
from expense_analyzer.queries.categorize import categories as category_queries
from expense_analyzer.queries.money import stats
from expense_analyzer.queries.planning import budgets as budget_queries
from expense_analyzer.templating import templates

router = APIRouter(
    prefix="/dashboard/budgets", tags=["budgets"], dependencies=[Depends(require_user)]
)

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")  # YYYY-MM, month 01-12


def _context(
    session: Session, user: Owner, selected_month: str, months: list[str], **extra
) -> dict:
    """Shared context: the month's budget overview, the set form, and the defined
    budgets list (recurring defaults + overrides). ``months`` is fetched once by
    the handler and threaded in (it also resolves ``selected_month``)."""
    all_categories = category_queries.list_categories(session)
    return {
        "user": user,
        "months": months,
        "month": selected_month,
        "statuses": budget_queries.budget_overview(session, selected_month, viewer_id=user.id),
        "budgets": budget_queries.list_budgets(session),
        "categories": budget_queries.budgetable_categories(session),
        "category_names": {c.id: c.name for c in all_categories if c.id is not None},
        "category_colors": {c.id: c.color for c in all_categories if c.id is not None},
        **extra,
    }


@router.get("", response_class=HTMLResponse)
def budgets_page(
    request: Request,
    user: CurrentUser,
    session: DbSession,
    month: str | None = None,
    edit: str | None = None,
) -> HTMLResponse:
    months = stats.available_months(session, viewer_id=user.id)
    selected = stats.default_month(months, month)

    # ``?edit=<id>`` prefills the form to change one budget's limit. Category and
    # month are the budget's identity (the upsert key), so they're shown read-only
    # — only the limit is editable, and the existing set_budget upsert hits the
    # same row. A non-numeric or stale id just falls back to the create form (taken
    # as a string so a malformed ``?edit=`` degrades gracefully, not a 422).
    edit_id = int(edit) if edit is not None and edit.isdigit() else None
    edit_budget = budget_queries.get_budget(session, edit_id) if edit_id is not None else None
    extra: dict = {}
    if edit_budget is not None:
        extra = {
            "edit_budget": edit_budget,
            "edit_limit": str(from_minor_units(edit_budget.limit_amount)),
        }

    return templates.TemplateResponse(
        request, "planning/budgets.html", _context(session, user, selected, months, **extra)
    )


@router.post("", response_class=HTMLResponse)
def set_budget(
    request: Request,
    form: Annotated[BudgetForm, Form()],
    user: CurrentUser,
    session: DbSession,
) -> Response:
    months = stats.available_months(session, viewer_id=user.id)
    month = form.month.strip() or None
    selected = stats.default_month(months, month)

    category = category_queries.get_category(session, form.category_id)
    error: str | None = None
    if category is None or category.kind != CategoryKind.expense:
        error = "Pick an expense category to budget."
    elif month is not None and not _MONTH_RE.match(month):
        error = "Month must be in YYYY-MM format (e.g. 2026-06)."
    else:
        try:
            limit_minor = parse_pln(form.limit_amount)
        except MoneyParseError as exc:
            error = f"Could not read the limit amount: {exc}"
        else:
            if limit_minor <= 0:
                error = "Budget limit must be a positive amount."

    if error is not None:
        return templates.TemplateResponse(
            request,
            "planning/budgets.html",
            _context(session, user, selected, months, error=error),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    budget_queries.set_budget(
        session, category_id=form.category_id, month=month, limit_amount=limit_minor
    )
    target = f"/dashboard/budgets?month={selected}" if selected else "/dashboard/budgets"

    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{budget_id}/delete")
def delete_budget(budget_id: int, session: DbSession) -> RedirectResponse:
    budget_queries.delete_budget(session, budget_id)

    return RedirectResponse("/dashboard/budgets", status_code=status.HTTP_303_SEE_OTHER)
