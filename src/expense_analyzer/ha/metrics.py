"""Glanceable household metrics for the Home Assistant push.

The data layer of the HA integration: gather a flat list of :class:`Metric` from
the existing query modules (net worth, monthly stats). Money is converted from
integer minor units to a display :class:`~decimal.Decimal` string **only here, at
the MQTT edge** (the same discipline as ``format_pln`` on the web edge) — the rest
of the app keeps integer minor units.

Each :class:`Metric` maps 1:1 to one Home Assistant sensor.
"""

from dataclasses import dataclass

from sqlmodel import Session, col, select

from expense_analyzer.clock import local_today
from expense_analyzer.config import get_settings
from expense_analyzer.models import Owner, Transaction
from expense_analyzer.money import from_minor_units
from expense_analyzer.queries.money import stats as stats_queries
from expense_analyzer.queries.planning import budgets as budget_queries
from expense_analyzer.queries.planning import planned as planned_queries
from expense_analyzer.queries.planning import subscriptions as subscription_queries
from expense_analyzer.queries.wealth import net_worth as net_worth_queries


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


def _headline_metrics(
    session: Session,
    spendable: list[Transaction],
    month: str,
    *,
    viewer_id: int | None,
) -> list[Metric]:
    """Net worth and the three month figures — the four every dashboard leads with,
    and the only ones published per member (see :func:`collect_member_metrics`)."""
    summary = stats_queries.month_summary(spendable, month, {})

    return [
        Metric(
            "net_worth",
            "Net Worth",
            _pln(net_worth_queries.current_net_worth(session, viewer_id=viewer_id)),
        ),
        Metric("month_spending", "Spending This Month", _pln(summary.spending)),
        Metric("month_income", "Income This Month", _pln(summary.income)),
        Metric("month_net", "Net This Month", _pln(summary.net)),
    ]


def collect_metrics(session: Session, *, viewer_id: int | None = None) -> list[Metric]:
    """One snapshot as a flat list of HA sensor metrics, through one viewer's eyes.

    Headline figures (net worth, this-month spending/income/net — transfers and
    loan installments excluded, as everywhere in
    :mod:`~expense_analyzer.queries.money.stats`), the total fixed monthly cost of
    detected subscriptions, one balance metric per account, and a "budget
    remaining" metric per budgeted category for the current month. HA turns the
    remaining sensors into glanceable "left in food budget" cards and can drive
    its own threshold automations off them.

    ``viewer_id=None`` is the shared home budget — the household figures every
    member's dashboard shows. A member's id adds their private rows on top, which is
    what :func:`collect_member_metrics` publishes alongside it.
    """
    today = local_today()
    month = today.strftime("%Y-%m")
    spendable = stats_queries.spendable_transactions(session, viewer_id=viewer_id)
    metrics = _headline_metrics(session, spendable, month, viewer_id=viewer_id)

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
        for balance in net_worth_queries.account_balances(session, viewer_id=viewer_id)
    ]

    # Reuse the spendable scan already loaded above instead of re-querying.
    metrics += [
        Metric(
            key=f"budget_{status.category_id}_remaining",
            name=f"{status.name} Budget Remaining",
            value=_pln(status.remaining),
        )
        for status in budget_queries.budget_overview(
            session, month, viewer_id=viewer_id, spendable=spendable
        )
    ]

    # Monthly cashflow checklist: the remainder after all obligations
    # ("FOR LIVING") and what's still unpaid this month. The paid X/Y progress and
    # overdue count ride on a separate plan sensor (see ``publish_plan``).
    plan = planned_queries.plan_overview(session, month, today=today, viewer_id=viewer_id)
    metrics += [
        Metric("plan_for_living", "For Living This Month", _pln(plan.for_living)),
        Metric("plan_left_to_pay", "Left To Pay This Month", _pln(plan.left_to_pay)),
    ]

    return metrics


def collect_member_metrics(session: Session, *, only_id: int | None = None) -> list[Metric]:
    """Per-member headline figures — each member's own rows plus the household's.

    ``only_id`` limits the result to one member, which is what any surface inside the
    app must pass. These figures include private rows by construction, so the full set
    may go to the broker (HA's own audience is the household) but must never be
    rendered back into the web UI, where the boundary is real.

    "My finances and the house's" is the question a member's dashboard answers, and
    that is the headline four through their own eyes. These publish *alongside* the
    unprefixed household sensors rather than replacing them, so a dashboard built on
    the household figures keeps working and the two can sit side by side.

    Only the headline four, deliberately: per-account and per-budget sensors would
    multiply by the number of members for figures that are already shared, and a
    member's own account has exactly one reader. Widen it when someone asks.

    **Home Assistant cannot keep one member's sensor from another.** Card visibility
    hides a card, not an entity, and HA has no per-entity permission to set — so
    these are a convenience filter, not the boundary. The boundary is the web UI,
    where :mod:`expense_analyzer.queries.visibility` enforces it.
    """
    # ponytail: a departed member's retained discovery config is never retracted, so HA
    # keeps their entity frozen on its last value. Fix by publishing an empty payload.
    month = local_today().strftime("%Y-%m")
    metrics: list[Metric] = []

    query = select(Owner).where(col(Owner.is_active).is_(True))

    if only_id is not None:
        query = query.where(Owner.id == only_id)

    for member in session.exec(query).all():
        if member.id is None:
            continue

        spendable = stats_queries.spendable_transactions(session, viewer_id=member.id)
        metrics += [
            # Keyed by id, like account_{id}_balance: a rename must not orphan the
            # entity HA has already registered.
            Metric(f"owner_{member.id}_{m.key}", f"{m.name} ({member.name})", m.value)
            for m in _headline_metrics(session, spendable, month, viewer_id=member.id)
        ]

    return metrics
