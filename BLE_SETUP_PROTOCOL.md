# BLE setup protocol (ESP ↔ Pi)

Phone talks only to the ESP; ESP forwards setup JSON to the Pi over BLE GATT.
ESP hands the Pi a full MQTT broker block in `WIFI_CONFIG`. The Pi stores it and
connects with **TLS + username/password** (no client certificates, no Account API
sign-in). Power cut uses MQTT (`OFF GPIO16 DELAY 15 <wake_secret>`), not UART.

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

Full broker handoff (Wi‑Fi + MQTT + device + optional Tailscale):

```json
{
  "type": "WIFI_CONFIG",
  "ssid": "...",
  "pass": "...",
  "mqtt_host": "jjcloud.tail9d0237.ts.net",
  "mqtt_port": 8883,
  "mqtt_url": "mqtts://jjcloud.tail9d0237.ts.net:8883",
  "mqtt_user": "<accountId>",
  "mqtt_pass": "<secret>",
  "topic_prefix": "tenants/<accountId>",
  "topic_cmd": "tenants/<accountId>/cmd",
  "topic_status": "tenants/<accountId>/status",
  "wake_secret": "...",
  "device_id": "...",
  "key": "tskey-auth-..."
}
```

| Group | Fields |
|-------|--------|
| Wi‑Fi | `ssid`, `pass` |
| Broker | `mqtt_host`, `mqtt_port`, `mqtt_url`, `mqtt_user`, `mqtt_pass` |
| Topics | `topic_prefix`, `topic_cmd`, `topic_status` |
| Device | `wake_secret`, `device_id` |
| Optional | `key` (Tailscale auth key) |

`mqtt_url` may stand in for host/port (and TLS scheme). Prefer explicit
`topic_cmd` / `topic_status`; if missing, derive from `topic_prefix`
(`{prefix}/cmd` + `{prefix}/status`); if still missing, fall back to
`tenants/<mqtt_user>/cmd` and `tenants/<mqtt_user>/status`.

Pi persists atomically at **`/var/lib/rover/mqtt.json`** (dir `0700`, file `0600`).  
Never log passwords, Tailscale keys, or wake secrets.

**Do not use** HiveMQ hosts or `rover/<user>/…` topics.  
**Do not** call Account API `/v1/auth/signin` for a broker session.

## MQTT (password auth)

- Host: `mqtt_host` (or host from `mqtt_url`)
- Port **8883**, **TLS** (`mqtts`) by default
- Username = `mqtt_user`, password = `mqtt_pass`
- **No client certificate / key files**
- Publish to `topic_cmd`, QoS 1
- Optional overrides: `ROVER_MQTT_HOST`, `ROVER_MQTT_PORT`, `ROVER_MQTT_URL`,
  `ROVER_MQTT_USER`, `ROVER_MQTT_PASS`, `ROVER_MQTT_TOPIC_CMD`,
  `ROVER_MQTT_TOPIC_STATUS`, `ROVER_MQTT_TOPIC_PREFIX`, `ROVER_MQTT_WAKE_SECRET`

## Wake secret (every ESP MQTT command)

ESP requires `wake_secret` as the **last token**. Without it: `NACK BAD_SECRET`.

| Command | Form |
|---------|------|
| Power on | `ON GPIO16 <wake_secret>` |
| Power off | `OFF GPIO16 <wake_secret>` |
| Delayed off (shutdown) | `OFF GPIO16 DELAY 15 <wake_secret>` |
| Ping | `PING <wake_secret>` |
| Factory | `FACTORY_RESET <wake_secret>` |

Factory reset (`WIPE_ALL`): delayed OFF first, then **delete** `mqtt.json`.

## Rail-off (shutdown)

Before Linux halt: publish with stored BLE credentials (or env overrides):

```text
OFF GPIO16 DELAY 15 <wake_secret>
```

Idle / manual halt drops `/tmp/esp-sleep.req`; daemon publishes then OS poweroff.

## Pi status (same `topic_status`)

iOS presence does not use Tailscale. The Pi publishes QoS 1, not retained, on
`topic_status`. Every payload starts with `PI` (ESP32 messages on this topic
do not). Subscribe QoS 1 with a persistent session to receive these after the
app was offline.

| Payload | When |
|---------|------|
| `PI boot_start …` | First thing after MQTT connects. `boot_ts` is kernel boot time. |
| `PI heartbeat …` | Every 5s. `phase=boot` until finished, then `phase=up`. `seq` counts. |
| `PI boot_finished …` | After the camera path is ready and control is accepting connections. |
| `PI shutdown reason=idle\|manual\|factory\|reboot\|halt` | Before rail-off, or on OS halt/reboot. No further heartbeats. |

Boot percentage is the span from `boot_start` / `boot_ts` to `boot_finished`.
`PI shutdown` means offline until the next `PI boot_start`. A missed heartbeat
means unreachable; there is no disconnect last-will, so a short broker blip
does not by itself mark the Pi offline.

## Transport env

| Variable | Default | Notes |
|----------|---------|--------|
| `ROVER_TRANSPORT` | `ble` | `uart` = legacy only |
| `ROVER_BLE_NAME` | `J2-Pi` | Must start with `J2-Pi` |
| `ROVER_MQTT_*` | _(empty)_ | Optional overrides for `mqtt.json` |

## End-to-end

1. ESP powers Pi (GPIO16 HIGH)
2. Pi boots → BLE advertises `J2-Pi`
3. ESP connects → `WIFI_CONFIG` (Wi‑Fi + full MQTT broker block + wake_secret + optional TS key)
4. Pi joins Tailscale → `NODE_READY` (MQTT already stored from BLE)
5. Later: Pi MQTT `OFF GPIO16 DELAY 15 <secret>` on `topic_cmd` → ESP waits → cuts rail
