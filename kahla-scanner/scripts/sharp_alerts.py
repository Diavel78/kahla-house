"""Telegram alerts for steam moves + Sharp Score 7+ thresholds.

Runs after each scanner-poll cron cycle (appended to the same workflow,
so no second cron registration needed). Reads the freshly-ingested
book_snapshots, detects:

  STEAM   — 5+ books moved the same direction on a (market, market_type,
            side) within the last ~30 min (one cron cycle). Indicates
            institutional-flow synchronization.
  SHARP7  — PIN movement on a market crossed Sharp Score ≥7. Same scoring
            logic as the on-card chip in templates/odds.html so alerts
            match what the user sees:
              ML  → |cent_distance(opener, current)| capped 10
              SPR → |point_diff|*10 + |price_diff_cents| capped 10
              TOT → same as SPR

Dedupe: writes each fired alert to the `sharp_alerts` Supabase table.
A duplicate (market_id, market_type, alert_type, side) within DEDUPE_HOURS
won't re-fire — keeps Telegram from getting spammy on big sustained moves.

Env vars (skip silently when missing):
  TELEGRAM_BOT_TOKEN   — from @BotFather
  TELEGRAM_CHAT_ID     — your user id (from /getUpdates)
  SUPABASE_URL / SUPABASE_SERVICE_KEY — already set for ingest

Run via:  python -m scripts.sharp_alerts
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

from storage import supabase_client as db

log = logging.getLogger(__name__)

# ─────────────────────────── Config ────────────────────────────
SHARP_THRESHOLD     = 7      # alert when sharp score crosses this
STEAM_BOOK_COUNT    = 5      # n books moving same direction = steam
STEAM_LOOKBACK_MIN  = 70     # how far back the "previous" snapshot can be
STEAM_RECENT_MIN    = 35     # what counts as "current"
DEDUPE_HOURS        = 6      # don't re-fire same alert inside this window
# Active games window: only consider markets whose game starts within this.
# Games already underway are still scored — closing-line freeze handles
# stale post-start data on the user-facing board, but for alerts we want
# the latest pre-start picture.
ACTIVE_WINDOW_HOURS = 24

# Sport code → display label for the alert message
_SPORT_LABEL = {
    "MLB":   "MLB",  "NBA":   "NBA",  "NHL":   "NHL",  "NFL":   "NFL",
    "CBB":   "CBB",  "NCAAF": "NCAAF","UFC":   "MMA",
}


# ──────────────────────── Math (mirrors odds.html) ──────────────────────
def _amer_to_cents(p):
    """American odds → 'cents from even money'. -110 → 10, +110 → -10.
    Lets us compute true cent-distance even if the line crosses 100.
    Mirror of _amerToCents() in templates/odds.html so alerts match the
    on-card chip score exactly."""
    if p is None:
        return None
    try:
        p = float(p)
    except (TypeError, ValueError):
        return None
    if p < 0: return -p - 100
    if p > 0: return -(p - 100)
    return 0


def _move_score_ml(opener_amer, current_amer):
    o = _amer_to_cents(opener_amer)
    c = _amer_to_cents(current_amer)
    if o is None or c is None:
        return None
    return min(10, round(abs(c - o)))


def _move_score_spr_tot(opener_line, current_line, opener_price, current_price):
    if opener_price is None or current_price is None:
        return None
    pt = abs((current_line or 0) - (opener_line or 0))
    px = abs(current_price - opener_price)
    return min(10, round(pt * 10 + px))


def _sharp_side_ml(home_diff_cents, away_diff_cents):
    """Whichever side moved MORE NEGATIVE in cents = sharp money there.
    diff = current_cents - opener_cents.  -110→-130 = +10 cents (more
    favored)."""
    if home_diff_cents is None and away_diff_cents is None: return None
    h = home_diff_cents if home_diff_cents is not None else -1e9
    a = away_diff_cents if away_diff_cents is not None else -1e9
    return "home" if h > a else "away"


def _sharp_side_spread(home_price_diff, away_price_diff,
                       home_point_diff, away_point_diff):
    """Side whose price decreased more (less attractive juice = books
    pushing money away from it = action there)."""
    h_px = home_price_diff if home_price_diff is not None else 0
    a_px = away_price_diff if away_price_diff is not None else 0
    if abs(h_px - a_px) >= 1:
        return "home" if h_px < a_px else "away"
    h_pt = home_point_diff if home_point_diff is not None else 0
    a_pt = away_point_diff if away_point_diff is not None else 0
    if h_pt < 0 and h_pt < a_pt: return "home"
    if a_pt < 0 and a_pt < h_pt: return "away"
    return None


def _sharp_side_total(over_point_diff, over_price_diff):
    """Total dropped OR Over price went UP = sharp on UNDER (books making
    Over more attractive to pull money to it, away from heavy Under)."""
    pt = over_point_diff or 0
    px = over_price_diff or 0
    if pt < 0: return "under"
    if pt > 0: return "over"
    if px > 0: return "under"   # Over got cheaper
    if px < 0: return "over"
    return None


# ──────────────────────── Telegram ────────────────────────────
def _telegram_send(token, chat_id, text):
    """POST to Telegram sendMessage. Returns True on 200, else False
    (logged). Uses Markdown so price/odds formatting renders nicely."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text":    text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }).encode()
    req = Request(url, data=payload,
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=10) as r:
            ok = (r.status == 200)
            if not ok:
                log.warning("telegram non-200: %s", r.status)
            return ok
    except (HTTPError, URLError) as e:
        log.error("telegram send failed: %s", e)
        return False


