# BLE setup protocol (ESP ↔ Pi)

Phone talks only to the ESP; ESP forwards setup JSON to the Pi over BLE GATT.
Power rail cut uses MQTT (`OFF GPIO16 DELAY 15 <wake_secret>`), not UART `SLEEP`.

## Advertisement

- Local name: **`J2-Pi`** (prefix required — ESP scans for this)
- Env override: `ROVER_BLE_NAME` (must still start with `J2-Pi`)

## GATT

| Role | UUID |
|------|------|
| Service | `6a2b0001-b5a3-f393-e0a9-e50e24dcca9e` |
| Write (ESP→Pi) | `6a2b0002-b5a3-f393-e0a9-e50e24dcca9e` |
| Notify (Pi→ESP) | `6a2b0003-b5a3-f393-e0a9-e50e24dcca9e` |

ESP negotiates MTU and may chunk writes; Pi reassembles until a newline.

## Framing

Newline-delimited JSON.

**ESP → Pi (write):** `COMMAND_LONG` (`WIPE_NET` / `WIPE_ALL`), `WIFI_CONFIG`, `TS_AUTHKEY`

**Pi → ESP (notify):** `HEARTBEAT`, `COMMAND_ACK`, `SETUP_STATUS`, `NODE_READY`, `TUNNEL_STATUS`

### WIFI_CONFIG

```json
{
  "type": "WIFI_CONFIG",
  "ssid": "HomeWiFi",
  "pass": "wifi-password",
  "mqtt_host": "cluster.hivemq.cloud",
  "mqtt_user": "rover-user",
  "mqtt_pass": "mqtt-password",
  "topic_cmd": "rover/rover-user/cmd",
  "topic_status": "rover/rover-user/status",
  "wake_secret": "generated-secret",
  "key": "tskey-auth-optional"
}
```

Pi persists MQTT + wake secret atomically at **`/var/lib/rover/mqtt.json`** (mode `0600`) and reloads after reboot.

## Transport env

| Variable | Default | Notes |
|----------|---------|--------|
| `ROVER_TRANSPORT` | `ble` | `uart` = legacy serial only |
| `ROVER_BLE_NAME` | `J2-Pi` | Must start with `J2-Pi` |

UART `/dev/serial0` is **not** required for setup when `ROVER_TRANSPORT=ble`.

## Rail-off (shutdown)

Before Linux halt, daemon publishes MQTT:

```text
OFF GPIO16 DELAY 15 <wake_secret>
```

Topic: `topic_cmd` from mqtt.json (else `rover/<mqtt_user>/cmd`).

`ROVER_MQTT_*` env vars are **optional overrides** (not required if BLE provisioning succeeded).

Idle / manual halt still drops `/tmp/esp-sleep.req`; daemon publishes MQTT rail-off then OS poweroff.

## End-to-end

1. ESP powers Pi (GPIO16 HIGH)
2. Pi boots → BLE advertises `J2-Pi`
3. ESP connects → `WIPE_NET` / `WIFI_CONFIG` (Wi‑Fi + MQTT + wake_secret + optional TS key)
4. Pi ACKs / `NODE_READY`
5. Later: Pi MQTT `OFF GPIO16 DELAY 15 <secret>` → ESP waits 15s → cuts rail
