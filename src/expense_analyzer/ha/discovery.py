"""MQTT topic layout and Home Assistant discovery payloads (design §9).

Pure functions — no MQTT, no DB — so the exact wire format is unit-testable on
its own.

Topic layout (one HA *device* groups every sensor under "Expense Analyzer"):

- discovery:    ``<discovery_prefix>/sensor/<base>/<key>/config``  (retained)
- state:        ``<base>/state`` — one JSON doc ``{key: value, ...}``  (retained)
- availability: ``<base>/availability`` — ``"online"``/``"offline"``  (retained, LWT)
- alert:        ``<base>/alert`` — a JSON event  (NOT retained — events shouldn't replay)

A single shared state topic plus a per-sensor ``value_template`` keeps the wire
small: one retained JSON publish refreshes every sensor at once, and HA re-reads
the retained discovery + state after a restart with no push from us.
"""

import json

from expense_analyzer.ha.metrics import Metric

ONLINE = "online"
OFFLINE = "offline"

# Stable identifier for the single HA device all sensors hang off of, and the
# prefix for every sensor's unique_id / object_id (HA entity_id becomes
# sensor.expense_analyzer_<key>).
_DEVICE_ID = "expense_analyzer"


def state_topic(base: str) -> str:
    return f"{base}/state"


def update_topic(base: str) -> str:
    return f"{base}/update"


def plan_topic(base: str) -> str:
    return f"{base}/plan"


def availability_topic(base: str) -> str:
    return f"{base}/availability"


def alert_topic(base: str) -> str:
    return f"{base}/alert"


def discovery_topic(discovery_prefix: str, base: str, key: str) -> str:
    return f"{discovery_prefix}/sensor/{base}/{key}/config"


def _device() -> dict:
    return {
        "identifiers": [_DEVICE_ID],
        "name": "Expense Analyzer",
        "manufacturer": "Expense Analyzer",
        "model": "Household finance",
    }


def discovery_config(metric: Metric, *, base: str) -> dict:
    """HA MQTT discovery config for one monetary sensor.

    ``device_class=monetary`` + ``state_class=total`` is the HA-correct pairing
    for an account-balance / net-worth style figure (a level, not a meter that
    only increases). The unit is the PLN ISO currency code.
    """
    return {
        "name": metric.name,
        "unique_id": f"{_DEVICE_ID}_{metric.key}",
        "object_id": f"{_DEVICE_ID}_{metric.key}",
        "state_topic": state_topic(base),
        "value_template": f"{{{{ value_json.{metric.key} }}}}",
        "availability_topic": availability_topic(base),
        "unit_of_measurement": "PLN",
        "device_class": "monetary",
        "state_class": "total",
        "device": _device(),
    }


def update_sensor_config(*, base: str) -> dict:
    """HA discovery config for the "deploy update available" sensor (Phase 18).

    A plain text sensor (no ``device_class``/``unit``) whose state is the latest
    available release tag; ``current``/``update_available`` ride along as
    attributes from the same retained topic, so an HA automation can notify off
    ``update_available`` while the entity stays glanceable on a dashboard. This is
    one-directional like the rest — HA never triggers a deploy back.
    """
    return {
        "name": "Update",
        "unique_id": f"{_DEVICE_ID}_update",
        "object_id": f"{_DEVICE_ID}_update",
        "state_topic": update_topic(base),
        "value_template": "{{ value_json.latest }}",
        "json_attributes_topic": update_topic(base),
        "availability_topic": availability_topic(base),
        "icon": "mdi:package-up",
        "device": _device(),
    }


def plan_sensor_config(*, base: str) -> dict:
    """HA discovery config for the monthly-plan progress sensor (Phase 19c).

    A plain text sensor whose state is the paid progress (``"8/14"``); the paid /
    total / overdue counts ride along as attributes from the same retained topic,
    so an HA automation can notify off ``overdue`` while the entity stays glanceable
    next to the FOR LIVING / Left To Pay money sensors. Read-only like the rest.
    """
    return {
        "name": "Monthly Plan",
        "unique_id": f"{_DEVICE_ID}_plan",
        "object_id": f"{_DEVICE_ID}_plan",
        "state_topic": plan_topic(base),
        "value_template": "{{ value_json.progress }}",
        "json_attributes_topic": plan_topic(base),
        "availability_topic": availability_topic(base),
        "icon": "mdi:calendar-check",
        "device": _device(),
    }


def state_payload(metrics: list[Metric]) -> str:
    """The retained state JSON: every metric keyed by its slug."""
    return json.dumps({m.key: m.value for m in metrics})


def plan_payload(*, paid: int, total: int, overdue: int) -> str:
    """Retained state for the plan sensor: paid progress plus the raw counts."""
    return json.dumps(
        {"progress": f"{paid}/{total}", "paid": paid, "total": total, "overdue": overdue}
    )


def update_payload(*, current: str | None, latest: str | None, update_available: bool) -> str:
    """Retained state for the update sensor: latest version + current + the flag."""
    return json.dumps(
        {
            "current": current or "unknown",
            "latest": latest or "unknown",
            "update_available": update_available,
        }
    )


def alert_payload(title: str, message: str, *, severity: str) -> str:
    """A one-off alert event for an HA automation to turn into a notification."""
    return json.dumps({"title": title, "message": message, "severity": severity})
