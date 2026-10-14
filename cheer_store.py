"""Cheer Scoring storage + leaderboards.

Firestore (Admin SDK, written only by Flask — no client rules needed):
  cheer_events/{event_id} = {id, name, start, end, status, season,
      fetched_at, n_divisions, n_scores, has_results,
      results: JSON string of [{div, round, cat, rows: [[rank, team, score,
      raw, ded], ...]}]}
Results ride as a JSON STRING because Firestore refuses nested arrays.

Seasons run Jul 1 → Jun 30 ("2025-26").
"""
import json
import re
import time

import cheer_scores as cs

COLL = "cheer_events"


def season_of(iso):
    """'2026-02-14T…' → '2025-26'. None when the date is unreadable."""
    m = re.match(r"(\d{4})-(\d{2})", iso or "")
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    start = y if mo >= 7 else y - 1
    return f"{start}-{(start + 1) % 100:02d}"


_ROUND_SPLIT = re.compile(r"\s*·\s*")
_ROUND_WORD = re.compile(r"^(finals?|prelim\w*|semi[- ]?finals?|day \d+|round \d+)\b", re.I)


def split_division(name):
    """'Div X · Finals' → ('Div X', 'Finals'). Unknown round → ('Div X', '')."""
    parts = [p for p in _ROUND_SPLIT.split(name or "") if p]
    if len(parts) >= 2 and _ROUND_WORD.match(parts[-1]):
        return " · ".join(parts[:-1]), parts[-1]
    if parts and _ROUND_WORD.match(parts[0]) and len(parts) == 1:
        return "", parts[0]
    return " · ".join(parts), ""


def category(div, cls=None):
    """CheerStats-style gym type, read off the division name (+ class):
    CheerABILITIES · Rec · School · Worlds · D2 · Small Gym · D1 · Dance."""
    d = (div or "").lower()
    c = (cls or "").lower()
    if re.search(r"abilities|special needs|adaptive", d):
        return "CheerABILITIES"
    if re.search(r"\brec\b|recreation|\baff\b|\b\d+u\b|traditional rec", d):
        return "Rec"
    if re.search(r"college|university|\bnaia\b|high school|\bvarsity\b|junior varsity|\bjv\b|freshman|junior high|middle school|elementary|game ?day|\bhs\b|\bjh\b|crowdleader|band chant|fight song|sideline|mascot|show cheer|pep flag", d):
        return "School"
    if c == "dance" or re.search(r"\bpom\b|jazz|hip ?hop|\bkick\b|contemporary|lyrical|\bdance\b", d):
        return "Dance"
    if re.search(r"international|\bopen\b|\bu1[68]\b|\bl7\b|level 7|\bl6\b|level 6|worlds?", d):
        return "Worlds"
    if re.search(r"small gym|\bsg\b", d):
        return "Small Gym"
    if re.search(r"\bd2\b|\bdii\b|division ii", d):
        return "D2"
    return "D1"


def _num(s):
    s = re.sub(r"[^\d.\-]", "", str(s or ""))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None


