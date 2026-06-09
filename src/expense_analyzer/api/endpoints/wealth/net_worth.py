"""Net worth page (design §7.3, §9): assets minus debt across all accounts.

A read-only summary — one headline number plus a per-account breakdown. The math
lives in :mod:`expense_analyzer.queries.wealth.net_worth`; this handler just renders it.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from expense_analyzer.api.deps import CurrentUser, DbSession
from expense_analyzer.auth import require_user
from expense_analyzer.models import AccountType
from expense_analyzer.queries.wealth import net_worth
from expense_analyzer.templating import templates

router = APIRouter(
    prefix="/dashboard/net-worth", tags=["net-worth"], dependencies=[Depends(require_user)]
)


@router.get("", response_class=HTMLResponse)
def net_worth_page(request: Request, user: CurrentUser, session: DbSession) -> HTMLResponse:
    balances = net_worth.account_balances(session)

    # A single chart axis lets a large mortgage squash every asset bar to a hair,
    # so split the figures: headline cards plus two independently-scaled charts.
    # One pass derives every total and both chart series.
    total = assets = liabilities = net_worth_excl_loans = 0
    assets_chart: dict[str, list] = {"labels": [], "data": []}
    liabilities_chart: dict[str, list] = {"labels": [], "data": []}
    for b in balances:
        total += b.balance
        # "Net worth without the mortgage" — the number the household actually
        # steers by month to month. Excludes every loan account, not just the
        # largest.
        if b.type != AccountType.loan:
            net_worth_excl_loans += b.balance
        if b.balance > 0:
            assets += b.balance
            assets_chart["labels"].append(b.name)
            assets_chart["data"].append(b.balance)
        elif b.balance < 0:
            liabilities += b.balance
            liabilities_chart["labels"].append(b.name)
            # Signed (negative) so the chart agrees with the headline card: bars
            # grow left from zero and tooltips show the same minus.
            liabilities_chart["data"].append(b.balance)

    return templates.TemplateResponse(
        request,
        "wealth/net_worth.html",
        {
            "user": user,
            "balances": balances,
            "total": total,
            "assets": assets,
            "liabilities": liabilities,
            "net_worth_excl_loans": net_worth_excl_loans,
            # Two charts on their own scales (built in the loop above).
            "assets_chart": assets_chart,
            "liabilities_chart": liabilities_chart,
        },
    )
