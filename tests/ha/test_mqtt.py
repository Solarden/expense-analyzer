"""MQTT publisher (Phase 7) — driven by a fake client, never a real broker."""

import json

import pytest
from _fakes import FakeMqttClient, _publisher

from expense_analyzer.config import Settings
from expense_analyzer.ha.metrics import Metric
from expense_analyzer.ha.mqtt import MqttError, MqttPublisher


def test_publish_metrics_announces_availability_discovery_and_state() -> None:
    client = FakeMqttClient()
    metrics = [Metric("net_worth", "Net Worth", "100.00"), Metric("month_net", "Net", "5.00")]

    _publisher(client).publish_metrics(metrics)

    assert client.connected_to == ("broker.local", 1883)
    # Last Will marks us offline if the connection drops unexpectedly.
    assert client.will == ("expense_analyzer/availability", "offline", True)

    by_topic = {p.topic: p for p in client.published}
    # Availability flips to online, retained.
    assert by_topic["expense_analyzer/availability"].payload == "online"
    assert by_topic["expense_analyzer/availability"].retain is True
    # One retained discovery config per metric, valid JSON for the right sensor.
    cfg = json.loads(by_topic["homeassistant/sensor/expense_analyzer/net_worth/config"].payload)
    assert cfg["unique_id"] == "expense_analyzer_net_worth"
    assert by_topic["homeassistant/sensor/expense_analyzer/net_worth/config"].retain is True
    # The state doc carries every metric, retained, QoS 1.
    state = by_topic["expense_analyzer/state"]
    assert json.loads(state.payload) == {"net_worth": "100.00", "month_net": "5.00"}
    assert state.retain is True
    assert state.qos == 1
    # Cleanly torn down.
    assert client.loop_stopped and client.disconnected


def test_credentials_only_set_when_a_username_is_configured() -> None:
    anon = FakeMqttClient()
    _publisher(anon).publish_metrics([])
    assert anon.credentials is None

    authed = FakeMqttClient()
    _publisher(authed, username="ha").publish_metrics([])
    assert authed.credentials == ("ha", "pw")


def test_publish_alert_is_a_non_retained_event() -> None:
    client = FakeMqttClient()

    _publisher(client).publish_alert("Budget exceeded", "Food over budget", severity="warning")

    assert len(client.published) == 1
    alert = client.published[0]
    assert alert.topic == "expense_analyzer/alert"
    assert alert.retain is False  # events must not replay to late subscribers
    assert json.loads(alert.payload)["title"] == "Budget exceeded"
    # No availability Last Will for a one-off alert.
    assert client.will is None


def test_connection_failure_becomes_mqtt_error_and_still_tears_down() -> None:
    client = FakeMqttClient(connect_error=True)

    with pytest.raises(MqttError):
        _publisher(client).publish_metrics([])

    # Teardown runs in finally even though connect raised.
    assert client.disconnected


def test_rejected_publish_becomes_mqtt_error_and_still_tears_down() -> None:
    # paho raises RuntimeError from wait_for_publish on a rejected message (e.g. bad
    # credentials); the publisher must wrap it as MqttError, not let it 500.
    client = FakeMqttClient(publish_rejected=True)

    with pytest.raises(MqttError):
        _publisher(client).publish_metrics([Metric("net_worth", "Net Worth", "1.00")])

    assert client.disconnected


def test_from_settings_refuses_when_not_configured() -> None:
    settings = Settings(mqtt_host="")

    with pytest.raises(MqttError, match="not configured"):
        MqttPublisher.from_settings(settings)


def test_publish_snapshot_fires_budget_exceeded_alert(
    db_session, make_account, make_category, make_transaction, make_budget
) -> None:
    """An over-budget category turns into a (non-retained) alert event alongside
    the normal metrics push — the Phase 7 publish_alert primitive wired up."""
    from expense_analyzer.clock import local_today
    from expense_analyzer.ha.mqtt import publish_snapshot
    from expense_analyzer.models import CategoryKind

    account = make_account()
    food = make_category(name="Food", kind=CategoryKind.expense)
    today = local_today()
    make_transaction(account_id=account.id, amount=-300_00, booked_date=today, category_id=food.id)
    make_budget(category_id=food.id, month=today.strftime("%Y-%m"), limit_amount=200_00)

    client = FakeMqttClient()
    count = publish_snapshot(db_session, Settings(mqtt_host="broker.local"), client=client)

    assert count >= 4  # at least the headline sensors were published
    alerts = [p for p in client.published if p.topic == "expense_analyzer/alert"]
    assert len(alerts) == 1
    payload = json.loads(alerts[0].payload)
    assert "Food" in payload["title"]
    assert alerts[0].retain is False  # events must not replay


def test_publish_snapshot_no_alert_when_within_budget(
    db_session, make_account, make_category, make_transaction, make_budget
) -> None:
    from expense_analyzer.clock import local_today
    from expense_analyzer.ha.mqtt import publish_snapshot
    from expense_analyzer.models import CategoryKind

    account = make_account()
    food = make_category(name="Food", kind=CategoryKind.expense)
    today = local_today()
    make_transaction(account_id=account.id, amount=-120_00, booked_date=today, category_id=food.id)
    make_budget(category_id=food.id, month=today.strftime("%Y-%m"), limit_amount=200_00)

    client = FakeMqttClient()
    publish_snapshot(db_session, Settings(mqtt_host="broker.local"), client=client)

    assert [p for p in client.published if p.topic == "expense_analyzer/alert"] == []


def _seed_subscription(make_transaction, account_id, *, merchant, amounts) -> None:
    """Three monthly charges (active as of 2026-06-04), amounts oldest-first."""
    from datetime import date

    for month, amount in zip((3, 4, 5), amounts, strict=True):
        make_transaction(
            account_id=account_id,
            amount=amount,
            booked_date=date(2026, month, 15),
            merchant_normalized=merchant,
        )


def test_publish_snapshot_fires_subscription_price_rise_alert(
    db_session, make_account, make_transaction
) -> None:
    from expense_analyzer.ha.mqtt import publish_snapshot

    account = make_account()
    _seed_subscription(
        make_transaction, account.id, merchant="NETFLIX", amounts=[-10_00, -10_00, -13_00]
    )

    client = FakeMqttClient()
    publish_snapshot(db_session, Settings(mqtt_host="broker.local"), client=client)

    alerts = [
        json.loads(p.payload) for p in client.published if p.topic == "expense_analyzer/alert"
    ]
    assert any("price went up" in a["title"].lower() and "NETFLIX" in a["title"] for a in alerts)


def test_publish_snapshot_skips_alerts_for_dismissed_subscription(
    db_session, make_account, make_transaction, make_subscription
) -> None:
    from expense_analyzer.ha.mqtt import publish_snapshot
    from expense_analyzer.models import SubscriptionStatus

    account = make_account()
    _seed_subscription(
        make_transaction, account.id, merchant="NETFLIX", amounts=[-10_00, -10_00, -13_00]
    )
    make_subscription(merchant="NETFLIX", status=SubscriptionStatus.dismissed)

    client = FakeMqttClient()
    publish_snapshot(db_session, Settings(mqtt_host="broker.local"), client=client)

    assert [p for p in client.published if p.topic == "expense_analyzer/alert"] == []
