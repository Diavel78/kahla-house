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
  python -m scripts.ingest_espn_markets --sport UFC
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

# Our sport code → (ESPN sport group, ESPN league slug).
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
}

# Per-sport match window for the find-or-create dedup — mirror
# odds_api._MATCH_WINDOW_BY_SPORT (30m MLB for doubleheader safety, wider
# elsewhere). Kept local so this script stays importable on its own.
# UFC 6h→48h (July 2026, Kalshi per-fight times): a bout's stored start can
# be an old ESPN BLOCK time while the incoming Kalshi occurrence time sits
# hours later on the same card — a 6h window would miss the match and mint
# a dupe. Same fighter PAIR never fights twice within 48h, so the wide
# window is dupe-safe for UFC (the tight windows exist for MLB
# doubleheaders, a team-sport problem).
_MATCH_WINDOW: dict[str, timedelta] = {
    "MLB": timedelta(minutes=30),
    "UFC": timedelta(hours=48),
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


# ── Kalshi per-fight times (UFC) ────────────────────────────────────────
# ESPN's mma/ufc scoreboard gives BLOCK times (a card's bouts share segment
# starts), so UFC countdowns/prime windows ran hours off — "the only thing
# missing is real UFC start times" (user, July 2026). Kalshi's UFC markets
# carry a per-fight `occurrence_datetime` (verified live July 2026: bouts on
# the same card 4h40m apart), free public API, no auth. ESPN stays
# authoritative for WHICH bouts exist; Kalshi refines WHEN. The offset
# constant exists because occurrence may be start-ish or settle-ish —
# calibrate against a live card (compare actual walkout times) and adjust.
_KALSHI_UFC_URL = ("https://api.elections.kalshi.com/trade-api/v2/markets"
                   "?series_ticker=KXUFCFIGHT&status=open&limit=200")
# −5h: Kalshi's occurrence_datetime runs a constant 5h AHEAD of the true
# fight instant. RECALIBRATED on UFC 329 (Jul 11 2026, verified against the
# published schedule — a numbered PPV, NOT a Fight Night): the opening bout
# (Costa/Durden) carries occurrence 02:00Z, and early prelims for that card
# actually open at 5:00 PM ET = 21:00Z → the raw stamp is exactly +5h. With
# −300 the WHOLE 14-bout card lands on its real segments (early prelims
# 5 PM ET, prelims 7 PM ET, main card 9 PM ET, McGregor/Holloway ~11 PM ET).
# The earlier −180 was off by one segment: it assumed early prelims began at
# 7 PM ET, but 7 PM ET is the PRELIMS slot — that mistake left every fight 2h
# late (users saw already-live fights counting down "1h 25m away"). Empirical
# — verify against actual walkout times on the next live card; if every fight
# suddenly shows ~5h early, Kalshi fixed their stamps → set toward 0.
_KALSHI_OCC_OFFSET_MIN = -300


def _kalshi_ufc_times() -> list[dict]:
    """Per-fight expected times from Kalshi's open UFC markets, grouped by
    event_ticker: [{"fighters": {name, name}, "when": datetime}]."""
    try:
        r = httpx.get(_KALSHI_UFC_URL, headers={"User-Agent": _UA}, timeout=15)
        r.raise_for_status()
        markets = (r.json() or {}).get("markets") or []
    except Exception as e:
        log.warning("  Kalshi UFC times fetch failed: %s", e)
        return []
    fights: dict[str, dict] = {}
    for m in markets:
        et = m.get("event_ticker")
        when = _parse_iso(m.get("occurrence_datetime")
                          or m.get("expected_expiration_time"))
        f1 = m.get("yes_sub_title") or ""
        if not (et and when and f1):
            continue
        e = fights.setdefault(et, {"fighters": set(), "when": when})
        e["fighters"].add(f1)
        if m.get("no_sub_title"):
            e["fighters"].add(m["no_sub_title"])
    return [f for f in fights.values() if len(f["fighters"]) == 2]


def _name_tokens(s: str) -> list[str]:
    import re
    return [t for t in re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split()
            if len(t) >= 3]


def _fighter_match(a: str, b: str) -> bool:
    """Last-name-token containment either direction — the resolver's
    _ufc_match_espn posture ('B. Susurkaev' vs 'Baysangur Susurkaev')."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return False
    return ta[-1] in tb or tb[-1] in ta


def _ufc_pair_match(a1: str, h1: str, a2: str, h2: str) -> bool:
    """Both fighters match by last-name token, either orientation. Needed in
    _find_or_create because the generic fuzzy matcher misses first-name
    spelling flips — ESPN renamed 'Zach Reese' → 'Zachary Reese' between
    ticks and fuzz.ratio scored 87 vs the 88 threshold, minting a dupe row
    (July 2026). Requiring BOTH last names keeps distinct bouts apart."""
    return ((_fighter_match(a1, a2) and _fighter_match(h1, h2))
            or (_fighter_match(a1, h2) and _fighter_match(h1, a2)))


def _apply_kalshi_ufc_times(games: list[dict]) -> int:
    """Override ESPN block times with Kalshi's per-fight occurrence times
    for name-matched bouts. Sanity: only within ±30h of ESPN's date (a
    postponed-fight Kalshi market must not drag a row across days)."""
    fights = _kalshi_ufc_times()
    if not fights:
        return 0
    n = 0
    for g in games:
        for f in fights:
            f1, f2 = tuple(f["fighters"])
            ok = ((_fighter_match(g["away"], f1) and _fighter_match(g["home"], f2))
                  or (_fighter_match(g["away"], f2) and _fighter_match(g["home"], f1)))
            if not ok:
                continue
            when = f["when"] + timedelta(minutes=_KALSHI_OCC_OFFSET_MIN)
            if (abs(when - g["commence"]) <= timedelta(hours=30)
                    and when != g["commence"]):
                log.info("  KALSHI-TIME %-28s %s → %s",
                         f"{g['away']} @ {g['home']}",
                         g["commence"].isoformat(), when.isoformat())
                g["commence"] = when
                n += 1
            break
    return n


def _find_or_create(sport: str, g: dict, aliases: dict[str, str],
                    existing: list[dict], commit: bool) -> tuple[str, str | None]:
    """Reuse an existing active markets row if teams match within the
    sport window, else create one. Mirrors
    odds_api._find_or_create_market. Returns (action, market_id) where
    action ∈ reuse/retime/create/create-dry. `existing` is mutated to include
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
                                      aliases) >= matcher.FUZZY_THRESHOLD or \
           (sport == "UFC" and _ufc_pair_match(g["away"], g["home"],
                                               row_away, row_home)):
            # Reuse the existing row (UUID preserved), but CORRECT a drifted
            # start in place — ESPN is authoritative on timing. odds_api used
            # to do this every tick (gotcha #30); post-cutover the ESPN spine
            # is the only writer, so a stale placeholder time (an old odds_api
            # card-time that never got refreshed) would otherwise freeze the
            # game in the past and drop it from the games list before it
            # starts — the UFC "no upcoming games" regression.
            if abs(row_start - g["commence"]) > timedelta(minutes=2):
                if commit:
                    try:
                        db.update_market_start(row["id"], g["commence"])
                    except Exception as e:
                        log.warning("  retime failed %s: %s", row["id"], e)
                # keep the candidate set fresh for later games in this run
                row["event_start"] = g["commence"].isoformat()
                return "retime", row["id"]
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


