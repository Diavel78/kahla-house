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
        if parts and _ROUND_RE.match(t):
            continue  # the previous table's round label, not ours
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

def fetch_event_page(ref, fresh=False):
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
    if name:
        name = re.sub(r"\s*-\s*Varsity\s*-\s*Varsity TV\s*$", "", name)

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


def fetch_event_api(event_id, find=None):
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
    # Where do the scores live? Paths to a known team string, plus every
    # follow-up apiCall / results-ish URL the payload references.
    needle = (find or "").lower()
    hits, calls = [], []
    for path, v in _walk(j):
        if isinstance(v, str):
            if needle and needle in v.lower() and len(hits) < 8:
                hits.append(path)
            if ("api" in v or "/results" in v) and v.startswith(("http", "/")) and len(calls) < 60:
                calls.append(v)
        if isinstance(v, dict) and v.get("apiCall") and len(calls) < 60:
            calls.append(json.dumps(v.get("apiCall"))[:400])
    out["hits"] = hits
    out["calls"] = sorted(set(calls))
    if hits:
        # the parent objects of the first hit, from the outside in
        p = hits[0]
        segs = re.findall(r"\.([^.\[]+)|\[(\d+)\]", p)
        node, chain = j, []
        for k, i in segs:
            node = node[k] if k else node[int(i)]
            chain.append(node)
        for anc in reversed(chain[:-1]):
            if isinstance(anc, dict) and len(json.dumps(anc)) > 600:
                out["hit_parent"] = json.dumps(anc)[:6000]
                break
    return out


def list_events(date=None):
    """Event links off the public /results index -> [{id, name, url}].
    Best effort: whatever /events/{id} anchors the page carries."""
    url = BASE + "/results" + (f"?date={date}" if date else "")
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
    diag["api_hints"] = [h[:400] for h in api_hints(r.text) if "results/tab" in h]
    out = {"ok": True, "events": events[:150], "diag": diag}
    _CACHE[url] = (time.time(), out)
    return out


_API_PARAMS = {"version": "1.33.2", "tz": "UTC", "isEventHubLayoutEnabled": "true",
               "isEventHubBracketsEnabled": "false", "isNextGenEventHub": "false", "site_id": "20"}


def _soup(html):
    from bs4 import BeautifulSoup
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def fetch_event(ref, fresh=False):
    """Varsity's JSON API first. Its results are CMS `html-view` items
    whose `htmlContent` is pasted spreadsheet HTML (found Oct 14 2026 —
    there is no structured score field), so we read the JSON for the
    event metadata and parse the tables inside each htmlContent. Falls
    back to the public results page when the API answers nothing usable."""
    event_id, url = parse_event_ref(ref)
    if not event_id:
        return {"ok": False, "error": "Couldn't read an event id from that. Paste a tv.varsity.com event link or its number."}
    ck = "api:" + event_id
    hit = _CACHE.get(ck)
    if hit and not fresh and time.time() - hit[0] < _CACHE_TTL_S:
        return {**hit[1], "cached": True}
    api_url = API.format(eid=event_id)
    diag = {"url": url, "api": api_url}
    t0 = time.time()
    try:
        r = requests.get(api_url, params=_API_PARAMS,
                         headers={**_HEADERS, "Accept": "application/json"}, timeout=12)
        diag.update({"status": r.status_code, "bytes": len(r.content),
                     "ms": int((time.time() - t0) * 1000)})
        j = r.json() if r.status_code == 200 else None
    except Exception as e:
        diag["error"] = str(e)
        j = None
    blocks, name, meta = [], None, {}
    if isinstance(j, dict):
        try:
            name = j["header"]["title"]["text"]
        except Exception:
            name = (j.get("analytics") or {}).get("name")
        m = j.get("metadata") or {}
        meta = {"start": m.get("startAt"), "end": m.get("endAt"),
                "status": m.get("liveEventStatus"), "days": m.get("numberOfDays"),
                "image": m.get("assetUrl")}
        views = [v for _p, v in _walk(j)
                 if isinstance(v, dict) and isinstance(v.get("htmlContent"), str)]
        # An event can carry several result sets (one per competition in
        # it); the API embeds only the first and LINKS the rest as
        # .../results/?resultId=N. Fetch each linked set the same way.
        have = {str(v.get("id")) for v in views}
        linked = sorted({m for _p, v in _walk(j) if isinstance(v, str)
                         for m in re.findall(r"resultId=(\d+)", v)} - have)
        diag["result_ids"] = sorted(have) + linked
        for rid in linked[:12]:
            try:
                rr = requests.get(api_url, params={**_API_PARAMS, "resultId": rid},
                                  headers={**_HEADERS, "Accept": "application/json"}, timeout=12)
                jj = rr.json() if rr.status_code == 200 else None
            except Exception:
                jj = None
            for _p, v in _walk(jj):
                if (isinstance(v, dict) and isinstance(v.get("htmlContent"), str)
                        and str(v.get("id")) not in have):
                    have.add(str(v.get("id")))
                    views.append(v)
        diag["html_views"] = [v.get("id") for v in views]
        for v in views:
            blocks.extend(dict(t, source="api", result_id=v.get("id"))
                          for t in parse_tables(_soup(v["htmlContent"])))
    if not blocks and isinstance(j, dict):
        sblocks, sdiag = fetch_structured(event_id)
        diag["structured"] = sdiag
        if sblocks:
            out = {"ok": True, "event_id": event_id, "event_name": name, "meta": meta,
                   "source": "api:list", "fetched_at": int(time.time()), "diag": diag,
                   "divisions": [dict(b, structured=True,
                                      teams=[{"rank": r["rank"], "team": " · ".join(x for x in (r["program"], r["team"]) if x),
                                              "score": r["score"]} for r in b["rows"]],
                                      columns=["Rank", "Program", "Team", "Raw", "Ded", "Perf", "Score"])
                                 for b in sblocks]}
            for d in out["divisions"]:
                d["division"] = d["division"] + (" · " + d["round"] if d.get("round") else "")
                d["division_name"] = d["division"].rsplit(" · ", 1)[0]
            _CACHE[ck] = (time.time(), out)
            return out
        # A listed event with no scores yet (upcoming) still has metadata.
        return {"ok": False, "event_id": event_id, "event_name": name, "meta": meta,
                "source": "api", "divisions": [], "error": "No results posted yet.", "diag": diag}
    if not blocks:
        out = fetch_event_page(ref, fresh=fresh)
        out.setdefault("diag", {})["api_fallback"] = diag
        return out
    out = {"ok": True, "event_id": event_id, "event_name": name, "meta": meta,
           "source": "api", "divisions": [normalize(b) for b in blocks],
           "fetched_at": int(time.time()), "diag": diag}
    _CACHE[ck] = (time.time(), out)
    return out


