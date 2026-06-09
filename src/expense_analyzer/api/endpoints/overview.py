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
from expense_analyzer.queries.transactions import UNCATEGORIZED
from expense_analyzer.templating import templates

# Months of history on the overview trend chart.
TREND_MONTHS = 12

# Neutral bar colour for the uncategorized bucket (no category id to key a palette
# slot on) — keeps it readable rather than the old all-red series.
DEFAULT_CATEGORY_COLOR = "#4f8cff"

# A small qualitative palette so categories *without* an explicit colour still get
# distinct bars instead of all sharing one hue (Phase 20b auto-palette). Keyed by
# category id (modulo), so a category keeps the same auto-colour across months.
AUTO_PALETTE = (
    "#4f8cff",
    "#3fb950",
    "#d29922",
    "#a371f7",
    "#f0686b",
    "#39c5cf",
    "#db61a2",
    "#e3b341",
)

router = APIRouter(prefix="/dashboard", tags=["overview"], dependencies=[Depends(require_user)])


def _bar_color(category_id: int | None, explicit: str | None) -> str:
    """Colour for one category bar: its own colour if set, else a stable palette
    slot keyed by id (auto-palette); the uncategorized bucket gets the neutral default."""
    if explicit:
        return explicit
    if category_id is None:
        return DEFAULT_CATEGORY_COLOR

    return AUTO_PALETTE[category_id % len(AUTO_PALETTE)]


def _drilldown_link(category_id: int | None, month: str) -> str:
    """Overview bar → the filtered transactions list for that category + month."""
    category = UNCATEGORIZED if category_id is None else category_id

    return f"/dashboard/transactions?category={category}&month={month}"


@router.get("/stats", response_class=HTMLResponse)
def stats_page(
    request: Request,
    user: CurrentUser,
    session: DbSession,
    month: str | None = None,
) -> HTMLResponse:
    months = stats.available_months(session)
    selected = stats.default_month(months, month)

    category_list = categories.list_categories(session)
    category_names = {c.id: c.name for c in category_list if c.id is not None}
    category_colors = {c.id: c.color for c in category_list if c.id is not None}
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
                # Per-bar colours: each category's own colour, else an auto-palette
                # slot (colourless categories no longer share one hue).
                "colors": [
                    _bar_color(c.category_id, category_colors.get(c.category_id))
                    for c in summary.by_category
                ],
                # Click a bar to drill into that category's transactions for the month.
                "links": [_drilldown_link(c.category_id, selected) for c in summary.by_category],
            },
            "trend_chart": {
                "labels": [m.month for m in trend],
                "spending": [m.spending for m in trend],
                "income": [m.income for m in trend],
            },
        },
    )
