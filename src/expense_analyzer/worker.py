"""Background worker entrypoint.

Phase 0: a placeholder loop so the `worker` compose service has something to
run. Later phases add watched-folder intake, batch imports and classifier
retraining (see internal_docs/expense-analyzer-design.md §3, §11).
"""

import logging
import time

from expense_analyzer.config import get_settings
from expense_analyzer.logging_config import configure_logging

log = logging.getLogger("expense_analyzer.worker")


def main() -> None:
    configure_logging(get_settings().debug)
    log.info("worker started (Phase 0 placeholder, no jobs yet)")
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
