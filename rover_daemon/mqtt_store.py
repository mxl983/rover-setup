"""Persist MQTT + device_id + wake-secret from BLE WIFI_CONFIG.

Primary path: phone → ESP → BLE ``WIFI_CONFIG`` → ``/var/lib/rover/mqtt.json``.
Directory ``/var/lib/rover`` mode ``0700``; file mode ``0600``.

Broker auth is TLS + username/password from BLE (no client certs, no Account API).
Topics prefer BLE ``topic_cmd`` / ``topic_status`` (or ``topic_prefix``); else
``tenants/<mqtt_user>/cmd|status``.

Also exported as ``rover_daemon.credential_store`` (spec name).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

log = logging.getLogger("rover.mqtt_store")

DEFAULT_PATH = "/var/lib/rover/mqtt.json"
DEFAULT_DIR_MODE = 0o700
DEFAULT_FILE_MODE = 0o600
# Keep short: this delay is added to every app power-off → next boot E2E time.
DEFAULT_RAIL_OFF_DELAY_SEC = 5
DEFAULT_MQTT_PORT_TLS = 8883
DEFAULT_MQTT_PORT_PLAIN = 1883


def parse_mqtt_url(mqtt_url: str) -> tuple[str, int, Optional[bool]]:
    """Return (host, port, tls_or_None) from mqtt:// or mqtts:// URL."""
    raw = (mqtt_url or "").strip()
    if not raw:
        return "", 0, None
    if "://" not in raw:
        # bare host or host:port
        if ":" in raw and raw.rsplit(":", 1)[-1].isdigit():
            host, port_s = raw.rsplit(":", 1)
            try:
                return host.strip(), int(port_s), None
            except ValueError:
                return raw.strip(), 0, None
        return raw.strip(), 0, None
    u = urlparse(raw)
    host = (u.hostname or "").strip()
    scheme = (u.scheme or "").lower()
    tls: Optional[bool]
    if scheme in ("mqtts", "ssl", "tls"):
        tls = True
        default_port = DEFAULT_MQTT_PORT_TLS
    elif scheme in ("mqtt", "tcp"):
        tls = False
        default_port = DEFAULT_MQTT_PORT_PLAIN
    else:
        tls = None
        default_port = DEFAULT_MQTT_PORT_TLS
    port = int(u.port) if u.port else default_port
    return host, port, tls


def default_topics(account_id: str) -> tuple[str, str]:
    """``tenants/<mqtt_user>/cmd`` + ``…/status``."""
    aid = (account_id or "").strip()
    if not aid:
        return "", ""
    return f"tenants/{aid}/cmd", f"tenants/{aid}/status"


def topics_from_prefix(prefix: str) -> tuple[str, str]:
    p = (prefix or "").strip().rstrip("/")
    if not p:
        return "", ""
    return f"{p}/cmd", f"{p}/status"


def _fill_topics(
    topic_cmd: str,
    topic_status: str,
    account_id: str,
    topic_prefix: str = "",
) -> tuple[str, str]:
    """Prefer explicit topics; else topic_prefix; else tenants/<user>/…."""
    t_cmd = (topic_cmd or "").strip()
    t_stat = (topic_status or "").strip()
    # Migrate legacy HiveMQ-style rover/ topics when account id is known.
    if t_cmd.startswith("rover/") and account_id:
        t_cmd = ""
    if t_stat.startswith("rover/") and account_id:
        t_stat = ""
    if not t_cmd or not t_stat:
        p_cmd, p_stat = topics_from_prefix(topic_prefix)
        t_cmd = t_cmd or p_cmd
        t_stat = t_stat or p_stat
    d_cmd, d_stat = default_topics(account_id)
    return t_cmd or d_cmd, t_stat or d_stat


