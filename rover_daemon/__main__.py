"""python -m rover_daemon"""

from __future__ import annotations

import logging
import signal
import sys

from ._vendor_path import ensure_vendor_path

ensure_vendor_path()

from .config import load_config
from .daemon import RoverDaemon


def main() -> int:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    daemon = RoverDaemon(cfg)

    def _stop(*_args) -> None:
        logging.getLogger("rover").info("signal received — stopping")
        daemon.stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        daemon.run()
    except Exception:
        logging.exception("daemon crashed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
