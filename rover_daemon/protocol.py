"""UART JSON-line protocol (see pi/UART_PROTOCOL.md)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

log = logging.getLogger("rover.protocol")


class Cmd(str, Enum):
    WIPE_NET = "WIPE_NET"
    WIPE_ALL = "WIPE_ALL"
    SLEEP = "SLEEP"


@dataclass
class Message:
    type: str
    raw: dict

    @property
    def cmd(self) -> Optional[str]:
        return self.raw.get("cmd")

    @property
    def ssid(self) -> str:
        return str(self.raw.get("ssid") or "").strip()

    @property
    def password(self) -> str:
        return str(self.raw.get("pass") or self.raw.get("password") or "").strip()

    @property
    def url(self) -> str:
        return str(self.raw.get("url") or "")

    @property
    def authkey(self) -> str:
        return str(self.raw.get("key") or self.raw.get("authkey") or "").strip()

    @property
    def mqtt_host(self) -> str:
        return str(self.raw.get("mqtt_host") or "").strip()

    @property
    def mqtt_user(self) -> str:
        return str(self.raw.get("mqtt_user") or "").strip()

    @property
    def mqtt_pass(self) -> str:
        return str(self.raw.get("mqtt_pass") or "")

    @property
    def topic_cmd(self) -> str:
        return str(self.raw.get("topic_cmd") or "").strip()

    @property
    def topic_status(self) -> str:
        return str(self.raw.get("topic_status") or "").strip()

    @property
    def wake_secret(self) -> str:
        return str(self.raw.get("wake_secret") or "").strip()

    @property
    def result(self) -> int:
        return int(self.raw.get("result") or 0)


def encode(msg_type: str, **fields: Any) -> bytes:
    payload = {"type": msg_type, **fields}
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    return line.encode("utf-8")


def try_parse_line(line: str) -> Optional[Message]:
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        # ESP USB debug lines must never appear here; if they do, wiring is on UART0.
        if line.startswith("["):
            log.debug("ignoring non-JSON UART line: %r", line[:80])
        else:
            log.warning("bad JSON from UART: %r", line[:120])
        return None
    if not isinstance(data, dict) or "type" not in data:
        return None
    return Message(type=str(data["type"]), raw=data)


def heartbeat(mode: str) -> bytes:
    return encode("HEARTBEAT", mode=mode)


def command_ack(cmd: str, result: int = 0) -> bytes:
    return encode("COMMAND_ACK", cmd=cmd, result=result)


def setup_status(
    stage: str,
    result: int = 0,
    detail: str = "",
    label: str = "",
) -> bytes:
    """Progress for ESP → phone checklist (see docs/SETUP_PROGRESS.md)."""
    fields: dict = {"stage": stage, "result": result}
    if detail:
        fields["detail"] = detail
    if label:
        fields["label"] = label
    else:
        # Default human labels when callers omit them.
        defaults = {
            "wipe": "Resetting computer network…",
            "wifi": "Computer joining Wi‑Fi…",
            "ts_join": "Computer joining Tailscale…",
            "ready": "Finishing setup…",
        }
        if stage in defaults:
            fields["label"] = defaults[stage]
    return encode("SETUP_STATUS", **fields)


def tunnel_status(url: str) -> bytes:
    return encode("TUNNEL_STATUS", url=url)


def node_ready(dns: str = "") -> bytes:
    if dns:
        return encode("NODE_READY", dns=dns)
    return encode("NODE_READY")


def sleep_command() -> bytes:
    return encode("COMMAND_LONG", cmd=Cmd.SLEEP.value)


def ts_authkey(key: str) -> bytes:
    """ESP → Pi: phone-provisional Tailscale auth key (Phase 3 handoff)."""
    return encode("TS_AUTHKEY", key=key)
