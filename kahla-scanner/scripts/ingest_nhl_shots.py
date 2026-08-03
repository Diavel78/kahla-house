"""Ingest per-shot NHL play-by-play events (the Crease IQ xG spine).

The P2 postmortem's verdict: raw shot-count sv% can't separate NHL
goalies — the published 56-60% models run xG built from SHOT LOCATION.
This spine captures every unblocked attempt (goal / shot-on-goal /
missed-shot = Fenwick) with rink x/y, shooter, goalie-in-net, shot type
and strength state. Blocked shots are excluded on purpose: the recorded
location is the BLOCK, not the shot origin (standard xG practice).

Game universe: nhl_goalie_games (the P1 spine — already regular season
+ playoffs only, preseason excluded), so no schedule re-walking and the
two spines can never disagree about which games exist.

Probe-first (the UFCStats lesson): the play-by-play field names
(typeDescKey, details.xCoord/yCoord/goalieInNetId/shootingPlayerId,
situationCode, eventId uniqueness) are verified from the Action log
before any backfill trusts the parser.

  python -m scripts.ingest_nhl_shots --probe
  python -m scripts.ingest_nhl_shots --backfill --commit
  python -m scripts.ingest_nhl_shots --delta --commit   # same incremental
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter

import httpx

from storage import supabase_client as db

log = logging.getLogger(__name__)

BASE = "https://api-web.nhle.com/v1"
_SLEEP = 0.25
_TRIES = 3
_FENWICK = ("goal", "shot-on-goal", "missed-shot")
_PROBE_GAME = 2024020001            # 2024-25 opening night — completed


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


def _i(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _n(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_game(pbp: dict, game_id: int, game_date: str) -> list[dict]:
    """Play-by-play blob → Fenwick shot-event rows."""
    rows = []
    for p in (pbp.get("plays") or []):
        et = p.get("typeDescKey")
        if et not in _FENWICK:
            continue
        det = p.get("details") or {}
        shooter = _i(det.get("shootingPlayerId"))
        if shooter is None:
            shooter = _i(det.get("scoringPlayerId"))   # goals name the scorer
        rows.append({
            "game_id": game_id,
            "event_id": _i(p.get("eventId")),
            "game_date": game_date,
            "event_type": et,
            "is_goal": et == "goal",
            "period": _i((p.get("periodDescriptor") or {}).get("number")),
            "time_in_period": p.get("timeInPeriod"),
            "situation": (str(p.get("situationCode"))
                          if p.get("situationCode") is not None else None),
            "team_id": _i(det.get("eventOwnerTeamId")),
            "shooter_id": shooter,
            "goalie_id": _i(det.get("goalieInNetId")),
            "x": _n(det.get("xCoord")),
            "y": _n(det.get("yCoord")),
            "shot_type": det.get("shotType"),
            "zone": det.get("zoneCode"),
        })
    return [r for r in rows if r["event_id"] is not None]


def probe(client) -> int:
    pbp = _get(client, f"{BASE}/gamecenter/{_PROBE_GAME}/play-by-play")
    if pbp is None:
        print("PROBE FAIL: play-by-play unreachable")
        return 2
    plays = pbp.get("plays") or []
    kinds = Counter(p.get("typeDescKey") for p in plays)
    print(f"PROBE game {_PROBE_GAME}: {len(plays)} plays; "
          f"types: {dict(kinds.most_common(12))}")
    rows = parse_game(pbp, _PROBE_GAME, "2024-10-08")
    if not rows:
        print("PROBE FAIL: no Fenwick rows parsed — typeDescKey drift?")
        return 2
    n = len(rows)
    ids = {r["event_id"] for r in rows}
    coords = sum(1 for r in rows if r["x"] is not None and r["y"] is not None)
    goalies = sum(1 for r in rows if r["goalie_id"] is not None)
    goals = sum(1 for r in rows if r["is_goal"])
    print(f"PROBE parsed: {n} Fenwick rows · {goals} goals · "
          f"coords {coords}/{n} · goalie-in-net {goalies}/{n} · "
          f"unique event_ids {len(ids)}/{n}")
    for r in rows[:3]:
        print(f"  {r}")
    if len(ids) != n:
        print("PROBE FAIL: eventId not unique within game")
        return 2
    if coords < n * 0.9:
        print("PROBE FAIL: coordinates missing on >10% of attempts")
        return 2
    if goalies < n * 0.8:
        print("PROBE WARN: goalie_id sparse (empty-netters explain a few, "
              "not this many)")
        return 1
    print("PROBE OK")
    return 0


def _goalie_spine_games(sb) -> list[tuple[int, str]]:
    """(game_id, game_date) universe from nhl_goalie_games — paged."""
    out, page = {}, 0
    while True:      # explicit paging — the 1,000-row lesson
        rows = (sb.table("nhl_goalie_games").select("game_id,game_date")
                .order("game_date").order("game_id")
                .range(page * 1000, page * 1000 + 999).execute().data) or []
        for r in rows:
            out[r["game_id"]] = r["game_date"]
        if len(rows) < 1000:
            break
        page += 1
    return sorted(out.items(), key=lambda kv: (kv[1], kv[0]))


def _have_games(sb) -> set:
    out, page = set(), 0
    while True:      # explicit paging — the 1,000-row lesson
        rows = (sb.table("nhl_shot_events").select("game_id")
                .order("game_id")
                .range(page * 1000, page * 1000 + 999).execute().data) or []
        out.update(r["game_id"] for r in rows)
        if len(rows) < 1000:
            break
        page += 1
    return out


def ingest(client, sb, commit: bool) -> int:
    games = _goalie_spine_games(sb)
    if not games:
        log.error("nhl_goalie_games is EMPTY — run the goalie ingest first")
        return 2
    have = _have_games(sb)
    todo = [(g, d) for g, d in games if g not in have]
    log.info("universe %d games · %d already ingested · %d to fetch",
             len(games), len(games) - len(todo), len(todo))
    done = rows_written = failed = 0
    for gid, gdate in todo:
        pbp = _get(client, f"{BASE}/gamecenter/{gid}/play-by-play")
        if pbp is None:
            failed += 1
            log.warning("skip game %s (fetch failed)", gid)
            continue
        rows = parse_game(pbp, gid, gdate)
        if rows and commit:
            sb.table("nhl_shot_events").upsert(
                rows, on_conflict="game_id,event_id").execute()
        done += 1
        rows_written += len(rows)
        if done % 100 == 0:
            log.info("  ...%d games (%d shot rows)", done, rows_written)
    log.info("done: %d games · %d shot rows · %d failed%s", done,
             rows_written, failed, "" if commit else " (dry-run)")
    return 0 if failed < max(5, len(todo) // 20) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--delta", action="store_true")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    client = httpx.Client(headers={"User-Agent": "kahla-house/1.0"})
    if args.probe:
        return probe(client)
    sb = db.client()
    if args.backfill or args.delta:     # both are the same incremental walk
        return ingest(client, sb, args.commit)
    log.error("nothing to do — pass --probe, --backfill or --delta")
    return 2


if __name__ == "__main__":
    sys.exit(main())
