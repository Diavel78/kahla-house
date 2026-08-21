"""Ingest per-game football player stat lines from ESPN boxscores.

The training spine for football PROP models (the mlb_pitcher_games
pattern): every player's actual line in every final game, leakage-free.
Game universe = game_results (already preseason-free, and it carries the
espn_id), so one summary call per known game is the whole ingest.

Probe-first (the UFCStats lesson): --probe fetches ONE game and prints the
raw category names + label arrays + parsed rows — read that Action log and
verify the labels before any backfill trusts the parser. Stats are located
by LABEL, never by position, so a reordered column can only null a stat,
not silently swap two.

No presence preload: upserts are idempotent on (sport, espn_event_id,
player_id), so re-fetching a game is only a wasted ESPN call — and a full
NFL 3-season backfill is ~860 calls, small enough to just re-run.

  python -m scripts.ingest_football_players --probe
  python -m scripts.ingest_football_players --backfill --start 2023-08-01 --commit
  python -m scripts.ingest_football_players --delta --commit    # last ~8 days
  python -m scripts.ingest_football_players --delta --sport NCAAF --commit
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

from storage import supabase_client as db

log = logging.getLogger(__name__)

_LEAGUE = {"NFL": ("football", "nfl"), "NCAAF": ("football", "college-football")}
_SLEEP = 0.25
_TRIES = 3


def _get(client: httpx.Client, url: str, params: dict) -> dict | None:
    for attempt in range(_TRIES):
        try:
            r = client.get(url, params=params, timeout=25)
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


def _summary(client, sport: str, event_id: str) -> dict | None:
    grp, lg = _LEAGUE[sport]
    return _get(client,
                f"https://site.api.espn.com/apis/site/v2/sports/{grp}/{lg}/summary",
                {"event": event_id})


# ── stat parsing ────────────────────────────────────────────────────────────
# Column -> (category, LABEL, extractor-index). Extractors handle ESPN's
# composite strings: "18/27" C/ATT, "2-15" SACKS, "2/3" FG. Located by label
# so a reordered boxscore can only miss a stat, never swap two.

def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _split2(v, sep):
    parts = str(v or "").split(sep)
    if len(parts) != 2:
        return None, None
    return _int(parts[0]), _int(parts[1])


_STAT_MAP = {
    # column: (category, label, part)  part: None=int, 0/1 = split index
    "pass_cmp":  ("passing", "C/ATT", ("/", 0)),
    "pass_att":  ("passing", "C/ATT", ("/", 1)),
    "pass_yds":  ("passing", "YDS", None),
    "pass_td":   ("passing", "TD", None),
    "pass_int":  ("passing", "INT", None),
    "sacks":     ("passing", "SACKS", ("-", 0)),
    "sack_yds":  ("passing", "SACKS", ("-", 1)),
    "rush_att":  ("rushing", "CAR", None),
    "rush_yds":  ("rushing", "YDS", None),
    "rush_td":   ("rushing", "TD", None),
    "rush_long": ("rushing", "LONG", None),
    "rec":       ("receiving", "REC", None),
    "rec_tgts":  ("receiving", "TGTS", None),
    "rec_yds":   ("receiving", "YDS", None),
    "rec_td":    ("receiving", "TD", None),
    "rec_long":  ("receiving", "LONG", None),
    "fum":       ("fumbles", "FUM", None),
    "fum_lost":  ("fumbles", "LOST", None),
    "fg_made":   ("kicking", "FG", ("/", 0)),
    "fg_att":    ("kicking", "FG", ("/", 1)),
    "xp_made":   ("kicking", "XP", ("/", 0)),
    "xp_att":    ("kicking", "XP", ("/", 1)),
    "kick_pts":  ("kicking", "PTS", None),
}


def _home_team_ids(data: dict) -> dict:
    """team_id -> homeAway from the summary header."""
    out = {}
    comps = ((data.get("header") or {}).get("competitions") or [{}])
    for c in (comps[0].get("competitors") or []):
        tid = str(((c.get("team") or {}).get("id")) or "")
        if tid:
            out[tid] = c.get("homeAway")
    return out


def parse_summary(data: dict, sport: str, meta: dict) -> list[dict]:
    """Player rows from one game summary. meta carries game_results fields."""
    box = (data or {}).get("boxscore") or {}
    sides = box.get("players") or []
    if len(sides) != 2:
        return []
    home_map = _home_team_ids(data)
    abbrs = [((s.get("team") or {}).get("abbreviation")) for s in sides]
    rows: dict[str, dict] = {}
    for i, side in enumerate(sides):
        team_o = side.get("team") or {}
        team, opp = abbrs[i], abbrs[1 - i]
        is_home = home_map.get(str(team_o.get("id") or "")) == "home"
        # index categories by lowercase name; labels located per category
        for cat in (side.get("statistics") or []):
            cname = (cat.get("name") or "").lower()
            labels = [str(x).upper() for x in (cat.get("labels") or [])]
            wanted = {col: (lab, part) for col, (c, lab, part) in _STAT_MAP.items()
                      if c == cname}
            if not wanted:
                continue
            for a in (cat.get("athletes") or []):
                ath = a.get("athlete") or {}
                pid = str(ath.get("id") or "")
                if not pid:
                    continue
                stats = a.get("stats") or []
                row = rows.setdefault(pid, {
                    "sport": sport, "espn_event_id": meta["espn_id"],
                    "event_start": meta.get("event_start"),
                    "game_date": meta.get("game_date"),
                    "season": meta.get("season"),
                    "team": team, "opponent": opp, "home": is_home,
                    "player_id": pid,
                    "player_name": ath.get("displayName"),
                    "position": ((ath.get("position") or {}).get("abbreviation")
                                 if isinstance(ath.get("position"), dict)
                                 else ath.get("position")),
                })
                for col, (lab, part) in wanted.items():
                    if lab not in labels:
                        continue
                    v = stats[labels.index(lab)] if labels.index(lab) < len(stats) else None
                    if part is None:
                        val = _int(v)
                    else:
                        sep, idx = part
                        val = _split2(v, sep)[idx]
                    if val is not None:
                        row[col] = val
    return list(rows.values())


# ── game universe from game_results ────────────────────────────────────────

def _season_of(date_iso: str | None) -> int | None:
    try:
        d = datetime.fromisoformat(str(date_iso)[:10])
        return d.year if d.month >= 7 else d.year - 1
    except (TypeError, ValueError):
        return None


def _games(sb, sport: str, start: str, end: str) -> list[dict]:
    """game_results rows in [start, end] — PAGED (gotcha #40: PostgREST
    caps one response at 1,000 rows regardless of .limit())."""
    rows: list[dict] = []
    for page in range(20):
        chunk = (sb.table("game_results")
                 .select("espn_id,event_start,game_date")
                 .eq("sport", sport)
                 .gte("game_date", start)
                 .lte("game_date", end)
                 .order("game_date")
                 .range(page * 1000, page * 1000 + 999)
                 .execute().data) or []
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
    out = []
    for r in rows:
        if not r.get("espn_id"):
            continue
        out.append({"espn_id": str(r["espn_id"]),
                    "event_start": r.get("event_start"),
                    "game_date": r.get("game_date"),
                    "season": _season_of(r.get("game_date"))})
    return out


# ── modes ───────────────────────────────────────────────────────────────────

def probe(client, sb, sport: str) -> int:
    games = _games(sb, sport, "2000-01-01", "2100-01-01")
    if not games:
        print(f"PROBE FAIL: no {sport} games in game_results")
        return 2
    g = games[-1]
    print(f"PROBE game: {sport} espn_id={g['espn_id']} date={g['game_date']}")
    data = _summary(client, sport, g["espn_id"])
    if not data:
        print("PROBE FAIL: summary unreachable")
        return 2
    box = (data.get("boxscore") or {}).get("players") or []
    print(f"PROBE raw: {len(box)} team blocks")
    for side in box:
        t = (side.get("team") or {}).get("abbreviation")
        for cat in (side.get("statistics") or []):
            aths = cat.get("athletes") or []
            print(f"  {t} {cat.get('name')}: labels={cat.get('labels')} "
                  f"athletes={len(aths)}")
            for a in aths[:1]:
                print(f"    sample: {(a.get('athlete') or {}).get('displayName')} "
                      f"stats={a.get('stats')}")
    rows = parse_summary(data, sport, g)
    print(f"PROBE parsed {len(rows)} player rows; samples:")
    for r in rows[:8]:
        line = {k: v for k, v in r.items()
                if k in _STAT_MAP and v is not None}
        print(f"  {r['team']} {r['player_name']} ({r.get('position')}) "
              f"home={r['home']}: {line}")
    qbs = [r for r in rows if r.get("pass_att")]
    if len(rows) < 20 or not qbs:
        print("PROBE WARN: expected 20+ rows incl. a QB with pass_att — "
              "check the labels above against _STAT_MAP")
        return 1
    print("PROBE OK")
    return 0


def ingest(client, sb, sport: str, start: str, end: str, commit: bool,
           cap: int) -> int:
    games = _games(sb, sport, start, end)
    if cap:
        games = games[:cap]
    log.info("%s games to fetch: %d (%s..%s)", sport, len(games), start, end)
    batch, done, wrote = [], 0, 0
    for g in games:
        data = _summary(client, sport, g["espn_id"])
        if data:
            batch.extend(parse_summary(data, sport, g))
        done += 1
        if len(batch) >= 400:
            if commit:
                sb.table("football_player_games").upsert(
                    batch, on_conflict="sport,espn_event_id,player_id").execute()
            wrote += len(batch)
            log.info("  ...%d/%d games (%d rows)", done, len(games), wrote)
            batch = []
    if batch:
        if commit:
            sb.table("football_player_games").upsert(
                batch, on_conflict="sport,espn_event_id,player_id").execute()
        wrote += len(batch)
    log.info("done: %d games · %d player rows%s", done, wrote,
             "" if commit else " (dry-run)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--delta", action="store_true")
    ap.add_argument("--sport", default="NFL", choices=list(_LEAGUE))
    ap.add_argument("--start", default="2023-08-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--limit", type=int, default=0,
                    help="cap games fetched this run (0 = all)")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    client = httpx.Client(headers={"User-Agent": "kahla-house/1.0"})
    sb = db.client()

    if args.probe:
        return probe(client, sb, args.sport)
    end = args.end or datetime.now(timezone.utc).date().isoformat()
    if args.backfill:
        return ingest(client, sb, args.sport, args.start, end, args.commit,
                      args.limit)
    if args.delta:
        start = (datetime.now(timezone.utc).date() - timedelta(days=8)).isoformat()
        return ingest(client, sb, args.sport, start, end, args.commit,
                      args.limit)
    log.error("nothing to do — pass --probe, --backfill or --delta")
    return 2


if __name__ == "__main__":
    sys.exit(main())
