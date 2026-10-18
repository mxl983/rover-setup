#!/usr/bin/env bash
# Install rover BLE daemon on a Pi that already has Raspberry Pi OS.
# Run ON the Pi, from this directory:
#   cd /path/to/pi && sudo ./install.sh
#
# Optional env for unattended Tailscale join (Phase 2):
#   sudo ROVER_TAILSCALE_AUTHKEY=tskey-auth-... ./install.sh
#
# Optional MQTT rail-off (required for idle/factory Pi power cut):
#   sudo ROVER_MQTT_HOST=... ROVER_MQTT_USER=... ROVER_MQTT_PASS=... ./install.sh
#
# Legacy UART transport:
#   sudo ROVER_TRANSPORT=uart ./install.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
DEST="${ROVER_INSTALL_ROOT:-/opt/rover-firmware}"
VENV="${DEST}/venv"
UNIT_SRC="${ROOT}/systemd/rover-daemon.service"
UNIT_DST="/etc/systemd/system/rover-daemon.service"
DROPIN_DIR="/etc/systemd/system/rover-daemon.service.d"
TRANSPORT="${ROVER_TRANSPORT:-ble}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo ./install.sh"
  exit 1
fi

echo "==> Installing rover daemon into ${DEST} (transport=${TRANSPORT})"

apt-get update -qq
apt-get install -y \
  python3 python3-pip python3-venv \
  python3-dbus python3-gi gir1.2-glib-2.0 \
  libdbus-1-3 \
  bluez bluetooth rfkill \
  network-manager curl ca-certificates openssl

echo "==> Ensuring NetworkManager + Tailscale start on every boot"
systemctl enable NetworkManager.service 2>/dev/null || true
systemctl start NetworkManager.service 2>/dev/null || true

echo "==> Enabling Bluetooth (BLE setup peripheral)"
if [[ -x "${ROOT}/scripts/enable_bluetooth.sh" ]]; then
  "${ROOT}/scripts/enable_bluetooth.sh" || true
else
  systemctl enable bluetooth.service 2>/dev/null || true
  systemctl start bluetooth.service 2>/dev/null || true
  rfkill unblock bluetooth 2>/dev/null || true
  timeout 5 bluetoothctl power on 2>/dev/null || true
fi
if systemctl is-active --quiet bluetooth.service; then
  echo "    bluetooth: active"
else
  echo "    warning: bluetooth.service not active yet"
fi

echo "==> Ensuring Tailscale is installed"
if command -v tailscale >/dev/null 2>&1; then
  echo "    already present: $(tailscale version 2>/dev/null | head -1 || echo ok)"
else
  curl -fsSL https://tailscale.com/install.sh | sh
  if ! command -v tailscale >/dev/null 2>&1; then
    echo "ERROR: Tailscale install failed" >&2
    exit 1
  fi
  echo "    installed: $(tailscale version 2>/dev/null | head -1 || echo ok)"
fi

echo "==> Enabling headless Tailscale service (tailscaled) — on by default at boot"
systemctl enable tailscaled.service
systemctl start tailscaled.service 2>/dev/null || true
systemctl is-active --quiet tailscaled.service \
  && echo "    tailscaled: active" \
  || echo "    warning: tailscaled not active yet (will retry after network)"

# Never `rm` the tree we're copying from (common when re-running
# /opt/rover-firmware/pi/install.sh). Stage via temp, then replace.
mkdir -p "${DEST}"
STAGE="$(mktemp -d /tmp/rover-pi-install.XXXXXX)"
cleanup_stage() { rm -rf "${STAGE}"; }
trap cleanup_stage EXIT
cp -a "${ROOT}/." "${STAGE}/"
rm -rf "${DEST}/pi"
mkdir -p "${DEST}/pi"
cp -a "${STAGE}/." "${DEST}/pi/"
chmod +x "${DEST}/pi/scripts/"*.sh
trap - EXIT
cleanup_stage

NEED_REBOOT=0
if [[ "${TRANSPORT}" == "uart" ]]; then
  echo "==> Preparing UART (legacy transport — disable serial getty / console)"
  PREPARE_OUT="$("${DEST}/pi/scripts/prepare_uart.sh" 2>&1 || true)"
  echo "${PREPARE_OUT}"
  if echo "${PREPARE_OUT}" | grep -q "REBOOT_REQUIRED"; then
    NEED_REBOOT=1
  fi
