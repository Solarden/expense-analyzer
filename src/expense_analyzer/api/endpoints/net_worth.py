"""Net worth page (design §7.3, §9): assets minus debt across all accounts.

A read-only summary — one headline number plus a per-account breakdown. The math
lives in :mod:`expense_analyzer.queries.net_worth`; this handler just renders it.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from expense_analyzer.api.deps import CurrentUser, DbSession
from expense_analyzer.auth import require_user
from expense_analyzer.queries import net_worth
from expense_analyzer.templating import templates

router = APIRouter(
    prefix="/dashboard/net-worth", tags=["net-worth"], dependencies=[Depends(require_user)]
)


@router.get("", response_class=HTMLResponse)
def net_worth_page(request: Request, user: CurrentUser, session: DbSession) -> HTMLResponse:
    balances = net_worth.account_balances(session)
    total = sum(b.balance for b in balances)

    return templates.TemplateResponse(
        request,
        "net_worth.html",
        {
            "user": user,
            "balances": balances,
            "total": total,
            # Assets (positive) vs debt (negative) for a simple chart.
            "breakdown_chart": {
                "labels": [b.name for b in balances],
                "data": [b.balance for b in balances],
            },
        },
    )
