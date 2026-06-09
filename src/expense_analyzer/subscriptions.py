"""Recurring-payment (subscription) detection — the logic, kept pure (no DB).

A subscription is **derived** from transaction history, not stored (design §7.5):
group a merchant's outflows and look for *regularity of date and amount*. A
streaming service, rent, or insurance charges a similar amount on a similar
cadence; a supermarket charges the same merchant at random amounts and dates and
must not be mistaken for one. So a group qualifies only when both its **interval**
(consistent cadence) and its **amount** (clustered around a typical value) are
regular — same merchant alone is not enough.

This module finds the candidates; persisting the user's confirm/dismiss verdict
over a group lives in :mod:`expense_analyzer.queries.planning.subscriptions`. The input is
expected to be already transfer- and loan-excluded (see
:func:`expense_analyzer.queries.money.stats.spendable_transactions`) — those are
recurring too but are not consumption subscriptions.

Money stays integer minor units throughout (never float; design §5); the
``monthly_equivalent`` cost is scaled with an exact integer ratio so a yearly
insurance and a monthly stream are comparable "fixed monthly cost" figures.
"""

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from itertools import pairwise

from expense_analyzer.models import Transaction


class Cadence(StrEnum):
    """How often a subscription charges."""

    weekly = "weekly"
    monthly = "monthly"
    quarterly = "quarterly"
    yearly = "yearly"


# Canonical period of each cadence in days.
_CADENCE_DAYS: dict[Cadence, int] = {
    Cadence.weekly: 7,
    Cadence.monthly: 30,
    Cadence.quarterly: 91,
    Cadence.yearly: 365,
}

# Each cadence's share of a month as an exact integer ratio (num/den), so the
# "monthly equivalent" cost is computed without ever touching a float.
_MONTHLY_RATIO: dict[Cadence, tuple[int, int]] = {
    Cadence.weekly: (52, 12),  # 52 weekly charges across 12 months
    Cadence.monthly: (1, 1),
    Cadence.quarterly: (1, 3),
    Cadence.yearly: (1, 12),
}


class DetectionStatus(StrEnum):
    """Where a detected subscription sits relative to today.

    This is the *detection* status (is it still charging?), distinct from the
    user's confirm/dismiss verdict stored in the ``subscription`` table.
    """

    active = "active"  # the next charge isn't overdue yet
    overdue = "overdue"  # past due but within ~one extra period (maybe just late)
    ended = "ended"  # no charge for more than two periods — probably cancelled


# A series counts as regular when at least this fraction of its consecutive gaps
# sit near the cadence period AND at least this fraction of its charges cluster
# around the typical amount. 0.6 tolerates the odd missed / doubled / early
# charge without admitting genuinely irregular same-merchant spending.
_REGULARITY_FRACTION = 0.6

# How far an interval may stray from a canonical cadence period (as a fraction of
# the period) and still count as "on cadence". 0.4 keeps the four cadences well
# separated: monthly 18–42d, weekly 4–10d, quarterly 55–127d, yearly 219–511d.
_CADENCE_TOLERANCE = 0.4


@dataclass(frozen=True, slots=True)
class PriceRise:
    """A detected jump in the latest charge over the established price."""

    old_amount: int  # minor units, the established (pre-rise) price
    new_amount: int  # minor units, the latest charge
    increase_pct: int  # how much bigger the latest charge is, percent


@dataclass(frozen=True, slots=True)
class DetectedSubscription:
    """One recurring payment derived from a merchant's charge history.

    Amounts are positive magnitudes in minor units (the charges are outflows).
    ``current_amount`` is the latest charge, ``typical_amount`` the median, and
    ``monthly_equivalent`` normalizes the cadence to a per-month cost so a yearly
    and a monthly subscription are directly comparable.
    """

    merchant: str
    cadence: Cadence
    occurrences: int
    current_amount: int
    typical_amount: int
    monthly_equivalent: int
    first_date: date
    last_date: date
    next_expected: date
    status: DetectionStatus
    is_new: bool  # first charge is within the "recently started" window
    price_rise: PriceRise | None
    category_id: int | None  # the group's dominant category, for display


def find_subscriptions(
    transactions: list[Transaction],
    *,
    today: date,
    min_occurrences: int,
    amount_tolerance_pct: int,
    price_rise_pct: int,
    new_window_days: int,
) -> list[DetectedSubscription]:
    """Detect recurring payments among ``transactions``, largest monthly cost first.

    Only outflows with a known ``merchant_normalized`` are considered (the
    grouping key). ``transactions`` should already exclude transfers and loan
    installments (they recur too, but aren't subscriptions).
    """
    groups: dict[str, list[Transaction]] = defaultdict(list)
    for tx in transactions:
        if tx.amount < 0 and tx.merchant_normalized:
            groups[tx.merchant_normalized].append(tx)

    subscriptions = [
        sub
        for merchant, txns in groups.items()
        if (
            sub := _detect_one(
                merchant,
                txns,
                today=today,
                min_occurrences=min_occurrences,
                amount_tolerance_pct=amount_tolerance_pct,
                price_rise_pct=price_rise_pct,
                new_window_days=new_window_days,
            )
        )
        is not None
    ]
    subscriptions.sort(key=lambda s: s.monthly_equivalent, reverse=True)

    return subscriptions


