"""FOOTBALL WEEKLY GAME SHEETS — mechanical data assembly.

Builds the per-game data_blob for every NFL + NCAAF game in the coming week
and upserts it into `football_sheets`. The narrative is NOT written here —
a Claude session (the Monday/Friday Routine) reads these blobs, writes the
analysis, and renders/publishes the PDFs. See docs/football-sheet-runbook.md.

Runs on GitHub Actions (football-sheets-data.yml) because that runner can
reach BOTH ESPN and Supabase. The CCR sandbox cannot reach ESPN, so every
ESPN-sourced section degrades to unavailable there — by design the sheet
renders "unavailable" for a section with no data, NEVER an invented figure.

DB access is raw PostgREST over httpx (no supabase-py): the generation
sandbox can't import the supabase client (cffi bug), and this way the same
script runs identically in both places. All reads PAGE via Range headers —
PostgREST hard-caps any single response at 1,000 rows (gotcha #40).

Usage:
  python -m scripts.football_sheet_data --mode monday --commit
  python -m scripts.football_sheet_data --mode friday --commit
  python -m scripts.football_sheet_data --mode monday --week-key 2026-08-24 --sport NCAAF
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _lib import gridiron_spread as gsp          # noqa: E402
from _lib import power_ratings as pr             # noqa: E402

log = logging.getLogger("football_sheet_data")
AZ = ZoneInfo("America/Phoenix")

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_LEAGUES = {"NFL": ("football", "nfl"), "NCAAF": ("football", "college-football")}

# Depth tiering (NCAAF long tail — resolved with Rob Aug 22 2026):
# NFL: every game deep. NCAAF: deep when both-ranked, or either-ranked with
# a ≥2pt model-vs-market spread gap, or an unranked game with a ≥3pt spread
# / ≥4pt total gap; capped at _NCAAF_DEEP_CAP by priority (both-ranked
# always survive the cap). Everything else = full data sheet + short read.
_NCAAF_DEEP_CAP = 18
# Aligned with the PLAY bar (1.5 pts, Aug 22 2026): a game the sheet calls
# a PLAY deserves the deep dive. The cap keeps the long tail bounded —
# priority ranking still puts the biggest edges first.
_EDGE_SPREAD_PTS = 1.5
_EDGE_SPREAD_RANKED_PTS = 1.0
_EDGE_TOTAL_PTS = 1.5

# Reliability floor (added Sep 3 2026 — Rob: "if no model, say excluded").
# `compute_ratings` will happily hand a team an off/def number off ONE
# game (gp=1) — that's not a rating, it's that game's score. Two straight
# "deep" tier sheets (Bethune-Cookman @ UCF, Merrimack @ Delaware) got
# built on gp=1 teams: the model's fabricated 20-36pt "edge" against the
# market was the LOUDEST signal in the pipeline, which is exactly what
# pushed both into the deep-dive cap ahead of games with a real number.
# Same claimed-edge-cliff lesson as NRFI/Diamond IQ/UFC duration —
# a huge edge off a team the model barely knows is noise, not signal.
# A team under this floor gets treated exactly like a team missing from
# ratings entirely: price_game() returns None, no bet_spread/bet_total
# enters the blob, decide_tiers can't be fooled by a fake edge, and the
# narrative rule ("every number must exist in the blob") can't cite one.
_MIN_TEAM_GP = 2

# Season floors — NO PRESEASON SHEETS (mirror of app.py _GRIDIRON_MIN_START;
# ⚠ UPDATE YEARLY). Without this the first shakedown run built 27 preseason
# NFL sheets priced off regular-season ratings — garbage projections for
# games where starters sit.
_SEASON_FLOOR = {"NFL": "2026-09-08", "NCAAF": "2026-08-29"}

# Friday diff thresholds — below these a move isn't worth a changes line.
_DIFF_SPREAD_PTS = 0.5
_DIFF_TOTAL_PTS = 1.0
_DIFF_ML_CENTS = 3


# ---------------------------------------------------------------- REST layer
def _sb_base() -> tuple[str, dict]:
    url = (os.environ.get("SUPABASE_URL") or "").strip().strip("<>").rstrip("/")
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not url or not key:
        raise SystemExit("SUPABASE_URL / SUPABASE_SERVICE_KEY not set")
    return url, {"apikey": key, "Authorization": f"Bearer {key}"}


def sb_select(table: str, params: dict, page: int = 1000) -> list[dict]:
    """Paged PostgREST read — never trust a single response past 1,000 rows."""
    base, headers = _sb_base()
    out: list[dict] = []
    lo = 0
    while True:
        h = dict(headers)
        h["Range"] = f"{lo}-{lo + page - 1}"
        h["Range-Unit"] = "items"
        r = httpx.get(f"{base}/rest/v1/{table}", params=params, headers=h,
                      timeout=30)
        r.raise_for_status()
        rows = r.json() or []
        out.extend(rows)
        if len(rows) < page:
            return out
        lo += page


def sb_upsert(table: str, rows: list[dict], on_conflict: str) -> None:
    if not rows:
        return
    base, headers = _sb_base()
    h = dict(headers)
    h["Content-Type"] = "application/json"
    h["Prefer"] = "resolution=merge-duplicates,return=minimal"
    for i in range(0, len(rows), 50):
        r = httpx.post(f"{base}/rest/v1/{table}",
                       params={"on_conflict": on_conflict},
                       headers=h, json=rows[i:i + 50], timeout=60)
        r.raise_for_status()


def sb_delete(table: str, filters: dict) -> None:
    base, headers = _sb_base()
    h = dict(headers)
    h["Prefer"] = "return=minimal"
    r = httpx.delete(f"{base}/rest/v1/{table}", params=filters, headers=h,
                     timeout=30)
    r.raise_for_status()


def sb_patch(table: str, filters: dict, patch: dict) -> None:
    base, headers = _sb_base()
    h = dict(headers)
    h["Content-Type"] = "application/json"
    h["Prefer"] = "return=minimal"
    r = httpx.patch(f"{base}/rest/v1/{table}", params=filters, headers=h,
                    json=patch, timeout=30)
    r.raise_for_status()


# ---------------------------------------------------------------- ESPN layer
def _espn_get(url: str, params: dict | None = None) -> dict | None:
    """GET with a host fallback: site.api.espn.com started hard-403'ing the
    Actions runners (Aug 2026, every request — the per-day trick died too),
    while site.web.api.espn.com serves the same site/v2 paths and is NOT
    blocked (proven by the FPI fetch). Try the canonical host, then the
    web host."""
    last = None
    for u in (url, url.replace("://site.api.espn.com/",
                               "://site.web.api.espn.com/")):
        try:
            # No spoofed UA at ESPN — Akamai 403s a fake Chrome UA while
            # httpx's honest default passes (Aug 31 2026, the dark-spine
            # root cause; see ingest_espn_markets._espn_games).
            r = httpx.get(u, params=params or {}, timeout=20)
            r.raise_for_status()
            return r.json() or {}
        except Exception as e:
            last = e
            if u == url and "site.api.espn.com" in url:
                continue
            break
    log.warning("ESPN fetch failed %s %s: %s", url, params, last)
    return None


def espn_week_games(sport: str, days: int) -> list[dict] | None:
    """Full slate for [today, today+days]. NCAAF passes groups=80&limit=400
    — without groups the CFB scoreboard returns only the featured slate,
    and Rob wants FULL FBS. Per-day fetch (the multi-day dates=A-B range is
    the code path ESPN 403'd on the Actions runners Aug 2026)."""
    grp, lg = _LEAGUES[sport]
    url = f"https://site.api.espn.com/apis/site/v2/sports/{grp}/{lg}/scoreboard"
    now = datetime.now(timezone.utc)
    events: list[dict] = []
    ok = False
    for i in range(days + 1):
        params = {"dates": f"{now + timedelta(days=i):%Y%m%d}"}
        if sport == "NCAAF":
            params.update({"groups": "80", "limit": "400"})
        d = _espn_get(url, params)
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
        home = away = None
        recs: dict[str, str] = {}
        abbr: dict[str, str] = {}
        locs: dict[str, str] = {}
        for c in comp.get("competitors") or []:
            t = c.get("team") or {}
            name = t.get("displayName") or t.get("name") or ""
            rec = ""
            for rr in c.get("records") or []:
                if rr.get("type") == "total":
                    rec = rr.get("summary") or ""
            if c.get("homeAway") == "home":
                home, recs["home"] = name, rec
                abbr["home"] = t.get("abbreviation") or ""
                locs["home"] = t.get("location") or ""
            elif c.get("homeAway") == "away":
                away, recs["away"] = name, rec
                abbr["away"] = t.get("abbreviation") or ""
                locs["away"] = t.get("location") or ""
        book = _parse_espn_odds(comp, abbr)
        state = ((ev.get("status") or {}).get("type") or {}).get("state")
        if not (home and away) or state != "pre":
            continue
        try:
            start = datetime.fromisoformat(
                (ev.get("date") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        out.append({
            "espn_id": str(ev.get("id")),
            "away": away, "home": home,
            "event_start": start,
            "neutral_site": bool(comp.get("neutralSite")),
            "venue": ((comp.get("venue") or {}).get("fullName")),
            "broadcast": ", ".join(
                b for bl in (comp.get("broadcasts") or [])
                for b in (bl.get("names") or [])),
            "records": recs,
            "locs": locs,      # ESPN team.location — the short handle
                               # ("TCU", "North Carolina"); mascots are
                               # multi-word too often to strip by heuristic
            "book_odds": book,
        })
    return out


def _parse_espn_odds(comp: dict, abbr: dict) -> dict | None:
    """Consensus/ESPN BET line from the scoreboard event itself — the line
    the Monday sheet prices against, since VSiN only enters its 80h window
    mid-week. `details` names the favorite by abbreviation ("TCU -7.5");
    orient to the home side. Tolerant: anything unexpected → None."""
    try:
        odds = (comp.get("odds") or [])
        if not odds:
            return None
        o = odds[0]
        out: dict = {"provider": ((o.get("provider") or {}).get("name")
                                  or "consensus")}
        ou = o.get("overUnder")
        if ou is not None:
            out["total"] = float(ou)
        spread = o.get("spread")
        details = o.get("details") or ""
        home_line = None
        if spread is not None:
            mag = abs(float(spread))
            fav_abbr = details.split()[0] if details else ""
            if fav_abbr and fav_abbr == abbr.get("away"):
                home_line = +mag
            elif fav_abbr and fav_abbr == abbr.get("home"):
                home_line = -mag
            else:
                hto = o.get("homeTeamOdds") or {}
                if hto.get("favorite") is True:
                    home_line = -mag
                elif (o.get("awayTeamOdds") or {}).get("favorite") is True:
                    home_line = +mag
        if home_line is not None:
            out["spread_home"] = home_line
        if details:
            out["details"] = details
        return out if ("spread_home" in out or "total" in out) else None
    except (TypeError, ValueError, IndexError, AttributeError):
        return None


def espn_injuries(sport: str) -> dict[str, list] | None:
    """League-wide injuries → {team displayName: [{name, position, status,
    detail}]}. One call covers every team (the pattern handicapper_web
    uses). Empty/missing → None = section unavailable."""
    grp, lg = _LEAGUES[sport]
    d = _espn_get(
        f"https://site.api.espn.com/apis/site/v2/sports/{grp}/{lg}/injuries")
    if d is None:
        return None
    out: dict[str, list] = {}
    for team_grp in d.get("injuries") or []:
        tname = (team_grp.get("displayName")
                 or (team_grp.get("team") or {}).get("displayName") or "")
        rows = []
        for it in (team_grp.get("injuries") or [])[:30]:
            ath = it.get("athlete") or {}
            det = it.get("details") or {}
            rows.append({
                "name": ath.get("displayName") or it.get("name") or "",
                "position": ((ath.get("position") or {}).get("abbreviation")
                             or ""),
                "status": it.get("status") or det.get("type") or "",
                "detail": (det.get("detail") or det.get("side") or ""),
                "comment": ((it.get("longComment") or it.get("shortComment")
                             or "")[:220]),
            })
        if tname and rows:
            out[tname] = rows
    return out or None


def espn_ap_ranks() -> dict[str, int] | None:
    """AP Top 25 → {FOLDED team alias: rank}. NCAAF tiering input.

    Keys are _fold()ed aliases (displayName, location, location+name,
    location+nickname) — the join in build_blob looks up _fold(our name).
    The original displayName-keyed join matched ZERO games in every build
    through Aug 31 2026 (the ranked-matchup tier never fired once; deep
    slots went to FCS rating-artifact 'edges' instead of Clemson@LSU) and
    nothing logged it. Exact-fold equality only — prefix matching would
    let a bare 'Miami' claim Miami (OH)."""
    d = _espn_get("https://site.api.espn.com/apis/site/v2/sports/football/"
                  "college-football/rankings")
    if d is None:
        return None
    out: dict[str, int] = {}
    polls = []
    for poll in d.get("rankings") or []:
        pname = poll.get("shortName") or poll.get("name") or ""
        polls.append(pname)
        if "ap" not in pname.lower():
            continue
        for rk in poll.get("ranks") or []:
            t = rk.get("team") or {}
            try:
                cur = int(rk.get("current"))
            except (TypeError, ValueError):
                continue
            loc = t.get("location") or ""
            aliases = [t.get("displayName") or "", loc,
                       f"{loc} {t.get('name') or ''}",
                       f"{loc} {t.get('nickname') or ''}"]
            for a in aliases:
                fa = _fold(a)
                if fa:
                    out.setdefault(fa, cur)
        break
    log.info("ap_ranks: polls=%s keys=%d sample=%s",
             polls, len(out), sorted(out)[:4])
    return out or None


def espn_fpi(sport: str) -> dict[str, dict] | None:
    """ESPN FPI/team power index → {team: {fpi, rank}}. The endpoint shape
    is unverified from the sandbox (ESPN blocked there) — tolerant parse,
    two candidate URLs, None on anything unexpected."""
    grp, lg = _LEAGUES[sport]
    for url in (
        f"https://site.web.api.espn.com/apis/fitt/v3/sports/{grp}/{lg}/powerindex",
        f"https://site.web.api.espn.com/apis/v2/sports/{grp}/{lg}/powerindex",
    ):
        d = _espn_get(url, {"limit": "400", "region": "us", "lang": "en"})
        if not d:
            continue
        teams = d.get("teams") or d.get("powerIndexes") or []
        out: dict[str, dict] = {}
        for t in teams:
            team = t.get("team") or {}
            name = team.get("displayName") or ""
            fpi_val = rank = None
            for cat in (t.get("categories") or []):
                if (cat.get("name") or "").lower() in ("fpi", "powerindex"):
                    vals = cat.get("totals") or cat.get("values") or []
                    if vals:
                        try:
                            fpi_val = float(vals[0])
                        except (TypeError, ValueError):
                            pass
                    rank = cat.get("ranks", [None])[0] if cat.get("ranks") else None
            if fpi_val is None:
                stats = t.get("stats") or []
                for st in stats:
                    if (st.get("name") or "").lower() == "fpi":
                        fpi_val = st.get("value")
                        rank = st.get("rank")
            if name and fpi_val is not None:
                out[name] = {"fpi": round(float(fpi_val), 1), "rank": rank}
        if out:
            return out
    return None


# ------------------------------------------------------------------- model
def _fold(s: str) -> str:
    """Accent/punctuation-fold for team-name joins — ESPN spells 'San José
    State' and \"Hawai'i\" with marks the spine names may lack, and
    project()'s substring fallback can't cross a diacritic."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s.lower() if c.isalnum())


def _resolve_team(name: str, teams: dict) -> str:
    if name in teams:
        return name
    folded = {_fold(k): k for k in teams}
    return folded.get(_fold(name), name)


def load_model(sport: str) -> dict | None:
    rows = sb_select("power_ratings", {
        "select": "computed_at,league_avg,n_games,ratings,params",
        "sport": f"eq.{sport}", "order": "computed_at.desc", "limit": "1"})
    if not rows:
        return None
    row = rows[0]
    return {
        "R": {"teams": row["ratings"] or {}, "league_avg": row["league_avg"]},
        "params": row["params"] or {},
        "computed_at": row["computed_at"], "n_games": row["n_games"],
    }


def price_game(model: dict, home: str, away: str, neutral: bool) -> dict | None:
    """Gridiron IQ read for one matchup. Raw margin drives win prob (the
    validated path); cover/total probabilities go through the walk-forward
    shrinkage fits (gridiron_spread — cover_prob shrinks internally, so it
    gets the RAW numbers). Displayed margin/total are the CALIBRATED ones —
    the raw projection is measurably too extreme (never price off raw)."""
    params = model["params"]
    hfa = 0.0 if neutral else float(params.get("hfa") or 0.0)
    teams = model["R"].get("teams") or {}
    proj = pr.project(model["R"], _resolve_team(home, teams),
                      _resolve_team(away, teams), hfa=hfa)
    if not proj:
        return None
    if proj["home_gp"] < _MIN_TEAM_GP or proj["away_gp"] < _MIN_TEAM_GP:
        # A gp=1 team's "rating" is just that one game's score reshuffled
        # through the solver — not a number worth projecting a line off.
        # Treat exactly like a team missing from ratings entirely.
        return None
    sf, tf = params.get("spread_fit"), params.get("total_fit")
    m_raw, t_raw = proj["margin"], proj["total"]
    out = {
        "margin_raw": m_raw, "total_raw": t_raw,
        "exp_home": proj["exp_home"], "exp_away": proj["exp_away"],
        "home_net": round(proj.get("home_net") or 0, 2),
        "away_net": round(proj.get("away_net") or 0, 2),
        "neutral_site": neutral,
        "win_prob_home": round(pr.margin_to_prob(
            m_raw, float(params.get("scale") or 7.0)), 4),
        "n_games": model["n_games"], "computed_at": model["computed_at"],
    }
    if sf:
        out["margin_cal"] = round(sf["alpha"] + sf["beta"] * m_raw, 1)
        out["spread_fit_n"] = sf.get("n")
    if tf:
        out["total_cal"] = round(tf["alpha"] + tf["beta"] * t_raw, 1)
        out["total_fit_n"] = tf.get("n")
    return out


def cover_at(model: dict, m_raw: float, home_line: float) -> float | None:
    sf = model["params"].get("spread_fit")
    if not sf:
        return None
    p = gsp.cover_prob(sf, m_raw, home_line, dist="normal")
    return round(p, 4) if p is not None else None


def over_at(model: dict, t_raw: float, total_line: float) -> float | None:
    # P(total > L): cover_prob computes P(x > -line), so pass line = -L.
    tf = model["params"].get("total_fit")
    if not tf:
        return None
    p = gsp.cover_prob(tf, t_raw, -float(total_line), dist="normal")
    return round(p, 4) if p is not None else None


# ------------------------------------------------------------ market tape
def spine_markets(sport: str) -> list[dict]:
    now = datetime.now(timezone.utc)
    return sb_select("markets", {
        "select": "id,event_name,event_start,status",
        "sport": f"eq.{sport}", "status": "eq.active",
        "event_start": f"gte.{(now - timedelta(hours=6)).isoformat()}",
        "order": "event_start.asc"})


def match_market(g: dict, spine: list[dict]) -> str | None:
    name = f"{g['away']} @ {g['home']}"
    best = None
    for m in spine:
        try:
            ms = datetime.fromisoformat(m["event_start"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if abs((ms - g["event_start"]).total_seconds()) > 12 * 3600:
            continue
        if (m.get("event_name") or "").lower() == name.lower():
            return m["id"]
        # substring fallback: both team names present in the stored name
        en = (m.get("event_name") or "").lower()
        if g["away"].lower() in en and g["home"].lower() in en:
            best = m["id"]
    return best


def _pm_lines(mid: str) -> dict | None:
    """pm_snapshots open→now per (source, market_type). ATM line = latest
    line whose cents sit closest to 50 (the at-the-money convention the
    sharp score uses). cents are PROB POINTS for the row's own side."""
    rows = sb_select("pm_snapshots", {
        "select": "source,market_type,side,line,cents,bid_c,ask_c,captured_at",
        "market_id": f"eq.{mid}",
        "order": "captured_at.asc"})
    if not rows:
        return None
    out: dict = {}
    by_key = defaultdict(list)
    for r in rows:
        by_key[(r["source"], r["market_type"])].append(r)
    for (src, mt), rs in by_key.items():
        node: dict = {}
        if mt == "ml":
            for side in ("home", "away"):
                srs = [r for r in rs if r["side"] == side]
                if srs:
                    node[side] = {"open_c": srs[0]["cents"],
                                  "now_c": srs[-1]["cents"],
                                  "opened_at": srs[0]["captured_at"],
                                  "as_of": srs[-1]["captured_at"]}
        else:
            anchor = "home" if mt == "spread" else "over"
            srs = [r for r in rs if r["side"] == anchor and r.get("line") is not None]
            if srs:
                latest_at = srs[-1]["captured_at"]
                recent = [r for r in srs if r["captured_at"] == latest_at] or [srs[-1]]
                atm = min(recent, key=lambda r: abs((r["cents"] or 50) - 50))
                same_line = [r for r in srs if r["line"] == atm["line"]]
                node = {"side": anchor, "line": atm["line"],
                        "open_c": same_line[0]["cents"], "now_c": atm["cents"],
                        "first_line": srs[0]["line"],
                        "opened_at": srs[0]["captured_at"],
                        "as_of": atm["captured_at"]}
        if node:
            out.setdefault(src, {})[mt] = node
    return out or None


def _vsin_lines(mid: str) -> dict | None:
    """vsin_snapshots open + latest per (book, market_type, side): line,
    handle%, bets% — plus computed RLM + money-arrival deltas."""
    rows = sb_select("vsin_snapshots", {
        "select": "book,market_type,side,line,handle_pct,bets_pct,captured_at",
        "market_id": f"eq.{mid}", "order": "captured_at.asc"})
    if not rows:
        return None
    out: dict = {}
    grouped = defaultdict(list)
    for r in rows:
        grouped[(r["book"], r["market_type"], r["side"])].append(r)
    for (book, mt, side), rs in grouped.items():
        first, last = rs[0], rs[-1]
        out.setdefault(book, {}).setdefault(mt, {})[side] = {
            "line": last["line"], "open_line": first["line"],
            "handle": last["handle_pct"], "bets": last["bets_pct"],
            "open_handle": first["handle_pct"], "as_of": last["captured_at"]}
    # RLM: heavy-handle side (≥60%) whose own line got EASIER since open —
    # the book moved against the money → sharp on the other side.
    flags = []
    for book, mts in out.items():
        for mt, sides in mts.items():
            if mt == "total":
                continue
            for side, v in sides.items():
                h = v.get("handle")
                if h is None or h < 60:
                    continue
                ln, op = v.get("line"), v.get("open_line")
                if ln is None or op is None or ln == op:
                    continue
                # spread: more points = easier; ML: American odds numerically
                # larger (-150→-140, +120→+130) = better price. Same test.
                if ln > op:
                    flags.append({"book": book, "market": mt, "heavy_side": side,
                                  "handle": h, "open_line": op, "line": ln})
    if flags:
        out["rlm"] = flags
    return out or None


# ---------------------------------------------------------------- history
def load_results(sport: str) -> list[dict]:
    since = "2023-08-01"
    return sb_select("game_results", {
        "select": "game_date,home,away,home_score,away_score",
        "sport": f"eq.{sport}", "game_date": f"gte.{since}",
        "order": "game_date.asc"})


def team_history(results: list[dict], team: str) -> list[dict]:
    out = []
    for g in results:
        if g["home"] == team or g["away"] == team:
            us_home = g["home"] == team
            us = float(g["home_score"] if us_home else g["away_score"])
            them = float(g["away_score"] if us_home else g["home_score"])
            out.append({"date": g["game_date"],
                        "opp": g["away"] if us_home else g["home"],
                        "home": us_home, "us": int(us), "them": int(them),
                        "won": us > them})
    return out


def history_block(results: list[dict], away: str, home: str) -> dict:
    names = {g["home"] for g in results} | {g["away"] for g in results}
    nm = {_fold(n): n for n in names}
    away = nm.get(_fold(away), away)
    home = nm.get(_fold(home), home)
    ah, hh = team_history(results, away), team_history(results, home)
    season_cut = "2025-08-01"          # last completed season onward
    a_recent = [g for g in ah if g["date"] >= season_cut][-5:]
    h_recent = [g for g in hh if g["date"] >= season_cut][-5:]
    h2h = [g for g in hh if g["opp"] == away][-3:]
    a_opps = {g["opp"]: g for g in ah if g["date"] >= season_cut}
    common = []
    for g in hh:
        if g["date"] >= season_cut and g["opp"] in a_opps and g["opp"] != away:
            ag = a_opps[g["opp"]]
            common.append({"opp": g["opp"],
                           "home_res": f"{'W' if g['won'] else 'L'} {g['us']}-{g['them']}",
                           "away_res": f"{'W' if ag['won'] else 'L'} {ag['us']}-{ag['them']}"})
    return {"last5_away": a_recent, "last5_home": h_recent,
            "h2h": h2h, "common_opponents": common[:8],
            "note": "2025 season results (current season not yet played)"}


# ---------------------------------------------------------------- assembly
def _best_market_lines(pm: dict | None, vs: dict | None,
                       book: dict | None) -> dict:
    """The posted lines the model prices against, with provenance.
    Preference: DK (the book the crew bets) → Circa → the ESPN consensus
    line (available Monday, before VSiN's 80h window opens) → PMM →
    Kalshi."""
    out: dict = {}
    def _set(kind, line, src):
        if line is not None and kind not in out:
            out[kind] = {"line": float(line), "src": src}
    for bk in ("draftkings", "circa"):
        b = (vs or {}).get(bk) or {}
        sp = (b.get("spread") or {}).get("home") or {}
        _set("spread_home", sp.get("line"), bk)
        tt = (b.get("total") or {}).get("over") or {}
        _set("total", tt.get("line"), bk)
    if book:
        _set("spread_home", book.get("spread_home"),
             book.get("provider") or "consensus")
        _set("total", book.get("total"), book.get("provider") or "consensus")
    for src in ("pmm", "kalshi"):
        s = (pm or {}).get(src) or {}
        sp = s.get("spread") or {}
        if sp.get("side") == "home":
            _set("spread_home", sp.get("line"), src)
        tt = s.get("total") or {}
        if tt.get("side") == "over":
            _set("total", tt.get("line"), src)
    return out


# Verdict thresholds, in model-vs-market points. Below `lean` = pass.
# PLAY = anything over 1.5 pts on spread AND total (Rob, Aug 22 2026 —
# "football, tight lines, the goal is to gamble").
_PLAY_SPREAD_PTS, _LEAN_SPREAD_PTS = 1.5, 0.75
_PLAY_TOTAL_PTS, _LEAN_TOTAL_PTS = 1.5, 0.75


def _bet_spread(model: dict, priced: dict, away: str, home: str,
                mkt: dict) -> dict | None:
    """The spread play, in a handicapper's terms: the side our number
    likes, the line, and the price the bet is good TO ('−7.5 at −135 or
    better'), plus a ±1pt shopping ladder. Fair price = the model's cover
    probability at that exact line."""
    node = mkt.get("spread_home")
    if not node:
        return None
    line = node["line"]
    margin = priced.get("margin_cal", priced["margin_raw"])
    edge = round(margin + line, 1)          # >0 → model likes home vs line
    side = "home" if edge >= 0 else "away"
    team = home if side == "home" else away

    def _fair(home_line: float) -> int | None:
        p = cover_at(model, priced["margin_raw"], home_line)
        if p is None:
            return None
        return gsp.american(p if side == "home" else 1.0 - p)

    own_line = line if side == "home" else -line
    ladder = []
    for d in (0.0, 1.0, -1.0):
        hl = line + (d if side == "home" else -d)
        f = _fair(hl)
        if f is not None:
            ladder.append({"line": round(own_line + d, 1), "fair": f})
    if not ladder:
        return None
    verdict = ("play" if abs(edge) >= _PLAY_SPREAD_PTS
               else "lean" if abs(edge) >= _LEAN_SPREAD_PTS else "pass")
    return {"side": side, "team": team, "line": ladder[0]["line"],
            "fair": ladder[0]["fair"], "ladder": ladder,
            "market_home_line": line, "src": node["src"],
            "edge_pts": edge, "verdict": verdict}


def _bet_total(model: dict, priced: dict, mkt: dict) -> dict | None:
    node = mkt.get("total")
    if not node:
        return None
    line = node["line"]
    total = priced.get("total_cal", priced["total_raw"])
    edge = round(total - line, 1)           # >0 → model likes the over
    side = "over" if edge >= 0 else "under"

    def _fair(tl: float) -> int | None:
        p = over_at(model, priced["total_raw"], tl)
        if p is None:
            return None
        return gsp.american(p if side == "over" else 1.0 - p)

    ladder = []
    for d in (0.0, 1.0, -1.0):
        f = _fair(line + d)
        if f is not None:
            ladder.append({"line": round(line + d, 1), "fair": f})
    if not ladder:
        return None
    verdict = ("play" if abs(edge) >= _PLAY_TOTAL_PTS
               else "lean" if abs(edge) >= _LEAN_TOTAL_PTS else "pass")
    return {"side": side, "line": ladder[0]["line"],
            "fair": ladder[0]["fair"], "ladder": ladder, "src": node["src"],
            "edge_pts": edge, "verdict": verdict}


def build_game_blob(g: dict, sport: str, model: dict | None,
                    results: list[dict], injuries: dict | None,
                    ranks: dict | None, fpi: dict | None) -> dict:
    unavailable = []
    blob: dict = {
        "game": {"sport": sport, "away": g["away"], "home": g["home"],
                 "event_start": g["event_start"].isoformat(),
                 "venue": g.get("venue"), "neutral_site": g.get("neutral_site"),
                 "broadcast": g.get("broadcast"), "records": g.get("records"),
                 "locs": g.get("locs")},
    }
    pm = _pm_lines(g["market_id"]) if g.get("market_id") else None
    vs = _vsin_lines(g["market_id"]) if g.get("market_id") else None
    blob["lines"] = pm or {}
    blob["splits"] = vs or {}
    blob["book_odds"] = g.get("book_odds")
    if not pm:
        unavailable.append("exchange_tape")
    if not vs:
        unavailable.append("vsin")

    mkt = _best_market_lines(pm, vs, g.get("book_odds"))
    if model:
        priced = price_game(model, g["home"], g["away"],
                            bool(g.get("neutral_site")))
        if priced:
            bs = _bet_spread(model, priced, g["away"], g["home"], mkt)
            if bs:
                priced["bet_spread"] = bs
            bt = _bet_total(model, priced, mkt)
            if bt:
                priced["bet_total"] = bt
            blob["model"] = priced
        else:
            unavailable.append("model_unrated_team")
    else:
        unavailable.append("model")

    blob["history"] = history_block(results, g["away"], g["home"])
    inj = {}
    for k in ("away", "home"):
        team = g[k]
        rows = (injuries or {}).get(team)
        if rows:
            inj[k] = rows
    blob["injuries"] = inj
    if injuries is None:
        unavailable.append("injuries")
    if ranks is not None:
        r = {k: ranks.get(_fold(g[k]))
             for k in ("away", "home") if ranks.get(_fold(g[k]))}
        if r:
            blob["ap_ranks"] = r
    elif sport == "NCAAF":
        unavailable.append("ap_ranks")
    if fpi is not None:
        f = {k: fpi.get(g[k]) for k in ("away", "home") if fpi.get(g[k])}
        if f:
            blob["fpi"] = f
    else:
        unavailable.append("fpi")
    blob["unavailable"] = unavailable
    return blob


def decide_tiers(sport: str, games: list[dict]) -> None:
    """Sets g['tier'] + g['tier_reasons'] in place. games carry 'blob'."""
    if sport == "NFL":
        for g in games:
            g["tier"], g["tier_reasons"] = "deep", ["nfl"]
        return
    scored = []
    for g in games:
        blob = g["blob"]
        model = blob.get("model") or {}
        ranks = blob.get("ap_ranks") or {}
        reasons, prio = [], 0.0
        both_ranked = len(ranks) == 2
        either_ranked = len(ranks) >= 1
        sp_edge = abs((model.get("bet_spread") or {}).get("edge_pts") or 0.0)
        tot_edge = abs((model.get("bet_total") or {}).get("edge_pts") or 0.0)
        if both_ranked:
            reasons.append("ranked matchup")
            prio += 100
        if either_ranked and sp_edge >= _EDGE_SPREAD_RANKED_PTS:
            reasons.append(f"ranked + {sp_edge:.1f}pt model gap")
        elif sp_edge >= _EDGE_SPREAD_PTS:
            reasons.append(f"{sp_edge:.1f}pt spread gap model-vs-market")
        if tot_edge >= _EDGE_TOTAL_PTS:
            reasons.append(f"{tot_edge:.1f}pt total gap model-vs-market")
        prio += sp_edge + 0.75 * tot_edge + (10 if either_ranked else 0)
        scored.append((prio, reasons, g))
    scored.sort(key=lambda t: -t[0])
    kept = 0
    for prio, reasons, g in scored:
        deep = bool(reasons) and (kept < _NCAAF_DEEP_CAP
                                  or "ranked matchup" in reasons)
        if deep:
            kept += 1
            g["tier"], g["tier_reasons"] = "deep", reasons
        else:
            g["tier"], g["tier_reasons"] = "data", reasons


# ------------------------------------------------------------- friday diff
def friday_diff(old_blob: dict, new_blob: dict) -> list[str]:
    """Human-readable changes since Monday: line moves + injury changes.
    Compares the Monday blob's lines/splits/injuries to fresh ones."""
    changes: list[str] = []

    def _line_of(blob, book, mt, side):
        if book in ("pmm", "kalshi"):
            node = (blob.get("lines") or {}).get(book, {}).get(mt) or {}
            return node.get("line")
        node = ((blob.get("splits") or {}).get(book, {}).get(mt) or {}).get(side) or {}
        return node.get("line")

    for book, label in (("draftkings", "DK"), ("circa", "Circa"),
                        ("pmm", "Polymarket"), ("kalshi", "Kalshi")):
        for mt, side, thresh, unit in (("spread", "home", _DIFF_SPREAD_PTS, "pt"),
                                       ("total", "over", _DIFF_TOTAL_PTS, "pt")):
            o, n = _line_of(old_blob, book, mt, side), _line_of(new_blob, book, mt, side)
            try:
                if o is not None and n is not None and abs(float(n) - float(o)) >= thresh:
                    changes.append(f"{label} {mt}: {float(o):g} → {float(n):g}")
            except (TypeError, ValueError):
                continue
        # ML cents move on the exchanges
        if book in ("pmm", "kalshi"):
            for side in ("home", "away"):
                oml = ((old_blob.get("lines") or {}).get(book, {}).get("ml") or {}).get(side) or {}
                nml = ((new_blob.get("lines") or {}).get(book, {}).get("ml") or {}).get(side) or {}
                o, n = oml.get("now_c"), nml.get("now_c")
                if o is not None and n is not None and abs(n - o) >= _DIFF_ML_CENTS:
                    changes.append(f"{label} ML {side}: {o}¢ → {n}¢")

    ob, nb = old_blob.get("book_odds") or {}, new_blob.get("book_odds") or {}
    for key, label, thresh in (("spread_home", "spread", _DIFF_SPREAD_PTS),
                               ("total", "total", _DIFF_TOTAL_PTS)):
        o, n = ob.get(key), nb.get(key)
        try:
            if o is not None and n is not None and abs(float(n) - float(o)) >= thresh:
                changes.append(f"{nb.get('provider') or 'Book'} {label}: "
                               f"{float(o):g} → {float(n):g}")
        except (TypeError, ValueError):
            pass

    def _inj_set(blob):
        out = {}
        for side, rows in (blob.get("injuries") or {}).items():
            for r in rows:
                out[(side, r.get("name"))] = r.get("status") or ""
        return out

    oi, ni = _inj_set(old_blob), _inj_set(new_blob)
    for key, status in ni.items():
        if key not in oi:
            changes.append(f"NEW injury: {key[1]} ({key[0]}) — {status}")
        elif oi[key] != status:
            changes.append(f"Injury status: {key[1]} ({key[0]}) {oi[key]} → {status}")
    for key in oi:
        if key not in ni:
            changes.append(f"Off injury report: {key[1]} ({key[0]})")
    return changes


# ------------------------------------------------------------------ driver
def week_key_default() -> str:
    az_today = datetime.now(AZ).date()
    return (az_today - timedelta(days=az_today.weekday())).isoformat()


def run(mode: str, sports: list[str], days: int, week_key: str,
        commit: bool) -> dict:
    summary: dict = {"mode": mode, "week_key": week_key}
    for sport in sports:
        model = load_model(sport)
        if not model:
            log.error("%s: no power_ratings snapshot — model sections dark",
                      sport)
        results = load_results(sport)
        injuries = espn_injuries(sport)
        ranks = espn_ap_ranks() if sport == "NCAAF" else None
        fpi = espn_fpi(sport)
        spine = spine_markets(sport)

        floor = datetime.fromisoformat(
            _SEASON_FLOOR[sport] + "T00:00:00+00:00")
        espn_ok = True
        games = espn_week_games(sport, days)
        if games is None:
            espn_ok = False
            # ESPN dark (e.g. sandbox). Fall back to the spine so line/model
            # sections still build; ESPN sections render unavailable.
            log.warning("%s: ESPN unreachable — falling back to markets spine",
                        sport)
            games = []
            now = datetime.now(timezone.utc)
            seen = set()
            for m in spine:
                try:
                    start = datetime.fromisoformat(
                        m["event_start"].replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    continue
                if not (now < start <= now + timedelta(days=days)):
                    continue
                name = m.get("event_name") or ""
                if " @ " not in name or name in seen:
                    continue
                seen.add(name)
                away, home = [s.strip() for s in name.split(" @ ", 1)]
                games.append({"espn_id": None, "away": away, "home": home,
                              "event_start": start, "neutral_site": False,
                              "venue": None, "broadcast": None, "records": {}})
        else:
            now = datetime.now(timezone.utc)
            games = [g for g in games
                     if now < g["event_start"] <= now + timedelta(days=days)]
        games = [g for g in games if g["event_start"] >= floor]

        for g in games:
            g["market_id"] = match_market(g, spine)

        if mode == "friday":
            existing = sb_select("football_sheets", {
                "select": "id,event_name,espn_id,data_blob",
                "week_key": f"eq.{week_key}", "sport": f"eq.{sport}"})
            by_name = {r["event_name"]: r for r in existing}
            n_diffed = 0
            for g in games:
                name = f"{g['away']} @ {g['home']}"
                row = by_name.get(name)
                if not row or not row.get("data_blob"):
                    continue
                fresh = build_game_blob(g, sport, model, results, injuries,
                                        ranks, fpi)
                changes = friday_diff(row["data_blob"], fresh)
                new_blob = dict(row["data_blob"])
                new_blob["friday"] = {
                    "built_at": datetime.now(timezone.utc).isoformat(),
                    "lines": fresh.get("lines"), "splits": fresh.get("splits"),
                    "book_odds": fresh.get("book_odds"),
                    "injuries": fresh.get("injuries"),
                    "model": fresh.get("model"),
                    "changes": changes}
                if commit:
                    sb_patch("football_sheets", {"id": f"eq.{row['id']}"},
                             {"data_blob": new_blob})
                n_diffed += 1
            summary[sport] = {"mode": "friday", "games": len(games),
                              "diffed": n_diffed}
            log.info("%s friday: %d games, %d diffed", sport, len(games),
                     n_diffed)
            continue

        rows = []
        with_blob = []
        for g in games:
            blob = build_game_blob(g, sport, model, results, injuries, ranks,
                                   fpi)
            g["blob"] = blob
            with_blob.append(g)
        decide_tiers(sport, with_blob)
        now_iso = datetime.now(timezone.utc).isoformat()
        for g in with_blob:
            g["blob"]["tier"] = g["tier"]
            g["blob"]["tier_reasons"] = g["tier_reasons"]
            rows.append({
                "week_key": week_key, "sport": sport,
                "event_name": f"{g['away']} @ {g['home']}",
                "event_start": g["event_start"].isoformat(),
                "market_id": g.get("market_id"), "espn_id": g.get("espn_id"),
                "tier": g["tier"], "data_blob": g["blob"],
                "data_built_at": now_iso,
            })
        if commit and rows:
            sb_upsert("football_sheets", rows, "week_key,sport,event_name")
            sb_upsert("football_sheet_weeks", [{
                "week_key": week_key, "sport": sport, "games": len(rows),
                "deep_games": sum(1 for r in rows if r["tier"] == "deep"),
            }], "week_key,sport")
        if commit and espn_ok:
            # Sweep stale rows this ESPN-backed build didn't touch — spelling
            # drift mints dupes ("San José State" vs "San Jose State": the
            # unique key treats them as different games), and season-floor
            # changes can orphan whole slates (the 27 preseason NFL rows).
            # Gated on espn_ok so a degraded spine-fallback run can never
            # delete a fuller ESPN-named build.
            sb_delete("football_sheets", {
                "week_key": f"eq.{week_key}", "sport": f"eq.{sport}",
                "data_built_at": f"lt.{now_iso}"})
        summary[sport] = {
            "games": len(rows),
            "deep": sum(1 for r in rows if r["tier"] == "deep"),
            "with_model": sum(1 for r in rows
                              if "model" in r["data_blob"]),
            "with_tape": sum(1 for r in rows
                             if r["data_blob"].get("lines")),
            "with_vsin": sum(1 for r in rows
                             if r["data_blob"].get("splits")),
            "with_ranks": sum(1 for r in rows
                              if r["data_blob"].get("ap_ranks")),
            "espn_dark": all(r["espn_id"] is None for r in rows) if rows else None,
        }
        log.info("%s: %s", sport, summary[sport])
    return summary


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["monday", "friday"], default="monday")
    ap.add_argument("--sport", choices=["NFL", "NCAAF"], default=None)
    ap.add_argument("--days", type=int, default=8)
    ap.add_argument("--week-key", default=None)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    sports = [args.sport] if args.sport else ["NFL", "NCAAF"]
    wk = args.week_key or week_key_default()
    summary = run(args.mode, sports, args.days, wk, args.commit)
    print(json.dumps(summary, indent=2, default=str))
    # A run that produced zero games across every sport is a dark spine —
    # exit red so the workflow shows it (the six-day-green lesson).
    total = sum((summary.get(s) or {}).get("games", 0) for s in sports)
    if args.mode == "monday" and total == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