else
  echo "==> BLE transport — skipping UART claim (ESP setup uses GATT, not /dev/serial0)"
fi

# Prefer system-site-packages so optional python3-dbus/gi remain available.
if [[ -d "${VENV}" ]] && ! grep -q '^include-system-site-packages = true' "${VENV}/pyvenv.cfg" 2>/dev/null; then
  echo "==> Recreating venv with --system-site-packages"
  rm -rf "${VENV}"
fi
python3 -m venv --system-site-packages "${VENV}"
"${VENV}/bin/pip" install --upgrade pip
"${VENV}/bin/pip" install -r "${DEST}/pi/requirements.txt"
if [[ "${TRANSPORT}" == "uart" ]]; then
  "${VENV}/bin/pip" install 'pyserial>=3.5'
fi

# Verify BLE imports when using default transport (dbus-next + paho; vendor/ fallback).
if [[ "${TRANSPORT}" == "ble" ]]; then
  if ! "${VENV}/bin/python" -c "
import sys
from pathlib import Path
v = Path('${DEST}/pi/vendor')
if v.is_dir():
    sys.path.insert(0, str(v))
import dbus_next
import paho.mqtt.client
print('ok', dbus_next.__file__)
" 2>/dev/null; then
    echo "ERROR: dbus-next / paho-mqtt not importable" >&2
    exit 1
  fi
fi

cp "${UNIT_SRC}" "${UNIT_DST}"
sed -i "s|^ExecStart=.*|ExecStart=${VENV}/bin/python -m rover_daemon|" "${UNIT_DST}"
sed -i "s|^Environment=ROVER_TRANSPORT=.*|Environment=ROVER_TRANSPORT=${TRANSPORT}|" "${UNIT_DST}"

if [[ "${TRANSPORT}" == "uart" ]]; then
  CLAIM_UART="${DEST}/pi/scripts/claim_uart.sh"
  SERIAL_DEV="/dev/serial0"
  if grep -q "Raspberry Pi 5" /proc/device-tree/model 2>/dev/null; then
    if [[ -e /dev/ttyAMA0 ]]; then
      SERIAL_DEV="/dev/ttyAMA0"
      echo "==> Pi 5 detected — defaulting ROVER_SERIAL=${SERIAL_DEV}"
    fi
  fi
  # Restore UART ExecStartPre + serial env for legacy mode.
  if ! grep -q '^ExecStartPre=' "${UNIT_DST}"; then
    sed -i "/^ExecStart=/i ExecStartPre=${CLAIM_UART}" "${UNIT_DST}"
  else
    sed -i "s|^ExecStartPre=.*|ExecStartPre=${CLAIM_UART}|" "${UNIT_DST}"
  fi
  if grep -q '^Environment=ROVER_SERIAL=' "${UNIT_DST}"; then
    sed -i "s|^Environment=ROVER_SERIAL=.*|Environment=ROVER_SERIAL=${SERIAL_DEV}|" "${UNIT_DST}"
  else
    sed -i "/Environment=ROVER_TRANSPORT=/a Environment=ROVER_SERIAL=${SERIAL_DEV}\nEnvironment=ROVER_BAUD=115200" "${UNIT_DST}"
  fi
fi

# HTTPS cert renew timer (Let's Encrypt via `tailscale cert`; ~90d lifetime).
CERT_UNIT_SRC="${ROOT}/systemd/rover-cert-renew.service"
CERT_TIMER_SRC="${ROOT}/systemd/rover-cert-renew.timer"
cp "${CERT_UNIT_SRC}" /etc/systemd/system/rover-cert-renew.service
cp "${CERT_TIMER_SRC}" /etc/systemd/system/rover-cert-renew.timer
chmod +x "${DEST}/pi/scripts/"*.sh
mkdir -p /var/lib/rover/certs

# Persist Tailscale + MQTT env from installer environment into drop-ins.
mkdir -p "${DROPIN_DIR}"

