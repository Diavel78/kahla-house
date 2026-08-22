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
    # A lane may drop its own stats here; the runner writes them to
    # cellar_ticks.detail. Engine stats used to exist ONLY in the daemon's
    # log file on the house box, which meant that after the cutover the
    # single most useful debugging signal was invisible to anyone not
    # sitting at that machine -- four rounds of guessing at the football
    # pass, when its stats dict said exactly what it was doing the whole
    # time. Bounded and best-effort: observability must never fail a lane.
    detail: dict | None = None


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
    """THE LOGGER ONLY. `engines=0` is load-bearing, not a tidiness flag.

    The paperlog route also carries opener / repeg / harvest / ledger /
    alerts. Each self-gates on its own cellar lease, which arbitrates
    correctly across TWO processes and fails inside ONE: this lane and the
    `opener` lane, in the same daemon, both ask "do we own opener?", both
    hear yes, and the engine runs twice -- a double-write on the money path.
    That is what runner.CONFLICTS used to forbid outright, at the cost of
    making paperlog un-movable and pinning Vercel Pro alive (spec 12b).

    So: pass engines=0 and own the engines through their own lanes. Vercel's
    cron ping omits the param and keeps running everything, lease-gated, as
    the standby. Selftest asserts this param is still here."""
    code, body = _drive_route("/api/handicapper/paperlog?engines=0")
    if code != 200:
        raise RuntimeError(f"paperlog HTTP {code}: {str(body)[:200]}")
    # The route reports its insert count as `new_rows` — the old key list
    # matched nothing, so this lane stamped work=0 forever while
    # pickbot_paperlog took 391 rows in 6 hours (the repeg counter bug's
    # second sighting; the third was the opener below, same night).
    return _work_from(body, "new_rows", "logged", "rows", "inserted")


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

    # THE FOOTBALL PASS RUNS HERE TOO, and forgetting it orphaned the lane.
    #
    # `_gridiron_opener_pass` had exactly ONE call site: inside the paperlog
    # ROUTE body. app.py's own lease-gate table says the `opener` lane means
    # "_opener_pass + _gridiron_opener_pass (one claim, both passes)" -- the
    # contract was written down and the implementation only honoured half of
    # it. That cost nothing while Vercel ran the route with engines on. The
    # moment the cellar took paperlog with engines=0, `_own_opener` went
    # False there and the football pass stopped executing ANYWHERE.
    #
    # Four consecutive fixes to that pass -- budget, backoff, rotation
    # stride, sport priority -- were all real defects and none of them could
    # possibly have produced a row, because the function was never called.
    # THE LESSON: before tuning a path, prove it EXECUTES.
    g_deadline = max(deadline, _t.time() + _app._GRIDIRON_BUDGET_S)
    g_rows, g_stats = _app._gridiron_opener_pass(ctx.sb, ctx.now, g_deadline)
    stats = {**(stats or {}), **(g_stats or {})}
    log.info("opener: %s", stats)
    if ctx.detail is not None and isinstance(stats, dict):
        ctx.detail.update(stats)
    # Both passes return VESTIGIAL empty row lists — persistence happens
    # inside them per game (_opener_persist), so len(rows) was zero on
    # every tick the lane has ever run and the card read `idle` on a lane
    # that placed real bets all day (the repeg counter bug's third
    # sighting in one night). Work = what the pass actually did: games
    # evaluated (each one is a dossier build + rent check + model price)
    # plus bets placed plus gridiron rows persisted. A zero now really
    # means "nothing listed to walk", which is the ESPN-dark-spine signal
    # the idle clock exists to catch.
    return (len(rows or []) + len(g_rows or [])
            + int(stats.get("opener_eval") or 0)
            + int(stats.get("opener_bets") or 0)
            + int(stats.get("g_eval") or 0)
            + int(stats.get("g_rows") or 0))


def lane_repeg(ctx: Ctx) -> int:
    """force=True on every engine below: the minute-modulo inside each tick
    is Vercel's 1-minute-tick throttle, not this lane's schedule. A 120s
    lane advances `minute` by exactly 2 per tick, so `% 2` never changes
    for the life of the process -- always true or always false, decided by
    the minute the daemon booted on. The ledger lane lost 22 hours to it.

    The LEASE is deliberately left in place: these move real money and must
    stay exactly-once, which is what a lease is actually for."""
    import app as _app
    if ctx.dry_run:
        log.info("repeg: DRY-RUN, not executing")
        return 0
    stats = _app._repeg_tick(ctx.sb, ctx.now, force=True) or {}
    log.info("repeg: %s", stats)
    # _repeg_tick reports {"acted", "shadow", "stopped"} — "moved"/"placed"
    # never existed on it, so this lane stamped work=0 forever while moving
    # real orders (14 in the 6h before this was caught; signal_blob.repeg is
    # the ground truth) and the dashboard showed a permanent `work never`.
    # A standing yellow on a healthy lane is the "off is not broken" failure:
    # it trains you to skip the panel.
    return int(stats.get("acted") or 0)


def lane_harvest(ctx: Ctx) -> int:
    import app as _app
    if ctx.dry_run:
        log.info("harvest: DRY-RUN, not executing")
        return 0
    stats = _app._harvest_tick(ctx.sb, ctx.now, force=True) or {}
    log.info("harvest: %s", stats)
    return int(stats.get("placed") or 0)


def lane_ledger(ctx: Ctx) -> int:
    import app as _app
    # force=True: the minute-modulo inside the tick is Vercel's
    # 1-minute-tick throttle. This lane HAS its own 300s schedule, and
    # obeying both means firing only when the two phases happen to
    # agree — which, for a daemon that booted at :03, is never.
    stats = _app._poly_ledger_tick(ctx.sb, ctx.now, force=True) or {}
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
        n += int(_app._outbid_alerts(ctx.sb, ctx.now, force=True) or 0)
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
# Was {"paperlog": {opener, repeg, harvest, ledger, alerts}} until Aug 20
# 2026. That entry existed because the paperlog ROUTE runs those engines
# inline; `lane_paperlog` now drives it with engines=0, so the combination is
# safe and paperlog can finally leave Vercel. Kept (empty) rather than deleted
# because the validator that reads it is the right place for the NEXT such
# coupling, and because an empty dict documents that the conflict was RESOLVED
# rather than forgotten.
CONFLICTS: dict[str, set[str]] = {}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
