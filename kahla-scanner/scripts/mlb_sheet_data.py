"""BASEBALL PICK SHEET — assembly (Oct 2026, Diamond IQ).

One row per upcoming MLB game into `football_sheets` (sport='MLB' — the
generic pick-sheet store; the sport CHECK already allows MLB), priced by
Diamond IQ (_lib/diamond_iq.py — the ITER3 champion: starter FIP + bullpen
+ lineup wOBA + park + bullpen fatigue, pythagenpat win prob):

    Line:  LAD −1.5 · ML −145 · O/U 8.5
    Model: LAD 58% · LAD 4.6 – MIL 3.9
    Picks: LAD ML (lean) · MIL +1.5 (pass)

Pipeline (runs on the box — the batch lane):
  1. mlb_pitcher_games + mlb_batter_games (the daily spines) → walk the
     champion config over every finished game, exactly as the backtest and
     the old compute_diamond_iq snapshot did
  2. ESPN slate (today + N days, AZ) with the book's lines + probable
     starters (matched to the spine by name — duplicates dropped)
  3. Pinnacle's own feed overlays the line (pin_overlay, sport="MLB")
  4. price + verdicts → upsert football_sheets / football_sheet_weeks

⚠ THE MODEL IS WEAKER THAN THE MARKET. Walk-forward on 2026 (n=1,667):
55.7% / Brier 0.2489 against a home-base 0.2498 — it barely separates
teams, and MLB closing moneylines run ~57-58%. Never measured against our
own closing prices. So EVERY verdict is LEAN at most (the NBA spread/total
rule): the sheet shows the model's side, never sells it as an edge.

  ML        lean ≥ 4pp vs the no-vig price, > 12pp = pass + flagged
  RUN LINE  P(cover) = P(win) × P(win by 2+ | win), the second term
            MEASURED from the spine for home and road winners (walk-off
            innings make home wins by one far likelier) — lean ≥ 4pp
  TOTAL     the model's number only, no pick: Diamond IQ's totals failed
            their backtest (over-projected 1.16 runs, 48.8% vs the line).

  python -m scripts.mlb_sheet_data [--days 1] [--commit] [--pin]
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
from _lib import diamond_iq as di  # noqa: E402
from scripts.backtest_diamond_iq import (load_rows, load_batter_rows,  # noqa: E402
                                         group_games, _team_update_rows)
from scripts.compute_diamond_iq import MASCOT_CODE  # noqa: E402
from scripts.football_sheet_data import _espn_get, sb_upsert, week_key_default  # noqa: E402
from scripts.nhl_sheet_data import _american, _no_vig, _parse_odds, pin_overlay  # noqa: E402

log = logging.getLogger("mlb_sheet_data")
AZ = ZoneInfo("America/Phoenix")

# The champion's home bump on the pythagenpat path (the backtest's
# train-fit, the value the live snapshot carried — compute_diamond_iq).
HFA_MULT = 0.08
_SB = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"


LEAN_PP, IMPLAUSIBLE_PP = 4.0, 12.0


def _spread_names(odds: dict) -> dict:
    """The NHL parser and the Pinnacle overlay call the spread `puck_home`;
    the sheet calls the run line `spread_home` (the NBA sheet's keys)."""
    if "puck_home" in odds:
        odds["spread_home"] = odds.pop("puck_home")
        odds["spread_home_odds"] = odds.pop("puck_home_odds", None)
        odds["spread_away_odds"] = odds.pop("puck_away_odds", None)
    return odds


def _side_pick(p_a, price_a, price_b, label_a, label_b, extra_a, extra_b):
    """The model's side vs the no-vig price — LEAN at most (see the
    module doc: Diamond IQ has no measured edge over the market)."""
    pa, pb = _no_vig(price_a or -110, price_b or -110)
    if pa is None:
        return None
    ea, eb = (p_a - pa) * 100, ((1 - p_a) - pb) * 100
    if ea >= eb:
        side, edge, p, mp, price, extra = label_a, ea, p_a, pa, price_a, extra_a
    else:
        side, edge, p, mp, price, extra = label_b, eb, 1 - p_a, pb, price_b, extra_b
    implaus = edge > IMPLAUSIBLE_PP
    v = "pass" if implaus else "lean" if edge >= LEAN_PP else "pass"
    out = {"side": side, "price": price, "fair": _american(p), "model_p": round(p, 4),
           "market_p": round(mp, 4), "edge_pp": round(edge, 1), "verdict": v,
           "model_gap_implausible": implaus,
           "capped": "Diamond IQ trails the market — lean max"}
    out.update(extra)
    return out


def team_code(display_name: str) -> str | None:
    n = (display_name or "").lower()
    for k in sorted(MASCOT_CODE, key=len, reverse=True):   # 'white sox' before 'sox'-ish
        if k in n:
            return MASCOT_CODE[k]
    return None


def build_state(sb):
    """Walk the champion config over the whole spine. Returns (state,
    last_lineup, name→pitcher_id, run-line shares)."""
    rows = load_rows(sb)
    b_rows = load_batter_rows(sb)
    games = group_games(rows, b_rows)
    state = di.DiamondState(opp_adj=True, park_adj=True, hr_shrink=2.5,
                            batter_blend=1.0, bp_fatigue=True)
    last_lineup: dict = {}
    for g in games:
        for t in (g["home"], g["away"]):
            lu = g["teams"][t].get("lineup")
            if lu:
                last_lineup[t] = lu
        state.update(g["date"], _team_update_rows(g))
    # pitcher names → ids; a name two pitchers share is dropped (a wrong
    # starter is worse than league average)
    seen: dict = {}
    for r in rows:
        pid, nm = r.get("pitcher_id"), (r.get("pitcher_name") or "").strip().lower()
        if pid and nm:
            seen.setdefault(nm, set()).add(pid)
    names = {nm: next(iter(ids)) for nm, ids in seen.items() if len(ids) == 1}
    # P(win by 2+ | win), home and road winners, measured
    hw = hw2 = aw = aw2 = 0
    for g in games:
        h_runs = g["teams"][g["away"]]["runs_allowed"]
        a_runs = g["teams"][g["home"]]["runs_allowed"]
        if h_runs > a_runs:
            hw += 1
            hw2 += (h_runs - a_runs) >= 2
        else:
            aw += 1
            aw2 += (a_runs - h_runs) >= 2
    shares = {"home": round(hw2 / hw, 4) if hw else 0.6,
              "away": round(aw2 / aw, 4) if aw else 0.7,
              "n_home_wins": hw, "n_road_wins": aw}
    log.info("Diamond IQ: %d games walked · %d pitcher names · run-line shares %s",
             len(games), len(names), shares)
    return state, last_lineup, names, shares


def espn_slate(days: int):
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
        if ((ev.get("season") or {}).get("type")) not in (2, 3):
            continue                      # spring training: no sheet
        comp = (ev.get("competitions") or [{}])[0]
        if ((ev.get("status") or {}).get("type") or {}).get("state") != "pre":
            continue
        sides = {}
        for c in comp.get("competitors") or []:
            t = c.get("team") or {}
            rec = next((r.get("summary") for r in (c.get("records") or [])
                        if r.get("type") == "total"), "")
            sp = None
            for p in c.get("probables") or []:
                a = p.get("athlete") or {}
                nm = a.get("fullName") or a.get("displayName")
                if nm:
                    sp = nm
                    break
            sides[c.get("homeAway")] = {"name": t.get("displayName") or "",
                                        "abbr": (t.get("abbreviation") or "").upper(),
                                        "id": str(t.get("id")), "record": rec,
                                        "starter": sp}
        if "home" not in sides or "away" not in sides:
            continue
        try:
            start = datetime.fromisoformat((ev.get("date") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        out.append({"espn_id": str(ev.get("id")), "start": start,
                    "home": sides["home"], "away": sides["away"],
                    "odds": _spread_names(_parse_odds(comp)),
                    "series": ((comp.get("series") or {}).get("summary")),
                    "broadcast": ", ".join(b for bl in (comp.get("broadcasts") or [])
                                           for b in (bl.get("names") or []))})
    return sorted(out, key=lambda g: g["start"])


def market_id(sb, name: str, start: datetime):
    """The `markets` row a bet on this game joins on — the SAME pick
    app._bet_sheet_market_id makes at placement (exact name in ±12h,
    active first, then nearest the start), so the Bet Sheets page can match
    a bet to its card by market id. None when the spine has no row yet."""
    try:
        rows = (sb.table("markets").select("id,event_name,event_start,status")
                .eq("sport", "MLB").eq("event_name", name)
                .gte("event_start", (start - timedelta(hours=12)).isoformat())
                .lte("event_start", (start + timedelta(hours=12)).isoformat())
                .limit(20).execute().data) or []
    except Exception:
        return None

    def _gap(r):
        try:
            t = datetime.fromisoformat(str(r.get("event_start")).replace("Z", "+00:00"))
            return abs((t - start).total_seconds())
        except (TypeError, ValueError):
            return float("inf")
    rows.sort(key=lambda r: (r.get("status") != "active", _gap(r)))
    return rows[0]["id"] if rows else None


def run_line_p(p_home: float, home_line: float, shares: dict) -> float | None:
    """P(home covers home_line) for the ±1.5 run line."""
    if home_line == -1.5:
        return p_home * shares["home"]
    if home_line == 1.5:
        return 1.0 - (1.0 - p_home) * shares["away"]
    return None


def verdicts(g, pr, shares):
    o, picks = g["odds"], {}
    if o.get("ml_home") is not None and o.get("ml_away") is not None:
        p = _side_pick(pr["win_home"], o["ml_home"], o["ml_away"], "home", "away",
                       {"team": g["home"]["name"], "abbr": g["home"]["abbr"]},
                       {"team": g["away"]["name"], "abbr": g["away"]["abbr"]})
        if p:
            picks["ml"] = p
    L = o.get("spread_home")
    if L is not None:
        pc = run_line_p(pr["win_home"], float(L), shares)
        if pc is not None:
            p = _side_pick(pc, o.get("spread_home_odds"), o.get("spread_away_odds"),
                           "home", "away",
                           {"team": g["home"]["name"], "abbr": g["home"]["abbr"], "line": L},
                           {"team": g["away"]["name"], "abbr": g["away"]["abbr"], "line": -L})
            if p:
                picks["spread"] = p
    return picks


def run(days: int, commit: bool, pin: bool | None = None) -> dict:
    from storage import supabase_client as db
    sb = db.client()
    state, last_lineup, names, shares = build_state(sb)
    slate = espn_slate(days)
    if slate is None:
        return {"games": 0, "error": "espn_dark"}
    pin_st = None
    if pin is not None:
        # Pinnacle's own feed every run (free); --pin may fall back to the
        # paid pull. Re-shape its spread keys like the NBA sheet does.
        pin_st = pin_overlay(slate, spend=bool(pin), sport="MLB")
        for g in slate:
            g["odds"] = _spread_names(g["odds"])
    wk = week_key_default()
    now_iso = datetime.now(timezone.utc).isoformat()
    out_rows = []
    summary = {"games": 0, "priced": 0, "leans": 0, "pinnacle": pin_st,
               "run_line_shares": shares, "starters_unknown": 0}
    for g in slate:
        d = g["start"].astimezone(AZ).date()
        h, a = team_code(g["home"]["name"]), team_code(g["away"]["name"])
        sp = {s: names.get((g[s]["starter"] or "").strip().lower()) for s in ("home", "away")}
        summary["starters_unknown"] += sum(1 for s in ("home", "away") if sp[s] is None)
        blob = {"game": {"sport": "MLB", "away": g["away"]["name"], "home": g["home"]["name"],
                         "away_abbr": g["away"]["abbr"], "home_abbr": g["home"]["abbr"],
                         "event_start": g["start"].isoformat(),
                         "records": {"away": g["away"]["record"], "home": g["home"]["record"]},
                         "series": g.get("series"), "broadcast": g.get("broadcast")},
                "lines": g["odds"], "lines_espn": g.get("odds_espn"),
                "starters": {s: {"name": g[s]["starter"], "rated": sp[s] is not None}
                             for s in ("home", "away")},
                "model_meta": {"engine": "diamond_iq_iter3", "hfa_mult": HFA_MULT,
                               "run_line_shares": shares, "computed_at": now_iso}}
        proj = (state.project(h, a, d, sp["home"], sp["away"], hfa=0.0,
                              home_lineup=last_lineup.get(h), away_lineup=last_lineup.get(a))
                if h and a else None)
        if proj is not None:
            xh = proj["xr_home_env"] * (1.0 + HFA_MULT)
            xa = proj["xr_away_env"]
            wh = di.pyth_prob(xh, xa)
            pr = {"win_home": round(wh, 4), "exp_home": round(xh, 2), "exp_away": round(xa, 2),
                  "exp_margin": round(xh - xa, 2), "exp_total": round(xh + xa, 1)}
            blob["model"] = pr
            blob["picks"] = verdicts(g, pr, shares)
            summary["priced"] += 1
            summary["leans"] += sum(1 for v in blob["picks"].values() if v.get("verdict") == "lean")
        else:
            blob["unavailable"] = ["model_unrated_team" if h and a else "team_unmapped"]
        # A SERIES PLAYS THE SAME MATCHUP ON BACK-TO-BACK DAYS, and the
        # sheet store's key is (week, sport, event_name): a bare
        # "Away @ Home" would let tonight's game overwrite last night's
        # graded row (and two in one upsert batch is a Postgres error). The
        # AZ date makes it unique; the page matches bets by market id.
        name = f"{g['away']['name']} @ {g['home']['name']}"
        out_rows.append({"week_key": wk, "sport": "MLB",
                         "event_name": f"{name} · {d.isoformat()}",
                         "market_id": market_id(sb, name, g["start"]),
                         "event_start": g["start"].isoformat(), "espn_id": g["espn_id"],
                         "tier": "data", "data_blob": blob, "data_built_at": now_iso})
        log.info("%s @ %s  %s", g["away"]["abbr"], g["home"]["abbr"], json.dumps({
            "line": g["odds"], "starters": blob["starters"],
            "model": blob.get("model"), "picks": blob.get("picks")}, default=str))
    summary["games"] = len(out_rows)
    if commit and out_rows:
        sb_upsert("football_sheets", out_rows, "week_key,sport,event_name")
        sb_upsert("football_sheet_weeks", [{"week_key": wk, "sport": "MLB",
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
    ap.add_argument("--pin", action="store_true",
                    help="allow the paid parlay-api fallback when Pinnacle's own feed fails")
    a = ap.parse_args()
    s = run(a.days, a.commit, pin=a.pin)
    print(json.dumps(s, indent=2, default=str))
    return 0 if s.get("error") is None else 1


if __name__ == "__main__":
    sys.exit(main())
