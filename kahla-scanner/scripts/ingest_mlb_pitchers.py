"""Ingest per-game MLB pitching logs from the official MLB Stats API.

Diamond IQ Phase 1: who ACTUALLY started every game (not the probable)
and every pitcher's line — the walk-forward raw material for the
pitcher-quality layer, leakage-free (the nhl_goalie_games pattern).
Source is the free, no-auth MLB Stats API (statsapi.mlb.com): schedule
date ranges give final gamePks, per-game boxscores give the ordered
`pitchers` list (first = the actual starter) + per-pitcher lines.
Spring training / exhibition / all-star (gameType S/E/A) EXCLUDED —
the power-ratings preseason lesson.

Probe-first (the UFCStats lesson): field names are verified from the
Action log before any backfill trusts the parser, empty phases fail
LOUD, and every full-table query pages explicitly.

  python -m scripts.ingest_mlb_pitchers --probe
  python -m scripts.ingest_mlb_pitchers --backfill --start 2025-03-01 --commit
  python -m scripts.ingest_mlb_pitchers --delta --commit     # last ~4 days
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from storage import supabase_client as db

log = logging.getLogger(__name__)

BASE = "https://statsapi.mlb.com/api/v1"
_SLEEP = 0.2
_TRIES = 3
_GAME_TYPES = {"R", "F", "D", "L", "W"}   # regular + postseason; S/E/A excluded
_SCHED_CHUNK_DAYS = 20


def _get(client: httpx.Client, url: str) -> dict | None:
    for attempt in range(_TRIES):
        try:
            r = client.get(url, timeout=25)
            if r.status_code == 200:
                time.sleep(_SLEEP)
                return r.json()
            if r.status_code == 404:
                return None
            log.warning("HTTP %s %s (try %d)", r.status_code, url, attempt + 1)
        except Exception as e:
            log.warning("GET %s failed (try %d): %s", url, attempt + 1, e)
        time.sleep(1.0 * (attempt + 1))
    return None


def schedule_games(client, start: str, end: str) -> list[dict]:
    """FINAL games in [start, end] (inclusive), regular + postseason only."""
    data = _get(client, f"{BASE}/schedule?sportId=1&startDate={start}&endDate={end}")
    out = []
    for d in ((data or {}).get("dates") or []):
        for g in (d.get("games") or []):
            if g.get("gameType") not in _GAME_TYPES:
                continue
            if ((g.get("status") or {}).get("codedGameState")) != "F":
                continue      # only completed games have real pitching lines
            out.append({"pk": g.get("gamePk"), "date": d.get("date"),
                        "type": g.get("gameType")})
    return out


def _ip_to_outs(ip) -> int | None:
    """'5.2' innings = 17 outs (the .1/.2 suffix is thirds, not tenths)."""
    if ip is None:
        return None
    try:
        s = str(ip)
        whole, _, frac = s.partition(".")
        return int(whole) * 3 + (int(frac) if frac else 0)
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_boxscore(data: dict, meta: dict) -> list[dict]:
    """Pitcher rows from one boxscore. First id in `pitchers` = starter."""
    rows = []
    teams = (data or {}).get("teams") or {}
    away_ab = (((teams.get("away") or {}).get("team")) or {}).get("abbreviation")
    home_ab = (((teams.get("home") or {}).get("team")) or {}).get("abbreviation")
    for side, team, opp in (("away", away_ab, home_ab), ("home", home_ab, away_ab)):
        t = teams.get(side) or {}
        order = [pid for pid in (t.get("pitchers") or []) if pid]
        players = t.get("players") or {}
        for i, pid in enumerate(order):
            p = players.get(f"ID{pid}") or {}
            st = ((p.get("stats") or {}).get("pitching")) or {}
            if not st:
                continue
            rows.append({
                "game_pk": meta["pk"], "pitcher_id": pid,
                "game_date": meta["date"], "game_type": meta["type"],
                "team": team, "opponent": opp, "home": side == "home",
                "pitcher_name": ((p.get("person") or {}).get("fullName")),
                "started": i == 0,
                "outs": _ip_to_outs(st.get("inningsPitched")),
                "batters_faced": _int(st.get("battersFaced")),
                "hits": _int(st.get("hits")),
                "runs": _int(st.get("runs")),
                "earned_runs": _int(st.get("earnedRuns")),
                "strikeouts": _int(st.get("strikeOuts")),
                "walks": _int(st.get("baseOnBalls")),
                "home_runs": _int(st.get("homeRuns")),
            })
    return rows


def probe(client) -> int:
    """Reachability + shape verification — read this log before backfill."""
    start, end = "2026-06-20", "2026-06-21"
    games = schedule_games(client, start, end)
    print(f"PROBE schedule {start}..{end}: {len(games)} final games")
    if not games:
        print("PROBE FAIL: schedule empty/unreachable")
        return 2
    print(f"  sample: {games[0]}")
    box = _get(client, f"{BASE}/game/{games[0]['pk']}/boxscore")
    if not box:
        print("PROBE FAIL: boxscore unreachable")
        return 2
    away = ((box.get("teams") or {}).get("away")) or {}
    order = away.get("pitchers") or []
    first = ((away.get("players") or {}).get(f"ID{order[0]}") if order else {}) or {}
    st = ((first.get("stats") or {}).get("pitching")) or {}
    print(f"PROBE raw: away pitchers order={order}")
    print(f"PROBE raw pitching stat keys: {sorted(st.keys())}")
    rows = parse_boxscore(box, games[0])
    starters = [r for r in rows if r["started"]]
    print(f"PROBE parsed {len(rows)} pitcher rows / {len(starters)} starters:")
    for r in rows[:6]:
        print(f"  {r['team']} {r['pitcher_name']}: started={r['started']} "
              f"outs={r['outs']} BF={r['batters_faced']} H={r['hits']} "
              f"R={r['runs']} ER={r['earned_runs']} K={r['strikeouts']} "
              f"BB={r['walks']} HR={r['home_runs']}")
    if len(rows) < 4 or len(starters) != 2:
        print(f"PROBE WARN: expected >=4 rows with exactly 2 starters — check "
              f"the `pitchers` order / stat keys above")
        return 1
    print("PROBE OK")
    return 0


def ingest(client, sb, start: str, end: str, commit: bool) -> int:
    have: set = set()
    try:
        page = 0
        while True:      # explicit paging — the 1,000-row lesson
            rows = (sb.table("mlb_pitcher_games").select("game_pk")
                    .range(page * 1000, page * 1000 + 999).execute().data) or []
            have.update(r["game_pk"] for r in rows)
            if len(rows) < 1000:
                break
            page += 1
    except Exception as e:
        log.warning("preload failed (%s) — treating all as new", e)

    d = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    todo: list[dict] = []
    while d <= stop:
        chunk_end = min(d + timedelta(days=_SCHED_CHUNK_DAYS - 1), stop)
        for g in schedule_games(client, d.isoformat(), chunk_end.isoformat()):
            if g["pk"] and g["pk"] not in have:
                todo.append(g)
                have.add(g["pk"])
        d = chunk_end + timedelta(days=1)
    log.info("games to fetch: %d", len(todo))
    if not todo:
        return 0

    batch, done, wrote = [], 0, 0
    for g in todo:
        box = _get(client, f"{BASE}/game/{g['pk']}/boxscore")
        if box:
            batch.extend(parse_boxscore(box, g))
        done += 1
        if len(batch) >= 400:
            if commit:
                sb.table("mlb_pitcher_games").upsert(
                    batch, on_conflict="game_pk,pitcher_id").execute()
            wrote += len(batch)
            log.info("  ...%d/%d games (%d rows)", done, len(todo), wrote)
            batch = []
    if batch:
        if commit:
            sb.table("mlb_pitcher_games").upsert(
                batch, on_conflict="game_pk,pitcher_id").execute()
        wrote += len(batch)
    log.info("done: %d games · %d pitcher rows%s", done, wrote,
             "" if commit else " (dry-run)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--delta", action="store_true")
    ap.add_argument("--start", default="2025-03-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    client = httpx.Client(headers={"User-Agent": "kahla-house/1.0"})

    if args.probe:
        return probe(client)
    end = args.end or datetime.now(timezone.utc).date().isoformat()
    sb = db.client()
    if args.backfill:
        return ingest(client, sb, args.start, end, args.commit)
    if args.delta:
        start = (datetime.now(timezone.utc).date() - timedelta(days=4)).isoformat()
        return ingest(client, sb, start, end, args.commit)
    log.error("nothing to do — pass --probe, --backfill or --delta")
    return 2


if __name__ == "__main__":
    sys.exit(main())
