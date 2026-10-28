"""Publish Pi lifecycle on ``tenants/<user>/status`` at QoS 1.

Every payload starts with ``PI`` so an iOS client can ignore ESP32 traffic on
the same topic. The second token is the event. Further tokens are ``key=value``
and never contain spaces.

    PI boot_start boot_id=… ts=… boot_ts=… uptime_s=… device=…
    PI boot_finished boot_id=… ts=… boot_ts=… uptime_s=… daemon=active …
    PI shutdown boot_id=… ts=… boot_ts=… uptime_s=… reason=idle|manual|factory|reboot|halt
    PI heartbeat boot_id=… ts=… boot_ts=… uptime_s=… seq=… phase=boot|up

``boot_ts`` is the kernel boot time (unix seconds), not the publish time, so a
late MQTT connect still tells the phone when power-on started. Messages are
not retained: a retained heartbeat would erase a queued boot or shutdown.
QoS 1 queues them for a persistent iOS session (clean session off, subscribe
QoS 1). Heartbeats are not a last-will: a broker disconnect would otherwise
mark the Pi offline during a short link drop.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

from . import mqtt_store
from .mqtt_store import MqttCreds

log = logging.getLogger("rover.pi_status")

RUN_DIR = Path("/run/rover")
BOOT_START_SENT = RUN_DIR / "pi-boot-start-sent"
BOOT_FINISHED_REQ = RUN_DIR / "pi-boot-finished.req"
BOOT_FINISHED_SENT = RUN_DIR / "pi-boot-finished-sent"
SHUTDOWN_REQ = RUN_DIR / "pi-shutdown.req"
SHUTDOWN_SENT = RUN_DIR / "pi-shutdown-sent"

# Camera / control stack must be up for boot_finished. Daemon needs Wi-Fi and
# used to hold cam=ready ~10s after the stream was already live — report it
# but do not block on it.
STACK_UNITS = (
    "rover-mediamtx",
    "rover-control",
    "rover-watch",
)
OPTIONAL_STACK_UNITS = (
    "rover-daemon",
)
STACK_WAIT_SEC = 90.0
HEARTBEAT_SEC = 5.0
PUBLISH_TIMEOUT_SEC = 8.0
MEDIAMTX_API = "http://127.0.0.1:9997/v3/paths/get/cam"

_TOKEN_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


def format_message(event: str, **fields: object) -> str:
    """``PI <event> key=value…`` — event and values are single tokens."""
    name = _token(event) or "heartbeat"
    parts = ["PI", name]
    for key, value in fields.items():
        k = _token(key)
        if not k:
            continue
        parts.append(f"{k}={_token(value)}")
    return " ".join(parts)


def normalize_reason(text: str) -> str:
    """Map a shutdown note to a single reason token."""
    raw = (text or "").strip().lower()
    if "idle" in raw:
        return "idle"
    if "factory" in raw:
        return "factory"
    if "reboot" in raw:
        return "reboot"
    if any(
        part in raw
        for part in ("manual", "shutdown requested", "watch_shutdown", "host_poweroff")
    ):
        return "manual"
    first = raw.split()[0] if raw else ""
    token = re.sub(r"[^a-z0-9_]+", "", first)
    return (token[:32] or "shutdown")


def _token(value: object) -> str:
    return _TOKEN_RE.sub("", str(value).strip().replace(" ", "_"))


def _ensure_run_dir() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def _uptime() -> float:
    try:
        return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, IndexError, ValueError):
        return 0.0


def _boot_ts() -> int:
    """Wall-clock unix second of kernel boot.

    ``time.time() - uptime`` stays correct across an NTP step. ``btime`` in
    ``/proc/stat`` does not, so a message sent before time sync would pin the
    phone to a boot time several minutes off.
    """
    return int(time.time() - _uptime())


def _uptime_s() -> int:
    return int(_uptime())


def _common_fields(creds: Optional[MqttCreds] = None) -> dict[str, object]:
    fields: dict[str, object] = {
        "boot_id": _boot_id(),
        "ts": int(time.time() * 1000),
        "boot_ts": _boot_ts(),
        "uptime_s": _uptime_s(),
    }
    device = (creds.device_id if creds else "") or ""
    if device.strip():
        fields["device"] = device.strip()
    return fields


def _interval_s() -> float:
    raw = os.environ.get("ROVER_PI_STATUS_INTERVAL", "").strip()
    if not raw:
        return HEARTBEAT_SEC
    try:
        return max(1.0, float(raw))
    except ValueError:
        return HEARTBEAT_SEC


def _systemctl(*args: str, timeout: float = 5.0) -> str:
    try:
        proc = subprocess.run(
            ["systemctl", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "").strip()


def _unit_loaded(name: str) -> bool:
    return _systemctl("show", "-p", "LoadState", "--value", f"{name}.service") == "loaded"


def _unit_active(name: str) -> str:
    state = _systemctl("show", "-p", "ActiveState", "--value", f"{name}.service")
    return state or "unknown"


def _control_port() -> int:
    raw = os.environ.get("ROVER_CONTROL_PORT", "").strip()
    if raw.isdigit():
        return int(raw)
    env_path = Path(
        os.environ.get("ROVER_SERVER_ENV", "/opt/rover-server/env/rover-server.env")
    )
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("PORT="):
                port = line.split("=", 1)[1].strip()
                if port.isdigit():
                    return int(port)
    except OSError:
        pass
    return 3000


def _tcp_open(port: int) -> bool:
    import socket

    sock = socket.socket()
    sock.settimeout(0.4)
    try:
        sock.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _cam_ready() -> bool:
    """True when MediaMTX is serving the camera, not merely when the unit forked."""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(MEDIAMTX_API, timeout=1.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False
    return bool(isinstance(data, dict) and data.get("ready"))


def stack_states() -> dict[str, str]:
    """ActiveState for required + optional core units. Missing units omitted."""
    states: dict[str, str] = {}
    for name in (*STACK_UNITS, *OPTIONAL_STACK_UNITS):
        if _unit_loaded(name):
            states[name.removeprefix("rover-")] = _unit_active(name)
    return states


def wait_for_stack(timeout_s: float = STACK_WAIT_SEC) -> dict[str, str]:
    """Block until the camera stack is actually up, or the timeout hits.

    ``systemctl is-active`` flips true as soon as a Type=simple process is
    forked. Waiting for the camera path and the control port keeps
    ``PI boot_finished`` aligned with a live stream — not with Wi-Fi/daemon.
    """
    deadline = time.monotonic() + max(0.0, timeout_s)
    want_cam = _unit_loaded("rover-mediamtx")
    want_control = _unit_loaded("rover-control")
    required = [name.removeprefix("rover-") for name in STACK_UNITS if _unit_loaded(name)]
    port = _control_port()
    while True:
        states = stack_states()
        units_ok = all(states.get(name) == "active" for name in required)
        cam_ok = _cam_ready() if want_cam else True
        control_ok = _tcp_open(port) if want_control else True
        if units_ok and cam_ok and control_ok:
            if want_cam:
                states["cam"] = "ready"
            if want_control:
                states["control_port"] = "open"
            return states
        if time.monotonic() >= deadline:
            if want_cam:
                states["cam"] = "ready" if cam_ok else "down"
            if want_control:
                states["control_port"] = "open" if control_ok else "down"
            return states
        time.sleep(0.5)


def system_is_stopping() -> bool:
    return _systemctl("is-system-running", timeout=3) == "stopping"


def stopping_reason() -> str:
    jobs = _systemctl("list-jobs", "--no-legend", timeout=3)
    if "reboot.target" in jobs:
        return "reboot"
    return "halt"


def _committed(path: Path) -> bool:
    """True only after a payload was acked. A pending claim is not committed."""
    try:
        return path.read_text(encoding="utf-8").startswith("PI ")
    except OSError:
        return False


def _claim(path: Path) -> bool:
    """Create ``path`` exclusively. False if another publisher holds it."""
    _ensure_run_dir()
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    try:
        os.write(fd, b"pending\n")
    finally:
        os.close(fd)
    return True


def _commit(path: Path, payload: str) -> None:
    path.write_text(payload + "\n", encoding="utf-8")


def _release(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


class _MqttSession:
    """One TLS session. Caller publishes; QoS 1 waits for PUBACK."""

    def __init__(self) -> None:
        self._client = None
        self._connected = False
        self._connect_err: Optional[BaseException] = None

    def connect(self, creds: MqttCreds, *, client_id: str, timeout_s: float) -> bool:
        self.close()
        host = creds.resolved_host()
        topic = (creds.topic_status or "").strip()
        if not host or not topic:
            log.error("PI status missing mqtt host or topic_status")
            return False
        from ._vendor_path import ensure_vendor_path

        ensure_vendor_path()
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            log.error("paho-mqtt not installed: %s", exc)
            return False

        self._connected = False
        self._connect_err = None

        def on_connect(_client, _userdata, _flags, reason_code, _properties=None):
            failed = getattr(reason_code, "is_failure", None)
            if failed is True:
                self._connect_err = RuntimeError(f"MQTT connect failed rc={reason_code}")
                return
            if failed is None:
                try:
                    rc = int(reason_code) if reason_code is not None else 1
                except (TypeError, ValueError):
                    rc = 0 if str(reason_code) in ("Success", "0") else 1
                if rc != 0:
                    self._connect_err = RuntimeError(
                        f"MQTT connect failed rc={reason_code}"
                    )
                    return
            self._connected = True

        def on_disconnect(_client, _userdata, _flags, reason_code=None, _properties=None):
            # paho v1 passes (client, userdata, rc). Accept both.
            self._connected = False

        try:
            from paho.mqtt.enums import CallbackAPIVersion

            client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=client_id)
        except Exception:
            client = mqtt.Client(client_id=client_id)

        if creds.mqtt_user:
            client.username_pw_set(creds.mqtt_user, creds.mqtt_pass or None)
        if creds.use_tls():
            import ssl

            client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
            client.tls_insecure_set(False)
        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        try:
            client.connect_timeout = max(1.0, min(float(timeout_s), 10.0))
            client.connect(host, int(creds.resolved_port()), keepalive=20)
            client.loop_start()
        except Exception as exc:
            log.error("PI status connect %s:%s failed: %s", host, creds.resolved_port(), exc)
            return False

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and not self._connected and self._connect_err is None:
            time.sleep(0.05)
        if self._connect_err is not None or not self._connected:
            log.error("PI status connect failed: %s", self._connect_err or "timeout")
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
            return False
        self._client = client
        return True

    @property
    def connected(self) -> bool:
        return self._connected and self._client is not None

    def publish(self, topic: str, payload: str, *, timeout_s: float) -> bool:
        if not self.connected or self._client is None:
            return False
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            return False
        info = self._client.publish(topic, payload, qos=1, retain=False)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            log.error("PI status publish rejected rc=%s payload=%r", info.rc, payload)
            self._connected = False
            return False
        try:
            info.wait_for_publish(timeout=timeout_s)
        except Exception as exc:
            log.error("PI status publish wait failed: %s", exc)
            self._connected = False
            return False
        if not info.is_published():
            log.error("PI status publish not acked payload=%r", payload)
            self._connected = False
            return False
        return True

    def close(self) -> None:
        client = self._client
        self._client = None
        self._connected = False
        if client is None:
            return
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass


def _publish_once(
    creds: MqttCreds,
    payload: str,
    *,
    client_id: str,
    timeout_s: float = PUBLISH_TIMEOUT_SEC,
) -> bool:
    if os.environ.get("ROVER_DRY_RUN", "").strip() in ("1", "true", "yes"):
        log.info("dry_run: would publish %s %r", creds.topic_status, payload)
        return True
    session = _MqttSession()
    try:
        if not session.connect(creds, client_id=client_id, timeout_s=timeout_s):
            return False
        ok = session.publish(creds.topic_status, payload, timeout_s=timeout_s)
        if ok:
            log.info("PI status %s", payload)
        return ok
    finally:
        session.close()


def _client_id(prefix: str) -> str:
    return f"{prefix}-{os.getpid()}-{int(time.time()) % 100000}"


def publish_boot_start(creds: Optional[MqttCreds] = None) -> bool:
    if _committed(BOOT_START_SENT):
        return True
    creds = creds or mqtt_store.load_effective()
    payload = format_message("boot_start", **_common_fields(creds))
    if not _claim(BOOT_START_SENT):
        return _committed(BOOT_START_SENT)
    if _publish_once(creds, payload, client_id=_client_id("rover-pi-boot")):
        _commit(BOOT_START_SENT, payload)
        return True
    _release(BOOT_START_SENT)
    return False


def publish_boot_finished(
    states: Optional[dict[str, str]] = None,
    creds: Optional[MqttCreds] = None,
) -> bool:
    if _committed(BOOT_FINISHED_SENT):
        return True
    if not _committed(BOOT_START_SENT):
        # Keep start → finished ordered even if the late unit wins the race.
        if not publish_boot_start(creds):
            return False
    creds = creds or mqtt_store.load_effective()
    fields = _common_fields(creds)
    if states:
        fields.update(states)
    payload = format_message("boot_finished", **fields)
    if not _claim(BOOT_FINISHED_SENT):
        return _committed(BOOT_FINISHED_SENT)
    if _publish_once(creds, payload, client_id=_client_id("rover-pi-ready")):
        _commit(BOOT_FINISHED_SENT, payload)
        return True
    _release(BOOT_FINISHED_SENT)
    return False


def publish_shutdown(reason: str, creds: Optional[MqttCreds] = None) -> bool:
    """Publish ``PI shutdown`` once per boot. True only after PUBACK."""
    if _committed(SHUTDOWN_SENT):
        return True
    creds = creds or mqtt_store.load_effective()
    token = normalize_reason(reason)
    payload = format_message("shutdown", reason=token, **_common_fields(creds))
    if not _claim(SHUTDOWN_SENT):
        return _committed(SHUTDOWN_SENT)
    if _publish_once(creds, payload, client_id=_client_id("rover-pi-down")):
        _commit(SHUTDOWN_SENT, payload)
        return True
    _release(SHUTDOWN_SENT)
    return False


def request_shutdown_status(reason: str, creds: Optional[MqttCreds] = None) -> None:
    """Ask the status service to publish, and publish here if it does not.

    Non-blocking. Called before MQTT rail-off so the phone is told offline
    without adding another TLS handshake to the power-off wait.
    """
    creds = creds or mqtt_store.load_effective()
    token = normalize_reason(reason)
    try:
        _ensure_run_dir()
        SHUTDOWN_REQ.write_text(token + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("could not write %s: %s", SHUTDOWN_REQ, exc)

    def _fallback() -> None:
        deadline = time.monotonic() + PUBLISH_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if _committed(SHUTDOWN_SENT) or publish_shutdown(token, creds):
                return
            time.sleep(0.4)

    threading.Thread(target=_fallback, name="pi-shutdown-status", daemon=True).start()


def mark_boot_finished() -> bool:
    """Late boot hook: wait for the core stack, then queue ``PI boot_finished``."""
    states = wait_for_stack()
    body = " ".join(f"{key}={_token(value)}" for key, value in states.items())
    try:
        _ensure_run_dir()
        BOOT_FINISHED_REQ.write_text(body + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("could not write %s: %s", BOOT_FINISHED_REQ, exc)
    log.info("boot finished queue %s", body or "(no core units)")
    # Publish directly as well, in case the heartbeat service is not up yet.
    return publish_boot_finished(states)


def _parse_state_line(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in (text or "").split():
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key_t = _token(key)
        value_t = _token(value)
        if key_t and value_t:
            fields[key_t] = value_t
    return fields


def run_service() -> None:
    """First boot process: ``PI boot_start``, then a heartbeat every 5s."""
    stop = False

    def _stop(_signum, _frame) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    session = _MqttSession()
    creds = MqttCreds()
    seq = 0
    # The first heartbeat used to go out with boot_start, so the phone saw
    # phase=boot and phase=up one interval apart and treated that as the boot.
    next_heartbeat = time.monotonic() + _interval_s()
    heartbeat_logs = 0
    client_id = _client_id("rover-pi-status")

    while not stop:
        now = time.monotonic()
        fresh = mqtt_store.load_effective()
        if fresh.has_broker() and (fresh.topic_status or "").strip():
            if (
                fresh.resolved_host() != creds.resolved_host()
                or fresh.topic_status != creds.topic_status
                or fresh.mqtt_user != creds.mqtt_user
                or fresh.mqtt_pass != creds.mqtt_pass
            ):
                session.close()
            creds = fresh

        if SHUTDOWN_REQ.is_file() and not _committed(SHUTDOWN_SENT):
            reason = "shutdown"
            try:
                reason = SHUTDOWN_REQ.read_text(encoding="utf-8")
            except OSError:
                pass
            if session.connected or session.connect(
                creds, client_id=client_id, timeout_s=PUBLISH_TIMEOUT_SEC
            ):
                _publish_shutdown_on(session, creds, reason)

        if _committed(SHUTDOWN_SENT):
            # Stay quiet so a heartbeat cannot mark the Pi online again.
            time.sleep(0.5)
            continue

        if not creds.has_broker() or not (creds.topic_status or "").strip():
            time.sleep(2.0)
            continue

        if not session.connected and not session.connect(
            creds, client_id=client_id, timeout_s=PUBLISH_TIMEOUT_SEC
        ):
            time.sleep(2.0)
            continue

        if not _committed(BOOT_START_SENT):
            payload = format_message("boot_start", **_common_fields(creds))
            if _claim(BOOT_START_SENT):
                if session.publish(
                    creds.topic_status, payload, timeout_s=PUBLISH_TIMEOUT_SEC
                ):
                    _commit(BOOT_START_SENT, payload)
                    log.info("PI status %s", payload)
                else:
                    _release(BOOT_START_SENT)
                    session.close()
                    time.sleep(2.0)
                    continue

        if BOOT_FINISHED_REQ.is_file() and not _committed(BOOT_FINISHED_SENT):
            try:
                extra = _parse_state_line(BOOT_FINISHED_REQ.read_text(encoding="utf-8"))
            except OSError:
                extra = {}
            fields = _common_fields(creds)
            fields.update(extra)
            payload = format_message("boot_finished", **fields)
            if _claim(BOOT_FINISHED_SENT):
                if session.publish(
                    creds.topic_status, payload, timeout_s=PUBLISH_TIMEOUT_SEC
                ):
                    _commit(BOOT_FINISHED_SENT, payload)
                    log.info("PI status %s", payload)
                else:
                    _release(BOOT_FINISHED_SENT)
                    session.close()
                    time.sleep(2.0)
                    continue

        if now >= next_heartbeat and _committed(BOOT_START_SENT):
            seq += 1
            phase = "up" if _committed(BOOT_FINISHED_SENT) else "boot"
            payload = format_message(
                "heartbeat",
                seq=seq,
                phase=phase,
                **_common_fields(creds),
            )
            if session.publish(creds.topic_status, payload, timeout_s=PUBLISH_TIMEOUT_SEC):
                heartbeat_logs += 1
                if heartbeat_logs == 1 or heartbeat_logs % 12 == 0:
                    log.info("PI status %s", payload)
                next_heartbeat = time.monotonic() + _interval_s()
            else:
                session.close()
                next_heartbeat = time.monotonic() + 1.0
                time.sleep(1.0)
                continue

        time.sleep(0.2)

    if system_is_stopping() and not _committed(SHUTDOWN_SENT):
        if not session.connected:
            session.connect(creds, client_id=client_id, timeout_s=PUBLISH_TIMEOUT_SEC)
        if session.connected:
            _publish_shutdown_on(session, creds, stopping_reason())
        else:
            publish_shutdown(stopping_reason(), creds)
    session.close()


def _publish_shutdown_on(session: _MqttSession, creds: MqttCreds, reason: str) -> bool:
    token = normalize_reason(reason)
    payload = format_message("shutdown", reason=token, **_common_fields(creds))
    if not _claim(SHUTDOWN_SENT):
        return _committed(SHUTDOWN_SENT)
    if session.publish(creds.topic_status, payload, timeout_s=PUBLISH_TIMEOUT_SEC):
        _commit(SHUTDOWN_SENT, payload)
        log.info("PI status %s", payload)
        return True
    _release(SHUTDOWN_SENT)
    return False


def main(argv: Optional[Iterable[str]] = None) -> int:
    from ._vendor_path import ensure_vendor_path

    ensure_vendor_path()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = list(argv) if argv is not None else []
    # Allow either main(["boot_finished"]) or main() reading sys.argv.
    if argv is None:
        import sys

        args = sys.argv[1:]
    cmd = args[0] if args else "run"
    if cmd == "run":
        run_service()
        return 0
    if cmd == "boot_finished":
        mark_boot_finished()
        return 0
    if cmd == "shutdown":
        reason = args[1] if len(args) > 1 else "halt"
        return 0 if publish_shutdown(reason) else 1
    log.error("usage: python -m rover_daemon.pi_status [run|boot_finished|shutdown [reason]]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