def compact(ev):
    """fetch_event output → the stored results list. Row =
    [rank, program, team, score, raw, ded, replay]. School events have
    no team name (the school IS the program)."""
    out = []
    for d in ev.get("divisions") or []:
        if d.get("structured"):
            div, rnd = d.get("division_name") or d.get("division") or "", d.get("round") or ""
            rows = [[r.get("rank") or "", r.get("program") or "", r.get("team") or "",
                     _num(r.get("score")), _num(r.get("raw")), _num(r.get("ded")), r.get("replay")]
                    for r in d.get("rows") or [] if (r.get("program") or r.get("team"))]
            if rows:
                out.append({"div": div, "round": rnd, "cat": category(div, d.get("cls")), "rows": rows})
            continue
        div, rnd = split_division(d.get("division") or "")
        cols = d.get("columns") or []
        lc = [c.lower() for c in cols]

        def idx(*names):
            for n in names:
                for i, c in enumerate(lc):
                    if n in c:
                        return i
            return None
        ri, di = idx("raw"), idx("deduction")
        rows = []
        for t, r in zip(d.get("teams") or [], d.get("rows") or []):
            name = (t.get("team") or "").strip()
            if not name:
                continue
            prog, team = name, ""
            m = re.match(r"^(.+?)\s+[-–]\s+(.+)$", name)
            if m and category(div) not in ("School",):
                prog, team = m.group(1), m.group(2)
            rows.append([t.get("rank") or "", prog, team, _num(t.get("score")),
                         _num(r[ri]) if ri is not None and ri < len(r) else None,
                         _num(r[di]) if di is not None and di < len(r) else None, None])
        if rows:
            out.append({"div": div or d.get("division") or "", "round": rnd,
                        "cat": category(div or d.get("division")), "rows": rows})
    return out


SCHEMA = 2   # bump to re-read every stored event on the next ingest


def save_event(db, ev, listing=None):
    meta = ev.get("meta") or {}
    res = compact(ev) if ev.get("ok") else []
    teams = {(r[1], r[2]) for b in res for r in b["rows"]}
    doc = {
        "id": str(ev.get("event_id")), "name": ev.get("event_name") or (listing or {}).get("title"),
        "location": (listing or {}).get("location"),
        "start": meta.get("start") or (listing or {}).get("month"), "end": meta.get("end"),
        "status": meta.get("status"),
        # Some events carry no start date; date them by the results-index
        # month they were listed under.
        "season": season_of(meta.get("start") or (listing or {}).get("month")),
        "fetched_at": int(time.time()), "v": SCHEMA,
        "n_divisions": len({b["div"] for b in res}), "n_scores": sum(len(x["rows"]) for x in res),
        "n_teams": len(teams), "has_results": bool(res), "source": ev.get("source"),
        "cats": sorted({b["cat"] for b in res}),
        "results": json.dumps(res, separators=(",", ":")),
    }
    db.collection(COLL).document(doc["id"]).set(doc)
    return doc


def _needs_fetch(existing, now):
    """Fetch a never-seen event or one stored by an older reader; refresh
    one whose results could still change (none yet, or ended in the last
    10 days); leave concluded older events alone."""
    if existing is None or (existing.get("v") or 1) < SCHEMA or not existing.get("season"):
        return True
    age = now - (existing.get("fetched_at") or 0)
    end = existing.get("end") or existing.get("start") or ""
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    recent = end >= time.strftime("%Y-%m-%d", time.gmtime(now - 10 * 86400))
    if not existing.get("has_results"):
        if end and end > today:
            return age > 3 * 86400          # upcoming: refresh the listing now and then
        return age > (2 * 3600 if recent else 7 * 86400)
    return recent and age > 3600


def discover(months):
    """Event ids from Varsity's results index, one call per month tab."""
    found, diag = {}, []
    for mo in months:
        page = cs.results_month(mo)
        diag.append({"month": mo, "n": len(page.get("events") or []), "err": page.get("error")})
        for e in page.get("events") or []:
            if "rebroadcast" in (e.get("title") or "").lower():
                continue
            found.setdefault(e["id"], dict(e, month=mo))
    return found, diag


