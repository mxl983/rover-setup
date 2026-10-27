# Master setup — new Pi / new SSD

**This is the canonical bring-up guide.** Agents also follow
[`.cursor/skills/ssd-migration/SKILL.md`](.cursor/skills/ssd-migration/SKILL.md),
which points here.

Use this when: flashing a new SSD/NVMe, cloning onto a fresh Raspberry Pi OS,
re-running installers, or **USB ports / serial devices moved** and
`rover-control` is crash-looping.

Supported boards: **Raspberry Pi 4 Model B** and **Raspberry Pi 5**.  
OS: Raspberry Pi OS **64-bit** (Bookworm or Trixie). Reference unit
`rover-demo` ran **2026-06-18** (Debian 13 / trixie).

**Default transport is BLE** (`ROVER_TRANSPORT=ble`). ESP still controls Pi
power via GPIO16; rail-off is MQTT `OFF GPIO16`, not UART `SLEEP`.

Related: [`BLE_SETUP_PROTOCOL.md`](BLE_SETUP_PROTOCOL.md) ·
[`UART_PROTOCOL.md`](UART_PROTOCOL.md) (legacy) ·
[`README.md`](README.md)

---

## Repos

| Role | Repo | Branch | Installer |
|------|------|--------|-----------|
| Setup (BLE, Tailscale, certs, rail-off) | [`mxl983/rover-setup`](https://github.com/mxl983/rover-setup) | `main` | `sudo ./install.sh` |
| Control (API, camera, motors, audio) | [`mxl983/rover`](https://github.com/mxl983/rover) | **`ios-flow`** | `cd pi-server && sudo ROVER_PROFILE=rover-demo ./install.sh` |

Convention on the Pi:

```text
~/pi          → rover-setup clone   → installs to /opt/rover-firmware
~/rover       → rover clone         → installs to /opt/rover-server
```

---

## A. Fresh device — full checklist

### A1. Flash OS

1. [Raspberry Pi Imager](https://www.raspberrypi.com/software/) → **Raspberry Pi OS (64-bit)** Lite or Desktop.
2. Set hostname, user/password, enable **SSH**, optional Wi‑Fi for first boot.
3. Boot, SSH in, update:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

### A2. Clone (correct branches)

```bash
git clone git@github.com:mxl983/rover-setup.git ~/pi
# or: git clone https://github.com/mxl983/rover-setup.git ~/pi

git clone git@github.com:mxl983/rover.git ~/rover
cd ~/rover && git checkout ios-flow
```

> Do **not** stay on `rover` `main` — that tree has no `pi-server/` installer.

### A3. Install setup first

```bash
cd ~/pi
sudo ./install.sh

# Optional unattended Tailscale join:
# sudo ROVER_TAILSCALE_AUTHKEY=tskey-auth-... \
#      ROVER_TAILSCALE_API_KEY=tskey-api-... \
#      ROVER_TAILSCALE_TAILNET=your.tailnet \
#      ./install.sh
```

This enables NetworkManager, Bluetooth (+ BlueZ `Experimental`), `tailscaled`,
`rover-daemon`, pi-status units, and weekly `rover-cert-renew.timer`.  
BLE advertises **`J2-Pi`**.

### A4. Install control second

```bash
cd ~/rover/pi-server
sudo ROVER_PROFILE=rover-demo ./install.sh
```

Enables **I2C**, Node 20, MediaMTX, hardware Python venv, ALSA, LiDAR udev,
and systemd units (`rover-control`, `rover-mediamtx`, audio/talk/watch/log/lidar).

If `/dev/i2c-1` is missing:

```bash
sudo reboot
```

### A5. Tailscale + HTTPS certs

```bash
# If not joined via auth key / BLE yet:
sudo tailscale up
# Open the login URL, then Admin → Machines → Disable key expiry

sudo systemctl start rover-cert-renew.service
sudo ls -l /var/lib/rover/certs/cert.crt /var/lib/rover/certs/cert.key
sudo systemctl restart rover-control rover-mediamtx
```

### A6. BLE provision MQTT (ESP + phone)

Run onboarding so `WIFI_CONFIG` writes `/var/lib/rover/mqtt.json` (needed for
rail-off / pi-status). Confirm:

```bash
systemctl is-active rover-daemon bluetooth tailscaled
sudo ls -l /var/lib/rover/mqtt.json
journalctl -u rover-daemon -n 40 --no-pager
```

### A7. USB serial + audio (do this if ports moved — see §B)

```bash
ls -l /dev/serial/by-id/
grep -E '^(PICO_MOTOR_SERIAL_PORT|GIMBAL_SERIAL_PORT)=' /opt/rover-server/env/rover-server.env
aplay -l; arecord -l
```

If by-id paths or ALSA card names do not match the env file, fix them **before**
leaving `rover-control` restarting forever (§B).

### A8. Smoke test

```bash
systemctl status rover-daemon rover-control rover-mediamtx rover-watch --no-pager
curl -fsS http://127.0.0.1:9997/v3/paths/get/cam | head
rover-log follow   # or: journalctl -u rover-control -f
```

Optional:

| Item | Action |
|------|--------|
| LiDAR | Plug in → `sudo udevadm trigger --subsystem-match=tty && ls -l /dev/ldlidar` |
| Piper TTS | Copy `.onnx` to `PIPER_MODEL_PATH` (default `/opt/rover-server/tts/zh_CN-huayan-medium.onnx`) — installer does **not** download the model |

---

## B. USB / serial port adjustments (prevents control crashes)

### Why control crashes after moving USB

`rover-control` opens motor + gimbal serial ports from
`/opt/rover-server/env/rover-server.env`.

| Path style | Survives plugging into a different USB jack? | Survives replacing the board/adapter? |
|------------|-----------------------------------------------|----------------------------------------|
| `/dev/serial/by-id/usb-…` | **Yes** (same physical device) | **No** — unique ID in the name changes |
| `/dev/ttyACM0` / `ttyACM1` | **No** — Linux renumbers every boot/plug order | No |

**Always use by-id.** Never leave production on `ttyACM*`.  
If Pico and the QinHeng gimbal adapter swap ACM numbers, control may open the
wrong device, hang, or crash-loop.

LiDAR should stay on the udev symlink `/dev/ldlidar` (not a raw `ttyUSB*`).

### B1. Discover current devices

```bash
ls -l /dev/serial/by-id/
# Example (rover-demo):
#   usb-MicroPython_Board_in_FS_mode_<serial>-if00  → Pico motors
#   usb-1a86_USB_Single_Serial_<serial>-if00        → F32C gimbal (QinHeng/WCH)

# Optional: see which ACM node each maps to
readlink -f /dev/serial/by-id/*
```

Identify:

- **Pico** → name contains `MicroPython_Board_in_FS_mode`
- **Gimbal** → name contains `1a86_USB_Single_Serial` (VID `1a86`)

### B2. Patch env and restart (safe procedure)

```bash
ENV=/opt/rover-server/env/rover-server.env

# 1) Stop control so it is not hammering a missing/wrong port
sudo systemctl stop rover-control

# 2) Set the live by-id paths (paste from ls output)
sudo sed -i 's|^PICO_MOTOR_SERIAL_PORT=.*|PICO_MOTOR_SERIAL_PORT=/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_XXXX-if00|' "$ENV"
sudo sed -i 's|^GIMBAL_SERIAL_PORT=.*|GIMBAL_SERIAL_PORT=/dev/serial/by-id/usb-1a86_USB_Single_Serial_XXXX-if00|' "$ENV"

# Or edit by hand:
#   sudo nano "$ENV"

# 3) Confirm
grep -E '^(PICO_MOTOR_SERIAL_PORT|GIMBAL_SERIAL_PORT)=' "$ENV"
ls -l "$(grep ^PICO_MOTOR_SERIAL_PORT= "$ENV" | cut -d= -f2-)"
ls -l "$(grep ^GIMBAL_SERIAL_PORT= "$ENV" | cut -d= -f2-)"

# 4) Start again
sudo systemctl reset-failed rover-control
sudo systemctl start rover-control
journalctl -u rover-control -n 30 --no-pager
```

Also update the git profile so the next SSD install stays correct:

```bash
# On the Pi, in the rover clone:
nano ~/rover/pi-server/env/profiles/rover-demo.env
# same PICO_MOTOR_SERIAL_PORT / GIMBAL_SERIAL_PORT values
# commit + push on ios-flow when ready
```

### B3. Helper script (known rover-demo IDs)

If this chassis still uses the original Pico + QinHeng IDs:

```bash
sudo /opt/rover-server/scripts/unblock_gimbal_serial.sh
```

That script **hardcodes** the demo by-id strings. After replacing hardware,
edit the script or use §B2 instead.

### B4. USB audio cards (mic / talk)

Moving USB audio can break `rover-audio` / `rover-talk` (usually idle-wait, not
a full control crash). Re-check card names:

```bash
aplay -l
arecord -l
grep -E '^ROVER_ALSA_|^ROVER_AUDIO_CAPTURE|^APLAY_DEVICE' /opt/rover-server/env/rover-server.env
```

Update `ROVER_ALSA_PLAYBACK_CARD`, `ROVER_ALSA_CAPTURE_CARD`,
`ROVER_AUDIO_CAPTURE`, `APLAY_DEVICE_FALLBACK` to match `CARD=…` names, then:

```bash
sudo /opt/rover-server/scripts/install_alsa.sh /opt/rover-server
sudo systemctl restart rover-audio rover-talk
```

### B5. Symptoms → fix

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `rover-control` restart loop; serial / Pico errors in log | Wrong or missing by-id; using `ttyACM*` | §B2 |
| Motors dead, gimbal works (or reverse) | Pico/gimbal ACM swapped under `ttyACM*` | Switch to by-id; assign correctly |
| Gimbal open fails after new adapter | New `…_Serial_<id>-if00` string | Update `GIMBAL_SERIAL_PORT` |
| `/dev/ldlidar` missing | Unplugged or udev not triggered | Plug + `udevadm trigger` |
| Audio unit flapping | Wrong ALSA `CARD=` name | §B4 |

---

## C. What each installer covers

### `rover-setup` (`~/pi/install.sh` → `/opt/rover-firmware`)

- apt: python, bluez, NetworkManager, curl, openssl  
- Tailscale + `tailscaled` enabled  
- Bluetooth + BlueZ Experimental  
- `rover-daemon`, `rover-pi-status`, `rover-pi-boot-finished`  
- `rover-cert-renew.timer` → `/var/lib/rover/certs/{cert.crt,cert.key}`  
- Optional: Tailscale/MQTT env drop-ins from installer env vars  

### `rover` (`~/rover/pi-server/install.sh` → `/opt/rover-server`)

- I2C enable (`raspi-config` / `config.txt`)  
- Node 20, MediaMTX, npm ci, hardware venv (`pyserial`, `smbus2`, …)  
- Copies profile → `env/rover-server.env` **only if that file does not exist yet**  
- ALSA `/etc/asound.conf`, LiDAR udev, canonical logging  
- Units: log, mediamtx, control, watch, audio, talk, lidar  

**Not automatic from git alone:** Tailscale login, BLE MQTT file, Piper `.onnx`,
and USB by-id if hardware differs from `rover-demo.env`.

---

## D. Tailscale key expiry (production)

Node keys expire (~180 days) unless disabled — bad for field units.

| Env (setup installer / drop-in) | Purpose |
|---------------------------------|---------|
| `ROVER_TAILSCALE_AUTHKEY` | Tagged auth key (`tag:rover`) — preferred |
| `ROVER_TAILSCALE_API_KEY` | API token to disable expiry after join |
| `ROVER_TAILSCALE_TAILNET` | Tailnet name (lookup helper) |

Manual: [Machines](https://login.tailscale.com/admin/machines) → device →
**Disable key expiry**.

HTTPS `tailscale cert` (~90d) renews via `rover-cert-renew.timer` while online.

---

## E. Legacy UART (optional)

Only if `sudo ROVER_TRANSPORT=uart ./install.sh`. Default is BLE — skip this.

| ESP32 | Pi header | GPIO |
|-------|-----------|------|
| GPIO 17 (TX) | Pin 10 | GPIO 15 RX |
| GPIO 16 (RX) | Pin 8 | GPIO 14 TX |
| GND | Pin 6 | GND |

`prepare_uart.sh` sets `enable_uart=1`, strips serial console from cmdline,
masks serial-getty. **Pi 5:** often need `dtparam=uart0=on` and
`ROVER_SERIAL=/dev/ttyAMA0` (not `serial0`).

Daemon env: `ROVER_SERIAL`, `ROVER_BAUD=115200`, `ROVER_DRY_RUN`,
`ROVER_CERT_DIR`, `ROVER_LOG_LEVEL`.

---

## F. Updating later

```bash
# Setup
cd ~/pi && git pull
sudo ./install.sh

# Control
cd ~/rover && git checkout ios-flow && git pull
cd pi-server && sudo ROVER_PROFILE=rover-demo ./install.sh
# Note: existing /opt/rover-server/env/rover-server.env is kept as-is
```

After USB hardware changes, still do §B even if you re-ran install.

---

## G. Agent / skill note

When the user asks to set up a new Pi, migrate an SSD, fix USB serial crashes,
or “run the installers”, follow this file. Prefer executing the commands on the
device when possible; stop `rover-control` before rewriting serial paths (§B2).
