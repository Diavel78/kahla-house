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

# Wall-clock ceiling for a single lane invocation. Not a Vercel-style budget
# (there is no platform killing us here) -- purely a stuck-lane backstop so
# one hung HTTP call can't wedge the scheduler forever.
LANE_TIMEOUT_S = float(os.environ.get("CELLAR_LANE_TIMEOUT_S", "180"))


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
        Lane("opener",          60,   180, writes_money=True,
             note="opener lane + autobet (NEW MONEY)"),
        Lane("repeg",          120,   300, writes_money=True, needs_owner=True,
             note="maker order chase"),
        Lane("harvest",        120,   300, writes_money=True, needs_owner=True,
             note="take-profit sells"),
        Lane("ledger",         300,   900, needs_owner=True,
             note="poly money ledger"),
        Lane("vsin",           900,  1800, note="circa/dk splits logger"),
        Lane("kalshi_autolog", 120,   300, needs_owner=True,
             note="kalshi fill -> bot_picks"),
        Lane("alerts",          60,   180, needs_owner=True,
             note="telegram flush + pings"),
        # Phase 1: the ~20 scheduled workflows. Ticks every minute but only
        # ACTS when something is due (see cellar/batch.py). Long TTL because
        # a model compute can legitimately run for tens of minutes.
        Lane("batch",           60,  3600, note="daily ingests + model computes"),
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
