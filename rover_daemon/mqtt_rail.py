"""Publish ESP MQTT commands (TLS + username/password; no client certs).

Credentials come from BLE WIFI_CONFIG (``mqtt.json``) or ``ROVER_MQTT_*`` env
overrides. Every ESP command must end with ``wake_secret``:

    NACK BAD_SECRET

Examples (secret last):

    ON GPIO16 <wake_secret>
    OFF GPIO16 <wake_secret>
    OFF GPIO16 DELAY 15 <wake_secret>
    PING <wake_secret>
    FACTORY_RESET <wake_secret>
"""

from __future__ import annotations

import logging
import ssl
import time
from typing import Optional

from . import mqtt_store
from .mqtt_store import MqttCreds

log = logging.getLogger("rover.mqtt")


def command_topic(mqtt_user: str, topic_override: str = "") -> str:
    override = (topic_override or "").strip()
    if override:
        return override
    user = (mqtt_user or "").strip()
    if not user:
        raise ValueError("mqtt_user or topic_cmd required for ESP MQTT")
    # Prefer BLE topics; fallback tenants/<mqtt_user>/cmd.
    return f"tenants/{user}/cmd"


# Back-compat name used by older call sites / docs.
rail_off_topic = command_topic


def publish_esp_command(
    creds: MqttCreds,
    command: str,
    *,
    retain: bool = False,
    qos: int = 1,
    timeout_s: float = 8.0,
) -> bool:
    """Publish ``command`` with wake_secret as the last token. Returns True on success.

    Connects with TLS (mqtts) using ``mqtt_user`` / ``mqtt_pass``. No client
    certificate or key files. Host from ``mqtt_host`` or parsed ``mqtt_url``.
    """
    try:
        body = mqtt_store.esp_command(command, creds)
    except ValueError as exc:
        log.error("MQTT ESP command rejected: %s", exc)
        return False

    host = creds.resolved_host()
    if not host:
        log.error(
            "MQTT host not set — provision via BLE WIFI_CONFIG "
            "(mqtt_host / mqtt_url) or ROVER_MQTT_HOST / ROVER_MQTT_URL"
        )
        return False
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        log.error("paho-mqtt not installed: %s", exc)
        return False

    try:
        full_topic = command_topic(creds.mqtt_user, creds.topic_cmd)
    except ValueError as exc:
        log.error("%s", exc)
        return False

    port = creds.resolved_port()
    use_tls = creds.use_tls()
    safe = mqtt_store.redact_command(body, creds.wake_secret)
    ok = False
    err: Optional[BaseException] = None

    def on_connect(client, _userdata, _flags, reason_code, _properties=None):
        nonlocal ok, err
        failed = getattr(reason_code, "is_failure", None)
        if failed is True:
            err = RuntimeError(f"MQTT connect failed rc={reason_code}")
            return
        if failed is None:
            try:
                rc = int(reason_code) if reason_code is not None else 1
            except (TypeError, ValueError):
                rc = 0 if str(reason_code) in ("Success", "0") else 1
            if rc != 0:
                err = RuntimeError(f"MQTT connect failed rc={reason_code}")
                return
        info = client.publish(full_topic, body, qos=qos, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            err = RuntimeError(f"MQTT publish failed rc={info.rc}")

    def on_publish(_client, _userdata, _mid, *_a, **_k):
        nonlocal ok
        ok = True

    try:
        from paho.mqtt.enums import CallbackAPIVersion

        client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=f"rover-pi-mqtt-{int(time.time()) % 100000}",
        )
    except Exception:
        client = mqtt.Client(client_id=f"rover-pi-mqtt-{int(time.time()) % 100000}")

    # Password auth only — never load client cert / key files.
    if creds.mqtt_user:
        client.username_pw_set(creds.mqtt_user, creds.mqtt_pass or None)
    if use_tls:
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        client.tls_insecure_set(False)
    client.on_connect = on_connect
    client.on_publish = on_publish

    try:
        log.info(
            "MQTT ESP cmd connect %s:%s tls=%s auth=password topic=%s payload=%r",
            host,
            port,
            use_tls,
            full_topic,
            safe,
        )
        client.connect_timeout = max(1.0, min(float(timeout_s), 10.0))
        client.connect(host, int(port), keepalive=30)
        client.loop_start()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and not ok and err is None:
            time.sleep(0.05)
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:
            pass
    except Exception as exc:
        log.error("MQTT ESP cmd error: %s", exc)
        return False

    if err is not None:
        log.error("MQTT ESP cmd failed: %s", err)
        return False
    if not ok:
        log.warning(
            "MQTT ESP cmd publish not confirmed within %.1fs — assuming sent (%s %r)",
            timeout_s,
            full_topic,
            safe,
        )
        return True

    log.info("MQTT ESP cmd published: %s %r", full_topic, safe)
    return True


def publish_rail_off(
    creds: MqttCreds,
    *,
    retain: bool = False,
    qos: int = 1,
    timeout_s: float = 8.0,
) -> bool:
    """Publish ``OFF GPIO16 DELAY <sec> <wake_secret>`` (shutdown / factory)."""
    try:
        body = mqtt_store.rail_off_payload(creds)
    except ValueError as exc:
        log.error("MQTT rail-off rejected: %s", exc)
        return False
    # Body already includes secret; strip for esp_command re-append (idempotent).
    secret = (creds.wake_secret or "").strip()
    base = body[: -len(secret) - 1] if secret and body.endswith(secret) else body
    return publish_esp_command(
        creds,
        base,
        retain=retain,
        qos=qos,
        timeout_s=timeout_s,
    )
