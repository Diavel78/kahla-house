"""Weekly auto-tuner for the Pick Bot's prime betting window.

The "prime window" (minutes-before-first-pitch where a pick is sized up and
glows green) was hand-set to (60, 180). Live data killed that guess: across
350 logged picks the prime window was FLAT (-0.15u) while the far and late
windows both won (+4.5u / +4.1u). So the window itself is now data-driven —
this script grid-searches pickbot_paperlog each week and writes the best
(lo, hi) to the `pickbot_tuning` singleton row, which handicapper_web reads
at runtime (_load_prime_window).

Objective (per the user): maximize UNIT-ROI with a CLV guardrail.
  • Score a candidate window by mean per-pick ROI (pnl_units / units) over
    settled, gate-cleared ML/SPR/TOT paperlog rows whose logged
    starts_in_min falls inside [lo, hi]. Per-unit normalization removes the
    historical sizing bias (old prime picks were sized up, far capped at
    1u), so we measure the TIMING edge, not the old unit policy.
  • GUARDRAIL: a candidate may only WIN if its mean CLV is not materially
    worse than the current window's — a window that beats the close is real
    edge; a units-only win on a small sample is probably variance.
  • Stability rails: a minimum sample per window, a max move per run so the
    window can't whipsaw week to week, and a minimum ROI improvement before
    we bother changing anything.

Safe by construction: if there isn't enough data, or nothing clears the
guardrail + improvement threshold, the current window is left untouched.

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

# ── search space + rails ─────────────────────────────────────────────
LO_GRID   = list(range(30, 151, 15))     # 30,45,…,150  (start of prime)
HI_GRID   = list(range(90, 301, 15))     # 90,105,…,300 (end of prime)
MIN_WIDTH = 45                           # prime must span at least 45 min
MIN_SAMPLE = 25                          # picks inside a window to trust it
MAX_STEP   = 30                          # each bound moves ≤ 30 min per run
CLV_TOL    = 0.25                        # candidate CLV may dip this far below current
MIN_ROI_GAIN = 0.03                      # require ≥ +0.03 mean-ROI improvement to change
LOOKBACK_DAYS_DEFAULT = 30
DEFAULT_WINDOW = (60, 180)
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
        sim = r.get("starts_in_min")
        u = r.get("units")
        pnl = r.get("pnl_units")
        if sim is None or u in (None, 0) or pnl is None:
            continue
        try:
            out.append({
                "sim": int(sim),
                "roi": float(pnl) / float(u),
                "clv": (float(r["clv_pp"]) if r.get("clv_pp") is not None else None),
            })
        except (TypeError, ValueError):
            continue
    return out


def _score(rows: list[dict], lo: int, hi: int) -> dict | None:
    """Mean ROI + mean CLV + n for picks whose sim ∈ [lo, hi]."""
    inside = [r for r in rows if lo <= r["sim"] <= hi]
    if len(inside) < MIN_SAMPLE:
        return None
    roi = sum(r["roi"] for r in inside) / len(inside)
    clvs = [r["clv"] for r in inside if r["clv"] is not None]
    clv = (sum(clvs) / len(clvs)) if clvs else None
    return {"lo": lo, "hi": hi, "n": len(inside),
            "mean_roi": round(roi, 4),
            "mean_clv": (round(clv, 3) if clv is not None else None)}


def _current_window(sb) -> tuple[int, int]:
    try:
        rows = (sb.table("pickbot_tuning")
                .select("prime_lo,prime_hi").eq("id", 1).limit(1)
                .execute().data) or []
        if rows:
            lo, hi = rows[0].get("prime_lo"), rows[0].get("prime_hi")
            if lo is not None and hi is not None and 0 <= lo < hi:
                return int(lo), int(hi)
    except Exception:
        pass
    return DEFAULT_WINDOW


def tune(sb, lookback_days: int, dry_run: bool) -> dict:
    cur_lo, cur_hi = _current_window(sb)
    rows = _clean(_fetch_rows(sb, lookback_days))
    log.info("usable paperlog rows: %d (lookback %dd)", len(rows), lookback_days)
    if len(rows) < MIN_SAMPLE:
        log.info("not enough data (<%d) — leaving window (%d,%d) untouched",
                 MIN_SAMPLE, cur_lo, cur_hi)
        return {"changed": False, "reason": "insufficient_data",
                "window": [cur_lo, cur_hi], "rows": len(rows)}

    cur = _score(rows, cur_lo, cur_hi)
    cur_roi = cur["mean_roi"] if cur else -1e9
    cur_clv = (cur or {}).get("mean_clv")

    # Candidate windows: full grid, constrained to width + a max step from
    # the current window so it can drift toward the truth, not teleport.
    best = None
    for lo in LO_GRID:
        if abs(lo - cur_lo) > MAX_STEP:
            continue
        for hi in HI_GRID:
            if hi - lo < MIN_WIDTH or abs(hi - cur_hi) > MAX_STEP:
                continue
            s = _score(rows, lo, hi)
            if not s:
                continue
            # CLV guardrail: don't accept a window materially worse on CLV.
            if (cur_clv is not None and s["mean_clv"] is not None
                    and s["mean_clv"] < cur_clv - CLV_TOL):
                continue
            if best is None or s["mean_roi"] > best["mean_roi"]:
                best = s

    if not best:
        return {"changed": False, "reason": "no_candidate",
                "window": [cur_lo, cur_hi]}

    improvement = best["mean_roi"] - cur_roi
    basis = {
        "metric": "mean_roi_with_clv_guardrail",
        "lookback_days": lookback_days,
        "current": cur, "chosen": best,
        "improvement_roi": round(improvement, 4),
        "tuned_at": datetime.now(timezone.utc).isoformat(),
    }

    if (best["lo"], best["hi"]) == (cur_lo, cur_hi) or improvement < MIN_ROI_GAIN:
        log.info("keep (%d,%d) roi=%.3f — best (%d,%d) roi=%.3f gain=%.3f < %.3f",
                 cur_lo, cur_hi, cur_roi, best["lo"], best["hi"],
                 best["mean_roi"], improvement, MIN_ROI_GAIN)
        if not dry_run:                      # still refresh basis for visibility
            _write(sb, cur_lo, cur_hi, {**basis, "action": "kept"})
        return {"changed": False, "reason": "below_threshold",
                "window": [cur_lo, cur_hi], "basis": basis}

    log.info("CHANGE window (%d,%d)→(%d,%d) roi %.3f→%.3f (n=%d, clv=%s)",
             cur_lo, cur_hi, best["lo"], best["hi"], cur_roi,
             best["mean_roi"], best["n"], best["mean_clv"])
    if not dry_run:
        _write(sb, best["lo"], best["hi"], {**basis, "action": "changed"})
    return {"changed": True, "window": [best["lo"], best["hi"]], "basis": basis}


def _write(sb, lo: int, hi: int, basis: dict) -> None:
    try:
        sb.table("pickbot_tuning").upsert({
            "id": 1, "prime_lo": lo, "prime_hi": hi,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "basis": basis,
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
