"""Cheer Scoring — reads competition results off Varsity TV (tv.varsity.com).

Varsity TV runs on FloSports' platform. Each event's results live at
    https://tv.varsity.com/events/{event_id}/results
and the page is not behind a login. We don't know for certain which of
these the page actually carries, so `fetch_event` tries every shape and
reports what it found (`diag`):

  1. HTML <table> rows (rank / program / scores), grouped by the nearest
     preceding heading = the division name;
  2. JSON embedded in the page (__NEXT_DATA__, window.__* state,
     application/json / ld+json script blobs) walked for result-shaped
     objects;
  3. API-looking URLs referenced by the page, reported in `diag` so the
     next iteration can read the JSON API directly.

Pure functions + one HTTP read; Flask wiring lives in app.py.
"""
import json
import re
import time

import requests

BASE = "https://tv.varsity.com"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/",
}
_CACHE = {}
_CACHE_TTL_S = 120


def parse_event_ref(ref):
    """'11267707', '11267707-2024-usa-...', or a full tv.varsity.com URL
    -> (event_id, results_url). Old-style /results/{id}/{division} URLs are
    passed through as-is."""
    ref = (ref or "").strip()
    if not ref:
        return None, None
    if ref.startswith("http"):
        m = re.search(r"/events/(\d+)", ref)
        if m:
            return m.group(1), f"{BASE}/events/{m.group(1)}/results"
        m = re.search(r"/results/(\d+)", ref)
        if m:
            return m.group(1), ref.split("?")[0]
        return None, None
    m = re.match(r"(\d+)", ref)
    if not m:
        return None, None
    return m.group(1), f"{BASE}/events/{m.group(1)}/results"


def _num(s):
    s = (s or "").strip().replace(",", "")
    m = re.fullmatch(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def _clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


# ── 1. HTML tables ──────────────────────────────────────────────────────

_HEAD_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "caption")


_ROUND_RE = re.compile(r"^(finals?|prelim(inarie)?s?|preliminaries|semi[- ]?finals?|day \d+|round \d+|results?)$", re.I)


def _division_for(table):
    """Nearest heading above the table. A round label ('Finals',
    'Preliminaries') is joined to the division heading above it."""
    cap = table.find("caption")
    if cap and _clean(cap.get_text()):
        return _clean(cap.get_text())
    parts = []
    for prev in table.find_all_previous(list(_HEAD_TAGS) + ["strong", "b"]):
        t = _clean(prev.get_text())
        if not t or len(t) > 160 or t in parts:
            continue
        parts.append(t)
        if not _ROUND_RE.match(t) or len(parts) >= 3:
            break
    if not parts:
        return None
    return " · ".join(reversed(parts))


def parse_tables(soup):
    """Every <table> with a header row -> a division block of row dicts."""
    out = []
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 2:
            continue
        head = [_clean(c.get_text()) for c in rows[0].find_all(["th", "td"])]
        if not any(head):
            continue
        body = []
        for tr in rows[1:]:
            cells = [_clean(c.get_text(" ")) for c in tr.find_all(["td", "th"])]
            if not any(cells):
                continue
            body.append(cells)
        if not body:
            continue
        out.append({"division": _division_for(tbl), "columns": head, "rows": body})
    return out


# ── 2. Embedded JSON ────────────────────────────────────────────────────

_SCORE_KEYS = re.compile(r"(score|rank|place|deduction|total|points)", re.I)
_TEAM_KEYS = re.compile(r"(team|program|gym|school|name|club)", re.I)


def _script_json_blobs(soup, html):
    blobs = []
    for s in soup.find_all("script"):
        typ = (s.get("type") or "").lower()
        txt = s.string or s.get_text() or ""
        if not txt.strip():
            continue
        if s.get("id") == "__NEXT_DATA__" or "json" in typ:
            try:
                blobs.append((s.get("id") or typ or "json", json.loads(txt)))
            except Exception:
                pass
            continue
        # window.__STATE__ = {...};  /  window.__NUXT__=...
        for m in re.finditer(r"window\.(__[A-Za-z0-9_]+__|[A-Za-z_]*[Ss]tate[A-Za-z_]*)\s*=\s*", txt):
            start = m.end()
            try:
                obj, _ = json.JSONDecoder().raw_decode(txt[start:].lstrip())
                blobs.append(("window." + m.group(1), obj))
            except Exception:
                pass
    return blobs


