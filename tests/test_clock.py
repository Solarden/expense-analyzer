from datetime import UTC, datetime

from expense_analyzer import clock


def test_utc_now_is_aware_utc():
    now = clock.utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset().total_seconds() == 0


def test_to_local_converts_from_utc():
    # 2026-06-02 00:30 UTC is 02:30 in Warsaw (CEST, UTC+2) — same instant,
    # but already the 2nd locally.
    instant = datetime(2026, 6, 2, 0, 30, tzinfo=UTC)
    local = clock.to_local(instant)
    assert local.hour == 2
    assert local.day == 2


def test_local_month_buckets_by_local_tz():
    # 2026-06-30 23:30 UTC is already 2026-07-01 01:30 in Warsaw, so it buckets
    # into July locally — the kind of edge a UTC-only bucketing would get wrong.
    instant = datetime(2026, 6, 30, 23, 30, tzinfo=UTC)
    assert clock.local_month(instant) == "2026-07"


def test_naive_datetime_assumed_utc():
    naive = datetime(2026, 6, 2, 0, 30)
    assert clock.to_local(naive).day == 2