# ──────────────────────── Supabase queries ──────────────────────
def _fetch_active_markets(sb):
    now = datetime.now(timezone.utc)
    low  = (now - timedelta(hours=2)).isoformat()
    high = (now + timedelta(hours=ACTIVE_WINDOW_HOURS)).isoformat()
    try:
        rows = (sb.table("markets")
                .select("id,sport,event_name,event_start")
                .eq("status", "active")
                .gte("event_start", low)
                .lte("event_start", high)
                .order("event_start")
                .limit(500)
                .execute().data) or []
    except Exception as e:
        log.error("markets fetch failed: %s", e)
        return []
    return rows


def _fetch_recent_snaps(sb, market_ids, since_iso):
    if not market_ids: return []
    try:
        return (sb.table("book_snapshots")
                .select("market_id,book,market_type,side,price_american,line,captured_at")
                .in_("market_id", market_ids)
                .gte("captured_at", since_iso)
                .order("captured_at", desc=True)
                .limit(50000)
                .execute().data) or []
    except Exception as e:
        log.error("snapshot fetch failed: %s", e)
        return []


def _fetch_pin_openers(sb, market_ids):
    """Earliest PIN snapshot per (market_id, market_type, side). Same
    'opener' the dashboard uses — anchors the cumulative score."""
    if not market_ids: return {}
    try:
        rows = (sb.table("book_snapshots")
                .select("market_id,market_type,side,price_american,line,captured_at")
                .in_("market_id", market_ids)
                .eq("book", "PIN")
                .order("captured_at")
                .limit(50000)
                .execute().data) or []
    except Exception as e:
        log.error("openers fetch failed: %s", e)
        return {}
    out = {}
    for r in rows:
        key = (r["market_id"], r["market_type"], r["side"])
        if key not in out:
            out[key] = r
    return out


def _already_alerted(sb, market_id, market_type, alert_type, side):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=DEDUPE_HOURS)).isoformat()
    try:
        rows = (sb.table("sharp_alerts")
                .select("id")
                .eq("market_id", market_id)
                .eq("market_type", market_type)
                .eq("alert_type", alert_type)
                .eq("side", side or "")
                .gte("sent_at", cutoff)
                .limit(1)
                .execute().data) or []
    except Exception as e:
        log.warning("dedupe check failed (will send): %s", e)
        return False
    return bool(rows)


def _record_alert(sb, market_id, market_type, alert_type, side, payload):
    try:
        sb.table("sharp_alerts").insert({
            "market_id":   market_id,
            "market_type": market_type,
            "alert_type":  alert_type,
            "side":        side or "",
            "payload":     payload,
        }).execute()
    except Exception as e:
        log.warning("dedupe record failed: %s", e)


# ──────────────────────── Detection ────────────────────────────
def _split_event_name(name):
    """'Away @ Home' → (away, home)."""
    if " @ " in name:
        a, h = name.split(" @ ", 1)
        return a.strip(), h.strip()
    return None, None


def _fmt_market(market_type, side, away, home):
    """Display label e.g. 'SPR · Padres' or 'TOT · OVER'."""
    if market_type == "total":
        side_label = side.upper() if side else "?"
    else:
        side_label = home if side == "home" else away if side == "away" else (side or "?")
    return f"{_short_market(market_type)} · {side_label}"


