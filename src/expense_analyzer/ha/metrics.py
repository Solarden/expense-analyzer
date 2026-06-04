"""Glanceable household metrics for the Home Assistant push (design §9).

The data layer of the HA integration: gather a flat list of :class:`Metric` from
the existing query modules (net worth, monthly stats). Money is converted from
integer minor units to a display :class:`~decimal.Decimal` string **only here, at
the MQTT edge** (the same discipline as ``format_pln`` on the web edge) — the rest
of the app keeps integer minor units (design §5).

Each :class:`Metric` maps 1:1 to one Home Assistant sensor.
"""

from dataclasses import dataclass

from sqlmodel import Session

from expense_analyzer.clock import local_today
from expense_analyzer.config import get_settings
from expense_analyzer.money import from_minor_units
from expense_analyzer.queries import budgets as budget_queries
from expense_analyzer.queries import net_worth as net_worth_queries
from expense_analyzer.queries import stats as stats_queries
from expense_analyzer.queries import subscriptions as subscription_queries


@dataclass(frozen=True)
class Metric:
    """One Home Assistant sensor's worth of state.

    ``key`` is a stable slug, unique within the device (it becomes the sensor's
    ``object_id`` and the state-JSON field). ``value`` is already a display
    decimal string in PLN (e.g. ``"-1234.56"``) — never a float.
    """

    key: str
    name: str
    value: str


def _pln(minor: int) -> str:
    """Integer minor units -> a plain decimal PLN string for an HA monetary sensor.

    Exact (via :class:`~decimal.Decimal`, never float) and HA-friendly:
    ``-1234.56`` not ``"-1234,56 zł"`` — HA wants a bare number it can cast.
    """
    return str(from_minor_units(minor))


def collect_metrics(session: Session) -> list[Metric]:
    """The current household snapshot as a flat list of HA sensor metrics.

    Headline figures (net worth, this-month spending/income/net — transfers and
    loan installments excluded, as everywhere in
    :mod:`~expense_analyzer.queries.stats`), the total fixed monthly cost of
    detected subscriptions (Phase 9), one balance metric per account, and a
    "budget remaining" metric per budgeted category for the current month (Phase
    8). HA turns the remaining sensors into glanceable "left in food budget" cards
    and can drive its own threshold automations off them (design §9).
    """
    today = local_today()
    metrics = [
        Metric("net_worth", "Net Worth", _pln(net_worth_queries.current_net_worth(session))),
    ]

    month = today.strftime("%Y-%m")
    spendable = stats_queries.spendable_transactions(session)
    summary = stats_queries.month_summary(spendable, month, {})
    metrics += [
        Metric("month_spending", "Spending This Month", _pln(summary.spending)),
        Metric("month_income", "Income This Month", _pln(summary.income)),
        Metric("month_net", "Net This Month", _pln(summary.net)),
    ]

    # Fixed monthly cost of detected subscriptions (dismissed false positives
    # excluded). Derived live from history — reuse the spendable scan above
    # instead of re-querying (see queries/subscriptions).
    views = subscription_queries.subscription_overview(
        session, get_settings(), today=today, spendable=spendable
    )
    metrics.append(
        Metric(
            "fixed_monthly_costs",
            "Fixed Monthly Costs",
            _pln(subscription_queries.active_monthly_cost(views)),
        )
    )

    metrics += [
        Metric(
            key=f"account_{balance.account_id}_balance",
            name=f"{balance.name} Balance",
            value=_pln(balance.balance),
        )
        for balance in net_worth_queries.account_balances(session)
    ]

    # Reuse the spendable scan already loaded above instead of re-querying.
    metrics += [
        Metric(
            key=f"budget_{status.category_id}_remaining",
            name=f"{status.name} Budget Remaining",
            value=_pln(status.remaining),
        )
        for status in budget_queries.budget_overview(session, month, spendable=spendable)
    ]

    return metrics
