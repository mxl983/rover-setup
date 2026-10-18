#!/usr/bin/env bash
# Enable BlueZ adapter for J2-Pi BLE advertising. Run as root.
set -euo pipefail
systemctl enable bluetooth.service 2>/dev/null || true
systemctl start bluetooth.service 2>/dev/null || true
rfkill unblock bluetooth 2>/dev/null || true
timeout 5 bluetoothctl power on 2>/dev/null || true
timeout 5 bluetoothctl pairable on 2>/dev/null || true
timeout 5 bluetoothctl discoverable on 2>/dev/null || true
# LE peripheral advertising from userspace often needs Experimental.
MAIN=/etc/bluetooth/main.conf
if [[ -f "${MAIN}" ]] && ! grep -qE '^\s*Experimental\s*=\s*true' "${MAIN}"; then
  if grep -qE '^\s*#?\s*Experimental\s*=' "${MAIN}"; then
    sed -i 's/^\s*#\?\s*Experimental\s*=.*/Experimental = true/' "${MAIN}"
  else
    if grep -q '^\[General\]' "${MAIN}"; then
      sed -i '/^\[General\]/a Experimental = true' "${MAIN}"
    else
      printf '\n[General]\nExperimental = true\n' >>"${MAIN}"
    fi
  fi
  systemctl restart bluetooth.service
  sleep 1
  rfkill unblock bluetooth 2>/dev/null || true
  timeout 5 bluetoothctl power on 2>/dev/null || true
  echo "Enabled BlueZ Experimental=true (restarted bluetooth)"
fi
bluetoothctl show 2>/dev/null | head -20 || true