def _short_market(mt):
    return {"moneyline": "ML", "spread": "SPR", "total": "TOT"}.get(mt, mt)


def _fmt_amer(p):
    if p is None: return "—"
    p = int(p) if abs(p - int(p)) < 0.01 else p
    return f"+{p}" if p > 0 else f"{p}"


def _fmt_pt(v):
    if v is None: return ""
    v = round(v, 1)
    return f"+{v}" if v > 0 else f"{v}"


def _direction(cur, ear, market_type):
    """Sign of movement: +1 / -1 / 0. Same composite as the JS scorer."""
    cp = cur.get("price_american")
    ep = ear.get("price_american")
    cl = cur.get("line")
    el = ear.get("line")
    if cp is None or ep is None: return 0
    if market_type == "moneyline":
        # American odds going more negative = book pulling toward favorite.
        # We want the direction of the line, which mirrors price direction.
        d = cp - ep
        return 1 if d > 0 else -1 if d < 0 else 0
    line_diff = (cl or 0) - (el or 0)
    price_diff = cp - ep
    composite = line_diff * 10 + price_diff
    return 1 if composite > 0.5 else -1 if composite < -0.5 else 0


def _detect_steam(snaps_recent, snaps_earlier, market_id):
    """Per (market_type, side), find groups of ≥STEAM_BOOK_COUNT books
    moving the same direction. Returns list of dicts."""
    # Latest snapshot per (book, market_type, side) in each window
    cur = {}
    for s in snaps_recent:
        if s["market_id"] != market_id: continue
        key = (s["book"], s["market_type"], s["side"])
        if key not in cur: cur[key] = s
    ear = {}
    for s in snaps_earlier:
        if s["market_id"] != market_id: continue
        key = (s["book"], s["market_type"], s["side"])
        if key not in ear: ear[key] = s

    # Group by (market_type, side, direction) → set of books
    grouped = {}
    for key, c in cur.items():
        e = ear.get(key)
        if not e: continue
        mt, side = key[1], key[2]
        d = _direction(c, e, mt)
        if d == 0: continue
        gk = (mt, side, d)
        grouped.setdefault(gk, []).append((c["book"], e, c))

    out = []
    for (mt, side, d), books in grouped.items():
        if len(books) >= STEAM_BOOK_COUNT:
            out.append({
                "market_type": mt,
                "side":        side,
                "direction":   d,
                "books":       [b[0] for b in books],
                "samples":     [(b[0], b[1].get("price_american"), b[2].get("price_american")) for b in books[:5]],
            })
    return out


def _compute_sharp_score(opener, current, market_type):
    """Returns int 0-10 or None."""
    if not opener or not current: return None
    if market_type == "moneyline":
        return _move_score_ml(opener.get("price_american"), current.get("price_american"))
    return _move_score_spr_tot(
        opener.get("line"), current.get("line"),
        opener.get("price_american"), current.get("price_american"),
    )


# ──────────────────────── Message format ────────────────────────────
def _fmt_local(iso_str):
    """ISO → 'h:mm AM/PM ET' for the alert. Uses Eastern since most users
    track sports there; tradeoff over per-user TZ which we don't have."""
    if not iso_str: return ""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York"))
        return dt.strftime("%-I:%M %p ET")
    except Exception:
        return iso_str[:16]


def _msg_steam(market, alert, away, home):
    sport = _SPORT_LABEL.get(market.get("sport"), market.get("sport") or "")
    direction_word = "favorite" if alert["direction"] < 0 else "underdog"
    if alert["market_type"] == "total":
        direction_word = "OVER" if alert["direction"] > 0 else "UNDER"
    sample_lines = []
    for bk, ep, cp in alert["samples"]:
        sample_lines.append(f"  {bk}: {_fmt_amer(ep)} → {_fmt_amer(cp)}")
    return (
        f"🚨 *STEAM* — {sport}\n"
        f"*{away} @ {home}* · {_fmt_local(market.get('event_start'))}\n"
        f"`{_short_market(alert['market_type'])}` · *{(home if alert['side']=='home' else away if alert['side']=='away' else alert['side']).upper()}* · "
        f"{len(alert['books'])} books → *{direction_word}*\n"
        + "\n".join(sample_lines)
    )


