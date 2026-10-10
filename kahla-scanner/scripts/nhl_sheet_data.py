"""HOCKEY PICK SHEET — assembly (Sep 30 2026, opening night).

Rob: "I want a hockey (and eventually all sports sheets) added to the
pick sheets ... ML (show model's expected spread) and O/U." One row per
upcoming NHL game into `football_sheets` (sport='NHL' — the table is the
generic pick-sheet store now; the sport CHECK was widened by
supabase/pick_sheets_sports.sql), priced by Crease IQ v2
(_lib/crease_iq2.py):

    Line:  BOS −145 · puck −1.5 +160 · O/U 6.5
    Model: BOS −0.6 goals (61%) · total 6.1
    Picks: BOS ML (play, good to −150) · Under 6.5 (lean)

Pipeline (runs on GitHub Actions — `.github/workflows/nhl-sheets.yml` —
the one compute that reaches ESPN, the NHL API and the cloud DB):
  1. (workflow) ingest_nhl_goalies/ingest_nhl_shots --delta into the DB
  2. finished games → per-game xG (cached in NHL_CACHE, delta-pulled)
  3. fit Crease IQ v2 on every finished game
  4. ESPN slate (today + tomorrow, AZ) with DraftKings/ESPN BET lines
  5. projected starting goalies (DailyFaceoff → heuristic fallback)
  6. price + verdicts → upsert football_sheets / football_sheet_weeks
  7. (workflow) football_sheet_sync pushes the rows to the site DB

Verdict rules (model vs the market's NO-VIG price):
  ML     play ≥ 4.0pp edge · lean ≥ 2.0pp · pass otherwise;
         > 12pp = pass + flagged (the model doesn't know about a scratch
         the market does — goalie news is the usual culprit)
  TOTAL  capped at LEAN (≥ 4pp) — the total has NOT beaten the base rate
         in the walk-forward backtest; it is shown as our number, not sold
         as an edge.

  python -m scripts.nhl_sheet_data [--days 1] [--commit]
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

_SCANNER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SCANNER)
sys.path.insert(0, os.path.dirname(_SCANNER))      # repo root: app.py (the Pinnacle slate)
from _lib import crease_iq2 as ci2  # noqa: E402
from scripts.football_sheet_data import (_espn_get, sb_select,  # noqa: E402
                                         sb_upsert, week_key_default)

log = logging.getLogger("nhl_sheet_data")
AZ = ZoneInfo("America/Phoenix")

ML_PLAY_PP, ML_LEAN_PP, ML_IMPLAUSIBLE_PP = 4.0, 2.0, 12.0
TOT_LEAN_PP = 4.0

# ESPN abbreviation → NHL API tricode (the goalie/shot spine's dialect).
_ESPN_TO_NHL = {"LA": "LAK", "NJ": "NJD", "SJ": "SJS", "TB": "TBL",
                "UTAH": "UTA", "UTA": "UTA", "WAS": "WSH", "MON": "MTL",
                "NAS": "NSH", "CLB": "CBJ", "VEG": "VGK", "LV": "VGK"}
_CACHE = os.path.expanduser(os.environ.get("NHL_CACHE", "~/.kahla/nhl_games_cache.json"))


# ----------------------------------------------------------------- data
def load_goalie_rows(since: str | None = None) -> list[dict]:
    p = {"select": "*", "order": "game_date.asc,game_id.asc,goalie_id.asc"}
    if since:
        p["game_date"] = f"gte.{since}"
    return sb_select("nhl_goalie_games", p)


def load_shot_rows(since: str | None = None) -> list[dict]:
    p = {"select": "game_id,event_id,game_date,event_type,is_goal,situation,"
                   "goalie_id,x,y,shot_type,team_id,period,time_in_period",
         "order": "game_id.asc,event_id.asc"}
    if since:
        p["game_date"] = f"gte.{since}"
    return sb_select("nhl_shot_events", p)


def finished_games() -> tuple[list[dict], list[dict]]:
    """(games, goalie_rows). Games are cached by game_id; only the tail
    (last 5 days of the cache) is re-pulled — a full shot pull is ~490k
    rows, minutes over REST."""
    cache: dict = {}
    try:
        cache = {g["game_id"]: g for g in json.load(open(_CACHE))}
    except (OSError, ValueError):
        pass
    goalies = load_goalie_rows()
    since = None
    if cache:
        last = max(g["date"] for g in cache.values())
        since = (date.fromisoformat(last) - timedelta(days=5)).isoformat()
    shots = load_shot_rows(since)
    fresh = ci2.build_games([r for r in goalies if not since or r["game_date"] >= since], shots)
    for g in fresh:
        cache[g["game_id"]] = g
    games = sorted(cache.values(), key=lambda g: (g["date"], g["game_id"]))
    try:
        os.makedirs(os.path.dirname(_CACHE), exist_ok=True)
        json.dump(games, open(_CACHE, "w"))
    except OSError as e:
        log.warning("cache write failed: %s", e)
    log.info("finished games: %d (%d fresh, shots pulled since %s: %d)",
             len(games), len(fresh), since or "the beginning", len(shots))
    return games, goalies


# ----------------------------------------------------------------- ESPN
def _num(v):
    try:
        if v is None or v == "":
            return None
        s = str(v).strip().lower().replace("o", "").replace("u", "")
        if s in ("even", "ev"):
            return 100.0
        return float(s)
    except (TypeError, ValueError):
        return None


def _parse_odds(comp: dict) -> dict:
    """ML / puck line / total off the scoreboard's first odds provider.
    Two ESPN shapes live side by side (the flat homeTeamOdds.moneyLine
    and the nested moneyline.home.close.odds) — read either."""
    odds = (comp.get("odds") or [])
    if not odds:
        return {}
    o = odds[0]
    out: dict = {"provider": (o.get("provider") or {}).get("name") or "book"}
    hto, ato = o.get("homeTeamOdds") or {}, o.get("awayTeamOdds") or {}
    ml = o.get("moneyline") or {}
    out["ml_home"] = _num(hto.get("moneyLine")) or _num(((ml.get("home") or {}).get("close") or {}).get("odds"))
    out["ml_away"] = _num(ato.get("moneyLine")) or _num(((ml.get("away") or {}).get("close") or {}).get("odds"))
    tot = o.get("total") or {}
    out["total"] = _num(o.get("overUnder")) or _num(((tot.get("over") or {}).get("close") or {}).get("line"))
    out["over_odds"] = _num(o.get("overOdds")) or _num(((tot.get("over") or {}).get("close") or {}).get("odds"))
    out["under_odds"] = _num(o.get("underOdds")) or _num(((tot.get("under") or {}).get("close") or {}).get("odds"))
    ps = o.get("pointSpread") or {}
    hl = _num(((ps.get("home") or {}).get("close") or {}).get("line"))
    if hl is None and o.get("spread") is not None:
        mag = abs(_num(o.get("spread")) or 0)
        hl = -mag if hto.get("favorite") else mag if ato.get("favorite") else None
    out["puck_home"] = hl
    out["puck_home_odds"] = _num(hto.get("spreadOdds")) or _num(((ps.get("home") or {}).get("close") or {}).get("odds"))
    out["puck_away_odds"] = _num(ato.get("spreadOdds")) or _num(((ps.get("away") or {}).get("close") or {}).get("odds"))
    out["details"] = o.get("details")
    return {k: v for k, v in out.items() if v is not None}


def espn_slate(days: int) -> list[dict] | None:
    url = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"
    today = datetime.now(AZ).date()
    events, ok = [], False
    for i in range(days + 1):
        d = _espn_get(url, {"dates": f"{today + timedelta(days=i):%Y%m%d}"})
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
        comp = (ev.get("competitions") or [{}])[0]
        st = ((ev.get("status") or {}).get("type") or {}).get("state")
        if st != "pre":
            continue
        sides: dict = {}
        for c in comp.get("competitors") or []:
            t = c.get("team") or {}
            ab = (t.get("abbreviation") or "").upper()
            rec = next((r.get("summary") for r in (c.get("records") or [])
                        if r.get("type") == "total"), "")
            prob = None
            for p in c.get("probables") or []:
                prob = ((p.get("athlete") or {}).get("displayName")) or prob
            sides[c.get("homeAway")] = {"name": t.get("displayName") or "",
                                        "abbr": _ESPN_TO_NHL.get(ab, ab),
                                        "record": rec, "probable": prob}
        if "home" not in sides or "away" not in sides:
            continue
        try:
            start = datetime.fromisoformat((ev.get("date") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        out.append({"espn_id": str(ev.get("id")), "start": start,
                    "home": sides["home"], "away": sides["away"],
                    "odds": _parse_odds(comp),
                    "broadcast": ", ".join(b for bl in (comp.get("broadcasts") or [])
                                           for b in (bl.get("names") or []))})
    return sorted(out, key=lambda g: g["start"])


# --------------------------------------------------------------- goalies
def dailyfaceoff(d: date) -> dict:
    """{last-name-lower: status} per team side is not needed — returns
    {(team_hint, 'home'|'away'): (name, status)} best-effort. Parsed off
    the page's __NEXT_DATA__ with a tolerant key walk; any shape change
    degrades to {} (the heuristic fills in)."""
    try:
        r = httpx.get(f"https://www.dailyfaceoff.com/starting-goalies/{d.isoformat()}",
                      headers={"User-Agent": "Mozilla/5.0 (kahla-house pick sheet)"},
                      timeout=20, follow_redirects=True)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            log.info("dailyfaceoff: no __NEXT_DATA__ (status %s)", r.status_code)
            return {}
        data = json.loads(m.group(1))
    except Exception as e:  # noqa: BLE001
        log.info("dailyfaceoff unavailable: %s", e)
        return {}
    out: dict = {}

    def walk(x):
        if isinstance(x, dict):
            keys = {k.lower(): k for k in x}
            if "homegoaliename" in keys and "awaygoaliename" in keys:
                for side in ("home", "away"):
                    nm = x.get(keys[f"{side}goaliename"])
                    team = (x.get(keys.get(f"{side}teamname", ""), "")
                            or x.get(keys.get(f"{side}teamslug", ""), "") or "")
                    status = (x.get(keys.get(f"{side}newsstrengthname", ""), "")
                              or x.get(keys.get(f"{side}goaliestatus", ""), "") or "")
                    if nm:
                        out[(str(team).lower(), side)] = (nm, str(status))
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(data)
    log.info("dailyfaceoff %s: %d goalie slots", d, len(out))
    return out


def _team_goalies(goalies: list[dict], team: str) -> list[dict]:
    """Goalies whose MOST RECENT game was for `team` (offseason movers
    leave their old team's pool), with their starts in the team's last
    25 games."""
    last_team: dict = {}
    for r in goalies:
        last_team[r["goalie_id"]] = (r["team"], r["goalie_name"])
    tg = [r for r in goalies if r["team"] == team]
    recent_ids = sorted({r["game_id"] for r in tg})[-25:]
    starts: dict = {}
    for r in tg:
        if r["game_id"] in recent_ids and r.get("starter"):
            starts[r["goalie_id"]] = starts.get(r["goalie_id"], 0) + 1
    last_date = {r["goalie_id"]: r["game_date"] for r in tg}
    out = []
    for gid, (t, nm) in last_team.items():
        if t == team:
            out.append({"id": gid, "name": nm, "starts": starts.get(gid, 0),
                        "last": last_date.get(gid)})
    return sorted(out, key=lambda x: -x["starts"])


def _match_name(full: str, pool: list[dict], all_goalies: dict) -> dict | None:
    """'Jeremy Swayman' → our 'J. Swayman'. Team pool first, league second
    (a goalie who changed teams this summer)."""
    parts = (full or "").replace(".", " ").split()
    if not parts:
        return None
    last, ini = parts[-1].lower(), parts[0][0].lower()
    for cand in pool + list(all_goalies.values()):
        nm = (cand["name"] or "").replace(".", " ").split()
        if nm and nm[-1].lower() == last and nm[0][0].lower() == ini:
            return cand
    return None


def project_goalie(team: str, side: str, g: dict, goalies: list[dict],
                   dfo: dict, all_goalies: dict, played_yesterday: bool) -> dict:
    pool = _team_goalies(goalies, team)
    named = g[side].get("probable")
    status = "espn probable" if named else None
    if not named:
        for (hint, s), (nm, stt) in dfo.items():
            if s == side and hint and (hint in g[side]["name"].lower()
                                       or g[side]["name"].lower() in hint
                                       or hint.split("-")[-1] in g[side]["name"].lower()):
                named, status = nm, (stt or "dailyfaceoff")
                break
    if named:
        m = _match_name(named, pool, all_goalies)
        return {"name": named, "id": m["id"] if m else None,
                "source": status, "known": bool(m)}
    if not pool:
        return {"name": None, "id": None, "source": "unknown", "known": False}
    pick = pool[0]
    if played_yesterday and len(pool) > 1 and pool[0]["last"] and \
            pool[0]["last"] == (datetime.now(AZ).date() - timedelta(days=1)).isoformat():
        pick = pool[1]                       # back-to-back: the backup starts
    return {"name": pick["name"], "id": pick["id"], "source": "projected (usage)",
            "known": True}


# ------------------------------------------------------------- pinnacle
def pin_odds_from_events(events, away: str, home: str, pin_outcomes, start=None) -> dict | None:
    """Pinnacle's ML / puck line / total for one game, in the ESPN odds
    shape the verdicts read, off a TOA-shaped slate. `pin_outcomes` is
    app._pin_outcomes (passed in so this stays a pure, testable function).
    None unless Pinnacle posts the moneyline (the sheet's primary market)."""
    if not events:
        return None
    mh, ma = pin_outcomes(events, away, home, "h2h", "home", start=start)
    if not mh or mh.get("price") is None or not ma or ma.get("price") is None:
        return None
    out: dict = {"provider": "pinnacle",
                 "ml_home": _num(mh.get("price")), "ml_away": _num(ma.get("price"))}
    ov, un = pin_outcomes(events, away, home, "totals", "over", start=start)
    if ov and ov.get("point") is not None:
        out["total"] = _num(ov.get("point"))
        out["over_odds"] = _num(ov.get("price"))
        out["under_odds"] = _num((un or {}).get("price"))
    ph, pa = pin_outcomes(events, away, home, "spreads", "home", start=start)
    if ph and ph.get("point") is not None:
        out["puck_home"] = _num(ph.get("point"))
        out["puck_home_odds"] = _num(ph.get("price"))
        out["puck_away_odds"] = _num((pa or {}).get("price"))
    return out


def pin_overlay(slate: list[dict], spend: bool, sport: str = "NHL") -> dict:
    """Rob, Oct 5 2026: the sheet's line is Pinnacle's. Pull (spend=True,
    ~3 credits, 90-min cache, 900/mo hard stop) or just read the cached
    parlay-api slate for `sport` (NHL here, NBA via nba_sheet_data) and
    swap each game's ESPN odds for Pinnacle's; ESPN's stay in
    g["odds_espn"]. Oct 6 2026 (Rob: "7 am pull pinnacle, daily, for all
    sports, then DK for the updates"): a slate older than the app's
    _SHEET_PIN_FRESH_S leaves the book line in place — the 07:00 run is
    the Pinnacle sheet, the later runs re-price off the book."""
    try:
        import app
    except Exception as e:
        log.warning("pinnacle: app import failed (%s) — ESPN lines stay", e)
        return {"error": "no_app"}
    sb = app.get_supabase()
    now = datetime.now(timezone.utc)
    st: dict = {"spend": spend}
    try:
        # Pinnacle's own feed every run (free, Oct 10 2026); only a
        # spend run may fall back to the paid parlay-api pull.
        app._pin_slate(sb, sport, now, paid=spend)
        events, age = app._pin_slate_cached(sb, sport, now)
    except Exception as e:
        log.warning("pinnacle: slate read failed: %s", e)
        return {"error": str(e)[:120]}
    st["sport"] = sport
    st["events"] = len(events or [])
    st["age_min"] = round(age, 1) if age is not None else None
    fresh_s = getattr(app, "_SHEET_PIN_FRESH_S", app._PIN_CENTER_MAX_AGE_S)
    if not events or age is None or age * 60.0 > fresh_s:
        st["used"] = 0
        st["why"] = "no_slate" if not events else "stale"
        return st
    n = 0
    for g in slate:
        po = pin_odds_from_events(events, g["away"]["name"], g["home"]["name"],
                                  app._pin_outcomes, start=g.get("start"))
        if po:
            g["odds_espn"] = g["odds"]
            g["odds"] = po
            n += 1
    st["used"] = n
    try:
        month_k = "usage:" + now.strftime("%Y-%m")
        st["credits_used"] = (((app._parlay_state_get(sb, month_k) or {}).get("v") or {})
                              .get("credits") or 0)
    except Exception:
        pass
    return st


# ---------------------------------------------------------------- pricing
def _implied(a: float | None) -> float | None:
    if a is None:
        return None
    return 100.0 / (a + 100.0) if a > 0 else -a / (-a + 100.0)


def _american(p: float) -> int:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return int(round(-100 * p / (1 - p))) if p >= 0.5 else int(round(100 * (1 - p) / p))


def _no_vig(a, b):
    pa, pb = _implied(a), _implied(b)
    if pa is None or pb is None or pa + pb <= 0:
        return None, None
    return pa / (pa + pb), pb / (pa + pb)


def verdicts(g: dict, pr: dict) -> dict:
    o = g["odds"]
    out: dict = {}
    ph, pa = _no_vig(o.get("ml_home"), o.get("ml_away"))
    if ph is not None:
        mh = pr["win_home"]
        e_home, e_away = (mh - ph) * 100, ((1 - mh) - pa) * 100
        side = "home" if e_home >= e_away else "away"
        edge = max(e_home, e_away)
        p_side = mh if side == "home" else 1 - mh
        v = ("play" if edge >= ML_PLAY_PP else "lean" if edge >= ML_LEAN_PP else "pass")
        implaus = edge > ML_IMPLAUSIBLE_PP
        if implaus:
            v = "pass"
        out["ml"] = {"side": side, "team": g[side]["name"], "abbr": g[side]["abbr"],
                     "price": o.get(f"ml_{side}"), "fair": _american(p_side),
                     "model_p": round(p_side, 4),
                     "market_p": round(ph if side == "home" else pa, 4),
                     "edge_pp": round(edge, 1), "verdict": v,
                     "model_gap_implausible": implaus}
    L = o.get("total")
    if L is not None:
        p_over = pr["over"].get(str(L))
        if p_over is not None:
            po, pu = _no_vig(o.get("over_odds") or -110, o.get("under_odds") or -110)
            e_over, e_under = (p_over - po) * 100, ((1 - p_over) - pu) * 100
            side = "over" if e_over >= e_under else "under"
            edge = max(e_over, e_under)
            out["total"] = {"side": side, "line": L,
                            "price": o.get(f"{side}_odds"),
                            "fair": _american(p_over if side == "over" else 1 - p_over),
                            "model_p": round(p_over if side == "over" else 1 - p_over, 4),
                            "edge_pp": round(edge, 1),
                            "verdict": "lean" if edge >= TOT_LEAN_PP else "pass",
                            "capped": "total model has no backtested edge — lean max"}
    return out


# ---------------------------------------------------------------- driver
def _ensure_sport_check() -> bool:
    """The football_sheets sport CHECK predates hockey. On the box (psql
    on disk) apply the widening migration in place; elsewhere report."""
    sql = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "supabase", "pick_sheets_sports.sql")
    for psql in ("/Applications/Postgres.app/Contents/Versions/latest/bin/psql", "psql"):
        try:
            r = subprocess.run([psql, os.environ.get("KAHLA_PGDB", "kahla"), "-f", sql],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                log.info("applied %s via %s", sql, psql)
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    log.error("sport CHECK rejects NHL and no psql here — run "
              "kahla-scanner/scripts/run_sql.sh -f supabase/pick_sheets_sports.sql")
    return False


def run(days: int, commit: bool, pin: bool | None = None) -> dict:
    games, goalies = finished_games()
    st, params = ci2.fit(games)
    log.info("Crease IQ v2 fit: n=%d coef=%s l3=%.2f delta=%.3f k=%.2f",
             params["n_fit"], [round(c, 3) for c in params["coef"]],
             params["lam3"], params["delta"], params["k"])
    slate = espn_slate(days)
    if slate is None:
        log.error("ESPN unreachable — no slate")
        return {"games": 0, "error": "espn_dark"}
    pin_st = pin_overlay(slate, spend=bool(pin)) if pin is not None else None
    today = datetime.now(AZ).date()
    dfo = {}
    for i in range(days + 1):
        dfo.update(dailyfaceoff(today + timedelta(days=i)))
    all_goalies = {}
    for r in goalies:
        all_goalies[r["goalie_id"]] = {"id": r["goalie_id"], "name": r["goalie_name"],
                                       "starts": 0, "last": r["game_date"]}
    yesterday = (today - timedelta(days=1)).isoformat()
    played_y = {g["home"] for g in games if g["date"] == yesterday} | \
               {g["away"] for g in games if g["date"] == yesterday}
    wk = week_key_default()
    now_iso = datetime.now(timezone.utc).isoformat()
    rows, summary = [], {"games": 0, "priced": 0, "plays": 0}
    for g in slate:
        h, a = g["home"]["abbr"], g["away"]["abbr"]
        gh = project_goalie(h, "home", g, goalies, dfo, all_goalies, h in played_y)
        ga = project_goalie(a, "away", g, goalies, dfo, all_goalies, a in played_y)
        totals = sorted({5.5, 6.5} | ({g["odds"]["total"]} if g["odds"].get("total") else set()))
        d_game = g["start"].astimezone(AZ).date()
        pr = ci2.price(st, params, h, a, d_game, gh["id"], ga["id"], totals=tuple(totals))
        blob = {"game": {"sport": "NHL", "away": g["away"]["name"], "home": g["home"]["name"],
                         "away_abbr": a, "home_abbr": h,
                         "event_start": g["start"].isoformat(),
                         "records": {"away": g["away"]["record"], "home": g["home"]["record"]},
                         "broadcast": g.get("broadcast")},
                "lines": g["odds"], "lines_espn": g.get("odds_espn"),
                "goalies": {"home": gh, "away": ga},
                "model_meta": {"engine": "crease_iq2", "n_fit": params["n_fit"],
                               "computed_at": now_iso}}
        if pr:
            blob["model"] = pr
            blob["picks"] = verdicts(g, pr)
            summary["priced"] += 1
            summary["plays"] += sum(1 for v in blob["picks"].values() if v.get("verdict") == "play")
        else:
            blob["unavailable"] = ["model_unrated_team"]
        rows.append({"week_key": wk, "sport": "NHL",
                     "event_name": f"{g['away']['name']} @ {g['home']['name']}",
                     "event_start": g["start"].isoformat(), "espn_id": g["espn_id"],
                     "tier": "data", "data_blob": blob, "data_built_at": now_iso})
        log.info("%s @ %s  %s", a, h, json.dumps({
            "line": g["odds"], "goalies": [ga.get("name"), gh.get("name")],
            "model": {k: (pr or {}).get(k) for k in ("win_home", "exp_margin", "exp_total")},
            "picks": blob.get("picks")}, default=str))
    summary["games"] = len(rows)
    if commit and rows:
        try:
            sb_upsert("football_sheets", rows, "week_key,sport,event_name")
        except httpx.HTTPStatusError as e:
            if "check" in e.response.text.lower() and _ensure_sport_check():
                sb_upsert("football_sheets", rows, "week_key,sport,event_name")
            else:
                raise
        sb_upsert("football_sheet_weeks", [{"week_key": wk, "sport": "NHL",
                                            "games": len(rows), "deep_games": 0}],
                  "week_key,sport")
    summary["week_key"] = wk
    if pin_st is not None:
        summary["pinnacle"] = pin_st
    return summary


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1, help="today + N days (AZ)")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--pin", action="store_true",
                    help="pull Pinnacle's NHL slate (parlay-api, ~3 credits, 90-min "
                         "cache) and price against it; without it the cached slate "
                         "is still used when fresh")
    args = ap.parse_args()
    s = run(args.days, args.commit, pin=args.pin)
    print(json.dumps(s, indent=2, default=str))
    return 0 if s.get("games") or s.get("error") is None else 1


if __name__ == "__main__":
    sys.exit(main())
