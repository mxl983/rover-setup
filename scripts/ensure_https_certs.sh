#!/usr/bin/env bash
# Issue/renew Tailscale HTTPS certs for this node's MagicDNS name.
# Installed as rover-cert-renew.service (weekly timer + on-demand).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"
CERT_DIR="${ROVER_CERT_DIR:-/var/lib/rover/certs}"
RENEW_WITHIN="${ROVER_CERT_RENEW_WITHIN_DAYS:-21}"

PY="${ROVER_PYTHON:-}"
if [[ -z "${PY}" ]]; then
  if [[ -x /opt/rover-firmware/venv/bin/python ]]; then
    PY=/opt/rover-firmware/venv/bin/python
  else
    PY=python3
  fi
fi

exec "${PY}" - <<PY
from rover_daemon import network
ok = network.ensure_https_certs(
    dry_run=False,
    cert_dir="${CERT_DIR}",
    renew_within_days=int("${RENEW_WITHIN}"),
)
raise SystemExit(0 if ok else 1)
PY
