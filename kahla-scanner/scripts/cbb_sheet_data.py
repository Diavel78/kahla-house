"""COLLEGE BASKETBALL PICK SHEET — assembly (Nov 15 2026, Rob: "College basketball time").

One row per upcoming Division I men's game into `football_sheets` (sport='NCAAB'), today
+ N days (AZ). No model: THE LINE IS PINNACLE'S (its own free feed, league 493) and the
🔥 zones pick the side at read time — /api/pick-sheets seeds the ml/spread/total picks
off Pinnacle's slate (`_sheet_seed_pin_picks`) the way it does for every sport. A game
Pinnacle does not price keeps the ESPN book line (tagged by provider on the page).

  python -m scripts.cbb_sheet_data [--days 1] [--commit]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

_SCANNER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SCANNER)
from scripts.football_sheet_data import _espn_get, sb_upsert, week_key_default  # noqa: E402
from scripts.nba_sheet_data import _spread_names  # noqa: E402
from scripts.nhl_sheet_data import _ensure_sport_check, _parse_odds, pin_overlay  # noqa: E402

log = logging.getLogger("cbb_sheet_data")
AZ = ZoneInfo("America/Phoenix")
SPORT = "NCAAB"
_SB = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard"


def espn_slate(days: int):
    """Every Division I game still to start, today + `days` (AZ). groups=50 is all of
    D-I; without it ESPN returns only the featured slate."""
    today = datetime.now(AZ).date()
    events, ok = [], False
    for i in range(days + 1):
        d = _espn_get(_SB, {"dates": f"{today + timedelta(days=i):%Y%m%d}", "groups": "50", "limit": "500"})
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
        out.append({"espn_id": str(ev.get("id")), "start": start,
                    "home": sides["home"], "away": sides["away"],
                    "neutral": bool(comp.get("neutralSite")),
                    "odds": _spread_names(_parse_odds(comp)),
                    "broadcast": ", ".join(b for bl in (comp.get("broadcasts") or [])
                                           for b in (bl.get("names") or []))})
    return sorted(out, key=lambda g: g["start"])


def run(days: int, commit: bool) -> dict:
    slate = espn_slate(days)
    if slate is None:
        return {"games": 0, "error": "espn_dark"}
    pin_st = pin_overlay(slate, spend=False, sport=SPORT)   # Pinnacle's free feed, never a paid pull
    for g in slate:
        g["odds"] = _spread_names(g["odds"])
    wk = week_key_default()
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = []
    for g in slate:
        blob = {"game": {"sport": SPORT, "away": g["away"]["name"], "home": g["home"]["name"],
                         "away_abbr": g["away"]["abbr"], "home_abbr": g["home"]["abbr"],
                         "event_start": g["start"].isoformat(), "neutral": g["neutral"],
                         "records": {"away": g["away"]["record"], "home": g["home"]["record"]},
                         "broadcast": g.get("broadcast")},
                "lines": g["odds"], "lines_espn": g.get("odds_espn"),
                "model_meta": {"engine": "none", "computed_at": now_iso}}
        rows.append({"week_key": wk, "sport": SPORT,
                     "event_name": f"{g['away']['name']} @ {g['home']['name']}",
                     "event_start": g["start"].isoformat(), "espn_id": g["espn_id"],
                     "tier": "data", "data_blob": blob, "data_built_at": now_iso})
    summary = {"games": len(rows), "pinnacle": pin_st, "week_key": wk,
               "pin_priced": sum(1 for g in slate if (g["odds"] or {}).get("provider") == "pinnacle")}
    log.info("%s", json.dumps(summary, default=str))
    if commit and rows:
        try:
            sb_upsert("football_sheets", rows, "week_key,sport,event_name")
        except httpx.HTTPStatusError as e:
            if "check" in e.response.text.lower() and _ensure_sport_check():
                sb_upsert("football_sheets", rows, "week_key,sport,event_name")
            else:
                raise
        sb_upsert("football_sheet_weeks", [{"week_key": wk, "sport": SPORT,
                                            "games": len(rows), "deep_games": 0}],
                  "week_key,sport")
    return summary


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1, help="today + N days (AZ)")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    s = run(a.days, a.commit)
    print(json.dumps(s, indent=2, default=str))
    return 0 if s.get("error") is None else 1


if __name__ == "__main__":
    sys.exit(main())
