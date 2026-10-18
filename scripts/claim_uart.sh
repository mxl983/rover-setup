#!/usr/bin/env bash
# Stop/mask serial login gettys so rover-daemon owns the ESP UART exclusively.
# Safe to run repeatedly (install.sh + systemd ExecStartPre).

set -euo pipefail

GETTYS=(
  serial-getty@ttyS0.service
  serial-getty@ttyAMA0.service
  serial-getty@serial0.service
  serial-getty@ttyAMA10.service
)

for unit in "${GETTYS[@]}"; do
  systemctl stop "${unit}" 2>/dev/null || true
  systemctl disable "${unit}" 2>/dev/null || true
  systemctl mask "${unit}" 2>/dev/null || true
done

exit 0
