"""Background worker entrypoint.

Currently runs one optional job: a periodic myFund.pl portfolio fetch (Phase 6).
It is **off by default** and only runs when *all* of these are set:
``EA_MYFUND_API_KEY`` + ``EA_MYFUND_PORTFOLIO`` (configured), a positive
``EA_MYFUND_FETCH_INTERVAL_HOURS``, and ``EA_MYFUND_ACCOUNT_ID`` (which portfolio
account to import into — the UI picks this per request, but the headless worker
needs it pinned). With any of these missing the worker stays alive but idle, so
the compose ``worker`` service still has something to run.

Later phases add watched-folder intake and classifier retraining (design §3, §11).
"""

import logging
import time

from expense_analyzer.clock import utc_now
from expense_analyzer.config import Settings, get_settings
from expense_analyzer.db import get_engine
from expense_analyzer.importers.myfund import MyFundClient, MyFundError
from expense_analyzer.importers.positions import import_positions
from expense_analyzer.logging_config import configure_logging

log = logging.getLogger("expense_analyzer.worker")

_IDLE_SLEEP_SECONDS = 3600


def _run_myfund_fetch(settings: Settings) -> None:
    """Fetch the configured portfolio once and upsert it into the pinned account.

    Validates that ``EA_MYFUND_ACCOUNT_ID`` points at a real ``portfolio`` account
    *before* the network call, so a misconfiguration is a clear log line rather
    than a wasted fetch + FK error.
    """
    from sqlmodel import Session

    from expense_analyzer.models import Account, AccountType

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

    log.info(
        "myFund fetch: %d positions (%d new, %d updated)",
        summary.imported,
        summary.inserted,
        summary.updated,
    )


def main() -> None:
    settings = get_settings()
    configure_logging(settings.debug)

    interval_hours = settings.myfund_fetch_interval_hours
    enabled = bool(settings.myfund_configured and interval_hours and settings.myfund_account_id)
    if not enabled:
        log.info("worker idle: periodic myFund fetch not configured")
        while True:
            time.sleep(_IDLE_SLEEP_SECONDS)

    log.info("worker started: myFund fetch every %d h", interval_hours)
    while True:
        try:
            _run_myfund_fetch(settings)
        except MyFundError as exc:
            log.warning("myFund fetch failed: %s", exc)
        except Exception:  # noqa: BLE001 — never let one bad cycle kill the worker
            log.exception("unexpected error during myFund fetch")
        time.sleep(interval_hours * 3600)


if __name__ == "__main__":
    main()