@dataclass
class MqttCreds:
    mqtt_host: str = ""
    mqtt_user: str = ""  # accountId / broker username
    mqtt_pass: str = ""  # broker password
    topic_cmd: str = ""
    topic_status: str = ""
    wake_secret: str = ""
    device_id: str = ""
    mqtt_port: int = 0  # 0 = auto (8883 mqtts)
    mqtt_tls: Optional[bool] = None  # None = mqtts (True) unless port 1883
    mqtt_url: str = ""  # optional mqtt(s)://… from BLE
    topic_prefix: str = ""  # optional; derives topic_cmd/status when missing
    rail_off_delay_sec: int = DEFAULT_RAIL_OFF_DELAY_SEC

    def has_broker(self) -> bool:
        return bool(self.mqtt_host.strip() or self.mqtt_url.strip())

    def account_id(self) -> str:
        return (self.mqtt_user or "").strip()

    def resolved_port(self) -> int:
        """Port for broker connect (default mqtts 8883)."""
        if self.use_tls():
            port = int(self.mqtt_port) if self.mqtt_port else DEFAULT_MQTT_PORT_TLS
            if port == DEFAULT_MQTT_PORT_PLAIN:
                return DEFAULT_MQTT_PORT_TLS
            return port
        if self.mqtt_port:
            return int(self.mqtt_port)
        return DEFAULT_MQTT_PORT_PLAIN

    def use_tls(self) -> bool:
        if self.mqtt_tls is not None:
            return bool(self.mqtt_tls)
        if self.mqtt_port == DEFAULT_MQTT_PORT_PLAIN:
            return False
        # Self-hosted broker + Tailnet: mqtts by default.
        return True

    def resolved_host(self) -> str:
        host = (self.mqtt_host or "").strip()
        if host:
            return host
        if self.mqtt_url:
            h, _, _ = parse_mqtt_url(self.mqtt_url)
            return h
        return ""


def store_path(path: str = "") -> Path:
    return Path(path or os.environ.get("ROVER_MQTT_STORE_PATH") or DEFAULT_PATH)


def _apply_mqtt_url(
    host: str,
    port: int,
    tls: Optional[bool],
    mqtt_url: str,
) -> tuple[str, int, Optional[bool], str]:
    """Fill host/port/tls gaps from mqtt_url; return (host, port, tls, url)."""
    url = (mqtt_url or "").strip()
    if not url:
        return (host or "").strip(), port, tls, ""
    h, p, t = parse_mqtt_url(url)
    out_host = (host or "").strip() or h
    out_port = port if port else p
    out_tls = tls if tls is not None else t
    return out_host, out_port, out_tls, url


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
    device_id = str(data.get("device_id") or "").strip()
    mqtt_user = str(data.get("mqtt_user") or "").strip()
    mqtt_url = str(data.get("mqtt_url") or "").strip()
    topic_prefix = str(data.get("topic_prefix") or "").strip().rstrip("/")
    host, port_i, tls_opt, mqtt_url = _apply_mqtt_url(
        str(data.get("mqtt_host") or "").strip(),
        port_i,
        tls_opt,
        mqtt_url,
    )
    topic_cmd, topic_status = _fill_topics(
        str(data.get("topic_cmd") or ""),
        str(data.get("topic_status") or ""),
        mqtt_user,
        topic_prefix,
    )
    return MqttCreds(
        mqtt_host=host,
        mqtt_user=mqtt_user,
        mqtt_pass=str(data.get("mqtt_pass") or ""),
        topic_cmd=topic_cmd,
        topic_status=topic_status,
        wake_secret=str(data.get("wake_secret") or "").strip(),
        device_id=device_id,
        mqtt_port=port_i,
        mqtt_tls=tls_opt,
        mqtt_url=mqtt_url,
        topic_prefix=topic_prefix,
        rail_off_delay_sec=max(1, delay_i),
    )


