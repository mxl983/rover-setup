---
name: ssd-migration
description: >-
  Bring up or migrate a rover Pi (new SSD, fresh OS, clone+install, USB serial
  remapping when ports move and rover-control crash-loops). Use when the user
  asks about SSD migration, new device setup, INSTALL.md, installers,
  by-id serial ports, Pico/gimbal USB, Tailscale/certs, or BLE provisioning.
---

# Rover setup / SSD migration (agent skill)

**Canonical human + agent guide:** [`INSTALL.md`](../../../INSTALL.md)
(repo root). Read and follow that file — do not invent a shorter alternate path.

## Quick map

| Step | Where |
|------|--------|
| Flash OS, clone both repos, dual installers | INSTALL.md §A |
| Tailscale + certs + BLE MQTT | INSTALL.md §A5–A6 |
| USB port moves / serial crash loops | INSTALL.md **§B** (stop control → fix by-id → restart) |
| What installers cover | INSTALL.md §C |
| Legacy UART | INSTALL.md §E |

## Hard rules for agents

1. Control branch is **`ios-flow`**, not `main`.
2. Order: **rover-setup `install.sh` first**, then **rover `pi-server/install.sh`**.
3. Serial ports must be `/dev/serial/by-id/...`, never `ttyACM*` in production.
4. Before rewriting `PICO_MOTOR_SERIAL_PORT` / `GIMBAL_SERIAL_PORT`,
   `systemctl stop rover-control`.
5. Do not commit `/var/lib/rover/mqtt.json`, Tailscale keys, or cert PEMs.
6. After fixing live `/opt/rover-server/env/rover-server.env`, mirror paths into
   `~/rover/pi-server/env/profiles/rover-demo.env` when this is the demo chassis.

## One-shot discovery (USB)

```bash
ls -l /dev/serial/by-id/
grep -E '^(PICO_MOTOR_SERIAL_PORT|GIMBAL_SERIAL_PORT)=' /opt/rover-server/env/rover-server.env
systemctl is-active rover-control rover-daemon
```
