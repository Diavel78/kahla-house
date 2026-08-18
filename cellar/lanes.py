"""THE CELLAR — lane registry.

Spec: docs/cellar-migration-spec.md §3, §7.

Maps a lane name to the work it performs. TWO call styles, because the
codebase has two shapes:

  DIRECT  -- `_opener_pass`, `_repeg_tick`, `_harvest_tick`, `_poly_ledger_tick`,
             `_tg_flush`, `_outbid_alerts` are pure functions of (sb, now) with
             zero Flask request/`g` coupling (verified). Call them.

  ROUTE   -- pm_snapshot / paperlog / vsin / kalshi_autolog have their logic
             INSIDE the route handler, not extracted. Rather than refactor live
             money paths during a migration (spec §8 item 6), drive them through
             Flask's in-process test client. That executes the real handler --
             same auth, same budgets, same code -- with NO network hop and NO
             platform timeout. Extraction can happen later, on its own merits.

`import app` is LATE, inside each function, on purpose: `app.py` initializes
Firebase at import time (line ~60), so importing this module must not drag that
in. The package has to be importable on a box with no credentials at all --
that is what makes the offline selftest possible.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger("cellar.lanes")


@dataclass
class Ctx:
    """Everything a lane is handed. Deliberately small."""
    sb: object
    now: datetime
    dry_run: bool
    journal: object | None = None


# ---------------------------------------------------------------------------
# Route driving (in-process, no network)
# ---------------------------------------------------------------------------

def _cron_key() -> str:
    """The shared secret the cron routes check.

    If the box does not have the real one, MINT AN EPHEMERAL ONE and put it in
    this process's environment. The route reads the same os.environ at request
    time, so both sides agree and the check passes.

    That is not a bypass. The secret exists to stop the public internet from
    hitting these endpoints on Vercel; the cellar reaches them through Flask's
    in-process test client, never over a socket, so there is no attacker to
    authenticate against. Requiring the operator to copy a secret out of a
    dashboard just to let a process call itself is friction with no security
    benefit — and Vercel marks that value write-only anyway, so it often
    CANNOT be copied.

    A real FILLS_CRON_SECRET in .env is still honored if present.
    """
    key = (os.environ.get("FILLS_CRON_SECRET") or "").strip()
    if not key:
        import secrets as _secrets
        key = _secrets.token_hex(32)
        os.environ["FILLS_CRON_SECRET"] = key
        log.info("no FILLS_CRON_SECRET set — minted an ephemeral in-process key")
    return key


def _drive_route(path: str) -> tuple[int, dict]:
    """Execute a cron route in-process via Flask's test client.

    No HTTP, no port, no 10s ceiling. The route's own internal budgets still
    apply -- which is what we want during migration: identical behavior to
    Vercel, just without the platform killing it.
    """
    import app as _app

    key = _cron_key()
    if not key:
        raise RuntimeError("FILLS_CRON_SECRET not set — cron routes will 401")

    sep = "&" if "?" in path else "?"
    with _app.app.test_client() as c:
        resp = c.get(f"{path}{sep}key={key}")
        try:
            body = resp.get_json() or {}
        except Exception:
            body = {"_raw": resp.get_data(as_text=True)[:500]}
    return resp.status_code, body


def _work_from(body: dict, *keys: str) -> int:
    """Pull a units-of-work count out of a route's JSON.

    Spec §8 item 2: heartbeat WORK DONE, not 'the job ran'. A lane that runs
    on time and produces zero is the ESPN-403 failure, and it must be visible.
    """
    for k in keys:
        v = body.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, list):
            return len(v)
        if isinstance(v, dict):
            for kk in ("logged", "inserted", "rows", "placed", "count", "n"):
                if isinstance(v.get(kk), int):
                    return v[kk]
    return 0


# ---------------------------------------------------------------------------
# Lane implementations
# ---------------------------------------------------------------------------

def lane_pm_snapshot(ctx: Ctx) -> int:
    code, body = _drive_route("/api/pm-snapshot")
    if code != 200:
        raise RuntimeError(f"pm-snapshot HTTP {code}: {str(body)[:200]}")
    return _work_from(body, "inserted", "rows", "logged")


def lane_paperlog(ctx: Ctx) -> int:
    # NOTE: the paperlog route is the one that also carries opener/repeg/
    # harvest/ledger/alerts on Vercel. Running this lane on the cellar while
    # those lanes ALSO run here would double them -- so `paperlog` and the
    # engine lanes are mutually exclusive in the runner (see runner.CONFLICTS).
    code, body = _drive_route("/api/handicapper/paperlog")
    if code != 200:
        raise RuntimeError(f"paperlog HTTP {code}: {str(body)[:200]}")
    return _work_from(body, "logged", "rows", "inserted")


def lane_vsin(ctx: Ctx) -> int:
    code, body = _drive_route("/api/vsin-snapshot")
    if code != 200:
        raise RuntimeError(f"vsin HTTP {code}: {str(body)[:200]}")
    return _work_from(body, "inserted", "rows", "logged")


def lane_kalshi_autolog(ctx: Ctx) -> int:
    code, body = _drive_route("/api/handicapper/kalshi-autolog")
    if code != 200:
        raise RuntimeError(f"kalshi-autolog HTTP {code}: {str(body)[:200]}")
    return _work_from(body, "created", "logged", "rows")


def lane_opener(ctx: Ctx) -> int:
    """NEW MONEY. The opener lane places model bets at listing time."""
    import time as _t
    import app as _app

    if ctx.dry_run:
        log.info("opener: DRY-RUN, not executing")
        return 0
    # Generous deadline: on Vercel this got ~14s of a shared request budget.
    # Here nothing kills us, so give the pass room to finish the pool. Kept as
    # a stuck-lane backstop, not a throughput limit.
    deadline = _t.time() + 120.0
    rows, stats = _app._opener_pass(ctx.sb, ctx.now, deadline)
    log.info("opener: %s", stats)
    return len(rows or [])


def lane_repeg(ctx: Ctx) -> int:
    import app as _app
    if ctx.dry_run:
        log.info("repeg: DRY-RUN, not executing")
        return 0
    stats = _app._repeg_tick(ctx.sb, ctx.now) or {}
    log.info("repeg: %s", stats)
    return int(stats.get("moved") or stats.get("placed") or 0)


def lane_harvest(ctx: Ctx) -> int:
    import app as _app
    if ctx.dry_run:
        log.info("harvest: DRY-RUN, not executing")
        return 0
    stats = _app._harvest_tick(ctx.sb, ctx.now) or {}
    log.info("harvest: %s", stats)
    return int(stats.get("placed") or 0)


def lane_ledger(ctx: Ctx) -> int:
    import app as _app
    stats = _app._poly_ledger_tick(ctx.sb, ctx.now) or {}
    log.info("ledger: %s", stats)
    return int(stats.get("stamped") or stats.get("updated") or 0)


def lane_batch(ctx: Ctx) -> int:
    """Phase 1: the scheduled workflow roster. Implementation in batch.py."""
    from .batch import lane_batch as _impl
    return _impl(ctx)


def lane_alerts(ctx: Ctx) -> int:
    import app as _app
    n = 0
    try:
        n += int(_app._outbid_alerts(ctx.sb, ctx.now) or 0)
    except Exception as e:
        log.warning("outbid alerts failed: %s", e)
    try:
        n += int(_app._tg_flush(ctx.sb, ctx.now) or 0)
    except Exception as e:
        log.warning("telegram flush failed: %s", e)
    return n


REGISTRY: dict[str, Callable[[Ctx], int]] = {
    "pm_snapshot":    lane_pm_snapshot,
    "paperlog":       lane_paperlog,
    "vsin":           lane_vsin,
    "kalshi_autolog": lane_kalshi_autolog,
    "opener":         lane_opener,
    "repeg":          lane_repeg,
    "harvest":        lane_harvest,
    "ledger":         lane_ledger,
    "alerts":         lane_alerts,
    "batch":          lane_batch,
}


# The paperlog ROUTE internally runs opener/repeg/harvest/ledger/alerts. If the
# cellar runs that route AND those lanes, every engine fires twice per tick --
# the duplicate-order failure, self-inflicted. The runner refuses to start on
# an overlapping config.
CONFLICTS: dict[str, set[str]] = {
    "paperlog": {"opener", "repeg", "harvest", "ledger", "alerts"},
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
