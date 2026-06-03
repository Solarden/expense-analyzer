"""MQTT topic layout and HA discovery payloads (Phase 7) — pure, no broker."""

import json

from expense_analyzer.ha.discovery import (
    alert_payload,
    alert_topic,
    availability_topic,
    discovery_config,
    discovery_topic,
    state_payload,
    state_topic,
)
from expense_analyzer.ha.metrics import Metric


def test_topics_follow_the_documented_layout() -> None:
    assert state_topic("expense_analyzer") == "expense_analyzer/state"
    assert availability_topic("expense_analyzer") == "expense_analyzer/availability"
    assert alert_topic("expense_analyzer") == "expense_analyzer/alert"
    assert (
        discovery_topic("homeassistant", "expense_analyzer", "net_worth")
        == "homeassistant/sensor/expense_analyzer/net_worth/config"
    )


def test_discovery_config_is_a_monetary_sensor_on_the_shared_state_topic() -> None:
    metric = Metric("net_worth", "Net Worth", "1234.56")
    config = discovery_config(metric, base="expense_analyzer")

    assert config["unique_id"] == "expense_analyzer_net_worth"
    assert config["object_id"] == "expense_analyzer_net_worth"
    assert config["state_topic"] == "expense_analyzer/state"
    assert config["value_template"] == "{{ value_json.net_worth }}"
    assert config["availability_topic"] == "expense_analyzer/availability"
    assert config["device_class"] == "monetary"
    assert config["state_class"] == "total"
    assert config["unit_of_measurement"] == "PLN"
    # Every sensor hangs off the one device so HA groups them together.
    assert config["device"]["identifiers"] == ["expense_analyzer"]


def test_state_payload_maps_keys_to_values() -> None:
    metrics = [Metric("net_worth", "Net Worth", "100.00"), Metric("month_net", "Net", "-5.00")]

    assert json.loads(state_payload(metrics)) == {"net_worth": "100.00", "month_net": "-5.00"}


def test_alert_payload_carries_title_message_severity() -> None:
    payload = json.loads(
        alert_payload("Budget exceeded", "Food over by 50 PLN", severity="warning")
    )

    assert payload == {
        "title": "Budget exceeded",
        "message": "Food over by 50 PLN",
        "severity": "warning",
    }
