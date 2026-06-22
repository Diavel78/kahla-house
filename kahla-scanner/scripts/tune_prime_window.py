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

PER-BET-TYPE (June 2026): the markets do NOT share a hot zone (live data
showed ML steams ~150-180m out, totals want ~30-60 + 180-210, NRFI lives
far out), so step 1-4 run BOTH pooled (all markets → the `zones` column)
AND per market_type (→ `zones_by_market`). A market only earns its own
zones once it clears MIN_MARKET_SAMPLE on its OWN rows; until then it's
omitted and inherits the pooled zones at runtime
(handicapper_web._load_prime_zones_by_market). The pooled `zones` column is
built from the SIDES only (ML/SPR/TOT); NRFI is tracked as its own market
in zones_by_market (TUNED_MARKETS) but never pooled — its timing edge is the
opposite shape. NRFI sizing is still flat 0.5u (not yet zone-gated); wiring
its zone to sizing is a follow-up.

Safe by construction: thin data, or no qualifying zone (pooled OR a given
market), leaves that scope untouched / falling back to pooled.

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
# Zones never form beyond this — it's the PRIME CEILING, not just a histogram
# edge. Capped at 210 (3.5h) to match (a) the strategy thesis ("sharp money
# moves early — proven ~3.5h out, never further") and (b) the frontend's
# chip/eval window (~210min). Beyond it there are no chips and the data is
# thin + mostly negative, so a lone barely-positive far band (e.g. the 270-300
# +0.08/u blip flanked by -0.20 and -0.29 neighbours) was getting crowned
# "prime" and lighting a green box on a 5-hours-out game that offered no
# action. If you ever raise this, the frontend EVAL window must move with it
# or the glow/chip mismatch returns.
MAX_MIN         = 210     # bands span 0..210; sim beyond is "far" (never prime)
MIN_BIN_SAMPLE  = 12      # picks in a band before it can be "good"
ROI_FLOOR       = 0.0     # a band must beat this mean unit-ROI to be "good"
MIN_ZONE_SAMPLE = 20      # total picks in a merged zone to keep it
MAX_ZONES       = 3       # keep at most this many zones (highest sum-ROI)
CLV_TOL         = 0.50    # zone mean CLV may dip this far below the slate mean
MIN_SAMPLE      = 25      # total usable rows before we tune the POOLED zones at all
# Per-bet-type floor (June 2026): a SINGLE market needs a much healthier
# sample than the pooled 25 before it earns its OWN zones — splitting by
# market divides the data, and acting on a thin per-market slice over-fits.
# Set above the high-volume markets' current ~15-day counts (~125-155) so
# nothing specializes until ~1 month of per-market sample has accrued; below
# this a market is omitted and inherits the pooled zones at runtime. Raise/
# lower this single knob to gate when per-market zones go live.
MIN_MARKET_SAMPLE = 200
LOOKBACK_DAYS_DEFAULT = 30
DEFAULT_ZONES = [[60, 180]]
# Pooled zones are built from the SIDES only — totals/ML/spread. NRFI's
# timing edge is the opposite shape (it wins far-out, dead in the sides'
# windows), so pooling it would pollute the shared fallback.
TIMED_MARKETS = ("moneyline", "spread", "total")
# Markets that earn their OWN per-bet-type zones. NRFI is included here
# (tracking only for now — its sizing is still flat 0.5u, not zone-gated;
# wiring NRFI's zone to sizing is a follow-up). NRFI has no CLV, so the CLV
# guardrail self-disables on it (slate_clv is None → guardrail skipped).
TUNED_MARKETS = ("moneyline", "spread", "total", "nrfi")


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
        mt = r.get("market_type")
        if mt not in TUNED_MARKETS:
            continue
        sim, u, pnl = r.get("starts_in_min"), r.get("units"), r.get("pnl_units")
        if sim is None or u in (None, 0) or pnl is None:
            continue
        try:
            out.append({"sim": int(sim), "roi": float(pnl) / float(u), "mt": mt,
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


def _zones_for(rows: list[dict]) -> tuple[list[list[int]], list[dict]]:
    """Detect zones for a row subset (pooled or one market). Returns
    (zones as sorted [[lo,hi],…], zones_detail). Empty when no band/zone
    qualifies — the caller treats that as 'keep current / fall back'."""
    clvs = [r["clv"] for r in rows if r["clv"] is not None]
    slate_clv = (sum(clvs) / len(clvs)) if clvs else None
    detail = _detect_zones(_bands(rows), slate_clv)
    return sorted([[z["lo"], z["hi"]] for z in detail]), detail


def tune(sb, lookback_days: int, dry_run: bool) -> dict:
    cur = _current_zones(sb)
    rows = _clean(_fetch_rows(sb, lookback_days))
    log.info("usable paperlog rows: %d (lookback %dd)", len(rows), lookback_days)
    if len(rows) < MIN_SAMPLE:
        return {"changed": False, "reason": "insufficient_data",
                "zones": cur, "rows": len(rows)}

    # Pooled zones — SIDES only (ML/SPR/TOT), excluding NRFI whose timing is
    # the opposite shape. Kept as the back-compat `zones` column AND the
    # per-market fallback for SIDE markets without their own sample.
    pooled_rows = [r for r in rows if r["mt"] in TIMED_MARKETS]
    new, pooled_detail = _zones_for(pooled_rows)
    if not new:
        return {"changed": False, "reason": "no_qualifying_zone", "zones": cur}

    # Per-bet-type zones — each market only earns its own once it clears
    # MIN_MARKET_SAMPLE on its OWN rows; otherwise it's omitted and inherits
    # the pooled zones at runtime. The markets don't share a hot zone (ML
    # steams ~150-180m out, totals ~30-60 + 180-210, NRFI far out), so pooling
    # masked real per-market structure.
    by_market: dict[str, list[list[int]]] = {}
    by_market_detail: dict[str, dict] = {}
    for mt in TUNED_MARKETS:
        mrows = [r for r in rows if r["mt"] == mt]
        if len(mrows) < MIN_MARKET_SAMPLE:
            by_market_detail[mt] = {"n": len(mrows), "zones": None,
                                    "reason": "below_market_floor"}
            continue
        mzones, mdetail = _zones_for(mrows)
        if not mzones:
            by_market_detail[mt] = {"n": len(mrows), "zones": None,
                                    "reason": "no_qualifying_zone"}
            continue
        by_market[mt] = mzones
        by_market_detail[mt] = {"n": len(mrows), "zones": mzones, "detail": mdetail}

    clvs = [r["clv"] for r in rows if r["clv"] is not None]
    slate_clv = (sum(clvs) / len(clvs)) if clvs else None
    basis = {
        "metric": "per_market_unit_roi_with_clv_guardrail",
        "lookback_days": lookback_days,
        "slate_clv": (round(slate_clv, 2) if slate_clv is not None else None),
        "zones_detail": pooled_detail,
        "by_market": by_market_detail,
        "tuned_at": datetime.now(timezone.utc).isoformat(),
    }
    prev_bm = _current_by_market(sb)
    changed = (sorted(cur) != new) or (prev_bm != by_market)
    log.info("%s pooled %s → %s | by_market %s",
             "CHANGE" if changed else "keep", cur, new, by_market)
    if not dry_run:
        _write(sb, new, by_market, basis, changed)
    return {"changed": changed, "zones": new, "by_market": by_market, "basis": basis}


def _current_by_market(sb) -> dict[str, list[list[int]]]:
    try:
        rows = (sb.table("pickbot_tuning")
                .select("zones_by_market").eq("id", 1).limit(1)
                .execute().data) or []
        if rows and rows[0].get("zones_by_market"):
            z = rows[0]["zones_by_market"]
            if isinstance(z, str):
                z = json.loads(z)
            return {k: [[int(a), int(b)] for a, b in v]
                    for k, v in (z or {}).items() if v}
    except Exception:
        pass
    return {}


def _write(sb, zones: list[list[int]], by_market: dict[str, list[list[int]]],
           basis: dict, changed: bool) -> None:
    lo = min(z[0] for z in zones)
    hi = max(z[1] for z in zones)
    try:
        sb.table("pickbot_tuning").upsert({
            "id": 1, "zones": zones,
            "zones_by_market": by_market,        # {} → every market falls back to pooled
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
