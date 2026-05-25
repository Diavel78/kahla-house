"""Handicapper Bot — pre-game dossier builder.

Called from the in-chat handicapper workflow. The CLI takes a freeform
team query and emits a JSON dossier on stdout that the analyst (Claude
in chat) reads to write the full write-up.

Coverage: ML / SPR / TOT only. No props.

Sources (free, no paid APIs):
  • Supabase book_snapshots — latest odds across all 14 books, plus PIN
    opener for sharp-side/score calc.
  • Action Network — public betting splits (% bets, % money).
  • ESPN public scoreboard — team records, recent results, injuries.
  • MLB Stats API — probable starting pitchers + season stats (MLB only).

CLI:
  python -m scripts.handicapper "Toronto vs Angels"
  python -m scripts.handicapper --sport mlb "Blue Jays @ Angels"
  python -m scripts.handicapper --json "Lakers vs Nuggets"     # default
  python -m scripts.handicapper --pretty "MIN @ DEN"          # human-readable

Match logic: search active markets where event_start is in the future
(or within the last 90 min — late questions are common), team-substring
match in either direction. Returns the FIRST match — most queries refer
to today's game.

This script is read-only. To log a recommendation as a pick, the analyst
calls scripts/handicapper_log_pick.py separately after writing the
analysis.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from _lib import paper_bets as pb
from _lib.normalize import american_to_prob, devig_two_way
from storage import supabase_client as db

log = logging.getLogger(__name__)

# Sports we cover. UFC included for ML picks (no SPR/TOT for fights —
# but the same dossier shape works; SPR/TOT just stay null).
SPORTS = ["MLB", "NBA", "NHL", "NFL", "NCAAF", "CBB", "UFC"]

# Sport → ESPN scoreboard path (mirrors paper_bets_resolver._ESPN_PATH).
_ESPN_PATH: dict[str, tuple[str, str]] = {
    "MLB":   ("baseball",   "mlb"),
    "NBA":   ("basketball", "nba"),
    "NHL":   ("hockey",     "nhl"),
    "NFL":   ("football",   "nfl"),
    "CBB":   ("basketball", "mens-college-basketball"),
    "NCAAF": ("football",   "college-football"),
}

# Action Network league code per sport. UFC + CBB college subset not
# supported by Action Network public-betting pages.
_ACTION_LEAGUE: dict[str, str] = {
    "MLB":   "mlb",
    "NBA":   "nba",
    "NHL":   "nhl",
    "NFL":   "nfl",
    "CBB":   "ncaab",
    "NCAAF": "ncaaf",
}

# 14-book allowlist (mirrors _ALLOWED_BOOKS in app.py + scrapers).
ALLOWED_BOOKS = {
    "PIN", "DK", "FD", "MGM", "CAE", "HR", "BET365",
    "BR", "BOL", "LV", "BVD", "ESPN", "FAN", "MB",
}
ENTRY_BOOKS = ALLOWED_BOOKS - {"PIN"}

HTTP_TIMEOUT = 10
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


# ──────────────────────────── Match resolution ────────────────────────────

def _split_event_name(name: str) -> tuple[str, str] | tuple[None, None]:
    if " @ " in name:
        a, h = name.split(" @ ", 1)
        return a.strip(), h.strip()
    return None, None


def _parse_query(query: str) -> list[str]:
    """Pull team tokens from a freeform query. Lowercased, punctuation
    stripped, common filler words dropped."""
    q = re.sub(r"[?!.,]", " ", query.lower())
    q = re.sub(r"\b(today|tonight|tomorrow|thoughts|pick|game|vs|versus|v|@|at|vs\.|on)\b",
               " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    # Split on remaining "vs" / "@" if any survived; otherwise just words.
    parts = [p.strip() for p in re.split(r"\s+vs?\s+|\s+@\s+", q) if p.strip()]
    if len(parts) >= 2:
        return parts
    return [w for w in q.split() if len(w) >= 3]


def _team_score(team_name: str, tokens: list[str]) -> int:
    """Substring-overlap score: how many query tokens appear in this team
    name. 0 = no match, higher = more confidence."""
    n = team_name.lower()
    return sum(1 for t in tokens if t in n)


def _find_market(sb, query: str, sport_hint: str | None
                 ) -> tuple[dict | None, list[dict]]:
    """Scan active markets for the best team-match. Returns (best, ranked).

    Window: event_start from -90 min (late-asks on already-started games)
    to +48h (lets you ask about tomorrow night)."""
    tokens = _parse_query(query)
    if not tokens:
        return None, []

    now = datetime.now(timezone.utc)
    after = (now - timedelta(minutes=90)).isoformat()
    before = (now + timedelta(hours=48)).isoformat()

    q = (sb.table("markets")
         .select("id,sport,event_name,event_start,status")
         .eq("status", "active")
         .gte("event_start", after)
         .lte("event_start", before)
         .order("event_start"))
    if sport_hint:
        q = q.eq("sport", sport_hint.upper())

    rows = q.limit(500).execute().data or []
    scored = []
    for m in rows:
        away, home = _split_event_name(m.get("event_name") or "")
        if not (away and home):
            continue
        s = _team_score(away, tokens) + _team_score(home, tokens)
        if s == 0:
            continue
        scored.append((s, m))
    scored.sort(key=lambda x: (-x[0], x[1]["event_start"]))
    if not scored:
        return None, []
    best = scored[0][1]
    return best, [m for _, m in scored[:5]]


# ──────────────────────────── Odds dossier ────────────────────────────

def _latest_snapshots(sb, market_id: str) -> dict:
    """Latest snapshot per (book, market_type, side) for one market —
    NO time cutoff (was 24h). PIN dedup means a steady line on PIN can
    have its only row hours or even days old (CLAUDE.md gotcha #10);
    a hard 24h cutoff would filter PIN out and show "no PIN data" while
    PIN is in fact on screen everywhere else.

    Scoped to one market_id so the result set is small — even a stable
    game has at most ~14 books × 3 markets × 2 sides ≈ 84 keys. We
    over-fetch (limit 5000) for safety, then take first-per-key from
    the desc-ordered list."""
    rows = (sb.table("book_snapshots")
            .select("book,market_type,side,price_american,line,captured_at")
            .eq("market_id", market_id)
            .order("captured_at", desc=True)
            .limit(5000)
            .execute().data) or []
    # First row per (book, market_type, side) wins (rows are desc by time).
    out: dict = {}
    for r in rows:
        if r["book"] not in ALLOWED_BOOKS:
            continue
        key = (r["book"], r["market_type"], r["side"])
        if key not in out:
            out[key] = r
    return out


# ─── Recency-weighted PIN history (Pick Bot scoring) ───────────────────
# Replaces the legacy "earliest PIN snap of all time" anchor with a
# 18h-capped rolling history. Sharp side + score are computed from the
# weighted sum of consecutive deltas, where each delta is amplified by
# how recent its newer endpoint is. Result: late steam (last 15 min)
# dominates the score, 18h-old news barely contributes.
#
# Pick Bot scope ONLY. _lib.sharp.* (used by paper_bets pickers and
# steam alerts) is unchanged — Sharp Bot keeps its own 1-12h opener
# window logic.

_PIN_HISTORY_HOURS = 18

# Same weight table as handicapper_web.py — keep them aligned so the
# web flow and the in-chat CLI produce the same score for the same data.
_RECENCY_WEIGHTS: tuple[tuple[float, float], ...] = (
    (15,   1.00),
    (60,   0.60),
    (120,  0.35),
    (360,  0.18),
    (1080, 0.08),
)


def _recency_weight(age_min: float) -> float:
    if age_min < 0:
        age_min = 0
    for cap, w in _RECENCY_WEIGHTS:
        if age_min < cap:
            return w
    return 0.0


def _amer_to_cents(p: Any) -> float | None:
    if p is None:
        return None
    try:
        p = float(p)
    except (TypeError, ValueError):
        return None
    if p < 0:
        return -p - 100
    if p > 0:
        return -(p - 100)
    return 0


def _snap_age_min(snap: dict, now: datetime) -> float:
    ts = snap.get("captured_at")
    if not ts:
        return 1e9
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return 1e9
    return max(0.0, (now - dt).total_seconds() / 60.0)


def _weighted_signed_delta(snaps: list[dict], field: str, *,
                           transform: Any = None,
                           now: datetime | None = None) -> float:
    if not snaps or len(snaps) < 2:
        return 0.0
    now = now or datetime.now(timezone.utc)
    total = 0.0
    prev_val = snaps[0].get(field)
    if transform is not None:
        prev_val = transform(prev_val)
    if prev_val is None:
        prev_val = 0
    for cur in snaps[1:]:
        cur_val = cur.get(field)
        if transform is not None:
            cur_val = transform(cur_val)
        if cur_val is None:
            continue
        delta = cur_val - prev_val
        if delta:
            total += delta * _recency_weight(_snap_age_min(cur, now))
        prev_val = cur_val
    return total


def _weighted_sharp_for_ml(home_snaps: list[dict],
                           away_snaps: list[dict]) -> tuple | None:
    now = datetime.now(timezone.utc)
    h_avail = len(home_snaps) >= 2
    a_avail = len(away_snaps) >= 2
    if not (h_avail or a_avail):
        return None
    h_w = (_weighted_signed_delta(home_snaps, "price_american",
                                   transform=_amer_to_cents, now=now)
           if h_avail else 0.0)
    a_w = (_weighted_signed_delta(away_snaps, "price_american",
                                   transform=_amer_to_cents, now=now)
           if a_avail else 0.0)
    if h_avail and a_avail:
        if abs(h_w - a_w) < 0.5:
            return None
        if h_w > a_w:
            side, snaps, weighted = "home", home_snaps, h_w
        else:
            side, snaps, weighted = "away", away_snaps, a_w
    elif h_avail:
        if h_w <= 0.5:
            return None
        side, snaps, weighted = "home", home_snaps, h_w
    else:
        if a_w <= 0.5:
            return None
        side, snaps, weighted = "away", away_snaps, a_w
    score = min(10, round(abs(weighted)))
    if score <= 0:
        return None
    return side, score, snaps[0], snaps[-1]


def _weighted_sharp_for_spread(home_snaps: list[dict],
                               away_snaps: list[dict]) -> tuple | None:
    now = datetime.now(timezone.utc)
    h_avail = len(home_snaps) >= 2
    a_avail = len(away_snaps) >= 2
    if not (h_avail or a_avail):
        return None
    h_line = _weighted_signed_delta(home_snaps, "line", now=now) if h_avail else 0.0
    a_line = _weighted_signed_delta(away_snaps, "line", now=now) if a_avail else 0.0
    h_px = (_weighted_signed_delta(home_snaps, "price_american",
                                    transform=_amer_to_cents, now=now)
            if h_avail else 0.0)
    a_px = (_weighted_signed_delta(away_snaps, "price_american",
                                    transform=_amer_to_cents, now=now)
            if a_avail else 0.0)

    if h_avail and a_avail:
        line_diff = h_line - a_line
        if abs(line_diff) >= 0.05:
            if h_line < a_line:
                side, snaps, score_mag = "home", home_snaps, abs(h_line) * 10
            else:
                side, snaps, score_mag = "away", away_snaps, abs(a_line) * 10
        else:
            px_diff = h_px - a_px
            if abs(px_diff) < 1.0:
                return None
            if h_px > a_px:
                side, snaps, score_mag = "home", home_snaps, abs(h_px)
            else:
                side, snaps, score_mag = "away", away_snaps, abs(a_px)
    else:
        ref_snaps = home_snaps if h_avail else away_snaps
        ref_is_home = h_avail
        rl = _weighted_signed_delta(ref_snaps, "line", now=now)
        rp = _weighted_signed_delta(ref_snaps, "price_american",
                                     transform=_amer_to_cents, now=now)
        if abs(rl) >= 0.05:
            ref_harder = rl < 0
            score_mag = abs(rl) * 10
        elif abs(rp) >= 1.0:
            ref_harder = rp > 0
            score_mag = abs(rp)
        else:
            return None
        if ref_is_home:
            side = "home" if ref_harder else "away"
        else:
            side = "away" if ref_harder else "home"
        if (side == "home") != ref_is_home:
            return None
        snaps = ref_snaps

    score = min(10, round(score_mag))
    if score <= 0:
        return None
    return side, score, snaps[0], snaps[-1]


def _weighted_sharp_for_total(over_snaps: list[dict],
                              under_snaps: list[dict]) -> tuple | None:
    now = datetime.now(timezone.utc)
    o_avail = len(over_snaps) >= 2
    u_avail = len(under_snaps) >= 2
    if not (o_avail or u_avail):
        return None

    def _mag(snaps: list[dict]) -> tuple[float, float]:
        ln = abs(_weighted_signed_delta(snaps, "line", now=now))
        px = abs(_weighted_signed_delta(snaps, "price_american",
                                         transform=_amer_to_cents, now=now))
        return ln, px

    if o_avail and u_avail:
        if _mag(over_snaps) >= _mag(under_snaps):
            ref_snaps, ref_is_over = over_snaps, True
        else:
            ref_snaps, ref_is_over = under_snaps, False
    elif o_avail:
        ref_snaps, ref_is_over = over_snaps, True
    else:
        ref_snaps, ref_is_over = under_snaps, False

    line_w = _weighted_signed_delta(ref_snaps, "line", now=now)
    price_w = _weighted_signed_delta(ref_snaps, "price_american",
                                       transform=_amer_to_cents, now=now)

    if abs(line_w) >= 0.05:
        side = "over" if line_w > 0 else "under"
        score_mag = abs(line_w) * 10
    elif abs(price_w) >= 1.0:
        if price_w > 0:
            side = "over" if ref_is_over else "under"
        else:
            side = "under" if ref_is_over else "over"
        score_mag = abs(price_w)
    else:
        return None

    score = min(10, round(score_mag))
    if score <= 0:
        return None
    ref_side = "over" if ref_is_over else "under"
    return side, score, ref_snaps[0], ref_snaps[-1], ref_side


def _pin_history(sb, market_id: str) -> dict[tuple[str, str], list[dict]]:
    """All PIN snapshots in the last 18h, grouped by (market_type, side),
    sorted ASC by captured_at. Replaces the legacy `_pin_opener` which
    returned only the all-time-earliest snap per side.
    """
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=_PIN_HISTORY_HOURS)).isoformat()
    rows = (sb.table("book_snapshots")
            .select("market_type,side,price_american,line,captured_at")
            .eq("market_id", market_id)
            .eq("book", "PIN")
            .gte("captured_at", cutoff)
            .order("captured_at")
            .limit(5000)
            .execute().data) or []
    out: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        key = (r["market_type"], r["side"])
        out.setdefault(key, []).append(r)
    return out


def _build_market_block(market_type: str, sides: tuple[str, str],
                        latest: dict, pin_history: dict) -> dict:
    """One block per market type. Shape:
       {pin: {a: {price, line, fair_prob}, b: {...}, devig: {a, b}},
        opener_pin: {a: {price, line}, b: {...}},
        movement: {sharp_side, score, opener_price, current_price, ...},
        best_entry: {a: {book, price, line, edge_pp}, b: {...}},
        all_books: [{book, a_price, b_price, ...}, ...]}

    `pin_history` is the new shape: {(market_type, side): [snaps...]}.
    The displayed opener is the oldest snap in the 18h window."""
    a, b = sides

    pin_a = latest.get(("PIN", market_type, a))
    pin_b = latest.get(("PIN", market_type, b))
    snaps_a = pin_history.get((market_type, a), [])
    snaps_b = pin_history.get((market_type, b), [])
    op_a  = snaps_a[0] if snaps_a else None
    op_b  = snaps_b[0] if snaps_b else None

    # Devig PIN current to fair prob per side. SPR/TOT need matched lines.
    fair_a = fair_b = None
    if pin_a and pin_b:
        line_match = True
        if market_type == "spread":
            la, lb = pin_a.get("line"), pin_b.get("line")
            line_match = la is not None and lb is not None and abs(la + lb) < 0.001
        elif market_type == "total":
            line_match = pin_a.get("line") == pin_b.get("line")
        if line_match:
            try:
                p_a = american_to_prob(int(pin_a["price_american"]))
                p_b = american_to_prob(int(pin_b["price_american"]))
                fair_a = devig_two_way(p_a, p_b)
                fair_b = 1.0 - fair_a
            except (ValueError, TypeError):
                pass

    # Recency-weighted sharp side + score. Local helpers, not _lib.sharp
    # (that one's reserved for paper_bets / steam — different window).
    if market_type == "moneyline":
        sharp_result = _weighted_sharp_for_ml(
            pin_history.get(("moneyline", "home"), []),
            pin_history.get(("moneyline", "away"), []),
        )
    elif market_type == "spread":
        sharp_result = _weighted_sharp_for_spread(
            pin_history.get(("spread", "home"), []),
            pin_history.get(("spread", "away"), []),
        )
    else:
        sharp_result = _weighted_sharp_for_total(
            pin_history.get(("total", "over"), []),
            pin_history.get(("total", "under"), []),
        )

    movement = None
    if sharp_result:
        if len(sharp_result) == 5:
            side, score, op, cu, ref_side = sharp_result
        else:
            side, score, op, cu = sharp_result
            ref_side = side
        movement = {
            "sharp_side": side,
            "sharp_score": score,
            "ref_side":   ref_side,
            "opener_price": op["price_american"],
            "opener_line":  op.get("line"),
            "opener_captured": op.get("captured_at"),
            "current_price": cu["price_american"],
            "current_line":  cu.get("line"),
            "window_hours":  _PIN_HISTORY_HOURS,
        }

    # Best non-PIN entry per side. For SPR/TOT we line-gate to PIN's
    # current line (same logic as paper_bets.find_best_entry).
    def _best_entry(side: str, fair_prob: float | None) -> dict | None:
        target_line = None
        pin_snap = pin_a if side == a else pin_b
        if pin_snap and market_type != "moneyline":
            target_line = pin_snap.get("line")
        best = None
        for book in ENTRY_BOOKS:
            snap = latest.get((book, market_type, side))
            if not snap:
                continue
            if market_type != "moneyline" and target_line is not None:
                if snap.get("line") != target_line:
                    continue
            price = snap.get("price_american")
            if price is None:
                continue
            if best is None or price > best["price_american"]:
                best = {
                    "book": book,
                    "price_american": int(price),
                    "line": snap.get("line"),
                }
        if best and fair_prob is not None:
            try:
                implied = american_to_prob(best["price_american"])
                best["edge_pp"] = round((fair_prob - implied) * 100, 2)
            except Exception:
                pass
        return best

    # All-book grid for the analyst to scan.
    all_books = []
    for book in sorted(ALLOWED_BOOKS):
        sa = latest.get((book, market_type, a))
        sb_ = latest.get((book, market_type, b))
        if not (sa or sb_):
            continue
        all_books.append({
            "book": book,
            f"{a}_price": sa["price_american"] if sa else None,
            f"{a}_line":  sa.get("line") if sa else None,
            f"{b}_price": sb_["price_american"] if sb_ else None,
            f"{b}_line":  sb_.get("line") if sb_ else None,
        })

    def _pin_block(snap: dict | None, fp: float | None) -> dict | None:
        if not snap:
            return None
        return {
            "price": int(snap["price_american"]),
            "line":  snap.get("line"),
            "fair_prob":     round(fp, 4) if fp is not None else None,
            "fair_american": _prob_to_amer(fp) if fp is not None else None,
        }

    def _opener_block(snap: dict | None) -> dict | None:
        if not snap:
            return None
        return {
            "price":      int(snap["price_american"]),
            "line":       snap.get("line"),
            "captured":   snap.get("captured_at"),
        }

    return {
        "pin_current": {a: _pin_block(pin_a, fair_a),
                        b: _pin_block(pin_b, fair_b)},
        "pin_opener":  {a: _opener_block(op_a),
                        b: _opener_block(op_b)},
        "movement":    movement,
        "best_entry":  {a: _best_entry(a, fair_a),
                        b: _best_entry(b, fair_b)},
        "all_books":   all_books,
    }


def _prob_to_amer(p: float | None) -> int | None:
    if p is None or not (0 < p < 1):
        return None
    if p >= 0.5:
        return int(round(-p / (1 - p) * 100))
    return int(round((1 - p) / p * 100))


# ──────────────────────────── Splits ────────────────────────────

def _fetch_splits(sport: str, away: str, home: str) -> dict | None:
    """Action Network public betting JSON API. Returns one event's splits
    or None. Same endpoint Flask's /api/splits hits."""
    league = _ACTION_LEAGUE.get(sport)
    if not league:
        return None
    et = ZoneInfo("America/New_York")
    today = datetime.now(et).strftime("%Y%m%d")
    url = f"https://api.actionnetwork.com/web/v2/scoreboard/{league}"
    headers = {
        "User-Agent": USER_AGENT,
        "Origin":  "https://www.actionnetwork.com",
        "Referer": "https://www.actionnetwork.com/",
    }
    try:
        r = httpx.get(url, params={"period": "game", "date": today},
                      headers=headers, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json() or {}
    except Exception as e:
        log.warning("action splits fetch failed: %s", e)
        return None

    games = data.get("games") or []
    away_n, home_n = away.lower(), home.lower()

    def _name(team_obj: dict | None) -> str:
        if not team_obj:
            return ""
        return (team_obj.get("full_name") or team_obj.get("display_name")
                or team_obj.get("short_name") or "").lower()

    for g in games:
        gh = _name(g.get("home_team"))
        ga = _name(g.get("away_team"))
        if not gh or not ga:
            continue
        if not ((home_n in gh or gh in home_n) and (away_n in ga or ga in away_n)):
            continue
        # Splits live under markets/event/insights/public_betting on
        # current shape — walk for first dict with *_percent keys.
        return _walk_splits(g)
    return None


def _walk_splits(node: Any, depth: int = 0) -> dict | None:
    """Find a dict that has at least one *_percent key for ML — recursive
    walk capped at depth 8 to avoid runaway."""
    if depth > 8 or not isinstance(node, (dict, list)):
        return None
    if isinstance(node, dict):
        keys = list(node.keys())
        # Heuristic: if this dict has bet/money percentage keys, capture.
        if any(re.search(r"(bet|ticket|money|handle).*percent", k, re.I)
               for k in keys):
            ab = _pct(node, ["away_bets_percent", "away_tickets_percent"])
            hb = _pct(node, ["home_bets_percent", "home_tickets_percent"])
            am = _pct(node, ["away_money_percent", "away_handle_percent"])
            hm = _pct(node, ["home_money_percent", "home_handle_percent"])
            if ab is not None or hb is not None or am is not None or hm is not None:
                sharp_diff = None
                if hm is not None and hb is not None:
                    sharp_diff = round(hm - hb, 1)
                return {
                    "away_bets":  ab, "home_bets":  hb,
                    "away_money": am, "home_money": hm,
                    "sharp_diff": sharp_diff,
                }
        for v in node.values():
            r = _walk_splits(v, depth + 1)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _walk_splits(v, depth + 1)
            if r:
                return r
    return None


def _pct(d: dict, keys: list[str]) -> float | None:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        try:
            return round(float(v), 1)
        except (TypeError, ValueError):
            pass
    return None


# ──────────────────────────── ESPN team data ────────────────────────────

def _espn_scoreboard(sport: str, date_yyyymmdd: str) -> list:
    pair = _ESPN_PATH.get(sport)
    if not pair:
        return []
    grp, lg = pair
    url = f"https://site.api.espn.com/apis/site/v2/sports/{grp}/{lg}/scoreboard"
    try:
        r = httpx.get(url, params={"dates": date_yyyymmdd}, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return []
        return (r.json() or {}).get("events", []) or []
    except Exception as e:
        log.warning("ESPN scoreboard %s %s failed: %s", sport, date_yyyymmdd, e)
        return []


def _espn_match_event(events: list, away: str, home: str,
                      bet_start: datetime | None) -> dict | None:
    """Two-way substring + ±90min match (mirrors paper_bets_resolver).
    Accents stripped so "Montréal Canadiens" matches ESPN's "Montreal"."""
    import unicodedata
    def _na(s: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                       if unicodedata.category(c) != "Mn").lower()
    away_n, home_n = _na(away), _na(home)
    for g in events:
        comp = (g.get("competitions") or [{}])[0]
        cs = comp.get("competitors") or []
        if len(cs) != 2:
            continue
        h = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
        a = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
        h_name = _na((h.get("team") or {}).get("displayName") or "")
        a_name = _na((a.get("team") or {}).get("displayName") or "")
        if not h_name or not a_name:
            continue
        if not ((home_n in h_name or h_name in home_n) and
                (away_n in a_name or a_name in away_n)):
            continue
        comp_dt_s = comp.get("date") or g.get("date") or ""
        try:
            comp_dt = datetime.fromisoformat(comp_dt_s.replace("Z", "+00:00"))
        except Exception:
            comp_dt = None
        if bet_start and comp_dt:
            if abs((bet_start - comp_dt).total_seconds()) > 90 * 60:
                continue
        return {"event": g, "home": h, "away": a}
    return None


def _team_block(comp_team: dict | None) -> dict:
    """Extract team-level info from an ESPN competitor object."""
    if not comp_team:
        return {}
    team = comp_team.get("team") or {}
    records = comp_team.get("records") or []
    overall = next((r for r in records if r.get("type") == "total"), records[0] if records else None)
    return {
        "id":           team.get("id"),
        "name":         team.get("displayName"),
        "abbreviation": team.get("abbreviation"),
        "record":       (overall or {}).get("summary") if overall else None,
        "score":        comp_team.get("score"),
    }


def _espn_team_injuries(sport: str, team_id: str | None) -> list:
    """ESPN injuries endpoint. Returns [{name, status, comment}] or []."""
    if not team_id:
        return []
    pair = _ESPN_PATH.get(sport)
    if not pair:
        return []
    grp, lg = pair
    url = (f"https://site.web.api.espn.com/apis/site/v2/sports/"
           f"{grp}/{lg}/teams/{team_id}/injuries")
    try:
        r = httpx.get(url, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return []
        data = r.json() or {}
    except Exception as e:
        log.warning("ESPN injuries %s %s failed: %s", sport, team_id, e)
        return []
    # ESPN injuries shape varies: flat list under "items", under
    # "injuries", or grouped per team ({team..., injuries:[...]}). Reading
    # only "items" left the list empty for every sport. Flatten all shapes.
    raw = data.get("injuries")
    if raw is None:
        raw = data.get("items") or []
    records: list = []
    for el in (raw or []):
        if not isinstance(el, dict):
            continue
        if el.get("athlete"):
            records.append(el)
        elif isinstance(el.get("injuries"), list):
            records.extend(x for x in el["injuries"] if isinstance(x, dict))
        else:
            records.append(el)
    out = []
    for it in records[:25]:
        ath = it.get("athlete") or {}
        out.append({
            "name":   ath.get("displayName") or ath.get("shortName"),
            "pos":    ((ath.get("position") or {}).get("abbreviation")),
            "status": it.get("status"),
            "type":   (it.get("type") or {}).get("description"),
            "detail": it.get("shortComment") or it.get("longComment"),
            "date":   it.get("date"),
        })
    return out


def _espn_score_int(raw) -> int | None:
    # ESPN's team-SCHEDULE endpoint returns competitor score as an object
    # ({"value": 5.0, "displayValue": "5"}), not the plain string the
    # scoreboard endpoint gives. Handle dict / str / number uniformly.
    if isinstance(raw, dict):
        raw = raw.get("value", raw.get("displayValue"))
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        return None


def _espn_team_recent(sport: str, team_id: str | None, n: int = 10) -> list:
    """Last N completed games for a team. Uses team schedule endpoint."""
    if not team_id:
        return []
    pair = _ESPN_PATH.get(sport)
    if not pair:
        return []
    grp, lg = pair
    url = (f"https://site.web.api.espn.com/apis/site/v2/sports/"
           f"{grp}/{lg}/teams/{team_id}/schedule")
    try:
        r = httpx.get(url, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return []
        events = (r.json() or {}).get("events", []) or []
    except Exception as e:
        log.warning("ESPN schedule %s %s failed: %s", sport, team_id, e)
        return []
    out = []
    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        state = ((comp.get("status") or {}).get("type") or {}).get("state")
        if state != "post":
            continue
        cs = comp.get("competitors") or []
        if len(cs) != 2:
            continue
        h = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
        a = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
        me = next((c for c in cs if (c.get("team") or {}).get("id") == team_id), None)
        opp = next((c for c in cs if (c.get("team") or {}).get("id") != team_id), None)
        if not (me and opp):
            continue
        me_score = _espn_score_int(me.get("score"))
        opp_score = _espn_score_int(opp.get("score"))
        if me_score is None or opp_score is None:
            continue
        result = "W" if me_score > opp_score else ("L" if me_score < opp_score else "T")
        out.append({
            "date":   comp.get("date") or ev.get("date"),
            "vs":     (opp.get("team") or {}).get("abbreviation"),
            "home":   me.get("homeAway") == "home",
            "score":  f"{me_score}-{opp_score}",
            "result": result,
        })
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:n]


# ──────────────────────────── MLB Stats API ────────────────────────────

def _mlb_probables(event_iso: str, away: str, home: str) -> dict:
    """Probable starting pitchers for an MLB game. Free, no auth.
    Returns {away: {...}, home: {...}} with pitcher name + season stats."""
    try:
        dt = datetime.fromisoformat(event_iso.replace("Z", "+00:00"))
    except Exception:
        return {}
    et = ZoneInfo("America/New_York")
    date_str = dt.astimezone(et).strftime("%Y-%m-%d")
    url = "https://statsapi.mlb.com/api/v1/schedule"
    params = {
        "sportId": 1, "date": date_str,
        "hydrate": "probablePitcher,team,game(content(media(epg)))",
    }
    try:
        r = httpx.get(url, params=params, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return {}
        data = r.json() or {}
    except Exception as e:
        log.warning("MLB schedule fetch failed: %s", e)
        return {}

    away_n, home_n = away.lower(), home.lower()
    target = dt   # parsed event_iso from above
    # Collect EVERY game matching by team name. A doubleheader returns two
    # (same teams, same day) — pick the one whose scheduled start is closest
    # to this dossier's event_start so game 2 doesn't inherit game 1's
    # probable pitchers.
    cands = []
    for d in data.get("dates", []) or []:
        for g in d.get("games", []) or []:
            home_t = ((g.get("teams") or {}).get("home") or {}).get("team", {})
            away_t = ((g.get("teams") or {}).get("away") or {}).get("team", {})
            hn = (home_t.get("name") or "").lower()
            an = (away_t.get("name") or "").lower()
            if not hn or not an:
                continue
            if not ((home_n in hn or hn in home_n) and
                    (away_n in an or an in away_n)):
                continue
            cands.append(g)
    if not cands:
        return {}
    g = cands[0]
    if target and len(cands) > 1:
        dated = []
        for c in cands:
            try:
                gd = datetime.fromisoformat((c.get("gameDate") or "").replace("Z", "+00:00"))
                dated.append((c, gd))
            except Exception:
                continue
        if dated:
            g = min(dated, key=lambda x: abs((x[1] - target).total_seconds()))[0]
    return {
        "venue": (g.get("venue") or {}).get("name"),
        "away":  _mlb_pitcher_block(((g.get("teams") or {}).get("away") or {}).get("probablePitcher")),
        "home":  _mlb_pitcher_block(((g.get("teams") or {}).get("home") or {}).get("probablePitcher")),
    }


def _mlb_pitcher_block(p: dict | None) -> dict:
    if not p:
        return {}
    pid = p.get("id")
    block = {
        "name":   p.get("fullName"),
        "id":     pid,
        # MLB Stats API's `probablePitcher` hydrate doesn't include
        # pitchHand by default — we backfill it from the person endpoint
        # below. Keep this as a first-pass fallback in case the schedule
        # response ever DOES include it.
        "throws": ((p.get("pitchHand") or {}).get("code")),
    }
    if not pid:
        return block
    # Single combined call: person details + season pitching stats.
    # `hydrate=stats(...)` nests the stats into the person response, so
    # we get throws + ERA/WHIP/etc in one round-trip.
    season = datetime.now(timezone.utc).year
    url = f"https://statsapi.mlb.com/api/v1/people/{pid}"
    params = {"hydrate": f"stats(group=[pitching],type=[season],season={season})"}
    try:
        r = httpx.get(url, params=params, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return block
        data = r.json() or {}
    except Exception:
        return block
    people = data.get("people") or []
    if not people:
        return block
    person = people[0]
    if not block["throws"]:
        block["throws"] = ((person.get("pitchHand") or {}).get("code"))
    stats_blocks = person.get("stats") or []
    splits = (stats_blocks[0].get("splits") if stats_blocks else []) or []
    if splits:
        s = splits[0].get("stat") or {}
        block.update({
            "era":     s.get("era"),
            "whip":    s.get("whip"),
            "ip":      s.get("inningsPitched"),
            "k":       s.get("strikeOuts"),
            "bb":      s.get("baseOnBalls"),
            "k_per_9": s.get("strikeoutsPer9Inn"),
            "bb_per_9": s.get("walksPer9Inn"),
            "hr_per_9": s.get("homeRunsPer9"),
            "record":  f"{s.get('wins', 0)}-{s.get('losses', 0)}",
        })
    return block


# ──────────────────────────── Dossier assembler ────────────────────────────

def build_dossier(sb, query: str, sport_hint: str | None) -> dict:
    """Top-level call. Returns a dossier dict shaped for the analyst."""
    market, alts = _find_market(sb, query, sport_hint)
    if not market:
        return {
            "ok": False,
            "error": "no market matched query",
            "query": query,
            "tokens": _parse_query(query),
            "hint": "Check spelling, or try 'Yankees vs Red Sox' style.",
        }

    sport = market["sport"]
    away, home = _split_event_name(market["event_name"])
    event_start = market["event_start"]
    try:
        bet_dt = datetime.fromisoformat(event_start.replace("Z", "+00:00"))
        starts_in_min = round((bet_dt - datetime.now(timezone.utc)).total_seconds() / 60)
    except Exception:
        bet_dt = None
        starts_in_min = None

    # Odds: ML / SPR / TOT
    latest = _latest_snapshots(sb, market["id"])
    pin_op = _pin_history(sb, market["id"])
    odds = {
        "moneyline": _build_market_block("moneyline", ("away", "home"),
                                          latest, pin_op),
        "spread":    _build_market_block("spread",    ("away", "home"),
                                          latest, pin_op),
        "total":     _build_market_block("total",     ("over", "under"),
                                          latest, pin_op),
    }

    # Splits
    splits = _fetch_splits(sport, away, home) if (away and home) else None

    # ESPN team data
    espn_block: dict = {}
    if sport in _ESPN_PATH and bet_dt and away and home:
        date_key = bet_dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")
        events = _espn_scoreboard(sport, date_key)
        m = _espn_match_event(events, away, home, bet_dt)
        if m:
            home_t = _team_block(m["home"])
            away_t = _team_block(m["away"])
            home_t["injuries"] = _espn_team_injuries(sport, home_t.get("id"))
            away_t["injuries"] = _espn_team_injuries(sport, away_t.get("id"))
            home_t["recent"]   = _espn_team_recent(sport, home_t.get("id"))
            away_t["recent"]   = _espn_team_recent(sport, away_t.get("id"))
            comp = (m["event"].get("competitions") or [{}])[0]
            espn_block = {
                "venue":        ((comp.get("venue") or {}).get("fullName")),
                "broadcasts":   [b.get("names") for b in comp.get("broadcasts") or [] if b.get("names")],
                "home":         home_t,
                "away":         away_t,
            }

    # MLB-specific: probable pitchers
    mlb_extra = {}
    if sport == "MLB" and event_start and away and home:
        mlb_extra["probable_pitchers"] = _mlb_probables(event_start, away, home)

    return {
        "ok":              True,
        "query":           query,
        "market_id":       market["id"],
        "sport":           sport,
        "event_name":      market["event_name"],
        "event_start_utc": event_start,
        "starts_in_min":   starts_in_min,
        "away":            away,
        "home":            home,
        "venue":           espn_block.get("venue"),
        "odds":            odds,
        "splits":          splits,
        "espn":            {
            "home": espn_block.get("home"),
            "away": espn_block.get("away"),
            "broadcasts": espn_block.get("broadcasts"),
        } if espn_block else None,
        "mlb":             mlb_extra or None,
        "alt_matches":     [
            {"id": m["id"], "event_name": m["event_name"],
             "event_start": m["event_start"], "sport": m["sport"]}
            for m in alts[1:]
        ],
        "generated_at":    datetime.now(timezone.utc).isoformat(),
    }


# ──────────────────────────── Pretty printer ────────────────────────────

def _pretty(d: dict) -> str:
    if not d.get("ok"):
        return f"NO MATCH for: {d.get('query')!r}\n  hint: {d.get('hint')}"
    lines = []
    lines.append(f"=== {d['event_name']} ({d['sport']}) ===")
    lines.append(f"Starts: {d['event_start_utc']}  (in {d['starts_in_min']} min)")
    if d.get("venue"):
        lines.append(f"Venue:  {d['venue']}")
    lines.append("")
    for mt in ("moneyline", "spread", "total"):
        blk = d["odds"].get(mt) or {}
        if not blk.get("pin_current"):
            continue
        lines.append(f"-- {mt.upper()} --")
        mv = blk.get("movement")
        if mv:
            o_line = mv.get("opener_line")
            c_line = mv.get("current_line")
            o_frag = f" ({o_line:+g})" if o_line is not None else ""
            c_frag = f" ({c_line:+g})" if c_line is not None else ""
            lines.append(
                f"  PIN: opener {mv['opener_price']:+d}{o_frag}"
                f"  →  current {mv['current_price']:+d}{c_frag}"
                f"   [SHARP {mv['sharp_side'].upper()} = {mv['sharp_score']}]"
            )
        for side, snap in (blk.get("pin_current") or {}).items():
            if not snap:
                continue
            ln = snap.get("line")
            line_frag = f" @ {ln:+g}" if ln is not None else ""
            fair = snap.get("fair_american")
            fair_frag = f", fair {fair:+d}" if fair is not None else ""
            lines.append(
                f"  PIN {side}: {snap['price']:+d}{line_frag}{fair_frag}"
            )
        for side, best in (blk.get("best_entry") or {}).items():
            if not best:
                continue
            ln = best.get("line")
            line_frag = f" @ {ln:+g}" if ln is not None else ""
            edge_pp = best.get("edge_pp")
            if edge_pp is None:
                edge_frag = ""
            elif edge_pp > 0:
                edge_frag = f" (+{edge_pp:.1f}pp edge)"
            else:
                edge_frag = f" ({edge_pp:+.1f}pp)"
            lines.append(
                f"  Best {side}: {best['price_american']:+d}{line_frag}"
                f" [{best['book']}]{edge_frag}"
            )
        lines.append("")
    if d.get("splits"):
        sp = d["splits"]
        lines.append(f"Splits ML: bets {sp.get('away_bets')}/{sp.get('home_bets')}  "
                     f"money {sp.get('away_money')}/{sp.get('home_money')}  "
                     f"(sharp_diff {sp.get('sharp_diff')})")
    if d.get("espn"):
        for which in ("away", "home"):
            t = d["espn"].get(which) or {}
            lines.append(f"{which.title()}: {t.get('name')}  ({t.get('record')})")
            inj = t.get("injuries") or []
            if inj:
                lines.append(f"  Injuries ({len(inj)}):")
                for it in inj[:8]:
                    lines.append(f"    • {it.get('name')} ({it.get('pos') or '?'}) "
                                 f"— {it.get('status')} — {it.get('detail') or ''}")
            rec = t.get("recent") or []
            if rec:
                lines.append("  Last 10: " + " ".join(f"{r['result']}({r['score']})"
                                                       for r in rec[:10]))
    if d.get("mlb") and d["mlb"].get("probable_pitchers"):
        pp = d["mlb"]["probable_pitchers"]
        lines.append("Probable Pitchers:")
        for which in ("away", "home"):
            p = pp.get(which) or {}
            if p.get("name"):
                lines.append(f"  {which.title()}: {p['name']} ({p.get('throws')})  "
                             f"ERA {p.get('era')}  WHIP {p.get('whip')}  "
                             f"K/9 {p.get('k_per_9')}  IP {p.get('ip')}")
    return "\n".join(lines)


# ──────────────────────────── CLI ────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="handicapper")
    parser.add_argument("query", nargs="+", help="Game query, e.g. 'Toronto vs Angels'")
    parser.add_argument("--sport", help="Sport hint: MLB / NBA / NFL / etc.")
    parser.add_argument("--pretty", action="store_true",
                        help="Human-readable output (default: JSON)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    query = " ".join(args.query)
    sb = db.client()
    dossier = build_dossier(sb, query, args.sport)
    if args.pretty:
        print(_pretty(dossier))
    else:
        print(json.dumps(dossier, default=str, indent=2))
    return 0 if dossier.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
