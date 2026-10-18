"""Publish rail-off to ESP over MQTT before Pi shutdown.

ESP listens on ``topic_cmd`` for
``OFF GPIO16 DELAY 15 <wake_secret>`` so the rail stays up while Linux halts.
"""

from __future__ import annotations

import logging
import ssl
import time
from typing import Optional

log = logging.getLogger("rover.mqtt")


def rail_off_topic(mqtt_user: str, topic_override: str = "") -> str:
    override = (topic_override or "").strip()
    if override:
        return override
    user = (mqtt_user or "").strip()
    if not user:
        raise ValueError("mqtt_user or topic_cmd required for rail-off")
    return f"rover/{user}/cmd"


def publish_rail_off(
    host: str,
    user: str,
    password: str,
    *,
    topic: str = "",
    payload: str = "OFF GPIO16",
    port: int = 1883,
    use_tls: bool = False,
    retain: bool = False,
    qos: int = 1,
    timeout_s: float = 8.0,
) -> bool:
    """Publish rail-off command. Returns True on success."""
    host = (host or "").strip()
    if not host:
        log.error(
            "MQTT host not set — provision via BLE WIFI_CONFIG or ROVER_MQTT_HOST"
        )
        return False
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        log.error("paho-mqtt not installed: %s", exc)
        return False

    try:
        full_topic = rail_off_topic(user, topic)
    except ValueError as exc:
        log.error("%s", exc)
        return False

    body = (payload or "OFF GPIO16").strip() or "OFF GPIO16"
    ok = False
    err: Optional[BaseException] = None

    def on_connect(client, _userdata, _flags, reason_code, _properties=None):
        nonlocal ok, err
        rc = int(reason_code) if reason_code is not None else 1
        failed = getattr(reason_code, "is_failure", None)
        if failed is True or (failed is None and rc != 0):
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
            client_id=f"rover-pi-rail-{int(time.time()) % 100000}",
        )
    except Exception:
        client = mqtt.Client(client_id=f"rover-pi-rail-{int(time.time()) % 100000}")

    if user:
        client.username_pw_set(user, password or None)
    if use_tls:
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        client.tls_insecure_set(False)
    client.on_connect = on_connect
    client.on_publish = on_publish

    try:
        log.info(
            "MQTT rail-off connect %s:%s tls=%s topic=%s",
            host,
            port,
            use_tls,
            full_topic,
        )
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
        log.error("MQTT rail-off error: %s", exc)
        return False

    if err is not None:
        log.error("MQTT rail-off failed: %s", err)
        return False
    if not ok:
        log.warning(
            "MQTT rail-off publish not confirmed within %.1fs — assuming sent (%s)",
            timeout_s,
            full_topic,
        )
        return True

    log.info("MQTT rail-off published: %s", full_topic)
    return True
