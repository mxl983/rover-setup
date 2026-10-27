"""Deprecated — Account API broker sessions are retired.

MQTT credentials arrive in BLE ``WIFI_CONFIG`` (or ``ROVER_MQTT_*`` overrides).
The Pi connects with TLS + username/password; it does not call
``/v1/auth/signin`` or ``/v1/accounts/provision``.

``parse_mqtt_url`` lives in ``mqtt_store`` for callers that still import here.
"""

from __future__ import annotations

from .mqtt_store import parse_mqtt_url  # noqa: F401

__all__ = ["parse_mqtt_url"]
