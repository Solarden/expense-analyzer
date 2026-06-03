"""MQTT publisher for the Home Assistant push (design §9).

Opt-in and gated by config (``EA_MQTT_HOST``), like the myFund pull — but the
broker is on the LAN (your HA broker), so this is **not** internet egress.

Connection lifecycle is **per publish** (connect → publish → disconnect): a
metrics push runs every few minutes, so a short-lived connection is simpler and
more robust than holding one open on a Pi for days. The retained Last Will
(``"offline"``) covers a crash *while connected*; on a clean disconnect the
retained ``"online"`` we publish stays, so HA keeps the sensors available between
cycles — the real freshness signal is the state value itself, not availability.

``MqttClient`` is a thin :class:`~typing.Protocol` over the slice of paho-mqtt we
drive, so tests inject a fake recorder instead of a broker (mirroring myFund's
injectable ``transport``). paho is imported lazily inside :meth:`_build_client`,
so importing this module costs nothing and an unconfigured app never touches it.
"""

import json
import logging
from collections.abc import Callable
from typing import Protocol

from sqlmodel import Session

from expense_analyzer.config import Settings
from expense_analyzer.ha import discovery
from expense_analyzer.ha.metrics import Metric, collect_metrics

log = logging.getLogger("expense_analyzer.ha.mqtt")

_KEEPALIVE_SECONDS = 60
_PUBLISH_TIMEOUT_SECONDS = 10.0
_QOS = 1


class MqttError(Exception):
    """An MQTT connect or publish failed."""


class MqttClient(Protocol):
    """The slice of ``paho.mqtt.client.Client`` that :class:`MqttPublisher` uses."""

    def username_pw_set(self, username: str, password: str | None = ...) -> object: ...
    def will_set(self, topic: str, payload: str, qos: int = ..., retain: bool = ...) -> object: ...
    def connect(self, host: str, port: int, keepalive: int = ...) -> object: ...
    def loop_start(self) -> object: ...
    def publish(self, topic: str, payload: str, qos: int = ..., retain: bool = ...) -> object: ...
    def loop_stop(self) -> object: ...
    def disconnect(self) -> object: ...


class MqttPublisher:
    """Connects to the broker and publishes HA discovery, state, and alerts.

    Pass ``client`` to inject a fake (tests); otherwise a paho client is built
    lazily per :meth:`_build_client`.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        base_topic: str,
        discovery_prefix: str,
        client: MqttClient | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._base_topic = base_topic
        self._discovery_prefix = discovery_prefix
        self._client = client

    @classmethod
    def from_settings(
        cls, settings: Settings, *, client: MqttClient | None = None
    ) -> "MqttPublisher":
        if not settings.mqtt_configured:
            raise MqttError("MQTT is not configured — set EA_MQTT_HOST.")
        return cls(
            host=settings.mqtt_host,
            port=settings.mqtt_port,
            username=settings.mqtt_username,
            password=settings.mqtt_password.get_secret_value(),
            base_topic=settings.mqtt_base_topic,
            discovery_prefix=settings.mqtt_discovery_prefix,
            client=client,
        )

    def _build_client(self) -> MqttClient:
        import paho.mqtt.client as mqtt

        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="expense-analyzer")

    def publish_metrics(self, metrics: list[Metric]) -> None:
        """Announce availability + discovery configs, then publish the state doc.

        Discovery is republished every cycle (retained, idempotent) so HA always
        re-discovers after a restart. All publishes are QoS 1 and waited on before
        the connection is torn down, so nothing is dropped on a quick disconnect.
        """
        avail = discovery.availability_topic(self._base_topic)

        def body(client: MqttClient) -> list[object]:
            infos = [client.publish(avail, discovery.ONLINE, qos=_QOS, retain=True)]
            for metric in metrics:
                topic = discovery.discovery_topic(
                    self._discovery_prefix, self._base_topic, metric.key
                )
                config = json.dumps(discovery.discovery_config(metric, base=self._base_topic))
                infos.append(client.publish(topic, config, qos=_QOS, retain=True))
            infos.append(
                client.publish(
                    discovery.state_topic(self._base_topic),
                    discovery.state_payload(metrics),
                    qos=_QOS,
                    retain=True,
                )
            )
            return infos

        self._with_connection(body, will=(avail, discovery.OFFLINE))

    def publish_alert(self, title: str, message: str, *, severity: str = "warning") -> None:
        """Publish a one-off alert event (not retained) for an HA automation.

        The Phase 7 primitive that later phases call: budget-exceeded (Phase 8)
        and new-subscription (Phase 9) alerts just invoke this.
        """

        def body(client: MqttClient) -> list[object]:
            return [
                client.publish(
                    discovery.alert_topic(self._base_topic),
                    discovery.alert_payload(title, message, severity=severity),
                    qos=_QOS,
                    retain=False,
                )
            ]

        self._with_connection(body)

    def _with_connection(
        self,
        body: Callable[[MqttClient], list[object]],
        *,
        will: tuple[str, str] | None = None,
    ) -> None:
        client = self._client or self._build_client()
        try:
            if self._username:
                client.username_pw_set(self._username, self._password or None)
            if will is not None:
                client.will_set(will[0], will[1], qos=_QOS, retain=True)
            client.connect(self._host, self._port, _KEEPALIVE_SECONDS)
            client.loop_start()
            infos = body(client)
            _wait_for_publish(infos)
        except OSError as exc:
            # paho surfaces connection failures as OSError/socket errors. Don't
            # leak host/credentials into the message.
            raise MqttError(f"MQTT publish failed: {type(exc).__name__}") from exc
        finally:
            _teardown(client)


def _wait_for_publish(infos: list[object]) -> None:
    """Block until each QoS-1 publish is acknowledged (best effort).

    paho returns an ``MQTTMessageInfo`` with ``wait_for_publish``; a fake test
    client may return anything, so we only wait when the method is present.
    """
    for info in infos:
        wait = getattr(info, "wait_for_publish", None)
        if callable(wait):
            wait(timeout=_PUBLISH_TIMEOUT_SECONDS)


def _teardown(client: MqttClient) -> None:
    """Stop the network loop and disconnect; never mask the real error.

    Teardown is best-effort: a failure here (e.g. the loop was never started
    because connect raised) is logged at debug and swallowed so it can't shadow
    the original exception propagating out of the ``finally``.
    """
    for step in ("loop_stop", "disconnect"):
        try:
            getattr(client, step)()
        except Exception:  # noqa: BLE001 — teardown must not mask the real error
            log.debug("MQTT %s during teardown failed", step, exc_info=True)


def publish_snapshot(
    session: Session, settings: Settings, *, client: MqttClient | None = None
) -> int:
    """Collect the household metrics and push them to HA. Returns the sensor count.

    The single entry point shared by the worker (periodic) and the dashboard's
    "Publish now" button.
    """
    metrics = collect_metrics(session)
    MqttPublisher.from_settings(settings, client=client).publish_metrics(metrics)

    return len(metrics)
