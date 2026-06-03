"""Home Assistant integration (design §9).

A one-directional, opt-in push of glanceable household metrics to Home Assistant
over MQTT: the app publishes auto-discovered sensors (net worth, monthly spend,
per-account balances) and one-off alerts. HA never writes back.

OFF by default — with no ``EA_MQTT_HOST`` the app makes zero MQTT connections.
The broker is your HA broker on the LAN, so this is **not** internet egress
(unlike the myFund pull); it stays consistent with the local-only principle.

Layers:
- :mod:`expense_analyzer.ha.metrics` — gather metrics from the query layer (pure
  over the DB; converts minor units to a display decimal here, at the edge).
- :mod:`expense_analyzer.ha.discovery` — MQTT topic layout and HA discovery
  payloads (pure, no MQTT/DB).
- :mod:`expense_analyzer.ha.mqtt` — the publisher that connects and pushes.
"""
