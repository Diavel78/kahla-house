"""THE CELLAR — entrypoint.

    python -m cellar              # run the daemon
    python -m cellar --status     # who owns what right now (read-only)
    python -m cellar --selftest   # offline checks, no network, no creds

Spec: docs/cellar-migration-spec.md
"""
from __future__ import annotations

import logging
import os
import sys
import time


def _setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("CELLAR_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
    )
    logging.Formatter.converter = time.localtime


def _supabase():
    """Reuse app.py's client so credentials resolve exactly one way."""
    import app as _app
    sb = _app.get_supabase()
    if sb is None:
        raise SystemExit("supabase unavailable — check SUPABASE_URL / SUPABASE_SERVICE_KEY")
    return sb


def cmd_status() -> int:
    from . import config
    from .lease import Lease
    lease = Lease(_supabase(), config.OWNER)
    rows = lease.status()
    if not rows:
        print("no lease rows — apply kahla-scanner/supabase/cellar.sql")
        return 1
    print(f"{'LANE':<16} {'OWNER':<8} {'AGE':>8}  NOTE")
    for r in rows:
        print(f"{r['lane']:<16} {r['owner']:<8} {'':>8}  {r.get('note') or ''}")
    return 0


def cmd_selftest() -> int:
    from .selftest import main as selftest_main
    return selftest_main()


def main(argv: list[str]) -> int:
    _setup_logging()

    if "--selftest" in argv:
        return cmd_selftest()
    if "--status" in argv:
        return cmd_status()

    from . import config
    from .journal import Journal
    from .lease import Lease
    from .runner import Runner

    if config.UNKNOWN_LANES:
        print(f"unknown lane(s) in CELLAR_LANES: {', '.join(config.UNKNOWN_LANES)}",
              file=sys.stderr)
        return 2

    # TZ before anything reads a clock. Every "today" here is an AZ day.
    os.environ.setdefault("TZ", config.TZ)
    try:
        time.tzset()
    except AttributeError:
        pass                      # not POSIX; macOS and Linux both have it

    sb = _supabase()
    journal = Journal(config.JOURNAL_PATH)
    lease = Lease(sb, config.OWNER)
    return Runner(sb, lease, journal).serve(config.LANES_ENABLED)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