def _walk(obj, path="", depth=0):
    if depth > 40:
        return
    yield path, obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}", depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:2000]):
            yield from _walk(v, f"{path}[{i}]", depth + 1)


def find_result_lists(blob):
    """Lists of dicts that look like result rows: a team-ish key and a
    score-ish key. Returns [(path, list)] largest first."""
    hits = []
    for path, v in _walk(blob):
        if not (isinstance(v, list) and len(v) >= 2 and all(isinstance(x, dict) for x in v[:5])):
            continue
        keys = set()
        for x in v[:5]:
            keys.update(x.keys())
        if any(_SCORE_KEYS.search(k) for k in keys) and any(_TEAM_KEYS.search(k) for k in keys):
            hits.append((path, v))
    hits.sort(key=lambda h: -len(h[1]))
    return hits


def _flat_scalars(d, prefix="", depth=0):
    out = {}
    if depth > 2 or not isinstance(d, dict):
        return out
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, (str, int, float)) or v is None:
            out[key] = v
        elif isinstance(v, dict):
            out.update(_flat_scalars(v, key + ".", depth + 1))
    return out


def json_lists_to_blocks(hits, limit=60):
    blocks = []
    seen = set()
    for path, lst in hits[:limit]:
        sig = (len(lst), json.dumps(lst[0], sort_keys=True, default=str)[:200])
        if sig in seen:
            continue
        seen.add(sig)
        flat = [_flat_scalars(x) for x in lst]
        cols = []
        for f in flat[:10]:
            for k in f:
                if k not in cols:
                    cols.append(k)
        cols = cols[:14]
        rows = [[("" if f.get(c) is None else str(f.get(c))) for c in cols] for f in flat]
        blocks.append({"division": path, "columns": cols, "rows": rows, "source": "json"})
    return blocks


# ── 3. API hints ────────────────────────────────────────────────────────

_API_RE = re.compile(r"""https?://[A-Za-z0-9.\-]*(?:flosports|varsity|flo)[A-Za-z0-9.\-]*/[^\s"'<>\\)]+""")


def api_hints(html):
    urls = sorted(set(u.rstrip(".,;") for u in _API_RE.findall(html or "")))
    keep = [u for u in urls if re.search(r"api|graphql|json|results|scores", u, re.I)]
    return keep[:40]


# ── normalize ───────────────────────────────────────────────────────────

def _col_idx(cols, *pats):
    for i, c in enumerate(cols):
        lc = c.lower()
        if any(p in lc for p in pats):
            return i
    return None


def normalize(block):
    """Pick out rank / team / final score columns when we can name them, so
    the page can render a clean standings list. Raw columns stay alongside."""
    cols = block.get("columns") or []
    ri = _col_idx(cols, "rank", "place", "pos")
    ti = _col_idx(cols, "program", "team", "gym", "school", "club", "name")
    si = None
    for pats in (("event score",), ("final",), ("total",), ("performance score",), ("score",)):
        si = _col_idx(cols, *pats)
        if si is not None:
            break
    teams = []
    for r in block.get("rows") or []:
        get = lambda i: r[i] if i is not None and i < len(r) else ""
        teams.append({"rank": get(ri), "team": get(ti) or (r[0] if r else ""),
                      "score": get(si)})
    return {**block, "teams": teams,
            "picked": {"rank": cols[ri] if ri is not None else None,
                       "team": cols[ti] if ti is not None else None,
                       "score": cols[si] if si is not None else None}}


# ── entry point ─────────────────────────────────────────────────────────