def ingest(db, months, budget_s=20, max_events=80):
    """Discover → fetch what's new/stale → store. Starts no new event
    after `budget_s`; call again until `remaining` is 0."""
    t0 = time.time()
    found, diag = discover(months)
    now = time.time()
    existing = {}
    ids = list(found)
    for i in range(0, len(ids), 30):
        refs = [db.collection(COLL).document(x) for x in ids[i:i + 30]]
        for snap in db.get_all(refs, field_paths=["fetched_at", "has_results", "end", "start", "v", "season"]):
            if snap.exists:
                existing[snap.id] = snap.to_dict()
    todo = [x for x in ids if _needs_fetch(existing.get(x), now)]
    done, errors = [], []
    for eid in todo:
        if len(done) >= max_events or time.time() - t0 > budget_s:
            break
        try:
            ev = cs.fetch_event(eid, fresh=True)
            doc = save_event(db, ev, found.get(eid))
            done.append({"id": eid, "name": doc["name"], "season": doc["season"], "divs": doc["n_divisions"],
                         "scores": doc["n_scores"], "src": doc["source"],
                         "struct": (ev.get("diag") or {}).get("structured")})
        except Exception as e:
            errors.append({"id": eid, "err": f"{type(e).__name__}: {e}"[:200]})
    _LB_CACHE.clear()
    return {"ok": True, "discovered": len(found), "todo": len(todo), "done": len(done),
            "remaining": max(0, len(todo) - len(done) - len(errors)), "events": done,
            "errors": errors, "months": diag, "secs": round(time.time() - t0, 1)}


# ── reads ───────────────────────────────────────────────────────────────

_LB_CACHE = {}
_LB_TTL = 600


def load_season(db, season):
    hit = _LB_CACHE.get(season)
    if hit and time.time() - hit[0] < _LB_TTL:
        return hit[1]
    evs = []
    for snap in db.collection(COLL).where("season", "==", season).stream():
        d = snap.to_dict()
        try:
            d["results"] = json.loads(d.get("results") or "[]")
        except Exception:
            d["results"] = []
        evs.append(d)
    evs.sort(key=lambda e: e.get("start") or "")
    perfs = []
    for ev in evs:
        for blk in ev["results"]:
            for row in blk["rows"]:
                if len(row) >= 7:
                    rank, prog, team, score = row[0], row[1], row[2], row[3]
                    replay = row[6]
                else:                       # v1 rows (pre-program/team split)
                    rank, prog, team, score, replay = row[0], row[1], "", row[2], None
                if score is None or not (0 < score <= 1000):
                    continue
                perfs.append({"event": ev.get("name"), "event_id": ev.get("id"),
                              "date": (ev.get("start") or "")[:10], "div": blk["div"],
                              "round": blk.get("round") or "", "cat": blk.get("cat"),
                              "rank": rank, "program": prog, "team": team, "score": score,
                              "replay": replay})
    data = {"events": evs, "perfs": perfs}
    _LB_CACHE[season] = (time.time(), data)
    return data


def seasons(db):
    out = {}
    for snap in db.collection(COLL).select(["season", "has_results"]).stream():
        d = snap.to_dict()
        s = d.get("season")
        if s and d.get("has_results"):
            out[s] = out.get(s, 0) + 1
    return [{"season": s, "events": n} for s, n in sorted(out.items(), reverse=True)]


def _match(p, division=None, cat=None, q=None):
    if division and p["div"] != division:
        return False
    if cat and p["cat"] != cat:
        return False
    if q:
        hay = f"{p['program']} {p['team']}".lower()
        if q not in hay:
            return False
    return True


def _summ(ps):
    sc = [p["score"] for p in ps]
    return {"events": len({p["event_id"] for p in ps}), "performances": len(sc),
            "avg": round(sum(sc) / len(sc), 4) if sc else None, "high": max(sc) if sc else None,
            "wins": sum(1 for p in ps if str(p["rank"]).strip() == "1")}


def leaderboard(db, season, division=None, cat=None, q=None, rank_by="avg", limit=300):
    """One row per team in a division (program + team + division):
    events, performances, average and high over every performance."""
    data = load_season(db, season)
    ql = (q or "").lower().strip()
    groups, divs, cats = {}, set(), set()
    for p in data["perfs"]:
        divs.add(p["div"])
        cats.add(p["cat"])
        if not _match(p, division, cat, ql):
            continue
        groups.setdefault((p["program"], p["team"], p["div"]), []).append(p)
    rows = []
    for (prog, team, div), ps in groups.items():
        ps.sort(key=lambda x: x["date"])
        rows.append({"program": prog, "team": team, "division": div, "cat": ps[0]["cat"],
                     **_summ(ps), "scores": ps})
    key = (lambda r: (-r["high"], -r["avg"])) if rank_by == "high" else (lambda r: (-r["avg"], -r["high"]))
    rows.sort(key=key)
    return {"season": season, "events_with_results": sum(1 for e in data["events"] if e.get("has_results")),
            "total": len(rows), "rows": rows[:limit],
            "divisions": sorted(d for d in divs if d), "cats": sorted(c for c in cats if c)}


