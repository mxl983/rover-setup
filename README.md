# Raspberry Pi — Rover BLE Daemon

**Required for BLE setup.** Without this daemon, the ESP never sees a BLE
`HEARTBEAT` / `NODE_READY` and phone setup stalls after powering the Pi.

**Customer path (zero manual network work):** factory flash runs `install.sh`
once → daemon + Bluetooth + `tailscaled` + NetworkManager on every boot → phone
BLE setup sends Wi‑Fi + auth key over GATT → Pi joins Wi‑Fi, joins Tailscale,
and stays online across reboots. Later ESP `WIPE_NET` / `WIFI_CONFIG` /
`WIPE_ALL` are always accepted while the daemon is running.

Always-on companion for the ESP32 portal:

1. Boot → advertise BLE **`J2-Pi`** → send `HEARTBEAT`
2. **IDLE_LISTENING** — normal when no onboarding commands arrive
3. On `WIPE_NET` + `WIFI_CONFIG` → backup Wi‑Fi, join, then Tailscale auth-key
   rejoin. Never drops live Wi‑Fi before the new join is proven.
4. **NORMAL_OPERATION** — periodic heartbeats; issues/renews HTTPS certs for MagicDNS
5. **Pi status** — `rover-pi-status` publishes `PI boot_start` as soon as Wi-Fi
   can reach the broker (it does not wait for `network-online`), then
   `PI cam_ready` when MediaMTX `cam` is live (iOS should start WHEP here),
   then `PI heartbeat` every 5s (includes `cam=`). `rover-pi-boot-finished`
   also emits early `cam_ready`, then `PI boot_finished` once control is
   listening. Idle, manual, factory, and OS halt publish `PI shutdown`
   before the rail drops.
6. Idle / manual halt (from `rover-control`): writes `/tmp/esp-sleep.req` →
   daemon publishes MQTT **`OFF GPIO16 DELAY 15 <wake_secret>`** → OS
   `poweroff` → ESP cuts rail after the delay
7. `WIPE_ALL` → factory reset networks + MQTT rail-off + shutdown

MQTT broker creds + `wake_secret` arrive in BLE `WIFI_CONFIG` (full broker
block: host/port/url, user/pass, topics, device_id) and are stored at
`/var/lib/rover/mqtt.json` (0600). Connect with TLS + username/password — no
client certs and no Account API. `ROVER_MQTT_*` env vars are optional overrides.

HTTPS certs (`tailscale cert`) live at `/var/lib/rover/certs/{cert.crt,cert.key}`.
Weekly `rover-cert-renew.timer` keeps them fresh.

Setup protocol: **[`BLE_SETUP_PROTOCOL.md`](BLE_SETUP_PROTOCOL.md)**  
Legacy UART framing reference: [`UART_PROTOCOL.md`](UART_PROTOCOL.md)

## Fresh Pi / new SSD (master guide)

**Follow [`INSTALL.md`](INSTALL.md)** — full dual-repo install, Tailscale/certs,
BLE MQTT, and **USB serial port remapping** when moving Pico/gimbal plugs
(prevents `rover-control` crash loops). Agents use
[`.cursor/skills/ssd-migration/SKILL.md`](.cursor/skills/ssd-migration/SKILL.md).

Quick path (details in INSTALL.md):

```bash
git clone git@github.com:mxl983/rover-setup.git ~/pi
git clone git@github.com:mxl983/rover.git ~/rover && cd ~/rover && git checkout ios-flow
cd ~/pi && sudo ./install.sh
cd ~/rover/pi-server && sudo ROVER_PROFILE=rover-demo ./install.sh
```

Setup install lands under `/opt/rover-firmware` (Bluetooth, `tailscaled`,
`rover-daemon`). Default transport is **BLE**. MQTT + `wake_secret` come from
phone BLE setup.

**Phase 2 unattended join** (optional):

```bash
sudo ROVER_TAILSCALE_AUTHKEY=tskey-auth-... \
     ROVER_TAILSCALE_API_KEY=tskey-api-... \
     ROVER_TAILSCALE_TAILNET=your.tailnet \
     ./install.sh
```

Full walkthrough: **[`INSTALL.md`](INSTALL.md)**.

## Layout

```
pi/
  INSTALL.md
  README.md
  BLE_SETUP_PROTOCOL.md
  UART_PROTOCOL.md          # legacy framing reference
  requirements.txt
  rover_daemon/             # Python package (ble_gatt, mqtt_rail, pi_status, daemon)
  systemd/rover-daemon.service
  systemd/rover-pi-status.service
  systemd/rover-pi-boot-finished.service
```

## Quick reinstall

```bash
sudo ./install.sh
sudo journalctl -u rover-daemon -f
```

## Environment

| Variable | Default | Meaning |
|----------|---------|---------|
| `ROVER_TRANSPORT` | `ble` | `ble` (default) or legacy `uart` |
| `ROVER_BLE_NAME` | `J2-Pi` | Must start with `J2-Pi` |
| `ROVER_MQTT_*` | _(optional)_ | Overrides for values in `/var/lib/rover/mqtt.json` |
| `ROVER_DRY_RUN` | `0` | Set `1` to skip nmcli/tailscale/shutdown |
| `ROVER_CERT_DIR` | `/var/lib/rover/certs` | MagicDNS HTTPS cert/key dir |
| `ROVER_SERIAL` / `ROVER_BAUD` | | Legacy UART only |

## Packages

```bash
sudo apt install python3-dbus python3-gi bluez   # optional dbus-python; BlueZ required
# install.sh venv gets: paho-mqtt dbus-next
# pyserial only if ROVER_TRANSPORT=uart
```

Vendored copies under `pi/vendor/` are used when the venv cannot install packages.
