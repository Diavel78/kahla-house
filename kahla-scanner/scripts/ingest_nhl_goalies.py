"""Ingest per-game NHL goalie logs from the official NHL API.

Crease IQ Phase 1: who started every game, and how they did — the
walk-forward raw material for the goalie-quality model. Source is the
free, no-auth NHL API (api-web.nhle.com): weekly schedule pages give
game ids, per-game boxscores give the goalie lines (starter flag,
saves/shots, TOI, decision). Preseason (gameType 1) is EXCLUDED — the
power-ratings lesson: exhibition starters poison form.

Probe-first (the UFCStats lesson): field names are verified from the
Action log before any backfill trusts the parser, empty phases fail
LOUD, and every full-table query carries an explicit limit.

  python -m scripts.ingest_nhl_goalies --probe
  python -m scripts.ingest_nhl_goalies --backfill --start 2024-09-15 --commit
  python -m scripts.ingest_nhl_goalies --delta --commit     # last ~4 days
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

BASE = "https://api-web.nhle.com/v1"
_SLEEP = 0.25
_TRIES = 3
_GAME_TYPES = (2, 3)     # regular season + playoffs; 1 (preseason) excluded


def _get(client: httpx.Client, url: str) -> dict | None:
    for attempt in range(_TRIES):
        try:
            r = client.get(url, timeout=20)
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


def schedule_games(client, day: str) -> list[dict]:
    """Games for the WEEK starting at `day` (the schedule endpoint returns
    a gameWeek). [{id, date, type, away, home}] for regular+playoffs."""
    data = _get(client, f"{BASE}/schedule/{day}")
    out = []
    for d in ((data or {}).get("gameWeek") or []):
        for g in (d.get("games") or []):
            if g.get("gameType") not in _GAME_TYPES:
                continue
            out.append({
                "id": g.get("id"), "date": d.get("date"),
                "type": g.get("gameType"),
                "away": ((g.get("awayTeam") or {}).get("abbrev")),
                "home": ((g.get("homeTeam") or {}).get("abbrev")),
            })
    return out


def _name(v) -> str | None:
    """NHL API name fields are {'default': 'Connor Hellebuyck'} dicts."""
    if isinstance(v, dict):
        return v.get("default")
    return v if isinstance(v, str) else None


def _saves_shots(g: dict) -> tuple[int | None, int | None]:
    """'24/26'-style saveShotsAgainst, with split-field fallbacks."""
    ss = g.get("saveShotsAgainst") or g.get("savesShotsAgainst")
    if isinstance(ss, str) and "/" in ss:
        try:
            a, b = ss.split("/")
            return int(a), int(b)
        except ValueError:
            pass
    sv, sa = g.get("saves"), g.get("shotsAgainst")
    try:
        return (int(sv) if sv is not None else None,
                int(sa) if sa is not None else None)
    except (TypeError, ValueError):
        return None, None


def parse_boxscore(data: dict, meta: dict) -> list[dict]:
    """Goalie rows from one boxscore."""
    rows = []
    pbs = (data or {}).get("playerByGameStats") or {}
    for side, team, opp in (("awayTeam", meta["away"], meta["home"]),
                            ("homeTeam", meta["home"], meta["away"])):
        for g in ((pbs.get(side) or {}).get("goalies") or []):
            gid = g.get("playerId")
            if not gid:
                continue
            saves, shots = _saves_shots(g)
            ga = g.get("goalsAgainst")
            try:
                ga = int(ga) if ga is not None else (
                    shots - saves if None not in (shots, saves) else None)
            except (TypeError, ValueError):
                ga = None
            rows.append({
                "game_id": meta["id"], "goalie_id": gid,
                "game_date": meta["date"], "game_type": meta["type"],
                "team": team, "opponent": opp, "home": side == "homeTeam",
                "goalie_name": _name(g.get("name")),
                "starter": bool(g.get("starter")),
                "shots_against": shots, "saves": saves, "goals_against": ga,
                "toi": g.get("toi"), "decision": g.get("decision"),
            })
    return rows


def probe(client) -> int:
    """Reachability + shape verification — read this log before backfill."""
    day = "2026-04-09"     # deep in the 2025-26 regular season
    games = schedule_games(client, day)
    print(f"PROBE schedule week of {day}: {len(games)} games")
    if not games:
        print("PROBE FAIL: schedule empty/unreachable")
        return 2
    print(f"  sample: {games[0]}")
    box = _get(client, f"{BASE}/gamecenter/{games[0]['id']}/boxscore")
    if not box:
        print("PROBE FAIL: boxscore unreachable")
        return 2
    pbs = (box.get("playerByGameStats") or {})
    raw = ((pbs.get("awayTeam") or {}).get("goalies") or [{}])
    print(f"PROBE raw goalie keys: {sorted((raw[0] or {}).keys())}")
    rows = parse_boxscore(box, games[0])
    print(f"PROBE parsed {len(rows)} goalie rows:")
    for r in rows:
        print(f"  {r['team']} {r['goalie_name']}: starter={r['starter']} "
              f"{r['saves']}/{r['shots_against']} GA {r['goals_against']} "
              f"toi {r['toi']} dec {r['decision']}")
    starters = sum(1 for r in rows if r["starter"])
    if len(rows) < 2 or starters < 2:
        print(f"PROBE WARN: expected >=2 rows with 2 starters, got "
              f"{len(rows)} rows / {starters} starters — check the "
              f"`starter` field name in the raw keys above")
        return 1
    print("PROBE OK")
    return 0


def ingest(client, sb, start: str, end: str, commit: bool) -> int:
    have: set = set()
    try:
        page = 0
        while True:
            rows = (sb.table("nhl_goalie_games").select("game_id")
                    .range(page * 1000, page * 1000 + 999).execute().data) or []
            have.update(r["game_id"] for r in rows)
            if len(rows) < 1000:      # explicit paging — the 1,000-row lesson
                break
            page += 1
    except Exception as e:
        log.warning("preload failed (%s) — treating all as new", e)

    d = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    todo: list[dict] = []
    while d <= stop:
        for g in schedule_games(client, d.isoformat()):
            if g["id"] and g["id"] not in have:
                todo.append(g)
                have.add(g["id"])
        d += timedelta(days=7)       # schedule endpoint returns a full week
    log.info("games to fetch: %d (already have %d)", len(todo), len(have) - len(todo))
    if not todo:
        return 0

    batch, done, wrote = [], 0, 0
    for g in todo:
        box = _get(client, f"{BASE}/gamecenter/{g['id']}/boxscore")
        if box:
            batch.extend(parse_boxscore(box, g))
        done += 1
        if len(batch) >= 200:
            if commit:
                sb.table("nhl_goalie_games").upsert(
                    batch, on_conflict="game_id,goalie_id").execute()
            wrote += len(batch)
            log.info("  ...%d/%d games (%d rows)", done, len(todo), wrote)
            batch = []
    if batch:
        if commit:
            sb.table("nhl_goalie_games").upsert(
                batch, on_conflict="game_id,goalie_id").execute()
        wrote += len(batch)
    log.info("done: %d games · %d goalie rows%s", done, wrote,
             "" if commit else " (dry-run)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--delta", action="store_true")
    ap.add_argument("--start", default="2024-09-15")
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