TAB_API = "https://tv-admin.varsity.com/api/experiences/web/results/tab"


def results_tab(extra=None):
    """Varsity's results index API (month tabs + event cards). `extra`
    params override/add to the defaults (e.g. a month's tab params).
    Returns {tabs:[{id,title,url,api}], events:[{id,title,sub,...}]}."""
    params = {"site_id": "20", "isEventHubLayoutEnabled": "true", "limit": "100",
              "offset": "0", "tz": "America/Phoenix", "version": "1.33.2"}
    params.update(extra or {})
    # The month is a PATH segment (results/tab/2026-02-01), read off the
    # page's own transfer state Oct 14 2026 — a ?date= query is ignored.
    month = params.pop("date", None)
    url = TAB_API + (f"/{month}" if month else "")
    try:
        r = requests.get(url, params=params,
                         headers={**_HEADERS, "Accept": "application/json"}, timeout=15)
        j = r.json() if r.status_code == 200 else None
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not isinstance(j, dict):
        return {"ok": False, "status": r.status_code, "head": r.text[:500]}
    tabs, events, seen = [], [], set()
    for _p, v in _walk(j):
        if not isinstance(v, dict):
            continue
        if v.get("type") == "tab":
            act = v.get("action") or {}
            tabs.append({"id": v.get("id"), "title": v.get("title"), "active": v.get("active"),
                         "url": act.get("url"), "api": act.get("apiCall") or act.get("mergeParams"),
                         "raw": json.dumps(act)[:1200] if not tabs else None})
        u = v.get("url") if isinstance(v.get("url"), str) else None
        m = re.search(r"/events/(\d+)", u or "")
        if m and m.group(1) not in seen and v.get("title"):
            seen.add(m.group(1))
            events.append({"id": m.group(1), "title": v.get("title"), "url": u})
    # Event cards: parent objects carrying a title plus labels/footers.
    cards = []
    for _p, v in _walk(j):
        if not (isinstance(v, dict) and isinstance(v.get("title"), str)):
            continue
        act = v.get("action") if isinstance(v.get("action"), dict) else {}
        u = act.get("url") or (v.get("url") if isinstance(v.get("url"), str) else None)
        m = re.search(r"/events/(\d+)", u or "")
        if m and v.get("title") not in ("Results", "View All", "View Results"):
            cards.append({"id": m.group(1) if m else None, "title": v.get("title"),
                          "sub1": v.get("subtitle1"), "sub2": v.get("subtitle2"),
                          "label1": v.get("label1"), "footer1": v.get("footer1"), "url": u})
    return {"ok": True, "status": r.status_code, "bytes": len(r.content),
            "tabs": tabs, "events": events[:200], "cards": cards[:200],
            "sections": [s.get("id") for s in (j.get("sections") or []) if isinstance(s, dict)]}


