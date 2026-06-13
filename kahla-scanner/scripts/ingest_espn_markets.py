"""ESPN → `markets` ingest (Odds-API-cutover schedule spine).

THE WHY: today the `markets` table — the schedule EVERYTHING joins on
(games list, pm-snapshot's per-game loop, the dossier, `market_id` on
every `bot_picks` row) — is written ONLY by `scrapers/odds_api.py`. When
The Odds API subscription lapses (June 25 2026) nothing populates it and
the whole Pick Bot starves. Decision (June 2026): ESPN's free scoreboard
becomes the event/schedule/grading spine; PMM + Kalshi supply ODDS matched
onto ESPN events. This script is that spine — it creates one `markets`
row per upcoming ESPN game, reusing odds_api's exact find-or-create dedup
so the UUID `market_id` scheme (and every downstream join) is unchanged.

STATUS: STAGED / DARK. Run by `.github/workflows/espn-markets-ingest.yml`
on `workflow_dispatch` only — NOT wired into the 1-min cron. It is
**dry-run by default** (prints would-create/reuse, writes nothing); pass
`--commit` to actually insert. Activate at cutover AFTER odds_api is
retired: running it --commit while odds_api still creates rows risks
duplicate markets (the two feeds name/time games slightly differently and
the cross-feed dedup is best-effort — gotcha #30). Until then, use it
dry-run to validate ESPN coverage per sport.

EXCEPTION (user): MMA method-of-victory props. ESPN's mma/ufc scoreboard
grades the winner/ML only and is per-fight (athletes, not team
competitors), so UFC is NOT ingested here yet — its schedule stays on
odds_api until a dedicated MMA parser lands; method props remain manual
settle exactly as today.

Usage:
  python -m scripts.ingest_espn_markets                 # dry-run, all sports
  python -m scripts.ingest_espn_markets --sport WORLDCUP
  python -m scripts.ingest_espn_markets --days 10 --commit
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

import httpx

from _lib import matcher
from storage import supabase_client as db
from storage.models import Market

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("ingest_espn_markets")

# Our sport code → (ESPN sport group, ESPN league slug). Soccer is
# per-competition; add leagues here as we cover them.
#
# UFC IS ingested (its SCHEDULE comes from ESPN's mma/ufc scoreboard — a
# flat list of fights). Schedule ingest and grading are independent: ESPN
# grades the winner/ML fine (the resolver already does), only
# method-of-victory PROPS can't auto-grade and stay manual-settle. So we
# get the upcoming fights here; props are unaffected.
_ESPN_SPORTS: dict[str, tuple[str, str]] = {
    "MLB":      ("baseball",   "mlb"),
    "NBA":      ("basketball", "nba"),
    "NHL":      ("hockey",     "nhl"),
    "NFL":      ("football",   "nfl"),
    "CBB":      ("basketball", "mens-college-basketball"),
    "NCAAF":    ("football",   "college-football"),
    "UFC":      ("mma",        "ufc"),
    "WORLDCUP": ("soccer",     "fifa.world"),
}

# Per-sport match window for the find-or-create dedup — mirror
# odds_api._MATCH_WINDOW_BY_SPORT (30m MLB for doubleheader safety, 6h UFC
# for same-card fights, wider elsewhere). Kept local so this script stays
# importable on its own.
_MATCH_WINDOW: dict[str, timedelta] = {
    "MLB": timedelta(minutes=30),
    "UFC": timedelta(hours=6),
}
_DEFAULT_WINDOW = timedelta(hours=12)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _competitor_name(grp: str, c: dict) -> str:
    """Display name for one ESPN competitor. MMA puts the fighter under
    `athlete` (not `team`) — see bot_picks_resolver._ufc_match_espn."""
    if grp == "mma":
        return ((c.get("athlete") or {}).get("displayName")
                or (c.get("team") or {}).get("displayName") or "")
    t = c.get("team") or {}
    return t.get("displayName") or t.get("name") or t.get("shortDisplayName") or ""


def _espn_games(grp: str, league: str, days: int) -> list[dict]:
    """Fetch ESPN scoreboard for [today, today+days] and return upcoming
    games as {away, home, commence}. Live/finished games are skipped — a
    schedule spine only needs pre-game rows. Team sports + soccer read
    competitor.team; MMA reads competitor.athlete (one event = one fight),
    falling back to competitor order when home/away isn't flagged (UFC
    home/away is arbitrary)."""
    now = datetime.now(timezone.utc)
    dates = f"{now:%Y%m%d}-{(now + timedelta(days=days)):%Y%m%d}"
    url = f"https://site.api.espn.com/apis/site/v2/sports/{grp}/{league}/scoreboard"
    try:
        r = httpx.get(url, params={"dates": dates},
                      headers={"User-Agent": _UA}, timeout=15)
        r.raise_for_status()
        events = (r.json() or {}).get("events", []) or []
    except Exception as e:
        log.warning("  ESPN fetch failed (%s/%s): %s", grp, league, e)
        return []

    games: list[dict] = []
    for ev in events:
        ev_date = ev.get("date")
        # Iterate ALL competitions, not just [0]. For team sports + soccer
        # each event has one competition (the game). For MMA an event is a
        # CARD and competitions[] is every bout — reading only [0] dropped
        # all but the main event (the "UFC shows 2 fights" bug).
        for comp in (ev.get("competitions") or []):
            try:
                st = (comp.get("status") or ev.get("status") or {})
                state = ((st.get("type") or {}).get("state"))
                if state and state != "pre":
                    continue  # only upcoming — skip in-progress / final
                cs = comp.get("competitors") or []
                home = away = None
                for c in cs:
                    name = _competitor_name(grp, c)
                    if c.get("homeAway") == "home":
                        home = name
                    elif c.get("homeAway") == "away":
                        away = name
                # MMA / any feed without home-away flags: take two in order.
                if not (home and away) and len(cs) == 2:
                    away = away or _competitor_name(grp, cs[0])
                    home = home or _competitor_name(grp, cs[1])
                commence = _parse_iso(comp.get("date") or ev_date)
                if away and home and commence:
                    games.append({"away": away, "home": home, "commence": commence})
            except Exception:
                continue
    return games


def _find_or_create(sport: str, g: dict, aliases: dict[str, str],
                    existing: list[dict], commit: bool) -> tuple[str, str | None]:
    """Reuse an existing active markets row if teams match within the
    sport window, else create one. Mirrors
    odds_api._find_or_create_market. Returns (action, market_id) where
    action ∈ reuse/create/create-dry. `existing` is mutated to include
    newly-created rows so later games in the same run dedup against them."""
    window = _MATCH_WINDOW.get(sport, _DEFAULT_WINDOW)
    venue_key = matcher._teams_key(g["home"], g["away"], aliases)
    for row in existing:
        row_start = _parse_iso(row.get("event_start"))
        if row_start is None or abs(row_start - g["commence"]) > window:
            continue
        row_away, row_home = matcher._split_event_name(row.get("event_name", ""))
        if not (row_home and row_away):
            continue
        if matcher._teams_key(row_home, row_away, aliases) == venue_key or \
           matcher._fuzzy_teams_match(g["home"], g["away"], row_home, row_away,
                                      aliases) >= matcher.FUZZY_THRESHOLD:
            return "reuse", row["id"]
    # No match — create.
    if not commit:
        return "create-dry", None
    m = Market(sport=sport, event_name=f"{g['away']} @ {g['home']}",
               event_start=g["commence"])
    row = db.upsert_market(m)
    new_id = row.get("id")
    if new_id:
        existing.append({"id": new_id, "event_name": m.event_name,
                         "event_start": m.event_start.isoformat(),
                         "sport": sport, "status": "active"})
    return "create", new_id


def ingest_sport(sport: str, days: int, commit: bool) -> dict:
    grp, league = _ESPN_SPORTS[sport]
    games = _espn_games(grp, league, days)
    if not games:
        log.info("%-9s no upcoming ESPN games", sport)
        return {"sport": sport, "games": 0, "create": 0, "reuse": 0}
    try:
        aliases = db.list_team_aliases(sport)
    except Exception:
        aliases = {}
    existing = db.list_active_markets(sport)
    created = reused = 0
    for g in games:
        action, _mid = _find_or_create(sport, g, aliases, existing, commit)
        if action == "reuse":
            reused += 1
        else:
            created += 1
            log.info("  %s %-28s %s", "CREATE" if commit else "would-create",
                     f"{g['away']} @ {g['home']}", g["commence"].isoformat())
    log.info("%-9s %2d games · %d %s · %d reused", sport, len(games), created,
             "created" if commit else "would-create", reused)
    return {"sport": sport, "games": len(games), "create": created, "reuse": reused}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ESPN → markets ingest (cutover spine)")
    ap.add_argument("--sport", help="single sport code (e.g. WORLDCUP); default all")
    ap.add_argument("--days", type=int, default=8, help="lookahead window in days")
    ap.add_argument("--commit", action="store_true",
                    help="actually write rows (default: dry-run, prints only)")
    args = ap.parse_args(argv)

    sports = [args.sport.upper()] if args.sport else list(_ESPN_SPORTS)
    bad = [s for s in sports if s not in _ESPN_SPORTS]
    if bad:
        log.error("unknown sport(s): %s (known: %s)", bad, list(_ESPN_SPORTS))
        return 2

    mode = "COMMIT" if args.commit else "DRY-RUN"
    log.info("ESPN→markets ingest [%s] · %d-day window · sports=%s",
             mode, args.days, sports)
    if not args.commit:
        log.info("(dry-run — no rows written; pass --commit to insert)")

    totals = {"games": 0, "create": 0, "reuse": 0}
    for sport in sports:
        try:
            r = ingest_sport(sport, args.days, args.commit)
            for k in totals:
                totals[k] += r[k]
        except Exception as e:
            log.warning("%-9s ERROR: %s", sport, e)
    log.info("TOTAL %d games · %d %s · %d reused", totals["games"],
             totals["create"], "created" if args.commit else "would-create",
             totals["reuse"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
