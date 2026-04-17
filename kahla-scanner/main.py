"""Kahla Scanner entrypoint. Runs the APScheduler in-process."""
from __future__ import annotations

import logging
import signal
import sys

from config import config
from jobs.scheduler import build_scheduler


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Quiet chatty libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def main() -> int:
    _setup_logging()
    log = logging.getLogger("kahla-scanner")
    log.info("starting scanner: sports=%s", ",".join(config.sports_enabled))

    sched = build_scheduler()

    def _shutdown(*_a):
        log.info("shutdown signal received")
        try:
            sched.shutdown(wait=False)
        finally:
            sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    sched.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
