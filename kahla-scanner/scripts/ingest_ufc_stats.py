"""Ingest the UFC fight-history + fighter-stats spine from UFCStats.com.

Phase 1 of the Fight IQ model (July 2026). UFCStats is the canonical free
MMA dataset — static, decade-stable HTML: a completed-events index, one
page per event (every bout with result/method/round/time), and one page
per fighter (career rates: SLpM, TD avg/def, sub avg + frame: reach,
stance, DOB). We scrape politely (~1 req/sec, identified UA) from GitHub
Actions egress and upsert into Supabase `ufc_fighters` / `ufc_fights`
(DDL: kahla-scanner/supabase/ufc_stats.sql).

Modes (combine freely; default is DRY-RUN — nothing writes without --commit):
  --probe             reachability + parse sanity: fetch the events index,
                      one event page, one fighter page; print what parsed.
                      THE FIRST THING TO RUN — the sandbox can't reach
                      UFCStats, so this log is how we verify the shape.
  --events            ingest completed events + their bouts (newest first,
                      skipping event_ids already stored → resumable
                      backfill AND the weekly delta are the same code).
  --fighters          roster sweep (26 index pages, cheap) + detail pages
                      for fighters not yet detail-scraped (career stats).
  --limit N           cap detail-page fetches per run (0 = no cap).
  --commit            actually write.

Backfill: --events --fighters --commit  (~700 event pages + ~4k fighter
pages at 0.7s spacing ≈ under an hour; safe to re-run — it resumes).
Weekly:  same flags with --limit 80 (one new event + its fighters).
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from storage import supabase_client as db

log = logging.getLogger(__name__)

BASE = "http://www.ufcstats.com"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 kahla-house-research")
_SLEEP = 0.7          # politeness between requests
_TRIES = 3


def _get(client: httpx.Client, url: str) -> str | None:
    """Polite GET with retries. Returns HTML text or None."""
    for attempt in range(_TRIES):
        try:
            r = client.get(url, timeout=20, follow_redirects=True)
            if r.status_code == 200 and r.text:
                time.sleep(_SLEEP)
                return r.text
            log.warning("HTTP %s %s (try %d)", r.status_code, url, attempt + 1)
        except Exception as e:
            log.warning("GET %s failed (try %d): %s", url, attempt + 1, e)
        time.sleep(1.5 * (attempt + 1))
    return None


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _tail_id(href: str) -> str:
    return (href or "").rstrip("/").rsplit("/", 1)[-1]


def _parse_date(s: str):
    """'July 04, 2026' / 'Jul 19, 1988' → ISO date string or None."""
    s = (s or "").strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _inches(s: str):
    """5' 11" → 71 ; 72" → 72 ; '--' → None."""
    s = (s or "").strip()
    m = re.match(r"(\d+)'\s*(\d+)", s)
    if m:
        return int(m.group(1)) * 12 + int(m.group(2))
    m = re.match(r"(\d+)\s*\"", s)
    if m:
        return int(m.group(1))
    return None


def _pct(s: str):
    m = re.search(r"(\d+)\s*%", s or "")
    return round(int(m.group(1)) / 100.0, 3) if m else None


def _num(s: str):
    m = re.search(r"-?\d+(?:\.\d+)?", s or "")
    return float(m.group(0)) if m else None


# ───────────────────────── events + fights ─────────────────────────

def fetch_completed_events(client) -> list[dict]:
    """[{id, name, date}] newest-first from the completed-events index."""
    html = _get(client, f"{BASE}/statistics/events/completed?page=all")
    if not html:
        return []
    out = []
    today = datetime.now(timezone.utc).date().isoformat()
    for tr in _soup(html).select("tr.b-statistics__table-row"):
        a = tr.select_one("a[href*='event-details']")
        d = tr.select_one("span.b-statistics__date")
        if not a or not d:
            continue
        date = _parse_date(d.get_text(strip=True))
        if not date or date > today:      # index's top row can be the NEXT event
            continue
        out.append({"id": _tail_id(a.get("href")),
                    "name": a.get_text(strip=True), "date": date})
    return out


