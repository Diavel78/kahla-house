"""Daily football QB adjustment → `football_qb_adj`.

Per team: the quarterback who throws the NEXT game (ESPN depth chart for
the NFL; the spine's most recent passer for college, where ESPN serves no
depth chart) vs the passer(s) who threw the games the rating was built on,
in ANY/A, times the fitted points-per-ANY/A. Spec:
docs/football-qb-adjust-spec.md. Math: _lib/football_qb.py. Fit:
scripts/backtest_football_qb.py (NFL k=4.84, t=4.5, Sep 13 2026).

Runs on the batch lane after power_ratings (same 365-day window, same
40-day half-life, so the baseline weights the same games the rating did).

  python -m scripts.compute_football_qb --sport NFL            # print only
  python -m scripts.compute_football_qb --commit               # both sports
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone

import httpx

from _lib import football_qb as fq
from _lib import power_ratings as pr
from scripts.backtest_football_qb import load
from storage import supabase_client as db

log = logging.getLogger(__name__)

_LEAGUE = {"NFL": ("football", "nfl"), "NCAAF": ("football", "college-football")}
_RATING_WINDOW_DAYS = {"NFL": 365, "NCAAF": 365}   # compute_power_ratings._WINDOW_DAYS
_BAD_STATUS = {"out", "injured reserve", "doubtful", "suspended",
               "physically unable to perform", "non-football injury",
               "reserve/covid-19", "inactive"}
_SLEEP = 0.2
_ATH_RE = re.compile(r"/athletes/(\d+)")


def _get(client, url, params=None):
    for attempt in range(3):
        try:
            r = client.get(url, params=params, timeout=25)
            if r.status_code == 200:
                time.sleep(_SLEEP)
                return r.json()
            if r.status_code in (400, 404):
                return None
        except Exception as e:
            log.warning("GET %s failed (%d): %s", url, attempt + 1, e)
        time.sleep(1.0 * (attempt + 1))
    return None


def espn_team_ids(client, sport) -> dict[str, str]:
    grp, lg = _LEAGUE[sport]
    params = {"limit": 400}
    if sport == "NCAAF":
        params["groups"] = 80
    d = _get(client, f"https://site.api.espn.com/apis/site/v2/sports/{grp}/{lg}/teams",
             params)
    out = {}
    try:
        for t in d["sports"][0]["leagues"][0]["teams"]:
            tm = t["team"]
            out[tm["displayName"]] = str(tm["id"])
    except (TypeError, KeyError, IndexError):
        pass
    return out


def roster_status(client, sport, team_id) -> dict[str, dict]:
    """athlete id → {name, active, flags}."""
    grp, lg = _LEAGUE[sport]
    # limit=200: the college page defaults to 100 athletes and a 120-man
    # roster loses its back half alphabetically (Stockton, Sep 13 2026).
    d = _get(client, f"https://site.api.espn.com/apis/site/v2/sports/{grp}/{lg}/teams/{team_id}/roster",
             {"limit": 200})
    out = {}
    for grpd in ((d or {}).get("athletes") or []):
        for a in (grpd.get("items") or []):
            st = a.get("status")
            st_name = (st.get("name") if isinstance(st, dict) else st) or ""
            flags = [str((i or {}).get("status") or "") for i in (a.get("injuries") or [])]
            bad = (st_name.lower() not in ("active", "")
                   or any(f.lower() in _BAD_STATUS for f in flags))
            out[str(a.get("id"))] = {"name": a.get("displayName"),
                                     "active": not bad,
                                     "status": st_name, "flags": flags,
                                     "pos": ((a.get("position") or {}).get("abbreviation"))}
    return out


def nfl_depth_qbs(client, team_id, season) -> list[str]:
    d = _get(client, "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/"
             f"seasons/{season}/teams/{team_id}/depthcharts")
    best: list[tuple[int, str]] = []
    for it in ((d or {}).get("items") or []):
        qb = (it.get("positions") or {}).get("qb")
        if not qb:
            continue
        for a in (qb.get("athletes") or []):
            m = _ATH_RE.search(((a.get("athlete") or {}).get("$ref")) or "")
            if m:
                best.append((int(a.get("rank") or 99), m.group(1)))
        if best:
            break
    return [pid for _, pid in sorted(best)]


def _k_for(sb, sport) -> tuple[float, float]:
    k, cap = fq.K_DEFAULT.get(sport, 2.0), fq.ADJ_CAP_PTS
    try:
        rows = (sb.table("machine_flags").select("value")
                .eq("key", "football_qb").limit(1).execute().data) or []
        v = rows[0]["value"] if rows else None
        if isinstance(v, dict):
            k = float(v.get(f"k_{sport.lower()}") or k)
            cap = float(v.get("cap") or cap)
    except Exception:
        pass
    return k, cap


def compute(sb, client, sport, commit) -> int:
    now = datetime.now(timezone.utc)
    season = now.year if now.month >= 3 else now.year - 1
    games, per_side, qb_games, team_games, all_rows = load(sb, sport)
    if not games:
        log.warning("%s: no games", sport)
        return 0
    params = pr.SPORT_PARAMS[sport]
    hl = params.get("half_life_days")
    window = _RATING_WINDOW_DAYS[sport]
    lg = fq.league_anya(all_rows, now)
    if lg is None:
        log.warning("%s: no league ANY/A (spine empty?)", sport)
        return 0
    repl = lg - fq.REPLACEMENT_BELOW_LG
    k, cap = _k_for(sb, sport)
    ids = espn_team_ids(client, sport)
    qname = {pid: (rs[0].get("player_name") if rs else pid)
             for pid, rs in qb_games.items()}
    qcache: dict[str, float] = {}

    def q_of(pid):
        if pid not in qcache:
            qcache[pid] = fq.qb_quality(qb_games.get(pid, []), now, repl)[0]
        return qcache[pid]

    rows_out = []
    for team, tg in sorted(team_games.items()):
        base = fq.team_baseline(tg, q_of, now, hl, window)
        if not base:
            continue
        recent = sorted([g for g in tg if g["date"] < now], key=lambda g: g["date"])
        last = recent[-1] if recent else None
        tid = ids.get(team)
        roster = roster_status(client, sport, tid) if tid else {}
        starter_id, src = None, "unknown"
        # NFL: depth chart first
        if sport == "NFL" and tid:
            for pid in nfl_depth_qbs(client, tid, season):
                st = roster.get(pid)
                if st is None or st["active"]:
                    starter_id, src = pid, "depth_chart"
                    break
        # fallback: the spine's most recent passer(s) this season, active
        if not starter_id and recent:
            season_start = datetime(season, 8, 1, tzinfo=timezone.utc)
            this_season = [g for g in recent if g["date"] >= season_start] or recent[-1:]
            usage: dict[str, int] = {}
            for g in this_season:
                usage[g["passer_id"]] = usage.get(g["passer_id"], 0) + 1
            cands = [last["passer_id"]] + [p for p, _ in
                                           sorted(usage.items(), key=lambda kv: -kv[1])
                                           if p != last["passer_id"]]
            for pid in cands:
                # ABSENCE IS NOT EVIDENCE: a passer missing from ESPN's
                # roster page (page cap, id drift, fetch miss) still starts
                # until a POSITIVE Out/IR/Doubtful flag says otherwise.
                st = roster.get(pid)
                # The one exception: a COMPLETE roster page (NFL 53, college
                # 100-130 — not a truncated 100/200) that does not list him
                # means he left (portal, graduation, release) — the Week-0
                # case, when "last game" is last season's finale.
                if st is None and 40 <= len(roster) < 190:
                    continue
                if st is None or st["active"]:
                    starter_id, src = pid, ("last_game" if pid == last["passer_id"]
                                            else "next_used")
                    break
        sq = q_of(starter_id) if starter_id else repl
        adj = fq.adjust_pts(sq, base["q"], k, cap)
        sname = (roster.get(starter_id, {}).get("name") if starter_id else None) \
            or qname.get(starter_id) or starter_id
        rows_out.append({
            "sport": sport, "team": team,
            "starter_id": starter_id, "starter_name": sname, "starter_src": src,
            "starter_q": round(sq, 3),
            "baseline_qb": base["top_name"], "baseline_q": round(base["q"], 3),
            "adj_pts": round(adj, 2), "k": k, "replacement_q": round(repl, 3),
            "detail": {"baseline_top_share": round(base["top_share"], 2),
                       "baseline_n_qbs": base["n"],
                       "starter_dropbacks": round(fq.qb_quality(
                           qb_games.get(starter_id, []), now, repl)[1], 0)
                       if starter_id else 0,
                       "last_game": (last["date"].date().isoformat() if last else None),
                       "last_passer": (last.get("passer_name") if last else None),
                       "espn_team_id": tid, "league_anya": round(lg, 3)},
            "computed_at": now.isoformat(),
        })

    rows_out.sort(key=lambda r: r["adj_pts"])
    print(f"\n{sport}: {len(rows_out)} teams · league ANY/A {lg:.2f} · "
          f"replacement {repl:.2f} · k {k}")
    for r in rows_out[:8] + [None] + rows_out[-5:]:
        if r is None:
            print("   …")
            continue
        print(f"  {r['adj_pts']:+5.1f}  {r['team']:<28} now {r['starter_name']!s:<22}"
              f"({r['starter_src']}, {r['starter_q']:.2f})  rated on "
              f"{r['baseline_qb']!s:<20}({r['baseline_q']:.2f})")
    unk = [r["team"] for r in rows_out if r["starter_src"] == "unknown"]
    if unk:
        print(f"  unknown starter: {len(unk)} — {', '.join(unk[:8])}")
    if commit and rows_out:
        for i in range(0, len(rows_out), 100):
            sb.table("football_qb_adj").upsert(rows_out[i:i + 100],
                                               on_conflict="sport,team").execute()
        print(f"  wrote {len(rows_out)} rows")
    return len(rows_out)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="all", choices=("all", "NFL", "NCAAF"))
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args(argv)
    sb = db.client()
    client = httpx.Client(headers={"User-Agent": "kahla-house/1.0"})
    sports = ["NFL", "NCAAF"] if args.sport == "all" else [args.sport]
    total = 0
    for sp in sports:
        try:
            total += compute(sb, client, sp, args.commit)
        except Exception as e:
            log.error("%s failed: %s", sp, e, exc_info=True)
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
