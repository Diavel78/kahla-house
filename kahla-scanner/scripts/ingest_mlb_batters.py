"""Ingest per-game MLB batting logs from the official MLB Stats API.

Diamond IQ Phase 3 — the BATTER spine. The model's pitching is
player-level (the proven +1pp layer) but its offense was a
team-aggregate blob; the market's number knows who's actually in the
lineup and how good each bat is. This table closes that gap: the
boxscore's `battingOrder` says who ACTUALLY started in which slot
(leakage-free — in reality the lineup posts pre-game) and every
batter's line feeds a rolling per-batter quality layer. Spring
training / exhibition / all-star (gameType S/E/A) EXCLUDED — the
power-ratings preseason lesson.

Probe-first (the UFCStats lesson): field names are verified from the
Action log before any backfill trusts the parser, empty phases fail
LOUD, and every full-table query pages explicitly.

  python -m scripts.ingest_mlb_batters --probe
  python -m scripts.ingest_mlb_batters --backfill --start 2025-03-01 --commit
  python -m scripts.ingest_mlb_batters --delta --commit     # last ~4 days
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
                continue      # only completed games have real batting lines
            out.append({"pk": g.get("gamePk"), "date": d.get("date"),
                        "type": g.get("gameType")})
    return out


def _int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_boxscore(data: dict, meta: dict) -> list[dict]:
    """Batter rows from one boxscore. battingOrder '100' = leadoff starter,
    '401' = first substitute into slot 4 — started = value % 100 == 0,
    slot = value // 100. Players with no batting stat line (e.g. pitchers
    who never batted) are skipped."""
    rows = []
    teams = (data or {}).get("teams") or {}
    away_ab = (((teams.get("away") or {}).get("team")) or {}).get("abbreviation")
    home_ab = (((teams.get("home") or {}).get("team")) or {}).get("abbreviation")
    for side, team, opp in (("away", away_ab, home_ab), ("home", home_ab, away_ab)):
        t = teams.get(side) or {}
        players = t.get("players") or {}
        for pid in (t.get("batters") or []):
            p = players.get(f"ID{pid}") or {}
            st = ((p.get("stats") or {}).get("batting")) or {}
            if not st:
                continue
            bo = _int(p.get("battingOrder"))
            rows.append({
                "game_pk": meta["pk"], "batter_id": pid,
                "game_date": meta["date"], "game_type": meta["type"],
                "team": team, "opponent": opp, "home": side == "home",
                "batter_name": ((p.get("person") or {}).get("fullName")),
                "batting_order": (bo // 100 if bo else None),
                "started": bool(bo is not None and bo % 100 == 0),
                "ab": _int(st.get("atBats")),
                "runs": _int(st.get("runs")),
                "hits": _int(st.get("hits")),
                "doubles": _int(st.get("doubles")),
                "triples": _int(st.get("triples")),
                "home_runs": _int(st.get("homeRuns")),
                "rbi": _int(st.get("rbi")),
                "walks": _int(st.get("baseOnBalls")),
                "strikeouts": _int(st.get("strikeOuts")),
                "hbp": _int(st.get("hitByPitch")),
                "sac_flies": _int(st.get("sacFlies")),
                "stolen_bases": _int(st.get("stolenBases")),
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
    batters = away.get("batters") or []
    first = ((away.get("players") or {}).get(f"ID{batters[0]}") if batters else {}) or {}
    st = ((first.get("stats") or {}).get("batting")) or {}
    print(f"PROBE raw: away batters ids={batters[:12]}")
    print(f"PROBE raw battingOrder of first: {first.get('battingOrder')!r}")
    print(f"PROBE raw batting stat keys: {sorted(st.keys())}")
    rows = parse_boxscore(box, games[0])
    starters = [r for r in rows if r["started"]]
    print(f"PROBE parsed {len(rows)} batter rows / {len(starters)} starters:")
    for r in rows[:6]:
        print(f"  {r['team']} #{r['batting_order']} {r['batter_name']}: "
              f"started={r['started']} AB={r['ab']} H={r['hits']} "
              f"2B={r['doubles']} HR={r['home_runs']} BB={r['walks']} "
              f"K={r['strikeouts']} HBP={r['hbp']} SF={r['sac_flies']}")
    if len(rows) < 18 or not (16 <= len(starters) <= 20):
        print(f"PROBE WARN: expected ~18+ rows with ~18 starters — check "
              f"the battingOrder / stat keys above")
        return 1
    print("PROBE OK")
    return 0


def ingest(client, sb, start: str, end: str, commit: bool) -> int:
    have: set = set()
    try:
        page = 0
        while True:      # explicit paging — the 1,000-row lesson
            rows = (sb.table("mlb_batter_games").select("game_pk")
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
                sb.table("mlb_batter_games").upsert(
                    batch, on_conflict="game_pk,batter_id").execute()
            wrote += len(batch)
            log.info("  ...%d/%d games (%d rows)", done, len(todo), wrote)
            batch = []
    if batch:
        if commit:
            sb.table("mlb_batter_games").upsert(
                batch, on_conflict="game_pk,batter_id").execute()
        wrote += len(batch)
    log.info("done: %d games · %d batter rows%s", done, wrote,
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
