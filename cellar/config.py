"""THE CELLAR — configuration.

Spec: docs/cellar-migration-spec.md

Every knob the daemon has lives here. Two rules govern this file:

1. DRY_RUN DEFAULTS TO TRUE. The cellar does nothing that costs money until
   someone explicitly sets CELLAR_DRY_RUN=0. A fresh clone on a new box is
   inert by construction.

2. LANES ARE OPT-IN. LANES_ENABLED is empty by default. Cutover of a lane
   (spec §6 Phase 3) is exactly "add its name to CELLAR_LANES and restart" --
   and rollback is removing it. Nothing else moves.

Cadences deliberately MIRROR what Vercel does today (spec §8 item 6: don't
tune while you migrate). The tighter loops the house box makes possible --
15s re-peg, full-slate processing -- are Phase 5, after this is boring.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

# Must be 'cellar'. The lease's asymmetric preempt rule (cellar is PRIMARY,
# vercel is STANDBY) keys off this exact string -- see cellar.sql.
OWNER = "cellar"

# America/Phoenix. Every "today" in this codebase is an AZ calendar day, and
# the box's clock should agree with the domain (spec §7, and the CLOCK RULE
# in CLAUDE.md). Set at process start, not just on the OS, so a misconfigured
# machine can't silently shift a slate boundary.
TZ = "America/Phoenix"


# --------------------------------------------------------------------------
# Safety
# --------------------------------------------------------------------------

def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# THE MASTER SWITCH. True => every lane runs its read path and logs what it
# WOULD do, but no venue write and no DB mutation from a write engine.
DRY_RUN = _flag("CELLAR_DRY_RUN", True)

# WS WAKE FEED (docs/ws-feed-spec.md). Default ON, and safe to be: the feed
# is a pure HINT — it only pulls the repeg lane's next-due forward, and it
# disables itself loudly when websocket-client or the API creds are missing.
# It never carries state into an engine, so there is no risk knob here worth
# a second switch. Off only for debugging the socket itself.
WS_FEED = _flag("CELLAR_WS", True)

# The markets socket (v2 — the OUTBID hint): MARKET_DATA_LITE on every
# market we quote, waking repeg the second a watched book moves. Same
# hint-only doctrine; its own flag because it is a second connection
# with its own failure modes — a bad first night is one env var
# (CELLAR_WS_MKTS=0), never a revert. Requires WS_FEED (it rides the
# private feed's watch list).
WS_MKTS = _flag("CELLAR_WS_MKTS", True)

# NO LANE_TIMEOUT_S (removed Aug 20 2026). It existed here for weeks as a
# "wall-clock ceiling for a single lane invocation" that NOTHING READ -- the
# runner never wrapped a lane call in it, so the reassurance was fictional.
# A dead knob that names a guarantee is worse than no knob: it stops you
# looking for the real thing.
#
# There is also nothing to enforce it WITH: a Python thread cannot be killed,
# so a genuinely hung lane runs until the process dies no matter what number
# sits here. What the runner does instead is detect the overrun against the
# lane's stuck line (stuck_s, defaulting to its ttl_s) and make it LOUD
# (failed tick row + urgent ping) rather than silent. See
# Runner._overrun_check for why it does not release the lease.
#
# ttl_s and stuck_s are DIFFERENT CLOCKS since the renewer shipped: the
# renew loop heartbeats every held lane every 30s independently of lane
# execution, so a lane legitimately running past its ttl_s never loses its
# lease. ttl_s is "how fast the standby takes over if the DAEMON dies";
# stuck_s is "how long before a running lane is presumed hung". They were
# the same number until the opener's designed workload (120s MLB pass +
# in-flight-game overshoot + the football pass) grew past its 180s ttl and
# every healthy ~182s tick got branded STUCK — a failed tick row and a
# false 🚨 page, 4-5 an hour, on a lane doing exactly its job.


# --------------------------------------------------------------------------
# Lanes
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Lane:
    name: str
    every_s: int                 # cadence
    ttl_s: int                   # lease TTL; must exceed every_s with margin
    writes_money: bool = False   # gates on DRY_RUN
    # Engine needs the admin's uid to know WHOSE book it is acting on. It
    # resolves from KALSHI_OWNER_UID, else the sole Firestore admin. On a box
    # with neither, the engine returns at its second line with a zero — no
    # error, no exception, just nothing. That is what happened when `ledger`
    # moved to the cellar: 242 consecutive healthy ticks, work=0, and the
    # dashboard's day card silently read $0.00. Refuse to start instead.
    needs_owner: bool = False
    # Stuck line for _overrun_check, when the lane's DESIGNED workload runs
    # longer than its lease TTL (see the two-clocks note above). None = the
    # ttl_s, which is right for every lane whose ticks are short.
    stuck_s: int | None = None
    note: str = ""


# The full roster. Cadences match today's Vercel behavior:
#   pm_snapshot/paperlog/opener  -> every tick (60s)
#   repeg/harvest/ledger         -> minute-modulo on Vercel, real schedule here
#   vsin                         -> 15 min
#   kalshi_autolog               -> 2 min
ALL_LANES: dict[str, Lane] = {
    l.name: l for l in [
        Lane("pm_snapshot",     60,   180, note="exchange cent logger"),
        Lane("paperlog",        60,   180, note="suggestion + shadow logger"),
        # needs_owner: _autobet_execute resolves _kalshi_owner_uid() and
        # returns False on None — so with no owner the lane still persists
        # its opener shadow rows (work>0, looks ALIVE) and places ZERO bets.
        # Worse than the ledger's silent zero, because the tick count lies.
        # Enforcement means Vercel stands down, so that is a total betting
        # blackout that reads healthy on every dashboard. Refuse to start.
        # stuck_s=540 (Aug 31 2026, was 420): the designed workload grew —
        # ~120s MLB pass + 25s OMS slice + 45s gridiron tape + sweep —
        # healthy ticks now run ~210-260s. 540 ≈ 2x that: a real hang,
        # not a full slate. Lease ttl stays 180 (daemon-death failover
        # speed; the renewer covers live execution).
        Lane("opener",          60,   180, writes_money=True, needs_owner=True,
             stuck_s=540, note="opener lane + autobet (NEW MONEY)"),
        # stuck_s=600 (Aug 31 2026): the chase-night rework made a BUSY
        # full-sweep lap legitimately ~300-330s (10-min full sweep over a
        # 130-pick book + up to 6 real amends + scalp + reconcile), and
        # the default stuck line (=ttl 300) was stamping healthy laps as
        # failed — the permanent-red-on-a-healthy-machine disease, third
        # sighting. 600 ≈ 2× the designed workload (the opener's own
        # precedent); lease ttl stays 300 — the renewer covers execution,
        # so failover speed is unchanged.
        Lane("repeg",          120,   300, writes_money=True, needs_owner=True,
             stuck_s=600, note="maker order chase"),
        Lane("harvest",        120,   300, writes_money=True, needs_owner=True,
             note="take-profit sells"),
        Lane("ledger",         300,   900, needs_owner=True,
             note="poly money ledger"),
        Lane("vsin",           900,  1800, note="circa/dk splits logger"),
        Lane("kalshi_autolog", 120,   300, needs_owner=True,
             note="kalshi fill -> bot_picks"),
        # 300 to match the TTL the engine's own _cellar_owns call passes;
        # the two sides naming different numbers means whoever claims last
        # silently changes the failover deadline (selftest enforces this).
        Lane("alerts",          60,   300, needs_owner=True,
             note="telegram flush + pings"),
        # Phase 1: the ~20 scheduled workflows. Ticks every minute but only
        # ACTS when something is due (see cellar/batch.py). Long TTL because
        # a model compute can legitimately run for tens of minutes.
        Lane("batch",           60,  3600, note="daily ingests + model computes"),
        # Cutover night (Sep 3 2026): the per-minute Actions jobs that
        # never moved — resolver grading every tick, ESPN markets spine
        # every 5 min inside the lane. Subprocess pattern (batch.py's).
        Lane("grader",          60,   300, note="pick grading + espn spine"),
    ]
}

# Comma-separated. EMPTY BY DEFAULT -- a fresh install runs nothing.
#   CELLAR_LANES=pm_snapshot,paperlog
_raw = (os.environ.get("CELLAR_LANES") or "").strip()
LANES_ENABLED: list[str] = [x.strip() for x in _raw.split(",") if x.strip()]

# Reject typos loudly at startup rather than silently running fewer lanes than
# you think -- a silently-missing lane is the ESPN-403 failure mode (spec §8.2).
UNKNOWN_LANES = [n for n in LANES_ENABLED if n not in ALL_LANES]

# Heartbeat renewal margin: renew at 1/3 of TTL so two consecutive misses
# still don't drop the lease.
def renew_every(lane: Lane) -> float:
    return max(10.0, lane.ttl_s / 3.0)


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

STATE_DIR = os.path.expanduser(os.environ.get("CELLAR_STATE_DIR", "~/.cellar"))
JOURNAL_PATH = os.path.join(STATE_DIR, "intents.sqlite3")


def owner_lanes_enabled() -> list[str]:
    """Enabled lanes that cannot function without the admin's uid."""
    return [n for n in LANES_ENABLED
            if n in ALL_LANES and ALL_LANES[n].needs_owner]