def _msg_sharp7(market, market_type, side, score, opener, current, away, home):
    sport = _SPORT_LABEL.get(market.get("sport"), market.get("sport") or "")
    if market_type == "total":
        side_label = (side or "").upper()
    else:
        side_label = home if side == "home" else away if side == "away" else (side or "")
    op_str = (
        f"{_fmt_amer(opener.get('price_american'))}"
        if market_type == "moneyline"
        else f"{_fmt_pt(opener.get('line'))} {_fmt_amer(opener.get('price_american'))}"
    )
    cur_str = (
        f"{_fmt_amer(current.get('price_american'))}"
        if market_type == "moneyline"
        else f"{_fmt_pt(current.get('line'))} {_fmt_amer(current.get('price_american'))}"
    )
    return (
        f"⚡ *SHARP {score}* — {sport}\n"
        f"*{away} @ {home}* · {_fmt_local(market.get('event_start'))}\n"
        f"`{_short_market(market_type)}` · *{side_label}*\n"
        f"PIN: {op_str} → *{cur_str}*"
    )


# ──────────────────────── Main ────────────────────────────
def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — exiting (no-op).")
        return 0

    sb = db.client()
    markets = _fetch_active_markets(sb)
    if not markets:
        log.info("no active markets — nothing to alert")
        return 0
    market_by_id = {m["id"]: m for m in markets}
    market_ids = list(market_by_id.keys())
    log.info("active markets: %d", len(market_ids))

    now = datetime.now(timezone.utc)
    since_iso = (now - timedelta(minutes=STEAM_LOOKBACK_MIN)).isoformat()
    cutoff_iso = (now - timedelta(minutes=STEAM_RECENT_MIN)).isoformat()

    snaps = _fetch_recent_snaps(sb, market_ids, since_iso)
    snaps_recent  = [s for s in snaps if s["captured_at"] >= cutoff_iso]
    snaps_earlier = [s for s in snaps if s["captured_at"] <  cutoff_iso]
    log.info("snaps: %d recent, %d earlier", len(snaps_recent), len(snaps_earlier))

    # PIN openers for sharp-score
    openers = _fetch_pin_openers(sb, market_ids)
    # PIN current = latest PIN snap per (market, market_type, side) in the
    # full window (recent OR earlier — whichever is freshest).
    pin_current = {}
    for s in snaps:
        if s.get("book") != "PIN": continue
        key = (s["market_id"], s["market_type"], s["side"])
        if key not in pin_current:
            pin_current[key] = s

    sent_steam = 0
    sent_sharp = 0
    for mid in market_ids:
        market = market_by_id[mid]
        away, home = _split_event_name(market.get("event_name") or "")
        if not (away and home): continue

        # ── Steam detection
        steams = _detect_steam(snaps_recent, snaps_earlier, mid)
        for alert in steams:
            if _already_alerted(sb, mid, alert["market_type"], "steam", alert["side"]):
                continue
            msg = _msg_steam(market, alert, away, home)
            if _telegram_send(token, chat_id, msg):
                sent_steam += 1
                _record_alert(sb, mid, alert["market_type"], "steam", alert["side"], {
                    "books": alert["books"], "direction": alert["direction"],
                })

        # ── Sharp 7+ detection (per market_type, on the side that fired)
        for mt in ("moneyline", "spread", "total"):
            sides = ("over", "under") if mt == "total" else ("home", "away")
            best_side, best_score, best_opener, best_cur = None, -1, None, None
            for sd in sides:
                op = openers.get((mid, mt, sd))
                cu = pin_current.get((mid, mt, sd))
                sc = _compute_sharp_score(op, cu, mt)
                if sc is not None and sc > best_score:
                    best_side, best_score, best_opener, best_cur = sd, sc, op, cu
            if best_score >= SHARP_THRESHOLD:
                if _already_alerted(sb, mid, mt, "sharp7", best_side):
                    continue
                msg = _msg_sharp7(market, mt, best_side, best_score,
                                  best_opener, best_cur, away, home)
                if _telegram_send(token, chat_id, msg):
                    sent_sharp += 1
                    _record_alert(sb, mid, mt, "sharp7", best_side, {
                        "score": best_score,
                        "opener_price": best_opener.get("price_american"),
                        "current_price": best_cur.get("price_american"),
                        "opener_line": best_opener.get("line"),
                        "current_line": best_cur.get("line"),
                    })

    log.info("alerts sent: steam=%d sharp7=%d", sent_steam, sent_sharp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