def _detect_one(
    merchant: str,
    txns: list[Transaction],
    *,
    today: date,
    min_occurrences: int,
    amount_tolerance_pct: int,
    price_rise_pct: int,
    new_window_days: int,
) -> DetectedSubscription | None:
    if len(txns) < min_occurrences:
        return None

    ordered = sorted(txns, key=lambda t: t.booked_date)
    dates = [t.booked_date for t in ordered]
    magnitudes = [-t.amount for t in ordered]  # outflows -> positive magnitudes

    gaps = [(b - a).days for a, b in pairwise(dates)]
    cadence = _classify_cadence(statistics.median(gaps))
    if cadence is None:
        return None
    period = _CADENCE_DAYS[cadence]

    if not _is_regular(gaps, near=period, tolerance=period * _CADENCE_TOLERANCE):
        return None

    typical = round(statistics.median(magnitudes))
    if typical <= 0 or not _is_regular(
        magnitudes, near=typical, tolerance=typical * amount_tolerance_pct / 100
    ):
        return None

    next_expected = dates[-1] + timedelta(days=period)

    return DetectedSubscription(
        merchant=merchant,
        cadence=cadence,
        occurrences=len(ordered),
        current_amount=magnitudes[-1],
        typical_amount=typical,
        monthly_equivalent=_scale(magnitudes[-1], *_MONTHLY_RATIO[cadence]),
        first_date=dates[0],
        last_date=dates[-1],
        next_expected=next_expected,
        status=_status(today, dates[-1], next_expected, period),
        is_new=dates[0] >= today - timedelta(days=new_window_days),
        price_rise=_detect_price_rise(magnitudes, price_rise_pct),
        category_id=_dominant_category(ordered),
    )


def _classify_cadence(median_gap: float) -> Cadence | None:
    """The cadence whose period the median interval is closest to, or ``None`` if
    the interval is too far from any canonical cadence."""
    best: Cadence | None = None
    best_error = _CADENCE_TOLERANCE
    for cadence, period in _CADENCE_DAYS.items():
        error = abs(median_gap - period) / period
        if error <= best_error:
            best_error = error
            best = cadence

    return best


def _is_regular(values: list[float], *, near: float, tolerance: float) -> bool:
    """True when at least :data:`_REGULARITY_FRACTION` of ``values`` fall within
    ``tolerance`` of ``near`` — the shared regularity test for both the date gaps
    and the charge amounts."""
    within = sum(1 for v in values if abs(v - near) <= tolerance)

    return within >= len(values) * _REGULARITY_FRACTION


def _detect_price_rise(magnitudes: list[int], threshold_pct: int) -> PriceRise | None:
    """Flag a rise when the latest charge exceeds the established price (the median
    of the earlier charges) by more than ``threshold_pct``.

    Comparing against the median of the *prior* charges catches "it quietly went
    up" — one recent increase among many stable charges — without firing on the
    normal small drift the amount-regularity test already tolerates.
    """
    if len(magnitudes) < 2:
        return None

    baseline = round(statistics.median(magnitudes[:-1]))
    current = magnitudes[-1]
    if baseline <= 0 or (current - baseline) * 100 <= baseline * threshold_pct:
        return None

    return PriceRise(
        old_amount=baseline,
        new_amount=current,
        increase_pct=round((current - baseline) * 100 / baseline),
    )


def _status(today: date, last_date: date, next_expected: date, period: int) -> DetectionStatus:
    grace = timedelta(days=round(period * _CADENCE_TOLERANCE))
    if today <= next_expected + grace:
        return DetectionStatus.active
    if today <= last_date + timedelta(days=2 * period):
        return DetectionStatus.overdue

    return DetectionStatus.ended


def _dominant_category(txns: list[Transaction]) -> int | None:
    counts = Counter(t.category_id for t in txns if t.category_id is not None)
    if not counts:
        return None

    return counts.most_common(1)[0][0]


def _scale(amount: int, num: int, den: int) -> int:
    """``round(amount * num / den)`` with integer-only arithmetic (no float)."""
    total = amount * num
    quotient, remainder = divmod(total, den)
    if remainder * 2 >= den:
        quotient += 1

    return quotient