DROPIN_TS="${DROPIN_DIR}/tailscale.conf"
{
  echo "[Service]"
  if [[ -n "${ROVER_TAILSCALE_AUTHKEY:-}" ]]; then
    echo "Environment=ROVER_TAILSCALE_AUTHKEY=${ROVER_TAILSCALE_AUTHKEY}"
  fi
  if [[ -n "${ROVER_TAILSCALE_API_KEY:-}" ]]; then
    echo "Environment=ROVER_TAILSCALE_API_KEY=${ROVER_TAILSCALE_API_KEY}"
  fi
  if [[ -n "${ROVER_TAILSCALE_TAILNET:-}" ]]; then
    echo "Environment=ROVER_TAILSCALE_TAILNET=${ROVER_TAILSCALE_TAILNET}"
  fi
} >"${DROPIN_TS}"
if [[ ! -s "${DROPIN_TS}" ]] || ! grep -q '^Environment=' "${DROPIN_TS}"; then
  rm -f "${DROPIN_TS}"
else
  echo "==> Wrote Tailscale env drop-in ${DROPIN_TS}"
fi

DROPIN_MQTT="${DROPIN_DIR}/mqtt.conf"
{
  echo "[Service]"
  if [[ -n "${ROVER_MQTT_HOST:-}" ]]; then
    echo "Environment=ROVER_MQTT_HOST=${ROVER_MQTT_HOST}"
  fi
  if [[ -n "${ROVER_MQTT_USER:-}" ]]; then
    echo "Environment=ROVER_MQTT_USER=${ROVER_MQTT_USER}"
  fi
  if [[ -n "${ROVER_MQTT_PASS:-}" ]]; then
    echo "Environment=ROVER_MQTT_PASS=${ROVER_MQTT_PASS}"
  fi
  if [[ -n "${ROVER_MQTT_PORT:-}" ]]; then
    echo "Environment=ROVER_MQTT_PORT=${ROVER_MQTT_PORT}"
  fi
  if [[ -n "${ROVER_MQTT_TOPIC_CMD:-}" ]]; then
    echo "Environment=ROVER_MQTT_TOPIC_CMD=${ROVER_MQTT_TOPIC_CMD}"
  fi
  if [[ -n "${ROVER_MQTT_RAIL_OFF:-}" ]]; then
    echo "Environment=ROVER_MQTT_RAIL_OFF=${ROVER_MQTT_RAIL_OFF}"
  fi
} >"${DROPIN_MQTT}"
if [[ ! -s "${DROPIN_MQTT}" ]] || ! grep -q '^Environment=' "${DROPIN_MQTT}"; then
  rm -f "${DROPIN_MQTT}"
  echo "==> MQTT env drop-in omitted (optional) — BLE WIFI_CONFIG provisions /var/lib/rover/mqtt.json"
else
  echo "==> Wrote optional MQTT override drop-in ${DROPIN_MQTT}"
fi
mkdir -p /var/lib/rover
chmod 755 /var/lib/rover

systemctl daemon-reload
systemctl enable rover-daemon.service
systemctl enable tailscaled.service
systemctl enable bluetooth.service 2>/dev/null || true
systemctl enable rover-cert-renew.timer

if [[ "${NEED_REBOOT}" -eq 1 ]]; then
  echo
  echo "==> UART config changed — reboot required."
  echo "    sudo reboot"
  echo "    After reboot: systemctl status rover-daemon bluetooth tailscaled"
  echo
  exit 0
fi

systemctl restart bluetooth.service 2>/dev/null || true
rfkill unblock bluetooth 2>/dev/null || true
timeout 5 bluetoothctl power on 2>/dev/null || true
systemctl restart rover-daemon.service
systemctl start rover-cert-renew.timer
systemctl start rover-cert-renew.service || true

echo
echo "==> Installed — zero customer manual network setup after this."
echo "    On every boot: NetworkManager + bluetooth + tailscaled + rover-daemon"
echo "    BLE setup: advertises J2-Pi; ESP sends WIFI_CONFIG over GATT"
echo "    Rail-off: MQTT OFF GPIO16 (not UART SLEEP)"
echo "    Certs: /var/lib/rover/certs/{cert.crt,cert.key} (auto-renew weekly)"
echo
echo "    Status:  systemctl status rover-daemon bluetooth tailscaled"
echo "    Logs:    journalctl -u rover-daemon -f"
echo "    Advertise check: sudo bluetoothctl show | grep -i powered"
echo "    Spec:    ${DEST}/pi/BLE_SETUP_PROTOCOL.md"
echo
systemctl --no-pager --full status rover-daemon || true
systemctl is-enabled rover-daemon bluetooth tailscaled NetworkManager rover-cert-renew.timer 2>/dev/null || true