def programs(db, season, division=None, cat=None, q=None, sort="alpha", limit=400):
    data = load_season(db, season)
    ql = (q or "").lower().strip()
    groups = {}
    for p in data["perfs"]:
        if not _match(p, division, cat, ql):
            continue
        groups.setdefault(p["program"], []).append(p)
    rows = []
    for prog, ps in groups.items():
        rows.append({"program": prog, "teams": len({(p["team"], p["div"]) for p in ps}),
                     "cats": sorted({p["cat"] for p in ps}), **_summ(ps)})
    if sort == "avg":
        rows.sort(key=lambda r: -r["avg"])
    elif sort == "high":
        rows.sort(key=lambda r: -r["high"])
    elif sort == "teams":
        rows.sort(key=lambda r: (-r["teams"], r["program"].lower()))
    else:
        rows.sort(key=lambda r: r["program"].lower())
    return {"season": season, "total": len(rows), "rows": rows[:limit]}


def program_detail(db, season, program):
    data = load_season(db, season)
    ps = [p for p in data["perfs"] if p["program"] == program]
    teams = {}
    for p in ps:
        teams.setdefault((p["team"], p["div"]), []).append(p)
    out = []
    for (team, div), tp in teams.items():
        tp.sort(key=lambda x: x["date"])
        out.append({"program": program, "team": team, "division": div, "cat": tp[0]["cat"], **_summ(tp), "scores": tp})
    out.sort(key=lambda r: -r["avg"])
    return {"program": program, "season": season, **_summ(ps), "teams": out}


def events_split(db, season):
    """Past (scored) and upcoming events for the Events tab."""
    data = load_season(db, season)
    today = time.strftime("%Y-%m-%d")
    past, upcoming = [], []
    for e in data["events"]:
        row = {"id": e.get("id"), "name": e.get("name"), "location": e.get("location"),
               "start": e.get("start"), "end": e.get("end"), "teams": e.get("n_teams") or 0,
               "divisions": e.get("n_divisions") or 0, "cats": e.get("cats") or []}
        if (e.get("start") or "")[:10] > today and not e.get("has_results"):
            upcoming.append(row)
        elif e.get("has_results"):
            past.append(row)
    upcoming.sort(key=lambda r: r["start"] or "")
    past.sort(key=lambda r: r["start"] or "", reverse=True)
    return {"past": past, "upcoming": upcoming}


def event_detail(db, eid):
    snap = db.collection(COLL).document(str(eid)).get()
    if not snap.exists:
        return None
    d = snap.to_dict()
    d["results"] = json.loads(d.get("results") or "[]")
    return d


# ── following (per user) ────────────────────────────────────────────────

FOLLOW = "cheer_follow"


def follow_get(db, uid):
    snap = db.collection(FOLLOW).document(uid).get()
    d = snap.to_dict() if snap.exists else {}
    return {"teams": d.get("teams") or [], "programs": d.get("programs") or [], "events": d.get("events") or []}


def follow_set(db, uid, kind, key, on):
    if kind not in ("teams", "programs", "events") or not key:
        raise ValueError("bad follow")
    cur = follow_get(db, uid)
    lst = [k for k in cur[kind] if k != key]
    if on:
        lst.append(key)
    cur[kind] = lst[-500:]
    db.collection(FOLLOW).document(uid).set({**cur, "updated_at": int(time.time())})
    return cur
