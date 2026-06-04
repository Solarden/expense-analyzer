"""Unit tests for the pure subscription-detection logic (no DB)."""

from datetime import date

from expense_analyzer.models import Transaction
from expense_analyzer.subscriptions import Cadence, DetectionStatus, find_subscriptions

# Default detector params (mirror the config defaults) so each test only varies
# what it cares about.
PARAMS = dict(
    min_occurrences=3,
    amount_tolerance_pct=15,
    price_rise_pct=10,
    new_window_days=35,
)


def _tx(
    tx_id: int,
    amount: int,
    booked: date,
    *,
    merchant: str | None = "NETFLIX",
    category_id: int | None = None,
) -> Transaction:
    return Transaction(
        id=tx_id,
        account_id=1,
        import_batch_id=1,
        amount=amount,
        booked_date=booked,
        raw_description=f"tx{tx_id}",
        merchant_normalized=merchant,
        category_id=category_id,
        fingerprint=f"fp{tx_id}",
    )


def _monthly(
    amounts: list[int], *, start_day: int = 10, merchant: str = "NETFLIX"
) -> list[Transaction]:
    """One charge per month from 2026-01, newest amounts last."""
    return [
        _tx(i, amt, date(2026, 1 + i, start_day), merchant=merchant)
        for i, amt in enumerate(amounts)
    ]


def test_clean_monthly_subscription_detected():
    txns = _monthly([-2999, -2999, -2999, -2999])

    [sub] = find_subscriptions(txns, today=date(2026, 4, 15), **PARAMS)

    assert sub.merchant == "NETFLIX"
    assert sub.cadence == Cadence.monthly
    assert sub.occurrences == 4
    assert sub.current_amount == 2999
    assert sub.typical_amount == 2999
    assert sub.monthly_equivalent == 2999  # monthly cadence -> 1:1
    assert sub.next_expected == date(2026, 5, 10)
    assert sub.status == DetectionStatus.active
    assert sub.price_rise is None


def test_below_min_occurrences_not_detected():
    assert find_subscriptions(_monthly([-2999, -2999]), today=date(2026, 3, 15), **PARAMS) == []


def test_irregular_amounts_not_detected():
    # Same merchant, regular dates, but wildly varying amounts -> a supermarket,
    # not a subscription.
    txns = _monthly([-5000, -12000, -3000, -20000])

    assert find_subscriptions(txns, today=date(2026, 4, 15), **PARAMS) == []


def test_irregular_dates_not_detected():
    # Equal amounts but scattered dates -> no cadence.
    txns = [
        _tx(0, -3000, date(2026, 1, 1)),
        _tx(1, -3000, date(2026, 1, 4)),
        _tx(2, -3000, date(2026, 3, 5)),
        _tx(3, -3000, date(2026, 3, 10)),
        _tx(4, -3000, date(2026, 6, 8)),
    ]

    assert find_subscriptions(txns, today=date(2026, 6, 15), **PARAMS) == []


def test_yearly_cadence_monthly_equivalent():
    txns = [
        _tx(0, -12000, date(2024, 1, 15)),
        _tx(1, -12000, date(2025, 1, 15)),
        _tx(2, -12000, date(2026, 1, 15)),
    ]

    [sub] = find_subscriptions(txns, today=date(2026, 2, 1), **PARAMS)

    assert sub.cadence == Cadence.yearly
    assert sub.monthly_equivalent == 1000  # 12000 / 12
    assert sub.status == DetectionStatus.active


def test_quarterly_cadence_monthly_equivalent():
    txns = [
        _tx(0, -9000, date(2026, 1, 1)),
        _tx(1, -9000, date(2026, 4, 1)),
        _tx(2, -9000, date(2026, 7, 1)),
    ]

    [sub] = find_subscriptions(txns, today=date(2026, 7, 10), **PARAMS)

    assert sub.cadence == Cadence.quarterly
    assert sub.monthly_equivalent == 3000  # 9000 / 3


def test_weekly_cadence_monthly_equivalent():
    txns = [_tx(i, -500, date(2026, 4, 1 + 7 * i)) for i in range(4)]

    [sub] = find_subscriptions(txns, today=date(2026, 4, 25), **PARAMS)

    assert sub.cadence == Cadence.weekly
    assert sub.monthly_equivalent == 2167  # round(500 * 52 / 12)


def test_price_rise_flagged():
    txns = _monthly([-1000, -1000, -1000, -1200])  # 20% jump on the latest

    [sub] = find_subscriptions(txns, today=date(2026, 4, 15), **PARAMS)

    assert sub.price_rise is not None
    assert sub.price_rise.old_amount == 1000
    assert sub.price_rise.new_amount == 1200
    assert sub.price_rise.increase_pct == 20


def test_small_drift_is_not_a_price_rise():
    # 5% wobble stays under the 10% rise threshold.
    txns = _monthly([-1000, -1000, -1000, -1050])

    [sub] = find_subscriptions(txns, today=date(2026, 4, 15), **PARAMS)

    assert sub.price_rise is None


def test_new_flag_when_recently_started():
    # First charge (2026-04-05) within a 70-day window of "today" (2026-06-05).
    txns = [
        _tx(0, -3000, date(2026, 4, 5)),
        _tx(1, -3000, date(2026, 5, 5)),
        _tx(2, -3000, date(2026, 6, 4)),
    ]
    params = {**PARAMS, "new_window_days": 70}

    [sub] = find_subscriptions(txns, today=date(2026, 6, 5), **params)

    assert sub.is_new


def test_ended_subscription_status():
    # Last charge long ago -> more than two periods stale.
    txns = _monthly([-3000, -3000, -3000])

    [sub] = find_subscriptions(txns, today=date(2026, 10, 1), **PARAMS)

    assert sub.status == DetectionStatus.ended


def test_inflows_and_no_merchant_ignored():
    txns = [
        *_monthly([3000, 3000, 3000]),  # positive -> income, not a subscription
        _tx(10, -3000, date(2026, 1, 1), merchant=None),
        _tx(11, -3000, date(2026, 2, 1), merchant=None),
        _tx(12, -3000, date(2026, 3, 1), merchant=None),
    ]

    assert find_subscriptions(txns, today=date(2026, 3, 15), **PARAMS) == []


def test_dominant_category_reported():
    txns = [
        _tx(0, -3000, date(2026, 1, 10), category_id=7),
        _tx(1, -3000, date(2026, 2, 10), category_id=7),
        _tx(2, -3000, date(2026, 3, 10), category_id=9),
    ]

    [sub] = find_subscriptions(txns, today=date(2026, 3, 15), **PARAMS)

    assert sub.category_id == 7


def test_two_merchants_sorted_by_monthly_cost():
    txns = [
        *_monthly([-1000, -1000, -1000], merchant="CHEAP"),
        *_monthly([-5000, -5000, -5000], merchant="PRICEY"),
    ]

    subs = find_subscriptions(txns, today=date(2026, 4, 15), **PARAMS)

    assert [s.merchant for s in subs] == ["PRICEY", "CHEAP"]
