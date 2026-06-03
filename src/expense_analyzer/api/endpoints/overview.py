"""Overview page: monthly spending/income summary and trend charts (design §8).

Charts use Chart.js served from vendored static assets so the Pi stays offline.
Amounts stay integer minor units; the template divides by 100 for display so
money never round-trips as a float.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from expense_analyzer.api.deps import CurrentUser, DbSession
from expense_analyzer.auth import require_user
from expense_analyzer.queries import categories, stats
from expense_analyzer.templating import templates

# Months of history on the overview trend chart.
TREND_MONTHS = 12

router = APIRouter(prefix="/dashboard", tags=["overview"], dependencies=[Depends(require_user)])


@router.get("/stats", response_class=HTMLResponse)
def stats_page(
    request: Request,
    user: CurrentUser,
    session: DbSession,
    month: str | None = None,
) -> HTMLResponse:
    months = stats.available_months(session)
    selected = stats.default_month(months, month)

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