def results_month(month):
    """Every event card on Varsity's results index for one month
    ('2026-02-01'), paging limit/offset until a page adds nothing."""
    events, seen, err = [], set(), None
    for off in range(0, 1000, 100):
        page = results_tab({"date": month, "offset": str(off)})
        if not page.get("ok"):
            err = page.get("error") or page.get("status")
            break
        new = 0
        for c in page.get("cards") or []:
            if c.get("id") and c["id"] not in seen:
                seen.add(c["id"])
                new += 1
                events.append({"id": c["id"], "title": c.get("title"), "location": c.get("sub1")})
        if new < 100:
            break
    return {"events": events, "error": err}


def raw_api(path, params=None, qs=None):
    """Probe: GET any tv-admin.varsity.com path, return its shape and the
    first few result-ish objects (anything carrying a score/team field)."""
    url = "https://tv-admin.varsity.com" + (path if path.startswith("/") else "/" + path)
    if qs:
        url += "?" + qs   # verbatim, already URL-encoded
    try:
        r = requests.get(url, params=params or None,
                         headers={**_HEADERS, "Accept": "application/json"}, timeout=15)
        j = r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    def shape(o, d=0):
        if d > 7:
            return "…"
        if isinstance(o, dict):
            return {k: shape(v, d + 1) for k, v in list(o.items())[:30]}
        if isinstance(o, list):
            return [len(o), shape(o[0], d + 1)] if o else []
        return o if not isinstance(o, str) else o[:100]
    tables = [v for _p, v in _walk(j) if isinstance(v, dict) and v.get("type") == "table"]
    if tables:
        tinfo = [{"id": t.get("id"), "rows": len(t.get("rows") or [])} for t in tables[:20]]
    else:
        tinfo = []
    items = (((j.get("data") or {}).get("content") or {}).get("items")) if isinstance(j, dict) else None
    if not items and isinstance(j, dict):
        items = [v for _p, v in _walk(j) if isinstance(v, dict) and v.get("type") in ("table", "title-with-action")][:4]
    samples = [json.dumps(x)[:3500] for x in (items or [])[:4]]
    return {"ok": True, "status": r.status_code, "bytes": len(r.content),
            "n_items": len(items or []), "tables": tinfo, "top_keys": list(j.keys())[:20] if isinstance(j, dict) else None,
            "samples": samples}


# ── All-star events: Varsity's STRUCTURED results (results/partial) ─────
# Found Oct 14 2026 on 2026 NCA All-Star Nationals: no pasted tables; the
# event hub serves keyed tables instead. Each division is a
# `title-with-action` item (analytics.extraContext = {class, division,
# roundName}) followed by a `table` whose cells are keyed: rank,
# program-team (text = program, subText = team), raw-score, deductions,
# performance-score, event-score. Paged by limit/offset over divisions.

PARTIAL_API = "https://tv-admin.varsity.com/api/experiences/web/event-hub/{eid}/results/partial"


def _cell_text(c):
    d = (c or {}).get("data") or {}
    return (d.get("text") or "").strip(), (d.get("subText") or "").strip()


_RND_TAIL = re.compile(r"\s+(finals?|prelims?|preliminaries|semi-?finals?|day \d+|round \d+)$", re.I)


def _items_of(j):
    """Every division title + results table in document order, wherever
    the response nests them (results/partial: data.content.items;
    results/list: deeper inside container)."""
    return [v for _p, v in _walk(j or {})
            if isinstance(v, dict) and v.get("type") in ("title-with-action", "table")]


