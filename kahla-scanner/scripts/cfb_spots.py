"""COLLEGE FOOTBALL SITUATIONS (Nov 15 2026, Rob: "We are betting college football, end of
story. Did you analyze by home team vs away team? Game start? Back to back road games?
Coming off bye week?").

Every FBS game, seasons back (ESPN scoreboard, groups=80), with DraftKings' OPEN and CLOSE
spread + total off ESPN's core odds. Each game is two team-sides; each side is tagged with
its situation and graded against the spread at the CLOSE (what you could still bet) and at
the OPEN. Totals graded over/under at the close. Pushes out. ROI at the actual closing price
when ESPN has it, else −110. Season-by-season splits so one lucky year can't pass for a spot.

  python -m scripts.cfb_spots --seasons 2023,2024,2025,2026 --cache .cfb_cache
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("cfb_spots")
SB = ("https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
      "?groups=80&limit=500&dates={y}&seasontype=2&week={w}")
CORE = ("https://sports.core.api.espn.com/v2/sports/football/leagues/college-football/events/{e}"
        "/competitions/{e}/odds")
ET = ZoneInfo("America/New_York")
S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0"


def get(url, tries=4):
    for i in range(tries):
        try:
            r = S.get(url, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
        except Exception:
            pass
        time.sleep(1.5 * (i + 1))
    return None


def fetch_games(y, cache):
    p = os.path.join(cache, f"games_{y}.json")
    if os.path.exists(p):
        return json.load(open(p))
    out, seen = [], set()
    for w in range(1, 17):
        d = get(SB.format(y=y, w=w)) or {}
        for ev in d.get("events") or []:
            if ev.get("id") in seen:
                continue
            seen.add(ev.get("id"))
            c = (ev.get("competitions") or [{}])[0]
            if not ((c.get("status") or {}).get("type") or {}).get("completed"):
                continue
            t = {x.get("homeAway"): x for x in c.get("competitors") or []}
            if "home" not in t or "away" not in t:
                continue
            try:
                hs, as_ = float(t["home"].get("score")), float(t["away"].get("score"))
            except (TypeError, ValueError):
                continue
            out.append({"id": ev["id"], "season": y, "week": w, "date": ev.get("date"),
                        "neutral": bool(c.get("neutralSite")), "conf": bool(c.get("conferenceCompetition")),
                        "home": t["home"]["team"].get("id"), "away": t["away"]["team"].get("id"),
                        "home_name": t["home"]["team"].get("displayName"),
                        "away_name": t["away"]["team"].get("displayName"),
                        "hs": hs, "as": as_})
        log.info("season %s week %s: %d games so far", y, w, len(out))
    if y < datetime.now(timezone.utc).year:
        json.dump(out, open(p, "w"))
    return out


def _num(x):
    try:
        s = str(x).strip().lower().replace("+", "")
        if s in ("even", "ev", "pk", "pick"):
            return 100.0
        s = s.lstrip("ou")
        return float(s) if s not in ("", "none") else None
    except (TypeError, ValueError):
        return None


def _am(d):
    if isinstance(d, dict):
        for k in ("american", "alternateDisplayValue", "displayValue", "value", "odds"):
            v = _num(d.get(k))
            if v is not None:
                return v
        return None
    return _num(d)


def parse_item(it):
    h, a = it.get("homeTeamOdds") or {}, it.get("awayTeamOdds") or {}
    out = {}
    for k in ("open", "close"):
        ho, ao, go = h.get(k) or {}, a.get(k) or {}, it.get(k) or {}
        tot = _am(go.get("total"))
        out[k] = {"sp_home": _am(ho.get("pointSpread")), "sp_home_odds": _am(ho.get("spread")),
                  "sp_away_odds": _am(ao.get("spread")), "total": abs(tot) if tot is not None else None,
                  "over": _am(go.get("over")), "under": _am(go.get("under"))}
    if out["close"].get("sp_home") is None and it.get("spread") is not None:
        # some items carry only the final line at the top level
        out["close"]["sp_home"] = _num(it.get("spread"))
        out["close"]["total"] = _num(it.get("overUnder"))
    return out


def fetch_odds(eid):
    b = get(CORE.format(e=eid)) or {}
    items = b.get("items") or []

    def has(i):
        return ((i.get("homeTeamOdds") or {}).get("close") or {}).get("pointSpread") is not None
    dk = [i for i in items if "draft" in str((i.get("provider") or {}).get("name") or "").lower()]
    pick = (next((i for i in dk if has(i)), None) or next((i for i in items if has(i)), None)
            or (items[0] if items else None))
    if not pick:
        return None
    r = parse_item(pick)
    r["provider"] = (pick.get("provider") or {}).get("name")
    return r


def fetch_all_odds(games, cache):
    p = os.path.join(cache, "odds.json")
    odds = json.load(open(p)) if os.path.exists(p) else {}
    need = [g["id"] for g in games if g["id"] not in odds]
    log.info("odds: %d cached, %d to fetch", len(odds), len(need))
    for i in range(0, len(need), 200):
        chunk = need[i:i + 200]
        with ThreadPoolExecutor(max_workers=8) as ex:
            for eid, r in zip(chunk, ex.map(fetch_odds, chunk)):
                odds[eid] = r
        json.dump(odds, open(p, "w"))
        log.info("odds %d/%d", min(i + 200, len(need)), len(need))
    return odds


def pay(px):
    px = px if px is not None else -110.0
    return px / 100.0 if px > 0 else 100.0 / -px


def sides(games, odds):
    """Every game → two team-sides with situation tags + ATS results."""
    games = sorted(games, key=lambda g: g["date"])
    last = {}           # team id -> (date, was_road)
    rows = []
    for g in games:
        d = datetime.fromisoformat(g["date"].replace("Z", "+00:00"))
        od = odds.get(g["id"]) or {}
        o, c = od.get("open") or {}, od.get("close") or {}
        kt = d.astimezone(ET)
        hr = kt.hour + kt.minute / 60
        slot = "noon" if hr < 14 else "afternoon" if hr < 18.5 else "night"
        day = kt.strftime("%a")
        for side in ("home", "away"):
            tid, oid = g[side], g["away" if side == "home" else "home"]
            pl, ol = last.get(tid), last.get(oid)
            rest = (d - pl[0]).days if pl else None
            orest = (d - ol[0]).days if ol else None
            r = {"season": g["season"], "week": g["week"], "side": "neutral" if g["neutral"] else side,
                 "slot": slot, "day": day, "conf": g["conf"],
                 "rest": rest, "orest": orest, "prev_road": pl[1] if pl else None}
            if c.get("sp_home") is not None:
                L = c["sp_home"] if side == "home" else -c["sp_home"]
                m = (g["hs"] - g["as"]) if side == "home" else (g["as"] - g["hs"])
                r["line"] = L
                r["fav"] = L < 0
                x = m + L
                r["ats"] = None if abs(x) < 1e-9 else (1 if x > 0 else -1)
                r["px"] = c.get("sp_home_odds") if side == "home" else c.get("sp_away_odds")
                if o.get("sp_home") is not None:
                    Lo = o["sp_home"] if side == "home" else -o["sp_home"]
                    r["move"] = L - Lo     # negative = the market moved TOWARD this team (+7 → +4, −3 → −6)
                    xo = m + Lo
                    r["ats_open"] = None if abs(xo) < 1e-9 else (1 if xo > 0 else -1)
            if side == "home" and c.get("total") is not None:
                t = g["hs"] + g["as"] - c["total"]
                r["ou"] = None if abs(t) < 1e-9 else (1 if t > 0 else -1)
                r["total"] = c["total"]
                if o.get("total") is not None:
                    r["tmove"] = c["total"] - o["total"]
            rows.append(r)
        last[g["home"]] = (d, False if not g["neutral"] else None)
        last[g["away"]] = (d, True if not g["neutral"] else None)
    return rows


def rec(sel, key="ats", pkey="px"):
    w = sum(1 for r in sel if r.get(key) == 1)
    l = sum(1 for r in sel if r.get(key) == -1)
    usd = sum(pay(r.get(pkey)) for r in sel if r.get(key) == 1) - l
    n = w + l
    if not n:
        return None
    return {"w": w, "l": l, "pct": 100 * w / n, "roi": 100 * usd / n, "z": (w - l) / math.sqrt(n)}


def fmt(x):
    return "—" if not x else f"{x['w']}-{x['l']} {x['pct']:.1f}% roi {x['roi']:+.1f}% z{x['z']:+.1f}"


def line(name, sel, seasons, key="ats", pkey="px"):
    a = rec(sel, key, pkey)
    by = " | ".join(f"{s}: " + (lambda q: f"{q['w']}-{q['l']}" if q else "—")(
        rec([r for r in sel if r["season"] == s], key, pkey)) for s in seasons)
    flag = " ◀" if a and abs(a["z"]) >= 2 else ""
    print(f"  {name:46s} {fmt(a):38s} [{by}]{flag}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2023,2024,2025,2026")
    ap.add_argument("--cache", default=".cfb_cache")
    a = ap.parse_args()
    os.makedirs(a.cache, exist_ok=True)
    seasons = [int(s) for s in a.seasons.split(",")]
    games = []
    for y in seasons:
        games += fetch_games(y, a.cache)
    odds = fetch_all_odds(games, a.cache)
    rows = sides(games, odds)
    sp = [r for r in rows if r.get("ats") is not None]
    print(f"\n==== COLLEGE FOOTBALL SITUATIONS — {len(games)} games, {len(sp)} graded team-sides "
          f"(ATS at DK's close; ◀ = |z| ≥ 2) ====")
    hm = lambda r: r["side"] == "home"
    aw = lambda r: r["side"] == "away"
    bye = lambda r: r["rest"] is not None and r["rest"] >= 13
    norm = lambda x: x is not None and 6 <= x <= 8
    print("\n-- home / away --")
    line("home teams", [r for r in sp if hm(r)], seasons)
    line("road teams", [r for r in sp if aw(r)], seasons)
    line("home favorites", [r for r in sp if hm(r) and r["fav"]], seasons)
    line("home dogs", [r for r in sp if hm(r) and not r["fav"]], seasons)
    line("road favorites", [r for r in sp if aw(r) and r["fav"]], seasons)
    line("road dogs", [r for r in sp if aw(r) and not r["fav"]], seasons)
    line("home dogs +7 or more", [r for r in sp if hm(r) and r["line"] >= 7], seasons)
    line("road dogs +14 or more", [r for r in sp if aw(r) and r["line"] >= 14], seasons)
    line("home dogs, conference game", [r for r in sp if hm(r) and not r["fav"] and r["conf"]], seasons)
    print("\n-- bye / rest --")
    line("off a bye (13+ days)", [r for r in sp if bye(r)], seasons)
    ob = [r for r in sp if bye(r) and norm(r["orest"])]
    line("off a bye vs a team that played last week", ob, seasons)
    line("  ...as the favorite", [r for r in ob if r["fav"]], seasons)
    line("  ...as the dog", [r for r in ob if not r["fav"]], seasons)
    line("  ...at home", [r for r in ob if hm(r)], seasons)
    line("  ...on the road", [r for r in ob if aw(r)], seasons)
    line("vs a team off a bye (we played last week)",
         [r for r in sp if norm(r["rest"]) and r["orest"] is not None and r["orest"] >= 13], seasons)
    line("short rest (≤5 days) vs normal rest",
         [r for r in sp if r["rest"] is not None and r["rest"] <= 5 and norm(r["orest"])], seasons)
    line("more rest than the opponent by 2+ days (no bye)",
         [r for r in sp if r["rest"] is not None and r["orest"] is not None
          and r["rest"] - r["orest"] >= 2 and r["rest"] < 13], seasons)
    print("\n-- road trips --")
    line("2nd straight road game", [r for r in sp if aw(r) and r["prev_road"] is True], seasons)
    line("  ...as a dog", [r for r in sp if aw(r) and r["prev_road"] is True and not r["fav"]], seasons)
    line("  ...as a favorite", [r for r in sp if aw(r) and r["prev_road"] is True and r["fav"]], seasons)
    line("home after a road game", [r for r in sp if hm(r) and r["prev_road"] is True], seasons)
    line("home after a home game", [r for r in sp if hm(r) and r["prev_road"] is False], seasons)
    print("\n-- game start (ET) --")
    for sl in ("noon", "afternoon", "night"):
        line(f"home teams, {sl} start", [r for r in sp if hm(r) and r["slot"] == sl], seasons)
        line(f"home dogs, {sl} start", [r for r in sp if hm(r) and not r["fav"] and r["slot"] == sl], seasons)
        line(f"road favorites, {sl} start", [r for r in sp if aw(r) and r["fav"] and r["slot"] == sl], seasons)
    for dy in ("Tue", "Wed", "Thu", "Fri"):
        line(f"home teams, {dy} game", [r for r in sp if hm(r) and r["day"] == dy], seasons)
    print("\n-- the line move (open → close), graded at the CLOSE --")
    mv = [r for r in sp if r.get("move") is not None]
    line("team the line moved toward 1+ pt", [r for r in mv if r["move"] <= -1], seasons)
    line("team the line moved toward 2+ pts", [r for r in mv if r["move"] <= -2], seasons)
    line("team the line moved toward 3+ pts", [r for r in mv if r["move"] <= -3], seasons)
    line("  ...home dog the line moved toward 1+", [r for r in mv if r["move"] <= -1 and hm(r) and not r["fav"]], seasons)
    line("  ...road dog the line moved toward 1+", [r for r in mv if r["move"] <= -1 and aw(r) and not r["fav"]], seasons)
    line("  ...favorite the line moved toward 1+", [r for r in mv if r["move"] <= -1 and r["fav"]], seasons)
    line("team the line moved toward 1+, at the OPEN line", [r for r in mv if r["move"] <= -1], seasons, key="ats_open")
    line("no move (open = close)", [r for r in mv if abs(r["move"]) < 0.25], seasons)
    print("\n-- spread size (by the team's own line) --")
    for lo, hi, nm in ((-99, -21, "fav -21 or more"), (-21, -10, "fav -10.5 to -20.5"), (-10, -3, "fav -3.5 to -10"),
                       (-3, 0, "fav -0.5 to -3"), (0, 3.01, "dog +0.5 to +3"), (3.01, 10.01, "dog +3.5 to +10"),
                       (10.01, 21.01, "dog +10.5 to +21"), (21.01, 99, "dog +21.5 or more")):
        line(nm, [r for r in sp if lo < r["line"] <= hi], seasons)
    print("\n-- totals (over %, at the close) --")
    tt = [r for r in rows if r.get("ou") is not None]
    line("all overs", tt, seasons, key="ou", pkey="_")
    for sl in ("noon", "afternoon", "night"):
        line(f"overs, {sl} start", [r for r in tt if r["slot"] == sl], seasons, key="ou", pkey="_")
    for dy in ("Tue", "Wed", "Thu", "Fri", "Sat"):
        line(f"overs, {dy}", [r for r in tt if r["day"] == dy], seasons, key="ou", pkey="_")
    for lo, hi in ((0, 45), (45, 52), (52, 58), (58, 65), (65, 99)):
        line(f"overs, total {lo}-{hi}", [r for r in tt if lo <= r["total"] < hi], seasons, key="ou", pkey="_")
    line("overs, total moved up 1.5+", [r for r in tt if (r.get("tmove") or 0) >= 1.5], seasons, key="ou", pkey="_")
    line("overs, total moved down 1.5+", [r for r in tt if (r.get("tmove") or 0) <= -1.5], seasons, key="ou", pkey="_")
    line("overs, home team off a bye", [r for r in tt if bye(r)], seasons, key="ou", pkey="_")
    line("overs, conference game", [r for r in tt if r["conf"]], seasons, key="ou", pkey="_")
    line("overs, non-conference", [r for r in tt if not r["conf"]], seasons, key="ou", pkey="_")
    provs: dict = {}
    for v in odds.values():
        if v:
            provs[v.get("provider")] = provs.get(v.get("provider"), 0) + 1
    print("\nProviders:", provs, "| games with no odds:", sum(1 for g in games if not odds.get(g["id"])))


if __name__ == "__main__":
    main()
