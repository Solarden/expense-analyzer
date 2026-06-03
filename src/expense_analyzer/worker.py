"""Background worker entrypoint.

Runs a small set of optional periodic jobs, each independently gated and OFF by
default, so the compose ``worker`` service always has something to run even when
nothing is configured:

- **myFund fetch** (Phase 6) — pulls the configured portfolio. Needs
  ``EA_MYFUND_API_KEY`` + ``EA_MYFUND_PORTFOLIO``, a positive
  ``EA_MYFUND_FETCH_INTERVAL_HOURS``, and ``EA_MYFUND_ACCOUNT_ID`` (which
  portfolio account to import into — the UI picks this per request, but the
  headless worker needs it pinned).
- **Home Assistant publish** (Phase 7) — pushes household metrics over MQTT.
  Needs ``EA_MQTT_HOST`` and a positive ``EA_MQTT_PUBLISH_INTERVAL_MINUTES``.

With nothing configured the worker stays alive but idle. Later phases add
watched-folder intake and classifier retraining (design §3, §11).
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from expense_analyzer.clock import utc_now
from expense_analyzer.config import Settings, get_settings
from expense_analyzer.db import get_engine
from expense_analyzer.importers.myfund import MyFundClient, MyFundError
from expense_analyzer.importers.positions import import_positions
from expense_analyzer.logging_config import configure_logging

log = logging.getLogger("expense_analyzer.worker")

_IDLE_SLEEP_SECONDS = 3600


@dataclass(frozen=True)
class _Job:
    """A periodic job: run :attr:`run` no more often than every :attr:`interval_seconds`."""

    name: str
    interval_seconds: float
    run: Callable[[], None]


def _run_myfund_fetch(settings: Settings) -> None:
    """Fetch the configured portfolio once and upsert it into the pinned account.

    Validates that ``EA_MYFUND_ACCOUNT_ID`` points at a real ``portfolio`` account
    *before* the network call, so a misconfiguration is a clear log line rather
    than a wasted fetch + FK error.
    """
    from sqlmodel import Session

    from expense_analyzer.models import Account, AccountType

    try:
        with Session(get_engine()) as session:
            account = session.get(Account, settings.myfund_account_id)
            if account is None or account.type != AccountType.portfolio:
                log.warning(
                    "myFund fetch skipped: EA_MYFUND_ACCOUNT_ID=%s is not a portfolio account",
                    settings.myfund_account_id,
                )
                return

            result = MyFundClient.from_settings(settings).fetch()
            summary = import_positions(
                session,
                account_id=account.id,
                result=result,
                source="myfund_api",
                fetched_at=utc_now(),
            )
    except MyFundError as exc:
        log.warning("myFund fetch failed: %s", exc)
        return

    log.info(
        "myFund fetch: %d positions (%d new, %d updated)",
        summary.imported,
        summary.inserted,
        summary.updated,
    )


def _run_mqtt_publish(settings: Settings) -> None:
    """Collect household metrics and push them to Home Assistant over MQTT."""
    from sqlmodel import Session

    from expense_analyzer.ha.mqtt import MqttError, publish_snapshot

    try:
        with Session(get_engine()) as session:
            count = publish_snapshot(session, settings)
    except MqttError as exc:
        log.warning("HA publish failed: %s", exc)
        return

    log.info("HA publish: %d sensors pushed to MQTT", count)


def _build_jobs(settings: Settings) -> list[_Job]:
    """The enabled periodic jobs for this configuration (possibly empty)."""
    jobs: list[_Job] = []

    myfund_hours = settings.myfund_fetch_interval_hours
    if settings.myfund_configured and myfund_hours and settings.myfund_account_id:
        jobs.append(_Job("myFund fetch", myfund_hours * 3600, lambda: _run_myfund_fetch(settings)))

    mqtt_minutes = settings.mqtt_publish_interval_minutes
    if settings.mqtt_configured and mqtt_minutes:
        jobs.append(_Job("HA publish", mqtt_minutes * 60, lambda: _run_mqtt_publish(settings)))

    return jobs


def _run_scheduler(jobs: list[_Job]) -> None:
    """Run each job once at startup, then on its own interval, forever.

    A single-threaded tick: a job that throws is logged and retried next interval
    (one bad cycle must never kill the worker). Uses a monotonic clock so it is
    immune to wall-clock jumps.
    """
    next_due = {job.name: 0.0 for job in jobs}  # 0 => due immediately on first tick
    while True:
        now = time.monotonic()
        for job in jobs:
            if now >= next_due[job.name]:
                try:
                    job.run()
                except Exception:  # noqa: BLE001 — never let one bad cycle kill the worker
                    log.exception("job %r failed", job.name)
                next_due[job.name] = time.monotonic() + job.interval_seconds
        time.sleep(max(1.0, min(next_due.values()) - time.monotonic()))


def main() -> None:
    settings = get_settings()
    configure_logging(settings.debug)

    jobs = _build_jobs(settings)
    if not jobs:
        log.info("worker idle: no periodic jobs configured")
        while True:
            time.sleep(_IDLE_SLEEP_SECONDS)

    log.info(
        "worker started: %s",
        ", ".join(f"{job.name} every {job.interval_seconds:.0f}s" for job in jobs),
    )
    _run_scheduler(jobs)


if __name__ == "__main__":
    main()
