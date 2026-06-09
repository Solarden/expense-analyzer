"""Shared MQTT test doubles for the Home Assistant publisher tests.

A fake paho client that records what the publisher does instead of talking to a
broker, used by both ``test_mqtt.py`` (Phase 7) and ``test_update_notify.py``
(Phase 18). Not a test module itself (no ``test_`` prefix), so pytest won't
collect it.
"""

from dataclasses import dataclass, field

from expense_analyzer.ha.mqtt import MqttPublisher


@dataclass
class _Published:
    topic: str
    payload: str
    qos: int
    retain: bool
    rejected: bool = False

    def wait_for_publish(self, timeout: float | None = None) -> None:
        # paho returns an MQTTMessageInfo with this method; mimic it so the
        # publisher's _wait_for_publish branch is exercised. A rejected message
        # (e.g. bad credentials) makes the real paho method raise RuntimeError.
        if self.rejected:
            raise RuntimeError("message publish failed")
        return None


@dataclass
class FakeMqttClient:
    """Records what the publisher does instead of talking to a broker."""

    connect_error: bool = False
    publish_rejected: bool = False
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
        info = _Published(topic, payload, qos, retain, rejected=self.publish_rejected)
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