def ingest_sport(sport: str, days: int, commit: bool, prune: bool = False) -> dict:
    grp, league = _ESPN_SPORTS[sport]
    games = _espn_games(grp, league, days)
    if not games:
        log.info("%-9s no upcoming ESPN games", sport)
        return {"sport": sport, "games": 0, "create": 0, "reuse": 0, "pruned": 0}
    if sport == "UFC":
        kt = _apply_kalshi_ufc_times(games)
        if kt:
            log.info("  Kalshi per-fight times applied to %d/%d bouts", kt, len(games))
    try:
        aliases = db.list_team_aliases(sport)
    except Exception:
        aliases = {}
    # Candidate set = the UPCOMING window only, NOT all-active. The markets
    # table is never cleaned (gotcha #12), so list_active_markets(sport)
    # returns hundreds of stale past games and gets truncated at PostgREST's
    # 1000-row cap — which dropped the upcoming rows we need to match
    # against and made the ingest mint a fresh dupe every tick. Windowing to
    # [now-6h, now+days+2] keeps the set small + complete → idempotent.
    now = datetime.now(timezone.utc)
    win_lo = (now - timedelta(hours=6)).isoformat()
    win_hi = (now + timedelta(days=days + 2)).isoformat()
    try:
        existing = (db.client().table("markets")
                    .select("id,event_name,event_start,sport,status")
                    .eq("status", "active").eq("sport", sport)
                    .gte("event_start", win_lo).lte("event_start", win_hi)
                    .order("event_start").limit(3000).execute().data) or []
    except Exception:
        existing = db.list_active_markets(sport)   # fallback
    created = reused = retimed = 0
    kept_ids: set[str] = set()
    for g in games:
        action, mid = _find_or_create(sport, g, aliases, existing, commit)
        if mid:
            kept_ids.add(mid)
        if action == "reuse":
            reused += 1
        elif action == "retime":
            retimed += 1
            reused += 1
            log.info("  %s %-28s → %s", "RETIME" if commit else "would-retime",
                     f"{g['away']} @ {g['home']}", g["commence"].isoformat())
        else:
            created += 1
            log.info("  %s %-28s %s", "CREATE" if commit else "would-create",
                     f"{g['away']} @ {g['home']}", g["commence"].isoformat())
    # Prune: ESPN is authoritative, so deactivate active FUTURE markets in
    # this sport that ESPN's scoreboard didn't account for — stale dupes,
    # The Odds API's kickboxing-in-UFC bleed, nonsense pairings. Only when
    # ESPN actually returned a slate (the `if not games` guard above) so a
    # transient empty can't wipe the board; only future rows (never touch
    # live/finished — the resolver + history need those). Matched/created
    # rows are in kept_ids and survive (market_id preserved).
    pruned = 0
    if prune and commit:
        now = datetime.now(timezone.utc)
        for row in existing:
            rid = row.get("id")
            rstart = _parse_iso(row.get("event_start"))
            if rid and rid not in kept_ids and rstart and rstart > now:
                try:
                    db.client().table("markets").update(
                        {"status": "inactive"}).eq("id", rid).execute()
                    pruned += 1
                    log.info("  prune (not on ESPN) %-30s %s",
                             row.get("event_name"), row.get("event_start"))
                except Exception as e:
                    log.warning("  prune failed %s: %s", rid, e)
    log.info("%-9s %2d games · %d %s · %d reused%s%s", sport, len(games), created,
             "created" if commit else "would-create", reused,
             f" · {retimed} retimed" if retimed else "",
             f" · {pruned} pruned" if prune else "")
    return {"sport": sport, "games": len(games), "create": created,
            "reuse": reused, "retime": retimed, "pruned": pruned}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ESPN → markets ingest (cutover spine)")
    ap.add_argument("--sport", help="single sport code (e.g. UFC); default all")
    ap.add_argument("--exclude", default="",
                    help="comma-list of sport codes to skip")
    ap.add_argument("--days", type=int, default=8, help="lookahead window in days")
    ap.add_argument("--commit", action="store_true",
                    help="actually write rows (default: dry-run, prints only)")
    ap.add_argument("--prune", action="store_true",
                    help="deactivate active FUTURE markets ESPN didn't return "
                         "for the sport (stale dupes / non-ESPN bleed). Needs "
                         "--commit; only prunes sports where ESPN returned a slate.")
    args = ap.parse_args(argv)

    sports = [args.sport.upper()] if args.sport else list(_ESPN_SPORTS)
    skip = {s.strip().upper() for s in args.exclude.split(",") if s.strip()}
    sports = [s for s in sports if s not in skip]
    bad = [s for s in sports if s not in _ESPN_SPORTS]
    if bad:
        log.error("unknown sport(s): %s (known: %s)", bad, list(_ESPN_SPORTS))
        return 2

    mode = "COMMIT" if args.commit else "DRY-RUN"
    log.info("ESPN→markets ingest [%s] · %d-day window · sports=%s",
             mode, args.days, sports)
    if not args.commit:
        log.info("(dry-run — no rows written; pass --commit to insert)")

    totals = {"games": 0, "create": 0, "reuse": 0, "pruned": 0}
    for sport in sports:
        try:
            r = ingest_sport(sport, args.days, args.commit, prune=args.prune)
            for k in totals:
                totals[k] += r.get(k, 0)
        except Exception as e:
            log.warning("%-9s ERROR: %s", sport, e)
    log.info("TOTAL %d games · %d %s · %d reused · %d pruned", totals["games"],
             totals["create"], "created" if args.commit else "would-create",
             totals["reuse"], totals["pruned"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
