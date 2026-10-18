"""Persist MQTT + wake-secret credentials from BLE WIFI_CONFIG.

Primary path: phone → ESP → BLE ``WIFI_CONFIG`` → ``/var/lib/rover/mqtt.json`` (0600).
Env vars (``ROVER_MQTT_*``) are optional overrides, not required provisioning.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("rover.mqtt_store")

DEFAULT_PATH = "/var/lib/rover/mqtt.json"
DEFAULT_RAIL_OFF_DELAY_SEC = 15


@dataclass
class MqttCreds:
    mqtt_host: str = ""
    mqtt_user: str = ""
    mqtt_pass: str = ""
    topic_cmd: str = ""
    topic_status: str = ""
    wake_secret: str = ""
    mqtt_port: int = 0  # 0 = auto (8883+TLS for HiveMQ Cloud, else 1883)
    mqtt_tls: Optional[bool] = None  # None = auto from host/port
    rail_off_delay_sec: int = DEFAULT_RAIL_OFF_DELAY_SEC

    def has_broker(self) -> bool:
        return bool(self.mqtt_host.strip())

    def resolved_port(self) -> int:
        if self.mqtt_port:
            return int(self.mqtt_port)
        if self.use_tls():
            return 8883
        return 1883

    def use_tls(self) -> bool:
        if self.mqtt_tls is not None:
            return bool(self.mqtt_tls)
        host = self.mqtt_host.lower()
        if self.mqtt_port == 8883:
            return True
        if "hivemq.cloud" in host or host.endswith(".hivemq.cloud"):
            return True
        return False


def store_path(path: str = "") -> Path:
    return Path(path or os.environ.get("ROVER_MQTT_STORE_PATH") or DEFAULT_PATH)


def load_file(path: str = "") -> MqttCreds:
    p = store_path(path)
    if not p.is_file():
        return MqttCreds()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("failed to read %s: %s", p, exc)
        return MqttCreds()
    if not isinstance(data, dict):
        return MqttCreds()
    try:
        port_raw = data.get("mqtt_port", 0)
        port_i = int(port_raw) if port_raw not in (None, "") else 0
    except (TypeError, ValueError):
        port_i = 0
    try:
        delay_i = int(data.get("rail_off_delay_sec", DEFAULT_RAIL_OFF_DELAY_SEC))
    except (TypeError, ValueError):
        delay_i = DEFAULT_RAIL_OFF_DELAY_SEC
    tls_val = data.get("mqtt_tls", None)
    if isinstance(tls_val, str):
        tls_opt: Optional[bool] = tls_val.strip().lower() in ("1", "true", "yes")
    elif tls_val is None:
        tls_opt = None
    else:
        tls_opt = bool(tls_val)
    return MqttCreds(
        mqtt_host=str(data.get("mqtt_host") or "").strip(),
        mqtt_user=str(data.get("mqtt_user") or "").strip(),
        mqtt_pass=str(data.get("mqtt_pass") or ""),
        topic_cmd=str(data.get("topic_cmd") or "").strip(),
        topic_status=str(data.get("topic_status") or "").strip(),
        wake_secret=str(data.get("wake_secret") or "").strip(),
        mqtt_port=port_i,
        mqtt_tls=tls_opt,
        rail_off_delay_sec=max(1, delay_i),
    )


def save_file(creds: MqttCreds, path: str = "", *, dry_run: bool = False) -> bool:
    """Atomically write mqtt.json with mode 0600."""
    p = store_path(path)
    payload = {
        "mqtt_host": creds.mqtt_host,
        "mqtt_user": creds.mqtt_user,
        "mqtt_pass": creds.mqtt_pass,
        "topic_cmd": creds.topic_cmd,
        "topic_status": creds.topic_status,
        "wake_secret": creds.wake_secret,
        "mqtt_port": creds.mqtt_port,
        "rail_off_delay_sec": creds.rail_off_delay_sec,
    }
    if creds.mqtt_tls is not None:
        payload["mqtt_tls"] = bool(creds.mqtt_tls)
    if dry_run:
        log.info("dry_run: would persist MQTT creds → %s (host=%s)", p, creds.mqtt_host)
        return True
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".mqtt.", suffix=".tmp", dir=str(p.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, p)
            os.chmod(p, 0o600)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        log.info(
            "persisted MQTT creds → %s (host=%s topic_cmd=%s)",
            p,
            creds.mqtt_host,
            creds.topic_cmd or (f"rover/{creds.mqtt_user}/cmd" if creds.mqtt_user else ""),
        )
        return True
    except Exception as exc:
        log.error("failed to persist MQTT creds to %s: %s", p, exc)
        return False


def from_wifi_config_fields(
    *,
    mqtt_host: str = "",
    mqtt_user: str = "",
    mqtt_pass: str = "",
    topic_cmd: str = "",
    topic_status: str = "",
    wake_secret: str = "",
    base: Optional[MqttCreds] = None,
) -> MqttCreds:
    """Merge WIFI_CONFIG MQTT fields onto existing creds (non-empty wins)."""
    cur = base or MqttCreds()
    return MqttCreds(
        mqtt_host=(mqtt_host or "").strip() or cur.mqtt_host,
        mqtt_user=(mqtt_user or "").strip() or cur.mqtt_user,
        mqtt_pass=mqtt_pass if (mqtt_pass or "").strip() else cur.mqtt_pass,
        topic_cmd=(topic_cmd or "").strip() or cur.topic_cmd,
        topic_status=(topic_status or "").strip() or cur.topic_status,
        wake_secret=(wake_secret or "").strip() or cur.wake_secret,
        mqtt_port=cur.mqtt_port,
        mqtt_tls=cur.mqtt_tls,
        rail_off_delay_sec=cur.rail_off_delay_sec,
    )


def apply_env_overrides(creds: MqttCreds) -> MqttCreds:
    """Non-empty ``ROVER_MQTT_*`` env vars override persisted values."""
    host = os.environ.get("ROVER_MQTT_HOST", "").strip() or creds.mqtt_host
    user = os.environ.get("ROVER_MQTT_USER", "").strip() or creds.mqtt_user
    if os.environ.get("ROVER_MQTT_PASS", "") != "":
        password = os.environ["ROVER_MQTT_PASS"]
    else:
        password = creds.mqtt_pass
    topic = os.environ.get("ROVER_MQTT_TOPIC_CMD", "").strip() or creds.topic_cmd
    topic_status = (
        os.environ.get("ROVER_MQTT_TOPIC_STATUS", "").strip() or creds.topic_status
    )
    wake = os.environ.get("ROVER_MQTT_WAKE_SECRET", "").strip() or creds.wake_secret
    port = creds.mqtt_port
    port_s = os.environ.get("ROVER_MQTT_PORT", "").strip()
    if port_s:
        try:
            port = int(port_s)
        except ValueError:
            pass
    tls = creds.mqtt_tls
    tls_s = os.environ.get("ROVER_MQTT_TLS", "").strip().lower()
    if tls_s in ("1", "true", "yes"):
        tls = True
    elif tls_s in ("0", "false", "no"):
        tls = False
    delay = creds.rail_off_delay_sec
    delay_s = os.environ.get("ROVER_MQTT_RAIL_OFF_DELAY", "").strip()
    if delay_s:
        try:
            delay = max(1, int(delay_s))
        except ValueError:
            pass
    return MqttCreds(
        mqtt_host=host,
        mqtt_user=user,
        mqtt_pass=password,
        topic_cmd=topic,
        topic_status=topic_status,
        wake_secret=wake,
        mqtt_port=port,
        mqtt_tls=tls,
        rail_off_delay_sec=delay,
    )


def load_effective(path: str = "") -> MqttCreds:
    """Persisted file, then optional env overrides."""
    return apply_env_overrides(load_file(path))


def rail_off_payload(creds: MqttCreds) -> str:
    """Build ``OFF GPIO16 DELAY <sec> <wake_secret>`` (or env full override)."""
    override = os.environ.get("ROVER_MQTT_RAIL_OFF", "").strip()
    # Treat bare legacy default as unset so DELAY+secret form is used.
    if override and override != "OFF GPIO16":
        return override
    delay = max(1, int(creds.rail_off_delay_sec or DEFAULT_RAIL_OFF_DELAY_SEC))
    secret = (creds.wake_secret or "").strip()
    if secret:
        return f"OFF GPIO16 DELAY {delay} {secret}"
    return f"OFF GPIO16 DELAY {delay}"


def redact(creds: MqttCreds) -> dict[str, Any]:
    d = asdict(creds)
    if d.get("mqtt_pass"):
        d["mqtt_pass"] = "***"
    if d.get("wake_secret"):
        d["wake_secret"] = "***"
    return d
