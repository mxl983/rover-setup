#!/usr/bin/env bash
# Enable header UART and remove kernel serial console so ESP link is free.
# Prints "REBOOT_REQUIRED" on stdout if cmdline/config changed.
# Run as root from install.sh.

set -euo pipefail

REBOOT=0

CONFIG=""
for candidate in /boot/firmware/config.txt /boot/config.txt; do
  if [[ -f "${candidate}" ]]; then
    CONFIG="${candidate}"
    break
  fi
done

CMDLINE=""
for candidate in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
  if [[ -f "${candidate}" ]]; then
    CMDLINE="${candidate}"
    break
  fi
done

if [[ -n "${CONFIG}" ]]; then
  if ! grep -qE '^enable_uart=1' "${CONFIG}"; then
    echo "" >>"${CONFIG}"
    echo "# Rover ESP UART" >>"${CONFIG}"
    echo "enable_uart=1" >>"${CONFIG}"
    REBOOT=1
    echo "==> Added enable_uart=1 to ${CONFIG}"
  fi

  if grep -q "Raspberry Pi 5" /proc/device-tree/model 2>/dev/null; then
    if ! grep -qE '^dtparam=uart0=on' "${CONFIG}"; then
      echo "dtparam=uart0=on" >>"${CONFIG}"
      REBOOT=1
      echo "==> Added dtparam=uart0=on (Pi 5 header UART)"
    fi
  fi
fi

if [[ -n "${CMDLINE}" ]]; then
  # Strip kernel console on the ESP UART (getty + console fight the daemon).
  before="$(cat "${CMDLINE}")"
  after="${before}"
  for tok in console=serial0,115200 console=ttyAMA0,115200 console=ttyS0,115200 \
    console=serial0 console=ttyAMA0 console=ttyS0; do
    after="$(echo "${after}" | sed -E "s/(^| )${tok}( |$)/ /g")"
  done
  after="$(echo "${after}" | sed -E 's/  +/ /g; s/^ //; s/ $//')"
  if [[ "${after}" != "${before}" ]]; then
    printf '%s\n' "${after}" >"${CMDLINE}"
    REBOOT=1
    echo "==> Removed serial console from ${CMDLINE}"
  fi
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
bash "${SCRIPT_DIR}/claim_uart.sh"

# Prefer raspi-config noninteractive when available (serial login off, HW on).
if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_serial_hw 0 2>/dev/null || true
  # Newer images: do_serial_cons 1 = disable console; older: do_serial 2
  raspi-config nonint do_serial_cons 1 2>/dev/null \
    || raspi-config nonint do_serial 2 2>/dev/null \
    || true
fi

if [[ "${REBOOT}" -eq 1 ]]; then
  echo "REBOOT_REQUIRED"
fi
