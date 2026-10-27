---
name: ssd-migration
description: >-
  Migrate a rover Pi to a new SSD/NVMe: flash Raspberry Pi OS, clone
  rover-setup + rover, run both installers, then finish BLE/Tailscale/certs
  and hardware checks. Use when the user asks about SSD migration, fresh Pi
  install, reinstall, clone+install, or bringing up rover-demo on new media.
---

# Rover SSD / fresh-Pi migration

## Goal

After flashing a new drive: **clone two repos → run two installers → finish
provisioning** so control, media, BLE setup, certs, and hardware match the
previous working Pi.

## Repos

| Role | Clone URL | Local path (convention) | Installer |
|------|-----------|-------------------------|-----------|
| Rover setup (BLE daemon, Tailscale, certs) | `git@github.com:mxl983/rover-setup.git` | `~/pi` | `sudo ./install.sh` |
| Rover control (API, MediaMTX, motors, audio) | `git@github.com:mxl983/rover.git` | `~/rover` | `cd pi-server && sudo ./install.sh` |

**Branch for control:** use `ios-flow` (production branch on this fleet) unless
you intentionally switch:

```bash
git clone git@github.com:mxl983/rover.git ~/rover
cd ~/rover && git checkout ios-flow
```

## Target OS

- **Raspberry Pi OS 64-bit** (Lite or Desktop), Bookworm or **Trixie**
- Reference image used on `rover-demo`: **2026-06-18** (Debian 13 / trixie)
- Boards: Pi 4B and Pi 5

In Raspberry Pi Imager: set hostname, user, SSH, locale; optional Wi‑Fi for
first `apt`/`git` (BLE can provision Wi‑Fi later).

## Order of operations

```bash
# 1) OS update (recommended once)
sudo apt update && sudo apt full-upgrade -y && sudo reboot

# 2) Clone
git clone git@github.com:mxl983/rover-setup.git ~/pi
git clone git@github.com:mxl983/rover.git ~/rover
cd ~/rover && git checkout ios-flow

# 3) Setup first (certs path + BLE + Tailscale + daemon)
cd ~/pi
sudo ./install.sh
# Optional unattended Tailscale join:
# sudo ROVER_TAILSCALE_AUTHKEY=tskey-auth-... ./install.sh

# 4) Control stack (needs certs dir; I2C enabled here)
cd ~/rover/pi-server
sudo ROVER_PROFILE=rover-demo ./install.sh

# 5) Reboot if /dev/i2c-1 missing after install
sudo reboot
```

Installers already cover: NetworkManager, Bluetooth (+ BlueZ Experimental),
`tailscaled`, `rover-daemon` + pi-status units, weekly `rover-cert-renew`,
Node 20, MediaMTX, Python hardware venv, ALSA `/etc/asound.conf`, I2C,
LiDAR udev, and systemd units for control/media/audio/talk/watch/log/lidar.

## Post-install action checklist

Do these after the two installers — they are **not** fully automatic from git.

### 1. Provision network + MQTT (BLE)

1. Power the ESP so it can talk to the Pi over BLE (`J2-Pi`).
2. Run phone / ESP setup so `WIFI_CONFIG` writes `/var/lib/rover/mqtt.json`.
3. Confirm:

```bash
systemctl is-active rover-daemon bluetooth tailscaled
sudo ls -l /var/lib/rover/mqtt.json
journalctl -u rover-daemon -n 50 --no-pager
```

### 2. Tailscale + HTTPS certs

- Prefer tagged auth key at install time, or complete join via BLE onboarding.
- Disable node key expiry (tagged auth key or Admin → Machines).
- Issue certs (timer also runs weekly):

```bash
sudo systemctl start rover-cert-renew.service
sudo ls -l /var/lib/rover/certs/cert.crt /var/lib/rover/certs/cert.key
```

Control + MediaMTX expect those paths. If missing, control will warn at install.

### 3. Confirm hardware profile

`ROVER_PROFILE=rover-demo` copies
`pi-server/env/profiles/rover-demo.env` → `/opt/rover-server/env/rover-server.env`
**only when that dest file does not already exist**.

On a new chassis, edit serial **by-id** paths after plugging USB devices:

```bash
ls -l /dev/serial/by-id/
sudo nano /opt/rover-server/env/rover-server.env
# PICO_MOTOR_SERIAL_PORT=...
# GIMBAL_SERIAL_PORT=...
sudo systemctl restart rover-control
```

Optional helper (same repo, after install):

```bash
sudo /opt/rover-server/scripts/unblock_gimbal_serial.sh
```

### 4. Reboot once for I2C (if needed)

```bash
ls /dev/i2c-1 || sudo reboot
# after reboot:
i2cdetect -y 1
systemctl restart rover-control
```

### 5. Verify core services

```bash
systemctl status rover-mediamtx rover-control rover-watch \
  rover-audio rover-talk rover-lidar rover-daemon --no-pager
rover-log follow   # or: journalctl -u rover-control -f
curl -fsS http://127.0.0.1:9997/v3/paths/get/cam | head
```

### 6. Optional: Piper TTS voice model

Installer installs `piper-tts` but **does not** download the `.onnx` model.
If voice TTS is required:

```bash
# Place model at path in rover-server.env (default):
# PIPER_MODEL_PATH=/opt/rover-server/tts/zh_CN-huayan-medium.onnx
sudo mkdir -p /opt/rover-server/tts
# copy model from backup / download into that path
sudo systemctl restart rover-control
```

### 7. Pi 5–only boot notes

On Pi 5, if ESP UART (legacy) is used: `dtparam=uart0=on` and often
`ROVER_SERIAL=/dev/ttyAMA0` (handled when `ROVER_TRANSPORT=uart`). Default is
**BLE** — no UART claim. The `nospi10` overlay is Pi 5–specific and not
required on Pi 4.

### 8. What you do **not** need to re-run

- Home one-offs like `~/apply-pi-cert-setup.sh` (superseded by `rover-setup` install + cert timer)
- Manual `raspi-config` for I2C (control installer enables it)
- Docker compose for production (systemd path is canonical)

## Smoke test (migration done)

| Check | Expect |
|-------|--------|
| BLE advertise | `bluetoothctl show` powered; ESP sees `J2-Pi` |
| Tailscale | `tailscale status` shows this host |
| Certs | `/var/lib/rover/certs/cert.{crt,key}` present |
| Camera | MediaMTX `cam` path ready; iOS WebRTC works |
| Drive | Pico by-id open; motors respond |
| Gimbal | F32C serial by-id; pan/tilt API works |
| Idle power | power-saving API + ESP rail-off still works |
| Audio | mic/talk units idle-wait if USB audio missing; no CPU storm |

## Agent notes

- Never commit `/var/lib/rover/mqtt.json`, Tailscale keys, or raw certs.
- Prefer updating `pi-server/env/profiles/<name>.env` in **rover** for
  chassis-specific paths (profiles are tracked; secrets stay out).
- After pulling new code on a live Pi: re-run the relevant `install.sh` (or
  rsync + `systemctl restart`), do not only `git pull` into `~/` without
  deploying to `/opt/...`.
