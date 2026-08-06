"""Serialize the live Whiff IQ state → whiff_iq_snapshot (id=1).

Compact state keyed by PITCHER NAME (the PMM prop question's identity)
and TEAM TRICODE (the question's "in ATL vs NYM" tokens), so the Flask
pricer needs zero extra lookups:

  lg       — league K/BF, starter BF, and the backtest-fitted BF_SD
  pitchers — {name: {team, hist: [[date, bf, k], ...]}} last 40 starts
  teams    — {tricode: [[date, pa, k], ...]} last 60 days of batting

Duplicate pitcher names (rare): the one with the most recent start
wins; the loser is DROPPED (a wrong-pitcher price is worse than none).

  python -m scripts.compute_whiff_iq            # writes the snapshot
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date, timedelta

from storage import supabase_client as db

ENGINE = "whiff-v1 log5+leash bf_sd=4.20"
BF_SD = 4.20                    # walk-forward train residual fit (Aug 2026)
MAX_STARTS = 40
TEAM_DAYS = 60


def _paged(sb, table: str, cols: str, order: list[str], flt=None) -> list[dict]:
    out, page = [], 0
    while True:      # explicit paging — the 1,000-row lesson
        q = sb.table(table).select(cols)
        if flt:
            q = flt(q)
        for c in order:
            q = q.order(c)
        rows = (q.range(page * 1000, page * 1000 + 999)
                .execute().data) or []
        out.extend(rows)
        if len(rows) < 1000:
            break
        page += 1
    return out


def main() -> int:
    sb = db.client()
    prows = _paged(sb, "mlb_pitcher_games",
                   "pitcher_id,pitcher_name,game_date,team,started,"
                   "batters_faced,strikeouts,outs", ["game_date", "game_pk"])
    cutoff = (date.today() - timedelta(days=TEAM_DAYS)).isoformat()
    brows = _paged(sb, "mlb_batter_games",
                   "game_pk,team,game_date,ab,walks,hbp,sac_flies,strikeouts",
                   ["game_date", "game_pk"],
                   flt=lambda q: q.gte("game_date", cutoff))

    # league constants from ALL starts (stable denominator)
    lg_k = lg_bf = lg_starts = 0
    outs_vals: list[float] = []
    by_pid: dict[int, dict] = {}
    for r in prows:
        if not r.get("started") or (r.get("batters_faced") or 0) <= 0:
            continue
        lg_k += r.get("strikeouts") or 0
        lg_bf += r["batters_faced"]
        lg_starts += 1
        if r.get("outs") is not None:
            outs_vals.append(float(r["outs"]))
        p = by_pid.setdefault(r["pitcher_id"],
                              {"name": r["pitcher_name"], "team": r["team"],
                               "hist": []})
        p["team"] = r["team"]                       # latest team wins
        # hist row gains OUTS as element [3] (Outs IQ, Aug 5 2026) —
        # the app slices [h[0], h[1], h[2]] for the K path, so the
        # extra element is backward-compatible.
        p["hist"].append([r["game_date"], r["batters_faced"],
                          r.get("strikeouts") or 0, r.get("outs")])
    if lg_starts < 500:
        print(f"only {lg_starts} starts — refusing to write a thin snapshot")
        return 1

    # name-keyed, duplicate names dropped (most-recent-start keeps the name
    # only if the other never played recently — ambiguity is a skip)
    by_name: dict[str, list[dict]] = defaultdict(list)
    for p in by_pid.values():
        p["hist"] = p["hist"][-MAX_STARTS:]
        by_name[p["name"]].append(p)
    pitchers = {}
    dupes = 0
    for name, ps in by_name.items():
        if len(ps) > 1:
            ps.sort(key=lambda p: p["hist"][-1][0], reverse=True)
            # both active this season → ambiguous, drop the name entirely
            if ps[1]["hist"][-1][0] >= (date.today()
                                        - timedelta(days=90)).isoformat():
                dupes += 1
                continue
        pitchers[name] = {"team": ps[0]["team"], "hist": ps[0]["hist"]}

    # team batting aggregates per game-date
    tagg: dict[tuple, dict] = {}
    for r in brows:
        k = (r["team"], r["game_date"], r["game_pk"])
        a = tagg.setdefault(k, {"pa": 0, "k": 0})
        a["pa"] += ((r.get("ab") or 0) + (r.get("walks") or 0)
                    + (r.get("hbp") or 0) + (r.get("sac_flies") or 0))
        a["k"] += r.get("strikeouts") or 0
    teams: dict[str, list] = defaultdict(list)
    for (team, gdate, _pk), a in sorted(tagg.items()):
        teams[team].append([gdate, a["pa"], a["k"]])

    o_m = sum(outs_vals) / max(1, len(outs_vals))
    o_s = (sum((x - o_m) ** 2 for x in outs_vals)
           / max(1, len(outs_vals) - 1)) ** 0.5
    state = {"lg": {"k_bf": round(lg_k / lg_bf, 5),
                    "bf": round(lg_bf / lg_starts, 3), "bf_sd": BF_SD,
                    "outs_m": round(o_m, 3), "outs_s": round(o_s, 3)},
             "pitchers": pitchers, "teams": dict(teams)}
    from datetime import datetime, timezone
    sb.table("whiff_iq_snapshot").upsert(
        {"id": 1, "engine": ENGINE, "state": state,
         "built_at": datetime.now(timezone.utc).isoformat()}).execute()
    print(f"snapshot written: {len(pitchers)} pitchers "
          f"({dupes} ambiguous names dropped), {len(teams)} teams, "
          f"lg K/BF {lg_k / lg_bf:.4f}, lg BF {lg_bf / lg_starts:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
