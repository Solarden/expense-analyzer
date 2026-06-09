"""Notify Home Assistant when a newer release tag is available (Phase 18).

The "smart" half of the updater. ``scripts/check_update.sh`` does the git
plumbing on the Pi host (``git fetch --tags`` from our OWN repo, then the tag
reachable from HEAD = what's deployed) and pipes the candidate tags here; this
module decides whether a newer release exists and pushes the verdict to HA over
MQTT — a retained ``sensor.expense_analyzer_update`` plus a one-off alert when an
update is waiting. It NEVER deploys: the owner runs ``make deploy`` when they
choose (notify + one-click, not unattended — keep-pi-fully-local stays intact;
the only egress is the host's git fetch of our own repo).

    git tag --list 'v*' | python -m expense_analyzer.ha.update_notify --current v1.2.0

With MQTT unconfigured this is a no-op (logs and exits 0). Versions are simple
``vMAJOR.MINOR.PATCH`` release tags; anything else is ignored.
"""

import argparse
import logging
import re
import sys
from dataclasses import dataclass

from expense_analyzer.config import get_settings
from expense_analyzer.logging_config import configure_logging

log = logging.getLogger("expense_analyzer.ha.update_notify")

# A release tag: vMAJOR.MINOR.PATCH (the leading v is optional). Pre-release or
# build-metadata suffixes are intentionally NOT matched — only clean releases
# auto-notify, so a `v1.4.0-rc1` tag never nudges the household to deploy.
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True)
class UpdateStatus:
    """The verdict: what's deployed, the newest release, and whether to nudge."""

    current: str | None
    latest: str | None
    update_available: bool


def parse_version(tag: str) -> tuple[int, int, int] | None:
    """Parse a ``vX.Y.Z`` release tag into a comparable tuple, else ``None``."""
    match = _VERSION_RE.match(tag.strip())
    if match is None:
        return None

    return (int(match[1]), int(match[2]), int(match[3]))


def select_update(current: str | None, tags: list[str]) -> UpdateStatus:
    """Pick the newest release tag and decide whether it beats ``current``.

    Tags that aren't clean ``vX.Y.Z`` releases are ignored. With no releases at
    all the verdict is "no update". An unparseable/absent ``current`` (e.g. the
    Pi has never been tagged) counts as behind any real release.
    """
    versioned = [(parse_version(t), t) for t in tags]
    releases = [(v, t) for v, t in versioned if v is not None]
    if not releases:
        return UpdateStatus(current=current, latest=None, update_available=False)

    latest_version, latest_tag = max(releases)
    current_version = parse_version(current) if current else None
    update_available = current_version is None or latest_version > current_version

    return UpdateStatus(current=current, latest=latest_tag, update_available=update_available)


def _publish(status: UpdateStatus) -> None:
    """Push the verdict to HA: a retained sensor + an alert if an update waits."""
    from expense_analyzer.ha.mqtt import MqttError, MqttPublisher

    settings = get_settings()
    if not settings.mqtt_configured:
        log.info("MQTT not configured — skipping HA update notification")
        return

    try:
        publisher = MqttPublisher.from_settings(settings)
        publisher.publish_update(
            current=status.current,
            latest=status.latest,
            update_available=status.update_available,
        )
        if status.update_available:
            publisher.publish_alert(
                title="Expense Analyzer update available",
                message=(
                    f"Release {status.latest} is available "
                    f"(running {status.current or 'an untagged build'}). "
                    'Run `make deploy a="--pull"` on the Pi to update.'
                ),
                severity="info",
            )
    except MqttError as exc:
        log.warning("HA update notification failed: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Notify Home Assistant of an available update.")
    parser.add_argument(
        "--current",
        default="",
        help="the deployed release tag (git describe --tags); empty if untagged",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        dest="tags",
        help="a candidate release tag; repeatable. If none given, tags are read from stdin.",
    )
    args = parser.parse_args()

    configure_logging(get_settings().debug)

    tags = args.tags or [line.strip() for line in sys.stdin if line.strip()]
    status = select_update(args.current or None, tags)

    if status.update_available:
        log.info("update available: %s (running %s)", status.latest, status.current or "untagged")
    else:
        log.info("up to date: running %s, latest %s", status.current, status.latest)

    _publish(status)


if __name__ == "__main__":
    main()
