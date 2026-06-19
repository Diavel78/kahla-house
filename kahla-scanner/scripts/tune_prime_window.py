"""Weekly auto-tuner for the Pick Bot's prime betting ZONES.

The "prime window" (minutes-before-first-pitch where a pick is sized up and
glows green) was hand-set to a single (60, 180) span. Live data killed that:
the edge is BIMODAL — picks logged 30-90 min and 150-210 min out win, while
90-120 is a losing HOLE between them and the tails are cold. A single
contiguous span can't express that, so the prime window is now a LIST OF
ZONES, detected from the data each week and written to
`pickbot_tuning.zones` (jsonb [[lo,hi],…]) which handicapper_web reads at
runtime (_load_prime_zones).

Method (per the user — units/ROI with a CLV guardrail):
  1. Bin settled, gate-cleared ML/SPR/TOT paperlog rows into 30-min bands.
  2. A band is GOOD when it has enough sample AND positive mean per-pick
     unit-ROI (pnl_units / units — sizing-neutral, isolates the TIMING
     edge, not the old unit policy).
  3. Merge consecutive good bands into zones; drop zones that are too thin
     or whose mean CLV is materially worse than the slate's (the guardrail
     — a winning band that doesn't also beat the close is likely variance).
  4. Cap the zone count; keep the highest-ROI zones.

Safe by construction: thin data, or no qualifying zone, leaves the current
zones untouched.

Usage:
  python -m scripts.tune_prime_window            # tune + write
  python -m scripts.tune_prime_window --dry-run  # print, write nothing
  python -m scripts.tune_prime_window --lookback-days 45
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone

from storage import supabase_client as db

log = logging.getLogger("tune_prime_window")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ── bands + rails ────────────────────────────────────────────────────
BIN_WIDTH       = 30      # minute band width
MAX_MIN         = 360     # bands span 0..360; sim beyond is "far" (never prime)
MIN_BIN_SAMPLE  = 12      # picks in a band before it can be "good"
ROI_FLOOR       = 0.0     # a band must beat this mean unit-ROI to be "good"
MIN_ZONE_SAMPLE = 20      # total picks in a merged zone to keep it
MAX_ZONES       = 3       # keep at most this many zones (highest sum-ROI)
CLV_TOL         = 0.50    # zone mean CLV may dip this far below the slate mean
MIN_SAMPLE      = 25      # total usable rows before we tune at all
LOOKBACK_DAYS_DEFAULT = 30
DEFAULT_ZONES = [[60, 180]]
TIMED_MARKETS = ("moneyline", "spread", "total")


def _fetch_rows(sb, lookback_days: int) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    try:
        return (sb.table("pickbot_paperlog")
                .select("starts_in_min,units,pnl_units,clv_pp,status,market_type,gates_cleared")
                .gte("logged_at", cutoff)
                .in_("status", ["won", "lost", "push"])
                .eq("gates_cleared", True)
                .limit(20000)
                .execute().data) or []
    except Exception as e:
        log.error("paperlog fetch failed: %s", e)
        return []


def _clean(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if r.get("market_type") not in TIMED_MARKETS:
            continue
        sim, u, pnl = r.get("starts_in_min"), r.get("units"), r.get("pnl_units")
        if sim is None or u in (None, 0) or pnl is None:
            continue
        try:
            out.append({"sim": int(sim), "roi": float(pnl) / float(u),
                        "clv": (float(r["clv_pp"]) if r.get("clv_pp") is not None else None)})
        except (TypeError, ValueError):
            continue
    return out


def _bands(rows: list[dict]) -> list[dict]:
    """Per 30-min band stats, ordered by lo."""
    buckets: dict[int, list[dict]] = {}
    for r in rows:
        if r["sim"] >= MAX_MIN:
            continue
        idx = r["sim"] // BIN_WIDTH
        buckets.setdefault(idx, []).append(r)
    bands = []
    for idx in sorted(buckets):
        rs = buckets[idx]
        clvs = [r["clv"] for r in rs if r["clv"] is not None]
        bands.append({
            "lo": idx * BIN_WIDTH, "hi": (idx + 1) * BIN_WIDTH,
            "idx": idx, "n": len(rs),
            "roi": sum(r["roi"] for r in rs) / len(rs),
            "sum_roi": sum(r["roi"] for r in rs),
            "clv": (sum(clvs) / len(clvs)) if clvs else None,
        })
    return bands


def _detect_zones(bands: list[dict], slate_clv: float | None) -> list[dict]:
    """Merge consecutive GOOD bands into zones, apply zone-level rails."""
    good = [b for b in bands if b["n"] >= MIN_BIN_SAMPLE and b["roi"] > ROI_FLOOR]
    zones, run = [], []
    for b in good:
        if run and b["idx"] == run[-1]["idx"] + 1:
            run.append(b)
        else:
            if run:
                zones.append(run)
            run = [b]
    if run:
        zones.append(run)

    out = []
    for run in zones:
        n = sum(b["n"] for b in run)
        if n < MIN_ZONE_SAMPLE:
            continue
        clvs = [(b["clv"], b["n"]) for b in run if b["clv"] is not None]
        zclv = (sum(c * w for c, w in clvs) / sum(w for _, w in clvs)) if clvs else None
        if (slate_clv is not None and zclv is not None and zclv < slate_clv - CLV_TOL):
            continue            # guardrail: zone doesn't beat the close enough
        out.append({
            "lo": run[0]["lo"], "hi": run[-1]["hi"], "n": n,
            "sum_roi": round(sum(b["sum_roi"] for b in run), 2),
            "mean_roi": round(sum(b["sum_roi"] for b in run) / n, 4),
            "mean_clv": (round(zclv, 2) if zclv is not None else None),
        })
    out.sort(key=lambda z: z["sum_roi"], reverse=True)
    return out[:MAX_ZONES]


def _current_zones(sb) -> list[list[int]]:
    try:
        rows = (sb.table("pickbot_tuning")
                .select("prime_lo,prime_hi,zones").eq("id", 1).limit(1)
                .execute().data) or []
        if rows:
            z = rows[0].get("zones")
            if isinstance(z, str):
                z = json.loads(z)
            if z:
                return [[int(a), int(b)] for a, b in z]
            lo, hi = rows[0].get("prime_lo"), rows[0].get("prime_hi")
            if lo is not None and hi is not None:
                return [[int(lo), int(hi)]]
    except Exception:
        pass
    return DEFAULT_ZONES


def tune(sb, lookback_days: int, dry_run: bool) -> dict:
    cur = _current_zones(sb)
    rows = _clean(_fetch_rows(sb, lookback_days))
    log.info("usable paperlog rows: %d (lookback %dd)", len(rows), lookback_days)
    if len(rows) < MIN_SAMPLE:
        return {"changed": False, "reason": "insufficient_data",
                "zones": cur, "rows": len(rows)}

    clvs = [r["clv"] for r in rows if r["clv"] is not None]
    slate_clv = (sum(clvs) / len(clvs)) if clvs else None
    zones = _detect_zones(_bands(rows), slate_clv)
    if not zones:
        return {"changed": False, "reason": "no_qualifying_zone", "zones": cur}

    new = sorted([[z["lo"], z["hi"]] for z in zones])
    basis = {
        "metric": "two_zone_unit_roi_with_clv_guardrail",
        "lookback_days": lookback_days,
        "slate_clv": (round(slate_clv, 2) if slate_clv is not None else None),
        "zones_detail": zones,
        "tuned_at": datetime.now(timezone.utc).isoformat(),
    }
    changed = sorted(cur) != new
    log.info("%s zones %s → %s", "CHANGE" if changed else "keep", cur, new)
    if not dry_run:
        _write(sb, new, basis, changed)
    return {"changed": changed, "zones": new, "basis": basis}


def _write(sb, zones: list[list[int]], basis: dict, changed: bool) -> None:
    lo = min(z[0] for z in zones)
    hi = max(z[1] for z in zones)
    try:
        sb.table("pickbot_tuning").upsert({
            "id": 1, "zones": zones,
            "prime_lo": lo, "prime_hi": hi,      # envelope for back-compat readers
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "basis": {**basis, "action": "changed" if changed else "kept"},
        }).execute()
    except Exception as e:
        log.error("write pickbot_tuning failed: %s", e)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS_DEFAULT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sb = db.client()
    result = tune(sb, args.lookback_days, args.dry_run)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
