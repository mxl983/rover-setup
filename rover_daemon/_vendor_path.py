"""Ensure vendored pure-Python deps (paho-mqtt, dbus-next) are importable."""

from __future__ import annotations

import sys
from pathlib import Path


def ensure_vendor_path() -> Path | None:
    # pi/vendor next to rover_daemon/
    vendor = Path(__file__).resolve().parent.parent / "vendor"
    if vendor.is_dir():
        path = str(vendor)
        if path not in sys.path:
            sys.path.insert(0, path)
        return vendor
    return None
