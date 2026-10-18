#!/usr/bin/env bash
# Root finish step after code sync: venv deps, systemd unit, BT, restart.
#   sudo /opt/rover-firmware/pi/scripts/finish_ble_deploy.sh
# Or full path from repo: sudo ./scripts/finish_ble_deploy.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${ROVER_INSTALL_ROOT:-/opt/rover-firmware}"
VENV="${DEST}/venv"
UNIT_SRC="${ROOT}/systemd/rover-daemon.service"
UNIT_DST="/etc/systemd/system/rover-daemon.service"
DROPIN_DIR="/etc/systemd/system/rover-daemon.service.d"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

# Stage-copy so re-running from /opt/rover-firmware/pi is safe.
STAGE="$(mktemp -d /tmp/rover-pi-deploy.XXXXXX)"
cp -a "${ROOT}/." "${STAGE}/"
mkdir -p "${DEST}"
rm -rf "${DEST}/pi"
mkdir -p "${DEST}/pi"
cp -a "${STAGE}/." "${DEST}/pi/"
rm -rf "${STAGE}"
ROOT="${DEST}/pi"
UNIT_SRC="${ROOT}/systemd/rover-daemon.service"
chmod +x "${ROOT}/scripts/"*.sh

"${ROOT}/scripts/enable_bluetooth.sh"

if [[ -d "${VENV}" ]] && ! grep -q '^include-system-site-packages = true' "${VENV}/pyvenv.cfg" 2>/dev/null; then
  rm -rf "${VENV}"
fi
python3 -m venv --system-site-packages "${VENV}"
"${VENV}/bin/pip" install --upgrade pip
"${VENV}/bin/pip" install -r "${ROOT}/requirements.txt"

cp "${UNIT_SRC}" "${UNIT_DST}"
sed -i "s|^ExecStart=.*|ExecStart=${VENV}/bin/python -m rover_daemon|" "${UNIT_DST}"
sed -i "s|^Environment=ROVER_TRANSPORT=.*|Environment=ROVER_TRANSPORT=${ROVER_TRANSPORT:-ble}|" "${UNIT_DST}"

mkdir -p "${DROPIN_DIR}"
if [[ -n "${ROVER_MQTT_HOST:-}" || -n "${ROVER_MQTT_USER:-}" ]]; then
  {
    echo "[Service]"
    [[ -n "${ROVER_MQTT_HOST:-}" ]] && echo "Environment=ROVER_MQTT_HOST=${ROVER_MQTT_HOST}"
    [[ -n "${ROVER_MQTT_USER:-}" ]] && echo "Environment=ROVER_MQTT_USER=${ROVER_MQTT_USER}"
    [[ -n "${ROVER_MQTT_PASS:-}" ]] && echo "Environment=ROVER_MQTT_PASS=${ROVER_MQTT_PASS}"
    [[ -n "${ROVER_MQTT_PORT:-}" ]] && echo "Environment=ROVER_MQTT_PORT=${ROVER_MQTT_PORT}"
    [[ -n "${ROVER_MQTT_TOPIC_CMD:-}" ]] && echo "Environment=ROVER_MQTT_TOPIC_CMD=${ROVER_MQTT_TOPIC_CMD}"
    [[ -n "${ROVER_MQTT_RAIL_OFF:-}" ]] && echo "Environment=ROVER_MQTT_RAIL_OFF=${ROVER_MQTT_RAIL_OFF}"
  } >"${DROPIN_DIR}/mqtt.conf"
  echo "Wrote ${DROPIN_DIR}/mqtt.conf"
elif [[ ! -f "${DROPIN_DIR}/mqtt.conf" ]]; then
  echo "warning: set ROVER_MQTT_HOST/USER/PASS for rail-off (no mqtt.conf yet)" >&2
fi

systemctl daemon-reload
systemctl enable bluetooth.service rover-daemon.service
systemctl restart bluetooth.service
rfkill unblock bluetooth || true
timeout 5 bluetoothctl power on || true
systemctl restart rover-daemon.service

echo
echo "Status:"
systemctl --no-pager --full status rover-daemon bluetooth | head -40
echo
journalctl -u rover-daemon -n 30 --no-pager || true
