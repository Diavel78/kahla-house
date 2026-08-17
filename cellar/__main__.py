"""THE CELLAR — entrypoint.

    python -m cellar              # run the daemon
    python -m cellar --status     # who owns what right now (read-only)
    python -m cellar --batch-status   # scheduled jobs: last run + what's due
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


def _supabase(*, standalone: bool = False):
    """A Supabase client.

    `standalone=True` builds one straight from the environment instead of going
    through `app.get_supabase()`. That matters: importing `app` drags in the
    whole Flask tree (firebase_admin at line ~24), so without this the
    read-only `--status` / `--batch-status` commands would be unusable on a
    box that has the scanner deps but not the site's. Diagnostics should work
    on a half-built machine -- that is when you most need them.

    The daemon proper still goes through `app`, since its lanes import it anyway
    and credentials should resolve exactly one way for anything that acts.
    """
    if standalone:
        url = (os.environ.get("SUPABASE_URL") or "").strip()
        key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
        if url and key:
            try:
                from supabase import create_client
                return create_client(url, key)
            except Exception as e:
                print(f"direct supabase client failed ({e}); falling back to app", file=sys.stderr)

    import app as _app
    sb = _app.get_supabase()
    if sb is None:
        raise SystemExit("supabase unavailable — check SUPABASE_URL / SUPABASE_SERVICE_KEY")
    return sb


def cmd_status() -> int:
    from . import config
    from .lease import Lease
    lease = Lease(_supabase(standalone=True), config.OWNER)
    rows = lease.status()
    if not rows:
        print("no lease rows — apply kahla-scanner/supabase/cellar.sql")
        return 1
    print(f"{'LANE':<16} {'OWNER':<8} {'AGE':>8}  NOTE")
    for r in rows:
        print(f"{r['lane']:<16} {r['owner']:<8} {'':>8}  {r.get('note') or ''}")
    return 0


def cmd_batch_status() -> int:
    from .batch import status
    rows = status(_supabase(standalone=True))
    print(f"{'JOB':<20} {'SCHEDULE':<16} {'LAST OK':<14} DUE  NOTE")
    for r in rows:
        print(f"{r['job']:<20} {r['sched']:<16} {r['last_ok']:<14} "
              f"{'YES' if r['due'] else '  .'}  {r['note']}")
    return 0


def cmd_selftest() -> int:
    from .selftest import main as selftest_main
    return selftest_main()


def main(argv: list[str]) -> int:
    _setup_logging()

    # Selftest runs BEFORE .env is loaded, deliberately. Its whole purpose is
    # to prove the package works with no credentials and no configuration, so
    # letting a local .env inject CELLAR_LANES / CELLAR_DRY_RUN would make it
    # assert against the box's config instead of the defaults.
    if "--selftest" in argv:
        return cmd_selftest()

    # Everything else needs .env. This MUST happen before `from . import
    # config`, because config reads CELLAR_DRY_RUN / CELLAR_LANES at import
    # time -- load it later and those settings are silently ignored, which is
    # the worst kind of bug here: the daemon would look configured and run
    # nothing. (The daemon path used to get this by accident, via app.py's own
    # load_dotenv; the standalone --status/--batch-status path never did.)
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), ".env"))
    except Exception as e:
        print(f"warning: could not load .env ({e})", file=sys.stderr)

    if "--status" in argv:
        return cmd_status()
    if "--batch-status" in argv:
        return cmd_batch_status()

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
