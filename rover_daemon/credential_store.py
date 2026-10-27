"""Credential store for BLE WIFI_CONFIG (spec name).

Implementation lives in ``mqtt_store``; this module re-exports the same API
so callers can ``from rover_daemon import credential_store``.
"""

from __future__ import annotations

from .mqtt_store import (  # noqa: F401
    DEFAULT_DIR_MODE,
    DEFAULT_FILE_MODE,
    DEFAULT_PATH,
    DEFAULT_RAIL_OFF_DELAY_SEC,
    MqttCreds,
    apply_env_overrides,
    clear_file,
    default_topics,
    esp_command,
    from_wifi_config_fields,
    load_effective,
    load_file,
    parse_mqtt_url,
    rail_off_payload,
    redact,
    redact_command,
    save_file,
    store_path,
    topics_from_prefix,
    with_wake_secret,
)

# Spec-friendly aliases
Creds = MqttCreds
persist = save_file
load = load_effective
