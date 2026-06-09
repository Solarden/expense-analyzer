"""Phase 19c — the monthly plan's Home Assistant surface.

The FOR LIVING / Left To Pay money sensors (via ``collect_metrics``), the dedicated
"Monthly Plan" progress sensor on its own retained topic, and the overdue-bills
alert — all read-only, mirroring the Phase 18 update-sensor wiring.
"""

import json
from collections.abc import Callable

from _fakes import FakeMqttClient
from sqlmodel import Session

from expense_analyzer.clock import local_today
from expense_analyzer.config import Settings
from expense_analyzer.ha import discovery
from expense_analyzer.ha.metrics import collect_metrics
from expense_analyzer.ha.mqtt import publish_snapshot
from expense_analyzer.models import PlannedItem
from expense_analyzer.queries import planned as pq


def test_plan_sensor_config_is_a_text_sensor_with_attributes() -> None:
    config = discovery.plan_sensor_config(base="expense_analyzer")
    assert config["state_topic"] == "expense_analyzer/plan"
    assert config["value_template"] == "{{ value_json.progress }}"
    assert config["json_attributes_topic"] == "expense_analyzer/plan"
    # A plain text sensor — no monetary device_class/unit (unlike the money sensors).
    assert "device_class" not in config and "unit_of_measurement" not in config


def test_plan_payload_shape() -> None:
    payload = json.loads(discovery.plan_payload(paid=8, total=14, overdue=2))
    assert payload == {"progress": "8/14", "paid": 8, "total": 14, "overdue": 2}


def test_collect_metrics_includes_plan_money_figures(
    db_session: Session, make_planned_item: Callable[..., PlannedItem]
) -> None:
    make_planned_item(name="Rent", expected_amount=-3000_00)
    keys = {m.key for m in collect_metrics(db_session)}
    assert "plan_for_living" in keys
    assert "plan_left_to_pay" in keys


def test_publish_snapshot_publishes_plan_progress_sensor(
    db_session: Session, make_planned_item: Callable[..., PlannedItem]
) -> None:
    month = local_today().strftime("%Y-%m")
    paid_item = make_planned_item(name="Rent", expected_amount=-3000_00)
    make_planned_item(name="Phone", expected_amount=-100_00)
    pq.mark_paid(db_session, planned_item_id=paid_item.id, month=month)

    client = FakeMqttClient()
    publish_snapshot(db_session, Settings(mqtt_host="broker.local"), client=client)

    plan = [p for p in client.published if p.topic == "expense_analyzer/plan"]
    assert len(plan) == 1
    assert plan[0].retain is True  # retained so HA re-reads after a restart
    payload = json.loads(plan[0].payload)
    assert payload["progress"] == "1/2"
    assert payload["paid"] == 1
    assert payload["total"] == 2


def test_publish_snapshot_overdue_alert(
    db_session: Session, make_planned_item: Callable[..., PlannedItem]
) -> None:
    # An unpaid item due on the 1st is overdue from the 2nd onward — so whether the
    # alert fires is exactly "are we past the 1st of the month", which is stable.
    today = local_today()
    make_planned_item(name="Rent", expected_amount=-3000_00, due_day=1)

    client = FakeMqttClient()
    publish_snapshot(db_session, Settings(mqtt_host="broker.local"), client=client)

    overdue = [
        p
        for p in client.published
        if p.topic == "expense_analyzer/alert"
        and "overdue" in json.loads(p.payload)["title"].lower()
    ]
    assert bool(overdue) == (today.day > 1)
    if overdue:
        assert overdue[0].retain is False  # events must not replay
