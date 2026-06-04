"""Subscription queries — detect recurring payments and persist the user verdict.

The DB side of Phase 9 (design §7.5). Detection itself is pure and stateless
(:mod:`expense_analyzer.subscriptions`); this layer feeds it the right
transactions (transfer- and loan-excluded, via :mod:`expense_analyzer.queries.stats`)
and overlays the persisted confirm/dismiss verdict stored per merchant in the
``subscription`` table.

:func:`subscription_overview` is the one entry point the page and the HA collector
share: it returns every detected subscription paired with its verdict
(``None`` == an un-acted-on suggestion). :func:`set_verdict` upserts so the app —
the single writer — never duplicates a merchant's verdict.
"""

from dataclasses import dataclass
from datetime import date

from sqlmodel import Session, col, select

from expense_analyzer.config import Settings
from expense_analyzer.models import Subscription, SubscriptionStatus, Transaction
from expense_analyzer.queries import stats
from expense_analyzer.subscriptions import DetectedSubscription, find_subscriptions


@dataclass(frozen=True)
class SubscriptionView:
    """A detected subscription paired with the user's stored verdict (if any)."""

    detected: DetectedSubscription
    verdict: SubscriptionStatus | None  # None == an un-acted-on suggestion

    @property
    def is_dismissed(self) -> bool:
        return self.verdict == SubscriptionStatus.dismissed

    @property
    def is_confirmed(self) -> bool:
        return self.verdict == SubscriptionStatus.confirmed


def detect(
    session: Session,
    settings: Settings,
    *,
    today: date,
    spendable: list[Transaction] | None = None,
) -> list[DetectedSubscription]:
    """Run live detection over the spendable (transfer/loan-excluded) outflows.

    Pass ``spendable`` (a preloaded :func:`stats.spendable_transactions`) to reuse
    a scan the caller already did — e.g. the HA metrics collector, which needs the
    month figures off the same list. Omit it for a self-contained single scan.
    """
    if spendable is None:
        spendable = stats.spendable_transactions(session)

    return find_subscriptions(
        spendable,
        today=today,
        min_occurrences=settings.subscription_min_occurrences,
        amount_tolerance_pct=settings.subscription_amount_tolerance_pct,
        price_rise_pct=settings.subscription_price_rise_pct,
        new_window_days=settings.subscription_new_window_days,
    )


def list_verdicts(session: Session) -> dict[str, SubscriptionStatus]:
    """Persisted verdicts keyed by merchant."""
    return {s.merchant: s.status for s in session.exec(select(Subscription)).all()}


def subscription_overview(
    session: Session,
    settings: Settings,
    *,
    today: date,
    spendable: list[Transaction] | None = None,
) -> list[SubscriptionView]:
    """Every detected subscription paired with its stored verdict, largest
    monthly cost first (inherited from detection order).

    ``spendable`` is threaded through to :func:`detect` so a caller that already
    loaded the spendable scan (the HA collector) need not repeat it."""
    verdicts = list_verdicts(session)

    return [
        SubscriptionView(d, verdicts.get(d.merchant))
        for d in detect(session, settings, today=today, spendable=spendable)
    ]


def active_monthly_cost(views: list[SubscriptionView]) -> int:
    """Total monthly-equivalent cost of the subscriptions that count toward
    "fixed monthly costs": everything except the dismissed false positives
    (minor units)."""
    return sum(v.detected.monthly_equivalent for v in views if not v.is_dismissed)


def set_verdict(session: Session, *, merchant: str, status: SubscriptionStatus) -> Subscription:
    """Create or update the verdict for ``merchant`` (upsert — one row per merchant)."""
    subscription = session.exec(
        select(Subscription).where(Subscription.merchant == merchant)
    ).first()

    if subscription is None:
        subscription = Subscription(merchant=merchant, status=status)
    else:
        subscription.status = status
    session.add(subscription)
    session.commit()
    session.refresh(subscription)

    return subscription


def clear_verdict(session: Session, merchant: str) -> bool:
    """Drop a merchant's verdict, returning it to an un-acted-on suggestion.

    Returns False if there was nothing stored. A hard delete: the verdict is
    config (like a budget), not a financial record.
    """
    subscription = session.exec(
        select(Subscription).where(col(Subscription.merchant) == merchant)
    ).first()
    if subscription is None:
        return False

    session.delete(subscription)
    session.commit()

    return True