def save_file(creds: MqttCreds, path: str = "", *, dry_run: bool = False) -> bool:
    """Atomically write mqtt.json (file 0600); ensure parent dir 0700."""
    p = store_path(path)
    host, port, tls, mqtt_url = _apply_mqtt_url(
        creds.mqtt_host, creds.mqtt_port, creds.mqtt_tls, creds.mqtt_url
    )
    topic_cmd, topic_status = _fill_topics(
        creds.topic_cmd,
        creds.topic_status,
        creds.account_id(),
        creds.topic_prefix,
    )
    payload: dict[str, Any] = {
        "mqtt_host": host,
        "mqtt_user": creds.mqtt_user,
        "mqtt_pass": creds.mqtt_pass,
        "topic_cmd": topic_cmd,
        "topic_status": topic_status,
        "wake_secret": creds.wake_secret,
        "device_id": creds.device_id,
        "mqtt_port": port,
        "rail_off_delay_sec": creds.rail_off_delay_sec,
    }
    if mqtt_url:
        payload["mqtt_url"] = mqtt_url
    if creds.topic_prefix:
        payload["topic_prefix"] = creds.topic_prefix.strip().rstrip("/")
    if tls is not None:
        payload["mqtt_tls"] = bool(tls)
    if dry_run:
        log.info("dry_run: would persist MQTT creds → %s (host=%s)", p, host)
        return True
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(p.parent, DEFAULT_DIR_MODE)
        except OSError:
            pass
        fd, tmp_name = tempfile.mkstemp(
            prefix=".mqtt.", suffix=".tmp", dir=str(p.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp_name, DEFAULT_FILE_MODE)
            os.replace(tmp_name, p)
            os.chmod(p, DEFAULT_FILE_MODE)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        log.info(
            "persisted MQTT creds → %s (host=%s user=%s topic_cmd=%s)",
            p,
            host,
            creds.account_id() or "-",
            topic_cmd,
        )
        return True
    except Exception as exc:
        log.error("failed to persist MQTT creds to %s: %s", p, exc)
        return False


def from_wifi_config_fields(
    *,
    mqtt_host: str = "",
    mqtt_port: int = 0,
    mqtt_url: str = "",
    mqtt_user: str = "",
    mqtt_pass: str = "",
    topic_prefix: str = "",
    topic_cmd: str = "",
    topic_status: str = "",
    wake_secret: str = "",
    device_id: str = "",
    base: Optional[MqttCreds] = None,
) -> MqttCreds:
    """Merge WIFI_CONFIG MQTT fields onto existing creds (non-empty wins)."""
    cur = base or MqttCreds()
    user = (mqtt_user or "").strip() or cur.mqtt_user
    did = (device_id or "").strip() or cur.device_id
    prefix = (topic_prefix or "").strip().rstrip("/") or cur.topic_prefix
    url = (mqtt_url or "").strip() or cur.mqtt_url
    host = (mqtt_host or "").strip() or cur.mqtt_host
    try:
        port = int(mqtt_port) if mqtt_port else cur.mqtt_port
    except (TypeError, ValueError):
        port = cur.mqtt_port
    tls = cur.mqtt_tls
    host, port, tls, url = _apply_mqtt_url(host, port, tls, url)
    t_cmd, t_stat = _fill_topics(
        (topic_cmd or "").strip() or cur.topic_cmd,
        (topic_status or "").strip() or cur.topic_status,
        user,
        prefix,
    )
    return MqttCreds(
        mqtt_host=host,
        mqtt_user=user,
        mqtt_pass=mqtt_pass if (mqtt_pass or "").strip() else cur.mqtt_pass,
        topic_cmd=t_cmd,
        topic_status=t_stat,
        wake_secret=(wake_secret or "").strip() or cur.wake_secret,
        device_id=did,
        mqtt_port=port,
        mqtt_tls=tls,
        mqtt_url=url,
        topic_prefix=prefix,
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
    prefix = (
        os.environ.get("ROVER_MQTT_TOPIC_PREFIX", "").strip().rstrip("/")
        or creds.topic_prefix
    )
    wake = os.environ.get("ROVER_MQTT_WAKE_SECRET", "").strip() or creds.wake_secret
    url = os.environ.get("ROVER_MQTT_URL", "").strip() or creds.mqtt_url
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
    host, port, tls, url = _apply_mqtt_url(host, port, tls, url)
    delay = creds.rail_off_delay_sec
    delay_s = os.environ.get("ROVER_MQTT_RAIL_OFF_DELAY", "").strip()
    if delay_s:
        try:
            delay = max(1, int(delay_s))
        except ValueError:
            pass
    device_id = (
        os.environ.get("ROVER_DEVICE_ID", "").strip() or creds.device_id
    )
    topic, topic_status = _fill_topics(topic, topic_status, user, prefix)
    return MqttCreds(
        mqtt_host=host,
        mqtt_user=user,
        mqtt_pass=password,
        topic_cmd=topic,
        topic_status=topic_status,
        wake_secret=wake,
        device_id=device_id,
        mqtt_port=port,
        mqtt_tls=tls,
        mqtt_url=url,
        topic_prefix=prefix,
        rail_off_delay_sec=delay,
    )


def load_effective(path: str = "") -> MqttCreds:
    """Persisted file, then optional env overrides."""
    return apply_env_overrides(load_file(path))


def with_wake_secret(command: str, wake_secret: str) -> str:
    """Ensure ``wake_secret`` is the last token (ESP NACK BAD_SECRET otherwise)."""
    body = (command or "").strip()
    secret = (wake_secret or "").strip()
    if not body:
        raise ValueError("empty MQTT command")
    if not secret:
        raise ValueError("wake_secret required for every ESP MQTT command")
    parts = body.split()
    if parts and parts[-1] == secret:
        return body
    return f"{body} {secret}"


def redact_command(command: str, wake_secret: str = "") -> str:
    """Log-safe command string (secret → ***)."""
    body = (command or "").strip()
    secret = (wake_secret or "").strip()
    if secret and body.endswith(secret):
        return body[: -len(secret)] + "***"
    parts = body.split()
    if len(parts) >= 2 and secret and parts[-1] == secret:
        parts[-1] = "***"
        return " ".join(parts)
    # Heuristic: last token looks like an opaque secret — still redact last token
    # when logging known ESP verbs so we never print a stored secret by mistake.
    if parts and parts[0] in {
        "ON",
        "OFF",
        "PING",
        "FACTORY_RESET",
    }:
        parts[-1] = "***"
        return " ".join(parts)
    return body


def esp_command(command: str, creds: MqttCreds) -> str:
    """Build an ESP MQTT command with trailing wake_secret from creds."""
    return with_wake_secret(command, creds.wake_secret)


def rail_off_payload(creds: MqttCreds) -> str:
    """Build ``OFF GPIO16 DELAY <sec> <wake_secret>`` (requires wake_secret)."""
    override = os.environ.get("ROVER_MQTT_RAIL_OFF", "").strip()
    # Treat bare legacy default as unset so DELAY+secret form is used.
    if override and override not in ("OFF GPIO16", "OFF GPIO16 DELAY 15"):
        base = override
    else:
        delay = max(1, int(creds.rail_off_delay_sec or DEFAULT_RAIL_OFF_DELAY_SEC))
        base = f"OFF GPIO16 DELAY {delay}"
    return with_wake_secret(base, creds.wake_secret)


def clear_file(path: str = "", *, dry_run: bool = False) -> bool:
    """Delete mqtt.json (factory reset — wipe wake_secret + broker creds)."""
    p = store_path(path)
    if dry_run:
        log.info("dry_run: would delete MQTT creds %s", p)
        return True
    try:
        if p.is_file():
            p.unlink()
            log.info("deleted MQTT creds %s (wake_secret cleared)", p)
        return True
    except OSError as exc:
        log.error("failed to delete %s: %s", p, exc)
        return False


def redact(creds: MqttCreds) -> dict[str, Any]:
    d = asdict(creds)
    if d.get("mqtt_pass"):
        d["mqtt_pass"] = "***"
    if d.get("wake_secret"):
        d["wake_secret"] = "***"
    if d.get("mqtt_url") and "@" in str(d["mqtt_url"]):
        # Drop any embedded userinfo if present.
        try:
            u = urlparse(str(d["mqtt_url"]))
            if u.password:
                netloc = u.netloc.replace(f":{u.password}", ":***")
                d["mqtt_url"] = u._replace(netloc=netloc).geturl()
        except Exception:
            d["mqtt_url"] = "***"
    return d