def parse_event_fights(html: str, ev: dict) -> list[dict]:
    """Bout rows from one event page → ufc_fights rows."""
    rows = []
    for tr in _soup(html).select("tr.b-fight-details__table-row[data-link]"):
        fid = _tail_id(tr.get("data-link"))
        tds = tr.select("td")
        if not fid or len(tds) < 10:
            continue
        flags = [i.get_text(strip=True).lower()
                 for i in tds[0].select("i.b-flag__text")]
        fighters = [( _tail_id(a.get("href")), a.get_text(strip=True))
                    for a in tds[1].select("a[href*='fighter-details']")]
        if len(fighters) != 2:
            continue
        result, winner_id = "win", fighters[0][0]
        if "draw" in flags:
            result, winner_id = "draw", None
        elif "nc" in flags:
            result, winner_id = "nc", None
        wc = tds[6].get_text(" ", strip=True)
        method = tds[7].get_text(" ", strip=True)
        rnd = _num(tds[8].get_text(strip=True))
        rows.append({
            "id": fid, "event_id": ev["id"], "event_name": ev["name"],
            "event_date": ev["date"],
            "fighter1_id": fighters[0][0], "fighter1_name": fighters[0][1],
            "fighter2_id": fighters[1][0], "fighter2_name": fighters[1][1],
            "winner_id": winner_id, "result": result,
            "weight_class": wc, "method": method,
            "round": int(rnd) if rnd else None,
            "fight_time": tds[9].get_text(strip=True) or None,
        })
    return rows


def ingest_events(client, sb, commit: bool, limit: int) -> dict:
    events = fetch_completed_events(client)
    log.info("events index: %d completed events", len(events))
    have: set = set()
    try:
        got = sb.table("ufc_fights").select("event_id").execute().data or []
        have = {r["event_id"] for r in got}
    except Exception as e:
        log.warning("event_id preload failed (%s) — treating all as new", e)
    new = [e for e in events if e["id"] not in have]
    if limit:
        new = new[:limit]
    log.info("events to scrape: %d (already have %d)", len(new), len(have))
    fights, fighter_ids, scraped = [], set(), 0
    for ev in new:
        html = _get(client, f"{BASE}/event-details/{ev['id']}")
        if not html:
            continue
        rows = parse_event_fights(html, ev)
        fights.extend(rows)
        for r in rows:
            fighter_ids.add(r["fighter1_id"])
            fighter_ids.add(r["fighter2_id"])
        scraped += 1
        if scraped % 25 == 0:
            log.info("  ...%d/%d events (%d bouts so far)", scraped, len(new), len(fights))
            if commit and fights:            # checkpoint — backfill is resumable
                sb.table("ufc_fights").upsert(fights, on_conflict="id").execute()
                fights = []
    if commit and fights:
        sb.table("ufc_fights").upsert(fights, on_conflict="id").execute()
    log.info("events done: %d scraped, %d bouts%s", scraped,
             len(fights) if not commit else 0,
             "" if commit else " (dry-run, nothing written)")
    return {"events_scraped": scraped, "fighters_seen": fighter_ids}


# ───────────────────────── fighters ─────────────────────────

