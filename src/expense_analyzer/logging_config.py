"""Centralized logging configuration shared by the app and the worker.

Timestamps are emitted in UTC to match the app's internal time policy
(see clock.py).
"""

import logging
import time

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"

_configured = False


def configure_logging(debug: bool = False) -> None:
    """Set up root logging. Idempotent, safe to call from every entrypoint."""
    global _configured
    if _configured:
        return

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    formatter.converter = time.gmtime  # UTC timestamps

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    _configured = True
