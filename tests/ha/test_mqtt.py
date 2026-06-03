"""MQTT publisher (Phase 7) — driven by a fake client, never a real broker."""

import json
from dataclasses import dataclass, field

import pytest

from expense_analyzer.config import Settings
from expense_analyzer.ha.metrics import Metric
from expense_analyzer.ha.mqtt import MqttError, MqttPublisher


@dataclass
class _Published:
    topic: str
    payload: str
    qos: int
    retain: bool

    def wait_for_publish(self, timeout: float | None = None) -> None:
        # paho returns an MQTTMessageInfo with this method; mimic it so the
        # publisher's _wait_for_publish branch is exercised.
        return None


@dataclass
class FakeMqttClient:
    """Records what the publisher does instead of talking to a broker."""

    connect_error: bool = False
    published: list[_Published] = field(default_factory=list)
    will: tuple[str, str, bool] | None = None
    credentials: tuple[str, str | None] | None = None
    connected_to: tuple[str, int] | None = None
    loop_started: bool = False
    loop_stopped: bool = False
    disconnected: bool = False

    def username_pw_set(self, username: str, password: str | None = None) -> None:
        self.credentials = (username, password)

    def will_set(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
        self.will = (topic, payload, retain)

    def connect(self, host: str, port: int, keepalive: int = 60) -> None:
        if self.connect_error:
            raise OSError("connection refused")
        self.connected_to = (host, port)

    def loop_start(self) -> None:
        self.loop_started = True

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> _Published:
        info = _Published(topic, payload, qos, retain)
        self.published.append(info)
        return info

    def loop_stop(self) -> None:
        self.loop_stopped = True

    def disconnect(self) -> None:
        self.disconnected = True


def _publisher(client: FakeMqttClient, *, username: str = "") -> MqttPublisher:
    return MqttPublisher(
        host="broker.local",
        port=1883,
        username=username,
        password="pw" if username else "",
        base_topic="expense_analyzer",
        discovery_prefix="homeassistant",
        client=client,
    )


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


def test_from_settings_refuses_when_not_configured() -> None:
    settings = Settings(mqtt_host="")

    with pytest.raises(MqttError, match="not configured"):
        MqttPublisher.from_settings(settings)