def roster_sweep(client, sb, commit: bool) -> int:
    """26 index pages → upsert id/name/frame/record for every listed
    fighter WITHOUT touching detail fields (career rates come from the
    per-fighter page). Cheap enough to run every time."""
    n = 0
    for ch in "abcdefghijklmnopqrstuvwxyz":
        html = _get(client, f"{BASE}/statistics/fighters?char={ch}&page=all")
        if not html:
            continue
        batch = []
        for tr in _soup(html).select("tr.b-statistics__table-row"):
            links = tr.select("a[href*='fighter-details']")
            tds = tr.select("td")
            if not links or len(tds) < 10:
                continue
            fid = _tail_id(links[0].get("href"))
            name = " ".join(a.get_text(strip=True) for a in links[:2]).strip()
            if not fid or not name:
                continue
            batch.append({
                "id": fid, "name": name,
                "height_in": _inches(tds[3].get_text(strip=True)),
                "reach_in": _inches(tds[5].get_text(strip=True)),
                "stance": tds[6].get_text(strip=True) or None,
                "wins": int(_num(tds[7].get_text()) or 0),
                "losses": int(_num(tds[8].get_text()) or 0),
                "draws": int(_num(tds[9].get_text()) or 0),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        n += len(batch)
        if commit and batch:
            sb.table("ufc_fighters").upsert(batch, on_conflict="id").execute()
    log.info("roster sweep: %d fighters%s", n, "" if commit else " (dry-run)")
    return n


# Keys normalized lowercase with TRAILING periods stripped — UFCStats'
# labels are inconsistent about them ("Str. Def" vs "TD Avg."), so we
# never match on the trailing dot.
_STAT_KEYS = {
    "slpm": "slpm", "str. acc": "str_acc", "sapm": "sapm",
    "str. def": "str_def", "td avg": "td_avg", "td acc": "td_acc",
    "td def": "td_def", "sub. avg": "sub_avg",
}
_PCT_KEYS = {"str_acc", "str_def", "td_acc", "td_def"}


def parse_fighter_detail(html: str, fid: str) -> dict:
    """Career rates + DOB + frame from one fighter page."""
    soup = _soup(html)
    row: dict = {"id": fid,
                 "detail_scraped_at": datetime.now(timezone.utc).isoformat(),
                 "updated_at": datetime.now(timezone.utc).isoformat()}
    name = soup.select_one("span.b-content__title-highlight")
    if name:
        row["name"] = name.get_text(strip=True)
    rec = soup.select_one("span.b-content__title-record")
    if rec:
        m = re.search(r"(\d+)-(\d+)-(\d+)", rec.get_text())
        if m:
            row["wins"], row["losses"], row["draws"] = (int(m.group(1)),
                                                        int(m.group(2)),
                                                        int(m.group(3)))
    for li in soup.select("li.b-list__box-list-item"):
        txt = li.get_text(" ", strip=True)
        if ":" not in txt:
            continue
        key, _, val = txt.partition(":")
        key, val = key.strip().lower().rstrip("."), val.strip()
        if key == "height":
            row["height_in"] = _inches(val)
        elif key == "reach":
            row["reach_in"] = _inches(val)
        elif key == "stance":
            row["stance"] = val or None
        elif key == "dob":
            row["dob"] = _parse_date(val)
        elif key in _STAT_KEYS:
            col = _STAT_KEYS[key]
            row[col] = _pct(val) if col in _PCT_KEYS else _num(val)
    return row


def ingest_fighter_details(client, sb, commit: bool, limit: int,
                           priority_ids: set | None = None) -> int:
    """Detail pages for fighters not yet detail-scraped. `priority_ids`
    (fighters seen in freshly ingested bouts) go first so the weekly
    delta refreshes exactly who just fought."""
    todo: list[str] = []
    try:
        rows = (sb.table("ufc_fighters").select("id")
                .is_("detail_scraped_at", "null").execute().data) or []
        todo = [r["id"] for r in rows]
    except Exception as e:
        log.warning("detail todo query failed: %s", e)
    pri = [i for i in (priority_ids or set()) if i]
    ordered = list(dict.fromkeys(pri + todo))       # priority first, deduped
    if limit:
        ordered = ordered[:limit]
    log.info("fighter details to scrape: %d (priority %d)", len(ordered), len(pri))
    done = 0
    batch: list[dict] = []
    for fid in ordered:
        html = _get(client, f"{BASE}/fighter-details/{fid}")
        if not html:
            continue
        batch.append(parse_fighter_detail(html, fid))
        done += 1
        if len(batch) >= 50:
            if commit:
                sb.table("ufc_fighters").upsert(batch, on_conflict="id").execute()
            log.info("  ...%d/%d fighter pages", done, len(ordered))
            batch = []
    if commit and batch:
        sb.table("ufc_fighters").upsert(batch, on_conflict="id").execute()
    log.info("fighter details done: %d%s", done, "" if commit else " (dry-run)")
    return done


# ───────────────────────── probe ─────────────────────────

def probe(client) -> int:
    """Reachability + parse sanity. Prints samples; exits non-zero on a
    hard failure so the Action run flags red."""
    events = fetch_completed_events(client)
    print(f"PROBE events index: {len(events)} completed events")
    if not events:
        print("PROBE FAIL: events index unreachable or parsed to zero")
        return 2
    print(f"  newest: {events[0]}")
    ev = events[0]
    html = _get(client, f"{BASE}/event-details/{ev['id']}")
    fights = parse_event_fights(html, ev) if html else []
    print(f"PROBE event page: {len(fights)} bouts parsed")
    for f in fights[:3]:
        print(f"  {f['fighter1_name']} vs {f['fighter2_name']} · {f['result']}"
              f" · {f['method']} R{f['round']} · {f['weight_class']}")
    if not fights:
        print("PROBE FAIL: event page parsed to zero bouts")
        return 2
    fid = fights[0]["fighter1_id"]
    fh = _get(client, f"{BASE}/fighter-details/{fid}")
    detail = parse_fighter_detail(fh, fid) if fh else {}
    print(f"PROBE fighter page ({fid}): "
          f"{ {k: v for k, v in detail.items() if k not in ('updated_at', 'detail_scraped_at')} }")
    core = [k for k in ("slpm", "td_avg", "dob", "reach_in") if detail.get(k) is not None]
    if len(core) < 2:
        print("PROBE WARN: fighter career stats mostly empty — check _STAT_KEYS vs live labels")
        return 1
    print("PROBE OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--events", action="store_true")
    ap.add_argument("--fighters", action="store_true")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap event/fighter detail pages per run (0 = all)")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    client = httpx.Client(headers={"User-Agent": _UA})
    if args.probe:
        return probe(client)

    sb = db.client()
    seen: set = set()
    if args.events:
        r = ingest_events(client, sb, args.commit, args.limit)
        seen = r.get("fighters_seen") or set()
    if args.fighters:
        roster_sweep(client, sb, args.commit)
        ingest_fighter_details(client, sb, args.commit, args.limit,
                               priority_ids=seen)
    if not (args.events or args.fighters):
        log.error("nothing to do — pass --probe, --events and/or --fighters")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
