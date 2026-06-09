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
import secrets
import time
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

        # Unique client_id per connection. The web app ("Publish now") and the
        # worker (periodic) both publish; an MQTT broker disconnects a duplicate
        # client_id, so a fixed id would make the two flap when they overlap.
        client_id = f"expense-analyzer-{secrets.token_hex(4)}"

        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)

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

    def publish_update(
        self, *, current: str | None, latest: str | None, update_available: bool
    ) -> None:
        """Publish the retained "update available" sensor (Phase 18).

        Mirrors :meth:`publish_metrics` (availability + retained discovery +
        retained state) but on its own ``<base>/update`` topic, so it never
        clobbers the money state doc. Published every check so the sensor always
        reflects reality (``update_available`` flips back to false once deployed).
        """
        avail = discovery.availability_topic(self._base_topic)

        def body(client: MqttClient) -> list[object]:
            return [
                client.publish(avail, discovery.ONLINE, qos=_QOS, retain=True),
                client.publish(
                    discovery.discovery_topic(self._discovery_prefix, self._base_topic, "update"),
                    json.dumps(discovery.update_sensor_config(base=self._base_topic)),
                    qos=_QOS,
                    retain=True,
                ),
                client.publish(
                    discovery.update_topic(self._base_topic),
                    discovery.update_payload(
                        current=current, latest=latest, update_available=update_available
                    ),
                    qos=_QOS,
                    retain=True,
                ),
            ]

        self._with_connection(body, will=(avail, discovery.OFFLINE))

    def publish_plan(self, *, paid: int, total: int, overdue: int) -> None:
        """Publish the retained monthly-plan progress sensor (Phase 19c).

        Mirrors :meth:`publish_update` (availability + retained discovery + retained
        state) on its own ``<base>/plan`` topic, so it never clobbers the money
        state doc. Published every cycle so the progress reflects the current month.
        """
        avail = discovery.availability_topic(self._base_topic)

        def body(client: MqttClient) -> list[object]:
            return [
                client.publish(avail, discovery.ONLINE, qos=_QOS, retain=True),
                client.publish(
                    discovery.discovery_topic(self._discovery_prefix, self._base_topic, "plan"),
                    json.dumps(discovery.plan_sensor_config(base=self._base_topic)),
                    qos=_QOS,
                    retain=True,
                ),
                client.publish(
                    discovery.plan_topic(self._base_topic),
                    discovery.plan_payload(paid=paid, total=total, overdue=overdue),
                    qos=_QOS,
                    retain=True,
                ),
            ]

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
        except (OSError, RuntimeError, ValueError) as exc:
            # paho surfaces failures as OSError (connect/socket), RuntimeError
            # (publish/wait_for_publish on a rejected message, e.g. bad creds) or
            # ValueError (bad args). Wrap them all so the "Publish now" handler
            # shows a flash instead of a 500. Don't leak host/credentials.
            raise MqttError(f"MQTT publish failed: {type(exc).__name__}") from exc
        finally:
            _teardown(client)


def _wait_for_publish(infos: list[object]) -> None:
    """Block until each QoS-1 publish is acknowledged, under one shared deadline.

    A single ``_PUBLISH_TIMEOUT_SECONDS`` budget across all messages — not per
    message — so a stuck broker can't amplify the wait to N×timeout. paho returns
    an ``MQTTMessageInfo`` with ``wait_for_publish``; a fake test client may return
    anything, so we only wait when the method is present. A rejected publish makes
    ``wait_for_publish`` raise ``RuntimeError``, which propagates to
    :meth:`MqttPublisher._with_connection` and becomes an :class:`MqttError`.
    """
    deadline = time.monotonic() + _PUBLISH_TIMEOUT_SECONDS
    for info in infos:
        wait = getattr(info, "wait_for_publish", None)
        if not callable(wait):
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        wait(timeout=remaining)


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
    "Publish now" button. After the metrics, fires one alert per over-budget
    category (Phase 8), per newly-detected subscription, and per subscription
    whose price went up (Phase 9) — the wiring of the Phase 7
    :meth:`MqttPublisher.publish_alert` primitive.

    Alerts are fired statelessly every cycle: per design §9 the app emits the
    event and an HA automation decides when to actually notify (HA's throttle /
    "fire once" semantics live there, not here). A household has few categories
    and subscriptions, so this is a handful of small events at most.
    """
    from expense_analyzer.clock import local_today
    from expense_analyzer.config import get_settings
    from expense_analyzer.money import format_pln
    from expense_analyzer.queries import budgets as budget_queries
    from expense_analyzer.queries import planned as planned_queries
    from expense_analyzer.queries import subscriptions as subscription_queries

    metrics = collect_metrics(session)
    publisher = MqttPublisher.from_settings(settings, client=client)
    publisher.publish_metrics(metrics)

    today = local_today()
    month = today.strftime("%Y-%m")

    # Monthly plan progress sensor + overdue alert (Phase 19c). The plan sensor is
    # its own retained topic; the alert is fired statelessly every cycle (HA's
    # throttle decides when to notify), like the budget/subscription alerts below.
    plan = planned_queries.plan_overview(session, month, today=today)
    paid = sum(1 for row in plan.rows if row.paid)
    overdue = sum(1 for row in plan.rows if row.overdue)
    publisher.publish_plan(paid=paid, total=len(plan.rows), overdue=overdue)
    if overdue:
        publisher.publish_alert(
            title="Bills overdue",
            message=(
                f"{overdue} planned bill(s) overdue this month; "
                f"{format_pln(plan.left_to_pay)} still to pay."
            ),
            severity="warning",
        )
    for status in budget_queries.budget_overview(session, month):
        if status.over:
            publisher.publish_alert(
                title=f"Budget exceeded: {status.name}",
                message=(
                    f"Spent {format_pln(status.spent)} of "
                    f"{format_pln(status.limit_amount)} on {status.name} this month "
                    f"({format_pln(-status.remaining)} over)."
                ),
                severity="warning",
            )

    # Subscription alerts (Phase 9). Dismissed false positives never alert; a
    # "new" alert stops once the user confirms the subscription (acknowledged).
    for view in subscription_queries.subscription_overview(session, get_settings(), today=today):
        if view.is_dismissed:
            continue
        sub = view.detected
        if sub.price_rise is not None:
            publisher.publish_alert(
                title=f"Subscription price went up: {sub.merchant}",
                message=(
                    f"{sub.merchant} now charges {format_pln(sub.price_rise.new_amount)} "
                    f"(was {format_pln(sub.price_rise.old_amount)}, "
                    f"+{sub.price_rise.increase_pct}%)."
                ),
                severity="warning",
            )
        if sub.is_new and not view.is_confirmed:
            publisher.publish_alert(
                title=f"New subscription detected: {sub.merchant}",
                message=(
                    f"{sub.merchant} looks like a {sub.cadence} subscription of "
                    f"{format_pln(sub.current_amount)}."
                ),
                severity="info",
            )

    return len(metrics)
