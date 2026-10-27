"""Configuration for the rover Pi daemon."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Config:
    # Transport: ``ble`` (default) or legacy ``uart``.
    transport: str = "ble"
    # BLE peripheral local name — ESP scans for prefix ``J2-Pi``.
    ble_name: str = "J2-Pi"
    # Legacy UART (only when transport=uart, or dual for debug).
    serial_port: str = "/dev/serial0"
    baud: int = 115200
    heartbeat_hz: float = 1.0
    tailscale_poll_sec: float = 2.0
    wifi_connect_timeout_sec: int = 45
    # When true, skip destructive nmcli/tailscale/shutdown (for laptop bring-up).
    dry_run: bool = False
    log_level: str = "INFO"
    # Prefer tagged auth-key join (tagged devices get key expiry disabled by default).
    tailscale_authkey: Optional[str] = None
    # After join, call Tailscale API to force keyExpiryDisabled=true (recommended).
    tailscale_api_key: Optional[str] = None
    tailscale_tailnet: Optional[str] = None  # e.g. example.com or example.github
    # ACL tag applied via auth key, e.g. tag:rover
    tailscale_tag: str = "tag:rover"
    # HTTPS certs for MagicDNS (MediaMTX / control-server). LE ~90d; auto-renewed.
    cert_dir: str = "/var/lib/rover/certs"
    cert_renew_within_days: int = 21
    # Written by rover-control / rover-watch before OS halt; daemon publishes MQTT rail-off.
    esp_sleep_req_path: str = "/tmp/esp-sleep.req"
    # MQTT creds from BLE WIFI_CONFIG → this file; env ROVER_MQTT_* optional overrides.
    # Broker auth: TLS + username/password (no client certs, no Account API).
    mqtt_store_path: str = "/var/lib/rover/mqtt.json"


def load_config() -> Config:
    transport = (os.environ.get("ROVER_TRANSPORT") or "ble").strip().lower()
    if transport not in ("ble", "uart"):
        transport = "ble"
    return Config(
        transport=transport,
        ble_name=os.environ.get("ROVER_BLE_NAME", "J2-Pi"),
        serial_port=os.environ.get("ROVER_SERIAL", "/dev/serial0"),
        baud=int(os.environ.get("ROVER_BAUD", "115200")),
        heartbeat_hz=float(os.environ.get("ROVER_HEARTBEAT_HZ", "1")),
        dry_run=os.environ.get("ROVER_DRY_RUN", "0") in ("1", "true", "TRUE"),
        log_level=os.environ.get("ROVER_LOG_LEVEL", "INFO"),
        tailscale_authkey=os.environ.get("ROVER_TAILSCALE_AUTHKEY") or None,
        tailscale_api_key=os.environ.get("ROVER_TAILSCALE_API_KEY") or None,
        tailscale_tailnet=os.environ.get("ROVER_TAILSCALE_TAILNET") or None,
        tailscale_tag=os.environ.get("ROVER_TAILSCALE_TAG", "tag:rover"),
        cert_dir=os.environ.get("ROVER_CERT_DIR", "/var/lib/rover/certs"),
        cert_renew_within_days=int(os.environ.get("ROVER_CERT_RENEW_WITHIN_DAYS", "21")),
        esp_sleep_req_path=os.environ.get(
            "ROVER_ESP_SLEEP_REQ_PATH", "/tmp/esp-sleep.req"
        ),
        mqtt_store_path=os.environ.get(
            "ROVER_MQTT_STORE_PATH", "/var/lib/rover/mqtt.json"
        ),
    )
