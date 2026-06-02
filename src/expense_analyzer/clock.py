"""Time handling.

Policy: the app works in **UTC internally** — every instant we generate or
persist is timezone-aware UTC. The configured ``EA_TIMEZONE`` (default
Europe/Warsaw) is applied only at the edges: presentation and grouping instants
into local days/months (e.g. monthly budgets).

Bank ``booked_date`` values are plain local dates from the CSV and are stored
as-is; they are already in the bank's (Polish) local calendar and do not need
conversion.
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from expense_analyzer.config import get_settings


def utc_now() -> datetime:
    """Current instant as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def local_tz() -> ZoneInfo:
    """The configured display/bucketing timezone."""
    return ZoneInfo(get_settings().timezone)


def to_local(dt: datetime) -> datetime:
    """Convert an instant to the configured local timezone.

    Naive datetimes are assumed to be UTC (that is how we store them).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(local_tz())


def local_today() -> date:
    """Today's date in the configured local timezone."""
    return to_local(utc_now()).date()


def local_month(dt: datetime) -> str:
    """Bucket an instant into a local ``YYYY-MM`` month key (for budgets)."""
    return to_local(dt).strftime("%Y-%m")
