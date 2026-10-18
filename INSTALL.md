# Install rover daemon on a fresh Raspberry Pi (4B & 5)

This guide takes a **new Raspberry Pi OS** install through enabling Bluetooth,
copying the BLE daemon, and verifying the ESP can provision Wi‑Fi over GATT.

Supported boards: **Raspberry Pi 4 Model B** and **Raspberry Pi 5**.

**Default transport is BLE** (`ROVER_TRANSPORT=ble`). UART setup wiring is
legacy only (`ROVER_TRANSPORT=uart`). ESP still controls Pi power via GPIO16;
rail-off is MQTT `OFF GPIO16`, not UART `SLEEP`.

Protocol: [`BLE_SETUP_PROTOCOL.md`](BLE_SETUP_PROTOCOL.md).

---

## 0. What you need

| Item | Notes |
|------|--------|
| Pi 4B or Pi 5 | Fresh Raspberry Pi OS (Bookworm recommended) |
| Network | Ethernet or Wi‑Fi so you can `apt` / copy files |
| ESP32 rover portal | BLE for setup; GPIO16 for Pi rail power |
| Phone + ESP | BLE `WIFI_CONFIG` includes MQTT + `wake_secret` |
| This repo’s `pi/` folder | Copied onto the Pi |

**Hardware:** unplug ESP↔Pi UART RX/TX for setup. Keep power control via ESP GPIO16.

---

## 1. Flash the OS

