# Rover JSON framing (v1) — legacy UART reference

> **Product transport is BLE GATT.** Same newline JSON over write/notify UUIDs.
> See [`BLE_SETUP_PROTOCOL.md`](BLE_SETUP_PROTOCOL.md). UART (`115200 8N1`) is
> legacy (`ROVER_TRANSPORT=uart`) only.

**Product path:** phone supplies Wi‑Fi + **Tailscale auth key** → ESP writes to
Pi GATT → Pi joins with `--authkey`. **No login URL** is sent to the phone.

## ESP → Pi

| type | fields | When |
|------|--------|------|
| `COMMAND_LONG` | `cmd`: `WIPE_NET` \| `WIPE_ALL` | Setup wipe / factory reset |
| `WIFI_CONFIG` | `ssid`, `pass`, **`key`** (required for product) | After wipe ACK |
| `TS_AUTHKEY` | `key` | Optional duplicate handoff |

`WIPE_NET`: **prepare only** — backup Wi‑Fi profiles; does **not** drop the
live Wi‑Fi link or log out of Tailscale yet. Destructive cleanup happens only
after a successful `WIFI_CONFIG` join (other Wi‑Fi profiles removed) and right
before Tailscale auth-key rejoin (`tailscale logout`). If Wi‑Fi join fails, the
previous profile is restored so the Pi stays reachable without Ethernet.

If `key` missing, Pi reports `SETUP_STATUS` failure (does **not** open SSO).
Wi‑Fi stays up; Tailscale is left alone until a key is provided.

## Pi → ESP

| type | fields | When |
|------|--------|------|
| `HEARTBEAT` | `mode` | Periodic |
| `COMMAND_ACK` | `cmd`, `result` | Wipe / wifi / ts ack |
| `SETUP_STATUS` | `stage`, `result`, optional `detail`, optional `label` | Progress / errors |
| `NODE_READY` | `dns` | Join succeeded |
| `COMMAND_LONG` | `cmd`: `SLEEP` | **Legacy UART only** — prefer MQTT `OFF GPIO16` |

Before OS `poweroff`, `rover-control` / `rover-watch` write `/tmp/esp-sleep.req`.
`rover-daemon` sees that file and publishes MQTT
**`OFF GPIO16 DELAY 15 <wake_secret>`** on `topic_cmd` from
`/var/lib/rover/mqtt.json` (see `BLE_SETUP_PROTOCOL.md`). Legacy UART mode still
TX `SLEEP` instead.

A bare `sudo poweroff` that skips this file will leave the rail on.

`SETUP_STATUS.stage`: `wipe` \| `wifi` \| `ts_join` \| `ready`.  
`result` `0` = ok, non-zero = fail (`detail` e.g. `authkey_failed`, `missing_authkey`).  
`label` = human-readable string forwarded to the phone as `SETUP_PROGRESS.label`.

BLE checklist protocol: [`docs/SETUP_PROGRESS.md`](../docs/SETUP_PROGRESS.md).

**Deprecated for product:** `TUNNEL_STATUS` / login URL (ignored by ESP if received).

```text
{"type":"WIFI_CONFIG","ssid":"Home","pass":"secret","key":"tskey-auth-..."}
{"type":"SETUP_STATUS","stage":"ts_join","result":0,"detail":"joining"}
{"type":"NODE_READY","dns":"rover.tailnet-name.ts.net"}
```

## BLE (setup / reset only)

```json
{"cmd":"SET_WIFI","ssid":"...","pass":"...","authkey":"tskey-auth-..."}
```

`authkey` is **required**. Progress: `PROGRESS` / `SETUP_FAIL` / `DONE` (see iOS plan).

After `DONE`, BLE advertising **stops**. Normal control = Tailscale UDP **9292**.

## Tailscale UDP (ESP port 9292)

| Request | Reply |
|---------|-------|
| `PING` | `PONG rail=ON\|OFF` |
| `POWER_ON` / `POWER_OFF` / `ROVER_WAKE` | `OK …` |

iOS plan: [`docs/IOS_IMPLEMENTATION_PLAN.md`](../docs/IOS_IMPLEMENTATION_PLAN.md).
