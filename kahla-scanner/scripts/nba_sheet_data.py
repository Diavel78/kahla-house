"""BASKETBALL PICK SHEET — assembly (Oct 2026, Hoops IQ).

One row per upcoming NBA game into `football_sheets` (sport='NBA' — the
generic pick-sheet store; the sport CHECK already allows NBA), priced by
Hoops IQ (_lib/nba_iq.py — pace × efficiency + availability):

    Line:  BOS −6.5 · ML −250 · O/U 224.5
    Model: BOS −5.1 · total 226.3 · BOS 68%
    Picks: BOS ML (lean) · DEN +6.5 (lean) · Over 224.5 (pass)

Pipeline (runs on the box — the batch lane — or anywhere ESPN answers):
  1. ESPN history → NBA_CACHE (scripts/nba_fetch, resumable; closed past
     seasons are never rescanned)
  2. walk Hoops IQ over every finished game (the exact backtest code path)
  3. ESPN slate (today + N days, AZ) with the book's lines
  4. ESPN injuries per game → who is OUT → availability features
  5. price + verdicts → upsert football_sheets / football_sheet_weeks

Verdict rules (model vs the market's NO-VIG price):
  ML      play ≥ 4.0pp · lean ≥ 2.0pp · pass; > 12pp = pass + flagged
  SPREAD  capped at LEAN (≥ 4pp) — walk-forward Brier vs the DK close is
  TOTAL   0.257 / 0.254 (no edge over the closing number). Shown as our
          number, never sold as an edge, until a backtest says otherwise.

  python -m scripts.nba_sheet_data [--days 1] [--commit]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_SCANNER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SCANNER)
from _lib import nba_iq as hq  # noqa: E402
from scripts import nba_fetch  # noqa: E402
from scripts.football_sheet_data import _espn_get, sb_upsert, week_key_default  # noqa: E402
from scripts.nhl_sheet_data import _american, _no_vig, _parse_odds  # noqa: E402

log = logging.getLogger("nba_sheet_data")
AZ = ZoneInfo("America/Phoenix")
CACHE = os.path.expanduser(os.environ.get("NBA_CACHE", "~/.kahla/nba_cache"))

# Selected on the 2023-24 season, scored on 2024-25 + 2025-26 (Oct 7 2026,
# scripts/backtest_nba_iq): ML Brier 0.2043 / 68.6% held-out (DK close
# 0.1965); spread 0.258 / total 0.259 vs the close — no edge, lean-capped.
# Rerun the sweep before changing these.
PARAMS = dict(hl_days=70.0, carry=0.7, ridge_eff=4.0, ridge_pace=4.0)
FEATS = "avail"

ML_PLAY_PP, ML_LEAN_PP, ML_IMPLAUSIBLE_PP = 4.0, 2.0, 12.0
LINE_LEAN_PP = 4.0
_OUT = ("out", "doubtful")
_SB = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
_SUM = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"


def current_season(now=None) -> int:
    now = now or datetime.now(AZ)
    return now.year + 1 if now.month >= 8 else now.year


def refresh_history(seasons):
    os.makedirs(CACHE, exist_ok=True)
    cur = max(seasons)
    for s in seasons:
        p = os.path.join(CACHE, f"nba_{s}.jsonl")
        if s < cur and os.path.exists(p) and sum(1 for _ in open(p)) >= 1150:
            continue                      # a closed season: nothing new to fetch
        nba_fetch.fetch_season(s, CACHE)
    return nba_fetch.load(CACHE, seasons)


def espn_slate(days: int, preseason: bool = False):
    today = datetime.now(AZ).date()
    events, ok = [], False
    for i in range(days + 1):
        d = _espn_get(_SB, {"dates": f"{today + timedelta(days=i):%Y%m%d}"})
        if d is not None:
            ok = True
            events.extend(d.get("events") or [])
    if not ok:
        return None
    out, seen = [], set()
    for ev in events:
        if ev.get("id") in seen:
            continue
        seen.add(ev.get("id"))
        if ((ev.get("season") or {}).get("type")) not in ((1, 2, 3) if preseason else (2, 3)):
            continue                      # preseason: no sheet
        comp = (ev.get("competitions") or [{}])[0]
        if ((ev.get("status") or {}).get("type") or {}).get("state") != "pre":
            continue
        sides = {}
        for c in comp.get("competitors") or []:
            t = c.get("team") or {}
            rec = next((r.get("summary") for r in (c.get("records") or [])
                        if r.get("type") == "total"), "")
            sides[c.get("homeAway")] = {"name": t.get("displayName") or "",
                                        "abbr": (t.get("abbreviation") or "").upper(),
                                        "id": str(t.get("id")), "record": rec}
        if "home" not in sides or "away" not in sides:
            continue
        try:
            start = datetime.fromisoformat((ev.get("date") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        odds = _parse_odds(comp)
        if "puck_home" in odds:           # the NHL parser's name for the spread
            odds["spread_home"] = odds.pop("puck_home")
            odds["spread_home_odds"] = odds.pop("puck_home_odds", None)
            odds["spread_away_odds"] = odds.pop("puck_away_odds", None)
        out.append({"espn_id": str(ev.get("id")), "start": start,
                    "home": sides["home"], "away": sides["away"], "odds": odds,
                    "broadcast": ", ".join(b for bl in (comp.get("broadcasts") or [])
                                           for b in (bl.get("names") or []))})
    return sorted(out, key=lambda g: g["start"])


def injuries(espn_id: str) -> dict:
    """{team_id: {athlete_id: (name, status)}} for players listed OUT/doubtful."""
    s = _espn_get(_SUM, {"event": espn_id}) or {}
    out: dict = {}
    for t in s.get("injuries") or []:
        tid = str((t.get("team") or {}).get("id"))
        for inj in t.get("injuries") or []:
            st = (inj.get("status") or (inj.get("type") or {}).get("description") or "").lower()
            if any(k in st for k in _OUT):
                a = inj.get("athlete") or {}
                out.setdefault(tid, {})[str(a.get("id"))] = (a.get("displayName"), st)
    return out


def _side_pick(p_a, price_a, price_b, label_a, label_b, extra_a, extra_b, lean_only):
    pa, pb = _no_vig(price_a or -110, price_b or -110)
    if pa is None:
        return None
    ea, eb = (p_a - pa) * 100, ((1 - p_a) - pb) * 100
    if ea >= eb:
        side, edge, p, mp, price, extra = label_a, ea, p_a, pa, price_a, extra_a
    else:
        side, edge, p, mp, price, extra = label_b, eb, 1 - p_a, pb, price_b, extra_b
    if lean_only:
        v = "lean" if edge >= LINE_LEAN_PP else "pass"
    else:
        v = "play" if edge >= ML_PLAY_PP else "lean" if edge >= ML_LEAN_PP else "pass"
    implaus = edge > ML_IMPLAUSIBLE_PP
    if implaus:
        v = "pass"
    out = {"side": side, "price": price, "fair": _american(p), "model_p": round(p, 4),
           "market_p": round(mp, 4), "edge_pp": round(edge, 1), "verdict": v,
           "model_gap_implausible": implaus}
    out.update(extra)
    if lean_only:
        out["capped"] = "no backtested edge vs the closing number — lean max"
    return out


def verdicts(g, pr):
    o, picks = g["odds"], {}
    if o.get("ml_home") is not None and o.get("ml_away") is not None:
        p = _side_pick(pr["win_home"], o["ml_home"], o["ml_away"], "home", "away",
                       {"team": g["home"]["name"], "abbr": g["home"]["abbr"]},
                       {"team": g["away"]["name"], "abbr": g["away"]["abbr"]}, False)
        if p:
            picks["ml"] = p
    L = o.get("spread_home")
    if L is not None:
        pc = hq.ncdf((pr["mu"] + L) / pr["sd"])
        p = _side_pick(pc, o.get("spread_home_odds"), o.get("spread_away_odds"),
                       "home", "away",
                       {"team": g["home"]["name"], "abbr": g["home"]["abbr"], "line": L},
                       {"team": g["away"]["name"], "abbr": g["away"]["abbr"], "line": -L},
                       True)
        if p:
            picks["spread"] = p
    T = o.get("total")
    if T is not None:
        po = hq.ncdf((pr["tu"] - T) / pr["tsd"])
        p = _side_pick(po, o.get("over_odds"), o.get("under_odds"), "over", "under",
                       {"line": T}, {"line": T}, True)
        if p:
            picks["total"] = p
    return picks


def run(days: int, commit: bool, preseason: bool = False) -> dict:
    cur = current_season()
    rows = refresh_history([cur - 3, cur - 2, cur - 1, cur])
    m, pairs, _preds, season_start = hq.walk(rows, PARAMS, FEATS)
    log.info("Hoops IQ: %d finished games walked, %d calibration pairs", len(rows), len(pairs))
    slate = espn_slate(days, preseason)
    if slate is None:
        return {"games": 0, "error": "espn_dark"}
    wk = week_key_default()
    now_iso = datetime.now(timezone.utc).isoformat()
    out_rows, summary = [], {"games": 0, "priced": 0, "plays": 0}
    for g in slate:
        d = hq.game_day(g["start"].isoformat())
        st = hq.HoopsIQ.season_time(cur, d, season_start)
        f = m.fit(st, cur)
        cal = hq.calibrate(pairs, st + 1e-6, FEATS) if f is not None else None
        h, a = g["home"]["abbr"], g["away"]["abbr"]
        inj = injuries(g["espn_id"])
        outs = {s: set((inj.get(g[s]["id"]) or {}).keys()) for s in ("home", "away")}
        played = {s: set(m.regulars(g[s]["abbr"], d)) - outs[s] for s in ("home", "away")}
        blob = {"game": {"sport": "NBA", "away": g["away"]["name"], "home": g["home"]["name"],
                         "away_abbr": a, "home_abbr": h,
                         "event_start": g["start"].isoformat(),
                         "records": {"away": g["away"]["record"], "home": g["home"]["record"]},
                         "broadcast": g.get("broadcast")},
                "lines": g["odds"],
                "injuries": {s: [v[0] for v in (inj.get(g[s]["id"]) or {}).values()]
                             for s in ("home", "away")},
                "model_meta": {"engine": "hoops_iq", "params": PARAMS,
                               "n_cal": (cal or {}).get("n"), "computed_at": now_iso}}
        if f is not None and cal is not None and h in m.teams and a in m.teams:
            hb, ab = hq.b2b_flags(m, h, a, d)
            rec = hq.features(m, f, h, a, d, played["home"], played["away"], hb, ab)
            mu, sd, tu, tsd = hq.priced(cal, rec)
            pr = {"mu": mu, "sd": sd, "tu": tu, "tsd": tsd,
                  "exp_margin": round(mu, 2), "exp_total": round(tu, 1),
                  "win_home": round(hq.ncdf(mu / sd), 4), "poss": round(rec["poss"], 1),
                  "b2b": {"home": bool(hb), "away": bool(ab)},
                  "missing": {"home": rec["miss_home"], "away": rec["miss_away"]}}
            blob["model"] = pr
            blob["picks"] = verdicts(g, pr)
            summary["priced"] += 1
            summary["plays"] += sum(1 for v in blob["picks"].values() if v.get("verdict") == "play")
        else:
            blob["unavailable"] = ["model_unrated_team"]
        out_rows.append({"week_key": wk, "sport": "NBA",
                         "event_name": f"{g['away']['name']} @ {g['home']['name']}",
                         "event_start": g["start"].isoformat(), "espn_id": g["espn_id"],
                         "tier": "data", "data_blob": blob, "data_built_at": now_iso})
        log.info("%s @ %s  %s", a, h, json.dumps({
            "line": g["odds"], "outs": blob["injuries"],
            "model": {k: (blob.get("model") or {}).get(k) for k in ("win_home", "exp_margin", "exp_total")},
            "picks": blob.get("picks")}, default=str))
    summary["games"] = len(out_rows)
    if commit and out_rows:
        sb_upsert("football_sheets", out_rows, "week_key,sport,event_name")
        sb_upsert("football_sheet_weeks", [{"week_key": wk, "sport": "NBA",
                                            "games": len(out_rows), "deep_games": 0}],
                  "week_key,sport")
    summary["week_key"] = wk
    return summary


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1, help="today + N days (AZ)")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--preseason", action="store_true",
                    help="include preseason games (dry runs only — never with --commit)")
    a = ap.parse_args()
    if a.preseason and a.commit:
        ap.error("--preseason is for dry runs")
    s = run(a.days, a.commit, a.preseason)
    print(json.dumps(s, indent=2, default=str))
    return 0 if s.get("error") is None else 1


if __name__ == "__main__":
    sys.exit(main())
