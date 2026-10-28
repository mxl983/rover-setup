"""Rover Pi daemon — BLE setup (default) + MQTT rail-off; UART optional legacy."""

from __future__ import annotations

import logging
import os
import threading
import time
from enum import Enum, auto
from typing import Optional

try:
    import serial
except ImportError:  # pragma: no cover - host without pyserial
    serial = None  # type: ignore

from . import mqtt_store, network, protocol
from .config import Config
from .mqtt_store import MqttCreds

log = logging.getLogger("rover.daemon")


class State(Enum):
    DAEMON_INIT = auto()
    IDLE_LISTENING = auto()
    WIPE_NETWORK_PROFILES = auto()
    CONNECT_WIFI = auto()
    FETCH_TAILSCALE_URL = auto()
    AWAITING_SSO_LOGIN = auto()
    NORMAL_OPERATION = auto()
    EXECUTE_FACTORY_RESET = auto()


class RoverDaemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.state = State.DAEMON_INIT
        self._ser: Optional["serial.Serial"] = None
        self._ble = None
        self._rx_buf = ""
        self._stop = threading.Event()
        self._last_heartbeat = 0.0
        self._last_heartbeat_log = 0.0
        self._pending_ssid = ""
        self._pending_pass = ""
        self._auth_url = ""
        self._phone_authkey: Optional[str] = None
        self._lock = threading.Lock()
        self._deferred_msgs: list = []
        self._use_ble = cfg.transport == "ble"
        self._use_uart = cfg.transport == "uart"
        self._mqtt: MqttCreds = mqtt_store.load_effective(cfg.mqtt_store_path)

    # ---------- lifecycle ----------

    def run(self) -> None:
        log.info(
            "rover daemon starting (transport=%s dry_run=%s mqtt=%s)",
            self.cfg.transport,
            self.cfg.dry_run,
            mqtt_store.redact(self._mqtt),
        )

        # Always-on: Tailscale systemd unit + resume if previously joined.
        network.ensure_tailscaled_service(self.cfg.dry_run)

        if self._use_ble:
            self._open_ble()
        if self._use_uart:
            self._open_serial()

        self._enter(State.IDLE_LISTENING)

        # Boot: if already provisioned, bring Tailscale online and go NORMAL.
        # Still listen forever for WIPE_NET / WIFI_CONFIG / WIPE_ALL from ESP.
        if network.wifi_already_connected():
            network.ensure_tailscale_enabled(
                self.cfg.dry_run,
                advertise_tag=self.cfg.tailscale_tag,
            )
            if network.tailscale_is_connected(self.cfg.dry_run):
                log.info("network already up on boot → NORMAL_OPERATION")
                self._enter(State.NORMAL_OPERATION)

        while not self._stop.is_set():
            self._pump_rx()
            while self._deferred_msgs:
                msg = self._deferred_msgs.pop(0)
                log.info("RX (deferred) %s %s", msg.type, msg.redacted_raw())
                self._on_message(msg)
            self._tick()
            time.sleep(0.05)

        self._close_transport()

    def stop(self) -> None:
        self._stop.set()

    def _close_transport(self) -> None:
        if self._ble is not None:
            try:
                self._ble.stop()
            except Exception:
                log.exception("BLE stop failed")
            self._ble = None
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    # ---------- BLE ----------

    def _open_ble(self) -> None:
        from .ble_gatt import BleGattServer

        name = self.cfg.ble_name or "J2-Pi"
        log.info("starting BLE GATT peripheral as %r", name)
        self._ble = BleGattServer(local_name=name)
        self._ble.start()
        # Announce presence immediately (ESP waits for HEARTBEAT during wake)
        self._send(protocol.heartbeat("idle"), important=True)

    # ---------- serial (legacy) ----------

    def _open_serial(self) -> None:
        if serial is None:
            raise RuntimeError(
                "pyserial is required for UART transport: pip install pyserial"
            )
        log.info("opening %s @ %d (exclusive)", self.cfg.serial_port, self.cfg.baud)
        try:
            self._ser = serial.Serial(
                self.cfg.serial_port,
                self.cfg.baud,
                timeout=0.05,
                write_timeout=1.0,
                exclusive=True,
            )
        except TypeError:
            self._ser = serial.Serial(
                self.cfg.serial_port,
                self.cfg.baud,
                timeout=0.05,
                write_timeout=1.0,
            )
        except serial.SerialException as exc:
            log.error(
                "UART open failed (%s). Is serial-getty using the port? "
                "Re-run: sudo /opt/rover-firmware/pi/scripts/claim_uart.sh "
                "&& sudo systemctl restart rover-daemon",
                exc,
            )
            raise
        self._send(protocol.heartbeat("idle"), important=True)

    def _send(self, data: bytes, *, important: bool = False) -> None:
        line = data.decode("utf-8", errors="replace").rstrip()
        if line.startswith('{"type":"HEARTBEAT"'):
            now = time.time()
            if important or (now - self._last_heartbeat_log) >= 10.0:
                self._last_heartbeat_log = now
                log.info("TX %s", line)
            else:
                log.debug("TX %s", line)
        else:
            log.info("TX %s", line)

        if self._ble is not None:
            try:
                self._ble.send_line(data)
            except Exception as exc:
                log.error("BLE TX failed: %s", exc)

        if self._ser is not None and self._ser.is_open:
            try:
                self._ser.write(data)
                self._ser.flush()
            except serial.SerialException as exc:
                log.error("UART TX failed: %s", exc)

    def _pump_rx(self) -> None:
        if self._ble is not None:
            for line in self._ble.pop_lines():
                msg = protocol.try_parse_line(line)
                if msg:
                    log.info("RX %s %s", msg.type, msg.redacted_raw())
                    self._on_message(msg)

        if not self._ser:
            return
        try:
            chunk = self._ser.read(256)
        except serial.SerialException as exc:
            log.error("UART RX failed: %s", exc)
            return
        if not chunk:
            return
        self._rx_buf += chunk.decode("utf-8", errors="replace")
        while "\n" in self._rx_buf:
            line, self._rx_buf = self._rx_buf.split("\n", 1)
            msg = protocol.try_parse_line(line)
            if msg:
                log.info("RX %s %s", msg.type, msg.redacted_raw())
                self._on_message(msg)

    def _drain_rx_deferred(self) -> list:
        """Drain RX during blocking work; return parsed messages to defer."""
        deferred: list = []
        if self._ble is not None:
            for line in self._ble.pop_lines(max_items=64):
                msg = protocol.try_parse_line(line)
                if msg:
                    deferred.append(msg)
        if self._ser:
            try:
                chunk = self._ser.read(256)
            except Exception:
                chunk = b""
            if chunk:
                self._rx_buf += chunk.decode("utf-8", errors="replace")
                while "\n" in self._rx_buf:
                    line, self._rx_buf = self._rx_buf.split("\n", 1)
                    msg = protocol.try_parse_line(line)
                    if msg:
                        deferred.append(msg)
        return deferred

    def _call_with_heartbeats(self, fn, *args, **kwargs):
        """
        Run blocking network work in a thread while HEARTBEATs keep flowing.

        ESP/phone treat a silent link as Pi death during setup; nmcli/tailscale
        can take tens of seconds. RX is drained so buffers do not overflow;
        state-changing commands are deferred until the blocking call finishes.
        """
        box: dict = {"value": None, "exc": None}
        done = threading.Event()

        def worker() -> None:
            try:
                box["value"] = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — surface to caller
                box["exc"] = exc
            finally:
                done.set()

        threading.Thread(target=worker, name="rover-net", daemon=True).start()
        deferred: list = []
        while not done.wait(timeout=0.25):
            self._send(protocol.heartbeat(self._heartbeat_mode()))
            deferred.extend(self._drain_rx_deferred())
        if box["exc"] is not None:
            raise box["exc"]
        self._deferred_msgs.extend(deferred)
        return box["value"]

    # ---------- state machine ----------

    def _enter(self, new: State) -> None:
        with self._lock:
            old = self.state
            self.state = new
        log.info("STATE %s → %s", old.name, new.name)

        if new == State.IDLE_LISTENING:
            pass
        elif new == State.WIPE_NETWORK_PROFILES:
            # Re-setup from ESP: snapshot Wi‑Fi only. Do NOT drop the live
            # association or Tailscale until the new join is proven.
            self._phone_authkey = None
            ok = network.begin_setup_clean_slate(self.cfg.dry_run)
            self._send(
                protocol.command_ack(
                    protocol.Cmd.WIPE_NET.value, 0 if ok else 1
                )
            )
            self._send(
                protocol.setup_status(
                    "wipe",
                    0 if ok else 1,
                    detail="prepared" if ok else "backup_failed",
                    label="Preparing computer network…"
                    if ok
                    else "Computer network prepare failed",
                )
            )
            if not ok:
                self._enter(State.IDLE_LISTENING)
        elif new == State.CONNECT_WIFI:
            self._send(
                protocol.setup_status(
                    "wifi",
                    0,
                    detail="connecting",
                    label="Computer joining Wi‑Fi…",
                )
            )
            ok = bool(
                self._call_with_heartbeats(
                    network.connect_wifi,
                    self._pending_ssid,
                    self._pending_pass,
                    self.cfg.dry_run,
                    self.cfg.wifi_connect_timeout_sec,
                )
            )
            self._send(
                protocol.command_ack("WIFI_CONFIG", 0 if ok else 1)
            )
            self._send(
                protocol.setup_status(
                    "wifi",
                    0 if ok else 1,
                    detail="ok" if ok else "wifi_failed",
                    label="Computer on Wi‑Fi"
                    if ok
                    else "Computer Wi‑Fi failed",
                )
            )
            if ok:
                self._enter(State.FETCH_TAILSCALE_URL)
            else:
                log.error(
                    "WIFI_CONFIG failed — previous Wi‑Fi should still be up; "
                    "staying reachable for retry"
                )
                self._enter(State.IDLE_LISTENING)
        elif new == State.FETCH_TAILSCALE_URL:
            authkey = self._phone_authkey or self.cfg.tailscale_authkey
            if not authkey:
                log.error(
                    "no Tailscale auth key from phone/env — refusing SSO URL path"
                )
                self._send(
                    protocol.setup_status(
                        "ts_join",
                        1,
                        detail="missing_authkey",
                        label="Computer Tailscale join failed",
                    )
                )
                self._send(protocol.command_ack("TS_AUTHKEY", 1))
                self._enter(State.IDLE_LISTENING)
            else:
                self._send(
                    protocol.setup_status(
                        "ts_join",
                        0,
                        detail="joining",
                        label="Computer joining Tailscale…",
                    )
                )
                ok = bool(
                    self._call_with_heartbeats(
                        network.join_tailscale_with_authkey,
                        self.cfg.dry_run,
                        authkey,
                        self.cfg.tailscale_tag,
                    )
                )
                if ok:
                    network.ensure_tailscale_enabled(
                        self.cfg.dry_run,
                        advertise_tag=self.cfg.tailscale_tag,
                    )
                    network.ensure_tailscale_key_never_expires(
                        self.cfg.dry_run,
                        self.cfg.tailscale_api_key,
                        self.cfg.tailscale_tailnet,
                    )
                    self._send(
                        protocol.setup_status(
                            "ts_join",
                            0,
                            detail="ok",
                            label="Computer on Tailscale",
                        )
                    )
                    self._enter(State.NORMAL_OPERATION)
                else:
                    log.error("Tailscale auth-key join failed")
                    self._send(
                        protocol.setup_status(
                            "ts_join",
                            1,
                            detail="authkey_failed",
                            label="Computer Tailscale join failed",
                        )
                    )
                    self._send(protocol.command_ack("TS_AUTHKEY", 1))
                    self._enter(State.IDLE_LISTENING)
        elif new == State.AWAITING_SSO_LOGIN:
            pass
        elif new == State.NORMAL_OPERATION:
            network.ensure_tailscale_enabled(
                self.cfg.dry_run,
                advertise_tag=self.cfg.tailscale_tag,
            )
            network.ensure_tailscale_key_never_expires(
                self.cfg.dry_run,
                self.cfg.tailscale_api_key,
                self.cfg.tailscale_tailnet,
            )
            dns = network.tailscale_magic_dns(self.cfg.dry_run)
            network.ensure_https_certs(
                self.cfg.dry_run,
                cert_dir=self.cfg.cert_dir,
                renew_within_days=self.cfg.cert_renew_within_days,
                dns=dns or None,
            )
            # MQTT broker session comes from BLE WIFI_CONFIG (or ROVER_MQTT_*).
            # Do not call Account API; password auth only (no client certs).
            self._mqtt = mqtt_store.load_effective(self.cfg.mqtt_store_path)
            self._send(
                protocol.setup_status(
                    "ready",
                    0,
                    detail=dns or "ok",
                    label="Finishing setup…",
                )
            )
            self._send(protocol.node_ready(dns))
        elif new == State.EXECUTE_FACTORY_RESET:
            network.factory_reset_network(self.cfg.dry_run)
            self._send(protocol.command_ack(protocol.Cmd.WIPE_ALL.value, 0))
            # Rail-off while wake_secret still on disk, then wipe mqtt.json.
            self._notify_esp_rail_off(reason="factory_wipe")
            mqtt_store.clear_file(
                self.cfg.mqtt_store_path, dry_run=self.cfg.dry_run
            )
            self._mqtt = MqttCreds()
            time.sleep(1.0)
            network.request_shutdown(self.cfg.dry_run)

    def _heartbeat_mode(self) -> str:
        if self.state == State.NORMAL_OPERATION:
            return "normal"
        if self.state in (
            State.WIPE_NETWORK_PROFILES,
            State.CONNECT_WIFI,
            State.FETCH_TAILSCALE_URL,
            State.AWAITING_SSO_LOGIN,
        ):
            return "setup"
        return "idle"

    def _persist_mqtt_from_wifi_config(self, msg: protocol.Message) -> None:
        """Save full BLE WIFI_CONFIG broker block → mqtt.json (0600)."""
        if not (
            msg.mqtt_host
            or msg.mqtt_url
            or msg.mqtt_user
            or msg.mqtt_pass
            or msg.mqtt_port
            or msg.topic_prefix
            or msg.topic_cmd
            or msg.topic_status
            or msg.wake_secret
            or msg.device_id
        ):
            log.info("WIFI_CONFIG has no MQTT fields — keeping existing mqtt.json")
            return
        merged = mqtt_store.from_wifi_config_fields(
            mqtt_host=msg.mqtt_host,
            mqtt_port=msg.mqtt_port,
            mqtt_url=msg.mqtt_url,
            mqtt_user=msg.mqtt_user,
            mqtt_pass=msg.mqtt_pass,
            topic_prefix=msg.topic_prefix,
            topic_cmd=msg.topic_cmd,
            topic_status=msg.topic_status,
            wake_secret=msg.wake_secret,
            device_id=msg.device_id,
            base=mqtt_store.load_file(self.cfg.mqtt_store_path),
        )
        ok = mqtt_store.save_file(
            merged, self.cfg.mqtt_store_path, dry_run=self.cfg.dry_run
        )
        if ok:
            self._mqtt = mqtt_store.apply_env_overrides(merged)
            log.info("MQTT provisioning updated: %s", mqtt_store.redact(self._mqtt))
        else:
            log.error("failed to persist MQTT credentials from WIFI_CONFIG")

    def _notify_esp_rail_off(self, reason: str = "shutdown") -> None:
        """Pi → ESP: MQTT ``OFF GPIO16 DELAY 15 <wake_secret>`` (or UART SLEEP)."""
        # Use BLE-stored (or ROVER_MQTT_*) credentials — no Account API session.
        self._mqtt = mqtt_store.load_effective(self.cfg.mqtt_store_path)
        # Tell the phone before rail-off. Parallel with the ESP publish so the
        # power-off wait does not grow by another TLS handshake.
        if not self.cfg.dry_run:
            try:
                from .pi_status import request_shutdown_status

                request_shutdown_status(reason, creds=self._mqtt)
            except Exception:
                log.exception("PI shutdown status request failed")

        if self.cfg.dry_run:
            try:
                payload = mqtt_store.rail_off_payload(self._mqtt)
                safe = mqtt_store.redact_command(payload, self._mqtt.wake_secret)
            except ValueError as exc:
                safe = f"<unavailable: {exc}>"
            log.info("dry_run: would rail-off ESP (%s) payload=%r", reason, safe)
            return

        if self._use_uart and not self._use_ble:
            log.warning(
                "TX SLEEP to ESP over UART (%s) — legacy rail cut", reason
            )
            self._send(protocol.sleep_command(), important=True)
            return

        from .mqtt_rail import publish_rail_off

        if not self._mqtt.wake_secret:
            log.error(
                "MQTT rail-off (%s) missing wake_secret — ESP would NACK BAD_SECRET; "
                "skipping publish (need BLE WIFI_CONFIG)",
                reason,
            )
            ok = False
        else:
            try:
                safe = mqtt_store.redact_command(
                    mqtt_store.rail_off_payload(self._mqtt),
                    self._mqtt.wake_secret,
                )
            except ValueError:
                safe = "OFF GPIO16 DELAY 15 ***"
            log.warning(
                "MQTT rail-off (%s) payload=%r — ESP delays GPIO16 cut for Linux halt",
                reason,
                safe,
            )
            ok = publish_rail_off(self._mqtt)
        # Let rover-watch wait for publish attempt before poweroff.
        try:
            done = os.environ.get(
                "ROVER_ESP_RAIL_OFF_DONE_PATH", "/tmp/esp-rail-off.done"
            )
            with open(done, "w", encoding="utf-8") as fh:
                fh.write(f"{'ok' if ok else 'fail'} {reason}\n")
        except OSError as exc:
            log.warning("could not write rail-off done marker: %s", exc)
        if not ok:
            log.error(
                "MQTT rail-off failed (%s) — need BLE WIFI_CONFIG mqtt_* + wake_secret "
                "(or ROVER_MQTT_* overrides); Pi will still halt",
                reason,
            )

    def _poll_esp_sleep_request(self) -> None:
        """rover-control / rover-watch drops this file before OS poweroff."""
        path = self.cfg.esp_sleep_req_path
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                note = fh.read(400).strip()
        except OSError:
            note = ""
        try:
            os.remove(path)
        except OSError as exc:
            log.warning("could not remove %s: %s", path, exc)
            return
        self._notify_esp_rail_off(reason=note or "shutdown")

    def _tick(self) -> None:
        now = time.monotonic()
        period = 1.0 / max(self.cfg.heartbeat_hz, 0.1)
        if now - self._last_heartbeat >= period:
            self._last_heartbeat = now
            self._send(protocol.heartbeat(self._heartbeat_mode()))

        self._poll_esp_sleep_request()

        if self.state == State.AWAITING_SSO_LOGIN:
            if network.tailscale_is_connected(self.cfg.dry_run):
                log.info("Tailscale connected after SSO")
                network.ensure_tailscale_enabled(
                    self.cfg.dry_run,
                    advertise_tag=self.cfg.tailscale_tag,
                )
                network.ensure_tailscale_key_never_expires(
                    self.cfg.dry_run,
                    self.cfg.tailscale_api_key,
                    self.cfg.tailscale_tailnet,
                )
                self._enter(State.NORMAL_OPERATION)

    def _on_message(self, msg: protocol.Message) -> None:
        if msg.type == "COMMAND_LONG":
            cmd = msg.cmd or ""
            if cmd == protocol.Cmd.WIPE_NET.value:
                if self.state in (
                    State.IDLE_LISTENING,
                    State.NORMAL_OPERATION,
                    State.WIPE_NETWORK_PROFILES,
                    State.CONNECT_WIFI,
                    State.FETCH_TAILSCALE_URL,
                    State.AWAITING_SSO_LOGIN,
                ):
                    self._enter(State.WIPE_NETWORK_PROFILES)
                return
            if cmd == protocol.Cmd.WIPE_ALL.value:
                if self.state in (
                    State.IDLE_LISTENING,
                    State.NORMAL_OPERATION,
                    State.WIPE_NETWORK_PROFILES,
                    State.CONNECT_WIFI,
                    State.FETCH_TAILSCALE_URL,
                    State.AWAITING_SSO_LOGIN,
                ):
                    self._enter(State.EXECUTE_FACTORY_RESET)
                return
            if cmd == protocol.Cmd.SLEEP.value:
                return

        if msg.type == "WIFI_CONFIG":
            # Accept during setup *and* re-provision from NORMAL_OPERATION so a
            # late/retried phone WIFI_CONFIG still enrolls Wi‑Fi (never ignore
            # after a successful Tailscale join left us without a saved profile).
            if self.state not in (
                State.WIPE_NETWORK_PROFILES,
                State.IDLE_LISTENING,
                State.CONNECT_WIFI,
                State.FETCH_TAILSCALE_URL,
                State.AWAITING_SSO_LOGIN,
                State.NORMAL_OPERATION,
            ):
                log.warning("WIFI_CONFIG ignored in state %s", self.state.name)
                return
            self._pending_ssid = msg.ssid
            self._pending_pass = msg.password
            key = msg.authkey.strip()
            if key:
                log.info("WIFI_CONFIG includes Tailscale auth key — SSO not required")
                self._phone_authkey = key
            self._persist_mqtt_from_wifi_config(msg)
            self._enter(State.CONNECT_WIFI)
            return

        if msg.type == "TS_AUTHKEY":
            key = msg.authkey.strip()
            if not key:
                log.warning("TS_AUTHKEY ignored (empty)")
                return
            self._phone_authkey = key
            if self.state == State.WIPE_NETWORK_PROFILES:
                log.info("TS_AUTHKEY buffered until WIFI_CONFIG / Wi‑Fi up")
                return
            if self.state not in (
                State.FETCH_TAILSCALE_URL,
                State.AWAITING_SSO_LOGIN,
                State.CONNECT_WIFI,
                State.IDLE_LISTENING,
            ):
                log.warning("TS_AUTHKEY ignored in state %s", self.state.name)
                return
            if self.state == State.CONNECT_WIFI:
                log.info("TS_AUTHKEY stored — will use after Wi‑Fi connects")
                return
            log.info("TS_AUTHKEY received — joining with phone-provisioned key")
            if not network.wifi_already_connected():
                log.error("TS_AUTHKEY ignored — Wi‑Fi not connected")
                self._send(
                    protocol.setup_status(
                        "ts_join", 1, detail="wifi_required"
                    )
                )
                self._send(protocol.command_ack("TS_AUTHKEY", 1))
                return
            ok = bool(
                self._call_with_heartbeats(
                    network.join_tailscale_with_authkey,
                    self.cfg.dry_run,
                    key,
                    self.cfg.tailscale_tag,
                )
            )
            if ok:
                network.ensure_tailscale_enabled(
                    self.cfg.dry_run,
                    advertise_tag=self.cfg.tailscale_tag,
                )
                network.ensure_tailscale_key_never_expires(
                    self.cfg.dry_run,
                    self.cfg.tailscale_api_key,
                    self.cfg.tailscale_tailnet,
                )
                self._send(protocol.setup_status("ts_join", 0, detail="ok"))
                self._enter(State.NORMAL_OPERATION)
            else:
                self._send(
                    protocol.setup_status(
                        "ts_join", 1, detail="authkey_failed"
                    )
                )
                self._send(protocol.command_ack("TS_AUTHKEY", 1))
                self._enter(State.IDLE_LISTENING)
            return
