"""Weekly auto-tuner for the Pick Bot's sizing AGGRESSION (Kelly fraction).

Sizing was hand-set to quarter-Kelly (KELLY_FRACTION=0.25), which made ~99%
of picks 1u — too timid if the edge estimate is real. But cranking it blind
is how you overbet a hot edge. So the aggression is now EARNED from data:
this measures whether the bot's edge estimate actually predicts results, and
nudges the Kelly fraction up (more 3u/5u) only when it does — or down when
it doesn't. Same agentic pattern as the prime-window tuner.

The test: split settled, gate-cleared ML/SPR/TOT picks by their edge_pp at
pick time into low/high halves and compare realized per-pick unit-ROI.
  • High-edge half earns MORE than low-edge AND beats the close (CLV>0)
    → the edge estimate is real → raise the Kelly fraction a step.
  • High-edge earns LESS → the estimate isn't discriminating → lower a step.
  • Otherwise hold.
Bounded ±step/run, floored/capped, so it drifts rather than whipsaws.

DORMANT until there's enough NEW-SCALE data. Everything before the June
2026 exchange cutover was scored on the old PIN scale, so we only count
picks whose game started on/after NEW_SCALE_SINCE — which means this does
nothing for ~2 weeks after the cutover, then self-activates. Writes to
pickbot_tuning.kelly_fraction; NULL there = handicapper_web uses the
KELLY_FRACTION constant (i.e. dormant).

Usage:
  python -m scripts.tune_sizing            # tune + write
  python -m scripts.tune_sizing --dry-run  # print, write nothing
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
from datetime import datetime, timezone

from storage import supabase_client as db

log = logging.getLogger("tune_sizing")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# The exchange cutover — only picks scored on the new scale count. Picks
# from before this were sized off the retired PIN score.
NEW_SCALE_SINCE = "2026-06-19"
TIMED_MARKETS   = ("moneyline", "spread", "total")

MIN_SAMPLE   = 60       # new-scale settled picks before we tune at all (the dormancy gate)
STEP         = 0.03     # max Kelly-fraction move per run
FRAC_MIN     = 0.15     # never below ~one-sixth Kelly
FRAC_MAX     = 0.40     # never above ~third-Kelly+ (overbet protection)
DEFAULT_FRAC = 0.25     # KELLY_FRACTION constant
ROI_MARGIN   = 0.05     # high-edge half must beat low-edge by this to raise


def _fetch(sb) -> list[dict]:
    try:
        return (sb.table("pickbot_paperlog")
                .select("edge_pp,units,pnl_units,clv_pp,status,market_type,"
                        "gates_cleared,event_start")
                .gte("event_start", NEW_SCALE_SINCE)
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
        e, u, pnl = r.get("edge_pp"), r.get("units"), r.get("pnl_units")
        if e is None or u in (None, 0) or pnl is None:
            continue
        try:
            out.append({"edge": float(e), "roi": float(pnl) / float(u),
                        "clv": (float(r["clv_pp"]) if r.get("clv_pp") is not None else None)})
        except (TypeError, ValueError):
            continue
    return out


def _current_frac(sb) -> float:
    try:
        rows = (sb.table("pickbot_tuning").select("kelly_fraction")
                .eq("id", 1).limit(1).execute().data) or []
        if rows and rows[0].get("kelly_fraction") is not None:
            return float(rows[0]["kelly_fraction"])
    except Exception:
        pass
    return DEFAULT_FRAC


def _bucket_mean(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return (sum(vals) / len(vals)) if vals else None


def tune(sb, dry_run: bool) -> dict:
    cur = _current_frac(sb)
    rows = _clean(_fetch(sb))
    log.info("new-scale settled rows: %d (since %s)", len(rows), NEW_SCALE_SINCE)
    if len(rows) < MIN_SAMPLE:
        return {"changed": False, "reason": "dormant_insufficient_new_scale_data",
                "rows": len(rows), "need": MIN_SAMPLE, "kelly_fraction": cur}

    med = statistics.median(r["edge"] for r in rows)
    low  = [r for r in rows if r["edge"] <= med]
    high = [r for r in rows if r["edge"] > med]
    if len(low) < 15 or len(high) < 15:
        return {"changed": False, "reason": "edge_split_too_thin",
                "kelly_fraction": cur}

    lo_roi, hi_roi = _bucket_mean(low, "roi"), _bucket_mean(high, "roi")
    hi_clv = _bucket_mean(high, "clv")
    discriminating = (hi_roi - lo_roi) >= ROI_MARGIN and (hi_clv or 0) > 0

    if discriminating:
        new = min(FRAC_MAX, round(cur + STEP, 3))
        verdict = "raise"
    elif hi_roi < lo_roi:
        new = max(FRAC_MIN, round(cur - STEP, 3))
        verdict = "lower"
    else:
        new, verdict = cur, "hold"

    basis = {
        "metric": "edge_predicts_roi_split", "verdict": verdict,
        "median_edge_pp": round(med, 3),
        "low_half":  {"n": len(low),  "mean_roi": round(lo_roi, 4)},
        "high_half": {"n": len(high), "mean_roi": round(hi_roi, 4),
                      "mean_clv": (round(hi_clv, 3) if hi_clv is not None else None)},
        "from": cur, "to": new, "tuned_at": datetime.now(timezone.utc).isoformat(),
    }
    changed = new != cur
    log.info("%s kelly_fraction %.3f -> %.3f (hi_roi=%.3f lo_roi=%.3f hi_clv=%s)",
             verdict, cur, new, hi_roi, lo_roi, hi_clv)
    if not dry_run and changed:
        try:
            sb.table("pickbot_tuning").upsert({
                "id": 1, "kelly_fraction": new,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "basis": {**basis, "tuner": "sizing"},
            }).execute()
        except Exception as e:
            log.error("write failed: %s", e)
    return {"changed": changed, "kelly_fraction": new, "basis": basis}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print(json.dumps(tune(db.client(), args.dry_run), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
