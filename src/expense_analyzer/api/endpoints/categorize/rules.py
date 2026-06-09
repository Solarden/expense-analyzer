"""Rules page: categorization layer 1 (design §7.7).

A rule is a case-insensitive substring matched against a transaction's merchant
(or, when absent, its raw description) that assigns a category. Rules run
automatically on import and on demand here ("Apply rules now"): they fill
uncategorized rows and refresh rule-set ones, but never overwrite a manual
categorization.

Handlers stay thin — all DB access goes through
:mod:`expense_analyzer.queries.categorize.rules`. Bad input (blank pattern, a missing or
transfer category) becomes a red flash, not a 500.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from expense_analyzer.api.deps import CurrentUser, DbSession
from expense_analyzer.api.forms import RuleForm
from expense_analyzer.auth import require_user
from expense_analyzer.models import Category, CategoryKind, Owner
from expense_analyzer.queries.categorize import categories as category_queries
from expense_analyzer.queries.categorize import rules as rule_queries
from expense_analyzer.templating import templates

router = APIRouter(prefix="/dashboard/rules", tags=["rules"], dependencies=[Depends(require_user)])

# Categories a rule may assign. A transfer category is managed by transfer linking,
# not by rules, so it's excluded from the form.
_ASSIGNABLE_KINDS = (CategoryKind.expense, CategoryKind.income)


def _assignable_categories(session: Session) -> list[Category]:
    return [c for c in category_queries.list_categories(session) if c.kind in _ASSIGNABLE_KINDS]


def _context(session: Session, user: Owner, **extra) -> dict:
    return {
        "user": user,
        "rules": rule_queries.list_rules(session),
        "categories": _assignable_categories(session),
        "category_names": {
            c.id: c.name for c in category_queries.list_categories(session) if c.id is not None
        },
        **extra,
    }


def _applied_flash(applied: int | None) -> str | None:
    """Render the apply-now result carried across the redirect (``?applied=N``).
    ``None`` means no apply happened (a plain page load)."""
    if applied is None:
        return None
    if applied == 0:
        return "Applied rules — nothing to categorize."

    return f"Applied rules — {applied} transaction{'s' if applied != 1 else ''} categorized."


@router.get("", response_class=HTMLResponse)
def rules_page(
    request: Request,
    user: CurrentUser,
    session: DbSession,
    pattern: str = "",
    applied: int | None = None,
) -> HTMLResponse:
    # `pattern` prefills the create form (the "make a rule from this merchant" link
    # on the transactions list); `applied` carries the apply-now count.
    return templates.TemplateResponse(
        request,
        "categorize/rules.html",
        _context(session, user, prefill_pattern=pattern, flash=_applied_flash(applied)),
    )


@router.post("", response_class=HTMLResponse)
def create_rule(
    request: Request, form: Annotated[RuleForm, Form()], user: CurrentUser, session: DbSession
) -> Response:
    pattern = form.pattern.strip()
    category = category_queries.get_category(session, form.category_id)

    error: str | None = None
    if not pattern:
        error = "Rule pattern can't be empty."
    elif category is None or category.kind not in _ASSIGNABLE_KINDS:
        error = "Pick an expense or income category for the rule."

    if error is not None:
        return templates.TemplateResponse(
            request,
            "categorize/rules.html",
            _context(session, user, error=error, prefill_pattern=pattern, flash=None),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    rule_queries.create_rule(
        session, pattern=pattern, category_id=form.category_id, priority=form.priority
    )

    return RedirectResponse("/dashboard/rules", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{rule_id}/delete")
def delete_rule(rule_id: int, session: DbSession) -> RedirectResponse:
    rule_queries.delete_rule(session, rule_id)

    return RedirectResponse("/dashboard/rules", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/apply")
def apply_rules(session: DbSession) -> RedirectResponse:
    changed = rule_queries.apply_rules(session)

    return RedirectResponse(
        f"/dashboard/rules?applied={changed}", status_code=status.HTTP_303_SEE_OTHER
    )