def money_lanes_enabled() -> list[str]:
    """Enabled lanes that can actually move money."""
    return [n for n in LANES_ENABLED
            if n in ALL_LANES and ALL_LANES[n].writes_money]


def dry_run_blackout(enabled: list[str]) -> list[str]:
    """Money lanes that are enabled while DRY_RUN is on.

    Non-empty means the daemon must refuse to start: a money lane claims its
    lease before it checks dry_run, so it would hold the lease, stand the
    enforced Vercel side down, and place nothing — a betting blackout that
    reads healthy on every dashboard.
    """
    if not DRY_RUN:
        return []
    return sorted(n for n in enabled
                  if n in ALL_LANES and ALL_LANES[n].writes_money)


def summary() -> str:
    """One-line startup banner. Printed to the log so a tail tells you the
    posture immediately -- the thing you most want after an unattended boot.

    The REAL MONEY warning fires only when a money lane is BOTH enabled and
    un-dry-run. Shouting it whenever CELLAR_DRY_RUN=0 would be a false alarm
    for the common case (running only `batch`, which touches no money), and a
    warning that cries wolf is worse than no warning -- you stop reading it,
    and then it is not there on the night it matters.
    """
    money = money_lanes_enabled()
    if DRY_RUN:
        mode = "DRY-RUN (money lanes inert)"
    elif money:
        mode = f"*** LIVE — REAL MONEY via {','.join(money)} ***"
    else:
        mode = "armed (no money lanes enabled)"
    lanes = ",".join(LANES_ENABLED) or "(none)"
    return f"cellar owner={OWNER} tz={TZ} mode={mode} lanes={lanes}"