def parse_partial(j):
    """A results/partial or results/list page → [{division, round, cls,
    more, rows:[...]}]. `more` = the division has a View All (the preview
    holds only its top 3)."""
    out, ctx = [], {}
    for it in _items_of(j):
        if not isinstance(it, dict):
            continue
        if it.get("type") == "title-with-action":
            acts = [it.get("action")] + list(it.get("actions") or [])
            ex, more = {}, False
            for a in acts:
                if not isinstance(a, dict):
                    continue
                e = (a.get("analytics") or {}).get("extraContext") or {}
                if e.get("division"):
                    ex = e
                if "view-all" in (a.get("url") or ""):
                    more = True
            title = it.get("title") or ""
            ctx = {"division": ex.get("division") or _RND_TAIL.sub("", title).strip(),
                   "round": ex.get("roundName") or (_RND_TAIL.search(title).group(1) if _RND_TAIL.search(title) else ""),
                   "cls": ex.get("class") or "", "more": more}
            continue
        if it.get("type") != "table":
            continue
        if not ctx.get("round"):
            m = re.search(r"-(\w+)$", it.get("id") or "")
            if m:
                ctx["round"] = m.group(1)
        rows = []
        for r in it.get("rows") or []:
            cells = {c.get("key"): c for c in (r.get("cells") or []) if isinstance(c, dict)}
            prog, team = _cell_text(cells.get("program-team") or cells.get("program-name"))
            if not prog and not team:
                continue
            num = lambda k: _cell_text(cells.get(k))[0]
            replay = (r.get("action") or {}).get("url") if isinstance(r.get("action"), dict) else None
            rows.append({"rank": num("rank"), "program": prog, "team": team,
                         "raw": num("raw-score"), "ded": num("deductions"),
                         "perf": num("performance-score"),
                         "score": num("event-score") or num("performance-score"), "replay": replay})
        if rows:
            out.append({**ctx, "rows": rows})
    return out


LIST_API = "https://tv-admin.varsity.com/api/experiences/web/event-hub/{eid}/results/list"
_LIST_PARAMS = {"version": "1.33.2", "tz": "UTC", "isEventHubLayoutEnabled": "true",
                "isEventHubBracketsEnabled": "false", "isNextGenEventHub": "false", "site_id": "20"}


_GJ_STATS = {}


def _get_json(url, params):
    try:
        r = requests.get(url, params=params, headers={**_HEADERS, "Accept": "application/json"}, timeout=15)
        k = str(r.status_code)
        _GJ_STATS[k] = _GJ_STATS.get(k, 0) + 1
        if r.status_code != 200:
            _GJ_STATS["last_bad"] = r.url[:300] + " → " + r.text[:200]
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        _GJ_STATS["exc"] = f"{type(e).__name__}: {e}"[:200]
        return None


def fetch_structured(event_id, budget_s=30, page=100):
    """All-star events: every division, every round, every team. The
    preview pages (results/partial) list the divisions with their top 3;
    each division with a View All is re-read in full from results/list
    (facets = class/division/round), 6 at a time."""
    from concurrent.futures import ThreadPoolExecutor
    t0, blocks, seen, pages = time.time(), [], set(), 0
    url = PARTIAL_API.format(eid=event_id)
    for off in range(0, 6000, page):
        if time.time() - t0 > budget_s:
            break
        j = _get_json(url, {"version": "1.33.2", "limit": str(page), "offset": str(off),
                            "tz": "UTC", "isEventHubLayoutEnabled": "true"})
        pages += 1
        got, new = parse_partial(j), 0
        for b in got:
            k = (b["division"], b["round"])
            if k in seen:
                continue
            seen.add(k)
            blocks.append(b)
            new += 1
        if not got or new == 0:
            break
    want = [b for b in blocks if b.get("more")]
    sample_empty = []
    _GJ_STATS.clear()

    def full(b):
        facets = json.dumps({"class": b.get("cls") or "Cheer", "division": b["division"],
                             "roundName": b["round"]}, separators=(",", ":"))
        jj = _get_json(LIST_API.format(eid=event_id), {**_LIST_PARAMS, "facets": facets})
        got = parse_partial(jj)
        if not got and jj is not None and not sample_empty:
            sample_empty.append({"facets": facets, "keys": list(jj.keys())[:12],
                                 "items": len(_items_of(jj))})
        return b, (got[0]["rows"] if got else None)
    filled = 0
    left = max(5.0, budget_s + 25 - (time.time() - t0))
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = [ex.submit(full, b) for b in want]
        for f in futs:
            try:
                b, rows = f.result(timeout=left)
            except Exception:
                continue
            if rows and len(rows) >= len(b["rows"]):
                b["rows"] = rows
                filled += 1
    return blocks, {"pages": pages, "divisions": len(blocks), "view_all": len(want),
                    "filled": filled, "secs": round(time.time() - t0, 1), "http": dict(_GJ_STATS),
                    "sample_empty": sample_empty}


def page_api_calls(path):
    """Probe: load a tv.varsity.com page and list the tv-admin API calls
    its server render made (they sit in the Angular transfer state)."""
    try:
        r = requests.get(BASE + path, headers=_HEADERS, timeout=15)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    calls = sorted(set(re.findall(r"https://tv-admin\.varsity\.com/api/[^&\"'\s]*(?:&a;[^&\"'\s]*)*", r.text)))
    calls = [c.replace("&a;", "&")[:600] for c in calls if "partials/" not in c]
    return {"ok": True, "status": r.status_code, "bytes": len(r.content), "calls": calls[:20]}
