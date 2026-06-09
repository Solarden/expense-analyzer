"""Subscriptions page: recurring costs at a glance (design §7.5).

Recurring payments are detected live from transaction history (merchant + date /
amount regularity) — nothing is stored except the user's verdict over a detected
group. GET is read-only: it recomputes detection every load, then overlays the
persisted confirm/dismiss verdict. Confirm acknowledges a real subscription
(silences its "new" alert); dismiss hides a false positive and drops it from the
"fixed monthly costs" total; restore returns it to a suggestion.

Handlers stay thin — all DB access goes through
:mod:`expense_analyzer.queries.subscriptions`.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from expense_analyzer.api.deps import CurrentUser, DbSession
from expense_analyzer.api.forms import SubscriptionVerdictForm
from expense_analyzer.auth import require_user
from expense_analyzer.clock import local_today
from expense_analyzer.config import get_settings
from expense_analyzer.models import SubscriptionStatus
from expense_analyzer.queries import categories as category_queries
from expense_analyzer.queries import subscriptions as subscription_queries
from expense_analyzer.templating import templates

router = APIRouter(
    prefix="/dashboard/subscriptions", tags=["subscriptions"], dependencies=[Depends(require_user)]
)


@router.get("", response_class=HTMLResponse)
def subscriptions_page(request: Request, user: CurrentUser, session: DbSession) -> HTMLResponse:
    views = subscription_queries.subscription_overview(session, get_settings(), today=local_today())
    all_categories = category_queries.list_categories(session)

    return templates.TemplateResponse(
        request,
        "subscriptions.html",
        {
            "user": user,
            "suggestions": [v for v in views if v.verdict is None],
            "confirmed": [v for v in views if v.is_confirmed],
            "dismissed": [v for v in views if v.is_dismissed],
            "monthly_total": subscription_queries.active_monthly_cost(views),
            "category_names": {c.id: c.name for c in all_categories if c.id is not None},
            "category_colors": {c.id: c.color for c in all_categories if c.id is not None},
        },
    )


def _set(session: DbSession, merchant: str, status_: SubscriptionStatus | None) -> RedirectResponse:
    if status_ is None:
        subscription_queries.clear_verdict(session, merchant)
    else:
        subscription_queries.set_verdict(session, merchant=merchant, status=status_)

    return RedirectResponse("/dashboard/subscriptions", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/confirm")
def confirm_subscription(
    form: Annotated[SubscriptionVerdictForm, Form()], session: DbSession
) -> RedirectResponse:
    return _set(session, form.merchant, SubscriptionStatus.confirmed)


@router.post("/dismiss")
def dismiss_subscription(
    form: Annotated[SubscriptionVerdictForm, Form()], session: DbSession
) -> RedirectResponse:
    return _set(session, form.merchant, SubscriptionStatus.dismissed)


@router.post("/restore")
def restore_subscription(
    form: Annotated[SubscriptionVerdictForm, Form()], session: DbSession
) -> RedirectResponse:
    return _set(session, form.merchant, None)