1. Use [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
2. Choose **Raspberry Pi OS (64-bit)** Lite or Desktop — either works.
3. In Imager options, set hostname, user/password, enable SSH, and (optional) Wi‑Fi.
4. Boot the Pi, SSH in:

```bash
ssh <user>@<pi-hostname>.local
```

Update packages:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

---

## 2. Wiring (ESP32 ↔ Pi)

**BLE setup (default):** no UART RX/TX between ESP and Pi. ESP still drives the
Pi power rail (GPIO16 HIGH = power on). Setup credentials go over BLE GATT.

**Legacy UART only** (`ROVER_TRANSPORT=uart`):

| ESP32 | Pi 40-pin header | Pi GPIO name |
|-------|------------------|--------------|
| **GPIO 17** (ESP TX) | **Pin 10** | GPIO 15 (RXD) |
| **GPIO 16** (ESP RX) | **Pin 8** | GPIO 14 (TXD) |
| **GND** | **Pin 6** (or any GND) | GND |

Baud rate: **115200 8N1** (legacy).

---

## 3. Enable Bluetooth (BLE) / UART (legacy)

**Preferred:** `sudo ./install.sh` enables Bluetooth and starts the BLE daemon.
For legacy UART it also runs `scripts/prepare_uart.sh`, which:

- enables `enable_uart=1` (and Pi 5 `dtparam=uart0=on`)
- removes `console=serial0` / `ttyS0` / `ttyAMA0` from cmdline
- **masks serial-getty** so a login shell cannot steal the ESP UART
- systemd `ExecStartPre` re-claims the port on every daemon start

If you already installed once, re-run `sudo ./install.sh` (or
`sudo /opt/rover-firmware/pi/scripts/prepare_uart.sh` then reboot if asked).

### 3a. Manual fallback — raspi-config

```bash
sudo raspi-config
```

**Interface Options → Serial Port**

1. Login shell over serial? → **No**
2. Serial port hardware enabled? → **Yes**

Then:

```bash
sudo /opt/rover-firmware/pi/scripts/claim_uart.sh
sudo systemctl restart rover-daemon
```

### 3b. Pi 4B — confirm `/boot/firmware/config.txt`

On Bookworm the file is `/boot/firmware/config.txt` (older images used `/boot/config.txt`).

You should see something like:

```ini
enable_uart=1
```

After reboot, check:

```bash
ls -l /dev/serial0
# typically → ttyS0 or ttyAMA0
```

Use **`/dev/serial0`** in the daemon (default). That is the console UART alias on Pi 4B GPIO 14/15 when serial is enabled correctly.

### 3c. Pi 5 — GPIO 14/15 UART (important)

On Pi 5, **`/dev/serial0` often points at the debug connector UART**, not the 40-pin header. For the ESP on pins 8/10 you must enable **UART0 on GPIO 14/15**.

Edit config:

```bash
sudo nano /boot/firmware/config.txt
```

Add at the bottom:

```ini
# Rover: UART on 40-pin header (GPIO14 TX, GPIO15 RX)
dtparam=uart0=on
```

Optional (makes `/dev/serial0` follow that UART — only if you also removed serial console from cmdline, or accept console on those pins):

```ini
dtparam=uart0_console=on
```

Safer for the rover: leave `serial0` alone and point the daemon at **`/dev/ttyAMA0`**:

```bash
ls -l /dev/ttyAMA0
```

Then set in the systemd unit (step 6):

```ini
Environment=ROVER_SERIAL=/dev/ttyAMA0
```

Reboot:

```bash
sudo reboot
```

### 3d. Quick UART sanity check

With ESP powered and sending (or loopback TX↔RX for a bench test):

```bash
# Pi 4B default
sudo cat /dev/serial0

# Pi 5 header UART (typical)
sudo cat /dev/ttyAMA0
```

You should eventually see JSON heartbeats once the daemon (or ESP) is running, e.g. `{"type":"HEARTBEAT",...}`.

---

## 4. Install system dependencies

```bash
sudo apt install -y python3 python3-pip python3-venv git network-manager curl
```

**Tailscale** is installed automatically by `sudo ./install.sh` (official
Tailscale install script). You do **not** need to run `tailscale up` by hand —
ESP onboarding (or `ROVER_TAILSCALE_AUTHKEY`) joins the tailnet later.

Manual fallback only if you skipped the installer:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

NetworkManager (`nmcli`) is used to wipe/connect Wi‑Fi during setup. Raspberry Pi OS Bookworm usually includes it; the package line above ensures it.

---

## 5. Copy the daemon onto the Pi and install

The OS will **not** load this code by itself from the SD/NVMe. You copy
`pi/` onto the Pi, then run the installer **once**.

### Easiest — `install.sh`

From your Mac (repo root):

```bash
scp -r pi user@pi-hostname.local:~/
```

On the Pi:

```bash
cd ~/pi
sudo ./install.sh
```

That script:

1. Installs `python3` / venv / NetworkManager packages  
2. Copies itself to `/opt/rover-firmware/pi`  
3. Creates `/opt/rover-firmware/venv` and installs `requirements.txt`  
4. Installs + enables `rover-daemon.service`  
5. On Pi 5, prefers `/dev/ttyAMA0` when present  

Then check:

```bash
sudo systemctl status rover-daemon
sudo journalctl -u rover-daemon -f
```

### Manual (same result)

See older step-by-step below only if you prefer not to use the script.

---

## 6. First-boot checklist

| Check | Command / expectation |
|-------|------------------------|
| Service running | `systemctl is-active rover-daemon` → `active` |
| UART device exists | `ls -l $ROVER_SERIAL` |
| Heartbeats TX | `journalctl -u rover-daemon` shows TX/STATE; ESP Serial Monitor shows `[UART] RX {"type":"HEARTBEAT"...}` |
| ESP sees Pi | ESP leaves `WAKING_PI_FOR_SETUP` after heartbeat |
| Dry-run bring-up | Set `ROVER_DRY_RUN=1`, restart service, exercise BLE setup without wiping Wi‑Fi |

Disable dry-run for real robots:

```bash
sudo systemctl edit rover-daemon
# or remove ROVER_DRY_RUN from the unit, then:
sudo systemctl daemon-reload
sudo systemctl restart rover-daemon
```

---

## 7. Environment reference

| Variable | Default | Meaning |
|----------|---------|---------|
| `ROVER_SERIAL` | `/dev/serial0` | UART device (`/dev/ttyAMA0` on many Pi 5 setups) |
| `ROVER_BAUD` | `115200` | Must match ESP |
| `ROVER_DRY_RUN` | `0` | `1` = skip nmcli wipe / Tailscale / shutdown |
| `ROVER_CERT_DIR` | `/var/lib/rover/certs` | HTTPS cert dir for MagicDNS (`cert.crt` / `cert.key`) |
| `ROVER_CERT_RENEW_WITHIN_DAYS` | `21` | Reissue when fewer than N days remain |
| `ROVER_LOG_LEVEL` | `INFO` | `DEBUG` for full TX/RX |

---

## 8. Updating the daemon later

```bash
# from Mac
scp -r pi user@pi-hostname.local:~/
# on Pi
sudo rm -rf /opt/rover-firmware/pi
sudo mv ~/pi /opt/rover-firmware/pi
sudo systemctl restart rover-daemon
```

---

## 9. Troubleshooting

| Symptom | Likely fix |
|---------|------------|
| `No such file or directory: '/dev/serial0'` | UART not enabled; re-run raspi-config + reboot |
| Pi 5: no data on serial0 | Use `/dev/ttyAMA0` + `dtparam=uart0=on` |
| Permission denied on serial | Service should run as `root` (default unit) |
| ESP never leaves WAKING_PI | TX/RX swapped, GND missing, or wrong `/dev` |
| `nmcli: command not found` | `sudo apt install network-manager` |
| `tailscale: not found` | Re-run `sudo ./install.sh` (installs Tailscale) |
| Daemon changes Wi‑Fi while testing | Keep `ROVER_DRY_RUN=1` until ESP↔Pi path is proven (live setup no longer drops Wi‑Fi before a successful rejoin) |
| Login shell steals UART | Fixed by install: `claim_uart.sh` masks getty; re-run install or that script |

### Pi 4B vs Pi 5 summary

| | Pi 4B | Pi 5 |
|--|-------|------|
| Enable serial | raspi-config + `enable_uart=1` | raspi-config + `dtparam=uart0=on` |
| Typical device for ESP on pins 8/10 | `/dev/serial0` | `/dev/ttyAMA0` |
| Debug connector UART | N/A (use header) | Separate; don’t assume `serial0` is the header |

---

## 10. Zero-maintenance Tailscale (no key expiry)

By default Tailscale **node keys expire** (~180 days). After expiry the device
falls off the tailnet until someone re-authenticates — **bad for customers**.

The daemon will try to **disable key expiry** after a successful join when you set:

| Env var | Purpose |
|---------|---------|
| `ROVER_TAILSCALE_API_KEY` | Tailscale API access token (Admin → Settings → Keys) |
| `ROVER_TAILSCALE_TAILNET` | Your tailnet name (optional fallback for device lookup) |
| `ROVER_TAILSCALE_AUTHKEY` | **Recommended:** reusable/one-off **tagged** auth key (`tag:rover`) |

### Recommended production setup

1. In Tailscale ACL, define `tag:rover` for devices.
2. Create an **auth key** that assigns `tag:rover`  
   ([tagged devices have key expiry disabled by default](https://tailscale.com/docs/features/access-control/key-expiry)).
3. Create an **API key** and put both in the systemd unit:

```ini
Environment=ROVER_TAILSCALE_AUTHKEY=tskey-auth-...
Environment=ROVER_TAILSCALE_API_KEY=tskey-api-...
Environment=ROVER_TAILSCALE_TAILNET=your-tailnet
```

4. After first connect, confirm in Admin → Machines that the rover shows
   **Key expiry disabled**.

### Manual fallback (SSO / interactive login)

If you only use the browser login URL (no auth key / API key):

1. Open [Machines](https://login.tailscale.com/admin/machines)
2. Find the Pi → **⋮** → **Disable key expiry**

Do this once per device after onboarding.

### What does *not* need customer maintenance

| Thing | Expires? | Notes |
|-------|----------|--------|
| Tailscale **node key** | Yes unless disabled | Must disable (API / tagged / admin UI) |
| Auth key used at join | Yes (1–90 days) | Only needed for *new* joins; existing nodes stay |
| HTTPS `tailscale cert` | ~90 days | Auto-renews if node stays online; not used by default daemon |

---

## 11. Related docs in this folder

- [`README.md`](README.md) — daemon behavior overview  
- [`UART_PROTOCOL.md`](UART_PROTOCOL.md) — JSON line messages ESP ↔ Pi  
- Root [`../README.md`](../README.md) — ESP GPIO wiring (power button, GPIO 4, etc.)