def fetch_event(ref, fresh=False):
    event_id, url = parse_event_ref(ref)
    if not url:
        return {"ok": False, "error": "Couldn't read an event id from that. Paste a tv.varsity.com event link or its number."}
    ck = url
    hit = _CACHE.get(ck)
    if hit and not fresh and time.time() - hit[0] < _CACHE_TTL_S:
        return {**hit[1], "cached": True}

    diag = {"url": url}
    t0 = time.time()
    try:
        r = requests.get(url, headers=_HEADERS, timeout=12, allow_redirects=True)
    except Exception as e:
        return {"ok": False, "event_id": event_id, "error": f"request failed: {e}", "diag": diag}
    diag.update({"status": r.status_code, "final_url": r.url, "bytes": len(r.content),
                 "ms": int((time.time() - t0) * 1000),
                 "content_type": r.headers.get("content-type"),
                 "server": r.headers.get("server")})
    html = r.text or ""
    if r.status_code != 200:
        diag["body_head"] = html[:600]
        return {"ok": False, "event_id": event_id,
                "error": f"Varsity answered HTTP {r.status_code}", "diag": diag}

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

    title = _clean(soup.title.get_text()) if soup.title else None
    og = soup.find("meta", attrs={"property": "og:title"})
    name = (og.get("content") if og else None) or title

    tables = parse_tables(soup)
    blobs = _script_json_blobs(soup, html)
    hits = []
    for _src, b in blobs:
        hits.extend(find_result_lists(b))
    hits.sort(key=lambda h: -len(h[1]))

    diag.update({
        "title": title,
        "tables": len(tables),
        "json_blobs": [s for s, _ in blobs][:20],
        "json_result_lists": [{"path": p, "n": len(l), "keys": sorted(l[0].keys())[:20]} for p, l in hits[:12]],
        "api_hints": api_hints(html),
        "script_srcs": [s.get("src") for s in soup.find_all("script") if s.get("src")][:25],
        "challenge": bool(re.search(r"cf-chl|challenge-platform|Just a moment", html[:5000])),
        "text_sample": _clean(soup.get_text(" "))[:800],
    })

    if tables:
        blocks, source = [dict(t, source="html") for t in tables], "html"
    elif hits:
        blocks, source = json_lists_to_blocks(hits), "json"
    else:
        blocks, source = [], None

    out = {"ok": bool(blocks), "event_id": event_id, "event_name": name,
           "source": source, "divisions": [normalize(b) for b in blocks],
           "fetched_at": int(time.time()), "diag": diag}
    if not blocks:
        out["error"] = "Page loaded but no score tables were found in it (see diagnostics)."
    _CACHE[ck] = (time.time(), out)
    return out


API = "https://tv-admin.varsity.com/api/experiences/web/event-hub/{eid}/results"


def fetch_event_api(event_id):
    """Varsity's own JSON behind the results page (found in the page's
    transfer state Oct 14 2026). Probe-only for now."""
    url = API.format(eid=event_id)
    params = {"version": "1.33.2", "tz": "UTC", "isEventHubLayoutEnabled": "true",
              "isEventHubBracketsEnabled": "false", "isNextGenEventHub": "false", "site_id": "20"}
    try:
        r = requests.get(url, params=params, headers={**_HEADERS, "Accept": "application/json"}, timeout=12)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    out = {"status": r.status_code, "bytes": len(r.content), "ct": r.headers.get("content-type")}
    try:
        j = r.json()
    except Exception:
        out["head"] = r.text[:1500]
        return out

    def shape(o, d=0):
        if d > 6:
            return "…"
        if isinstance(o, dict):
            return {k: shape(v, d + 1) for k, v in list(o.items())[:25]}
        if isinstance(o, list):
            return [len(o), shape(o[0], d + 1)] if o else []
        return o if not isinstance(o, str) else o[:80]
    out["shape"] = shape(j)
    return out


def list_events():
    """Event links off the public /results index -> [{id, name, url}].
    Best effort: whatever /events/{id} anchors the page carries."""
    url = BASE + "/results"
    hit = _CACHE.get(url)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        r = requests.get(url, headers=_HEADERS, timeout=12)
    except Exception as e:
        return {"ok": False, "error": f"request failed: {e}", "events": []}
    diag = {"status": r.status_code, "bytes": len(r.content)}
    if r.status_code != 200:
        return {"ok": False, "error": f"Varsity answered HTTP {r.status_code}", "events": [], "diag": diag}
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.text, "html.parser")
    seen, events = set(), []
    for a in soup.find_all("a", href=True):
        m = re.search(r"/events/(\d+)(?:-[^/?#]*)?", a["href"])
        if not m or m.group(1) in seen:
            continue
        name = _clean(a.get_text(" "))
        if not name:
            continue
        seen.add(m.group(1))
        events.append({"id": m.group(1), "name": name[:160],
                       "url": f"{BASE}/events/{m.group(1)}/results"})
    diag["api_hints"] = api_hints(r.text)
    out = {"ok": True, "events": events[:150], "diag": diag}
    _CACHE[url] = (time.time(), out)
    return out
