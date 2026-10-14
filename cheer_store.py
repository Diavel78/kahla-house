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
            div, rnd = d.get("division") or "", d.get("round") or ""
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


def save_event(db, ev):
    meta = ev.get("meta") or {}
    res = compact(ev) if ev.get("ok") else []
    doc = {
        "id": str(ev.get("event_id")), "name": ev.get("event_name"),
        "start": meta.get("start"), "end": meta.get("end"), "status": meta.get("status"),
        "season": season_of(meta.get("start")), "fetched_at": int(time.time()),
        "n_divisions": len(res), "n_scores": sum(len(x["rows"]) for x in res),
        "has_results": bool(res), "source": ev.get("source"),
        "results": json.dumps(res, separators=(",", ":")),
    }
    db.collection(COLL).document(doc["id"]).set(doc)
    return doc


def _needs_fetch(existing, now):
    """Fetch a never-seen event; refresh one whose results could still
    change (no results yet, or ended within the last 10 days); leave
    concluded older events alone."""
    if existing is None:
        return True
    age = now - (existing.get("fetched_at") or 0)
    end = existing.get("end") or existing.get("start") or ""
    recent = end >= time.strftime("%Y-%m-%d", time.gmtime(now - 10 * 86400))
    if not existing.get("has_results"):
        return age > (6 * 3600 if recent else 7 * 86400)
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
            found.setdefault(e["id"], e)
    return found, diag


def ingest(db, months, budget_s=40, max_events=60):
    """Discover → fetch what's new/stale → store. Bounded per call; call
    again until `remaining` is 0."""
    t0 = time.time()
    found, diag = discover(months)
    now = time.time()
    existing = {}
    ids = list(found)
    for i in range(0, len(ids), 30):
        refs = [db.collection(COLL).document(x) for x in ids[i:i + 30]]
        for snap in db.get_all(refs, field_paths=["fetched_at", "has_results", "end", "start"]):
            if snap.exists:
                existing[snap.id] = snap.to_dict()
    todo = [x for x in ids if _needs_fetch(existing.get(x), now)]
    done, errors = [], []
    for eid in todo:
        if len(done) >= max_events or time.time() - t0 > budget_s:
            break
        try:
            ev = cs.fetch_event(eid, fresh=True)
            doc = save_event(db, ev)
            done.append({"id": eid, "name": doc["name"], "season": doc["season"],
                         "divs": doc["n_divisions"], "scores": doc["n_scores"]})
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
    _LB_CACHE[season] = (time.time(), evs)
    return evs


def seasons(db):
    out = {}
    for snap in db.collection(COLL).select(["season", "has_results"]).stream():
        d = snap.to_dict()
        s = d.get("season")
        if s and d.get("has_results"):
            out[s] = out.get(s, 0) + 1
    return [{"season": s, "events": n} for s, n in sorted(out.items(), reverse=True)]


def leaderboard(db, season, division=None, cat=None, q=None, rank_by="avg", limit=300):
    """One row per (team, division): every event it scored at this season,
    the event's FINAL score when a finals round exists else its best round,
    then average / high across events."""
    evs = load_season(db, season)
    perf = {}
    divs = {}
    for ev in evs:
        best = {}
        for blk in ev["results"]:
            div, rnd = blk["div"], (blk.get("round") or "").lower()
            divs[div] = blk.get("cat")
            if division and div != division:
                continue
            if cat and blk.get("cat") != cat:
                continue
            for row in blk["rows"]:
                # v2 rows: [rank, program, team, score, raw, ded, replay];
                # v1 rows: [rank, name, score, raw, ded]
                if len(row) >= 7:
                    team, score = " · ".join(x for x in (row[1], row[2]) if x), row[3]
                else:
                    team, score = row[1], row[2]
                if score is None or not (0 < score <= 1000):
                    continue
                k = (team, div)
                is_final = rnd.startswith("final")
                cur = best.get(k)
                if cur is None or (is_final and not cur[1]) or (is_final == cur[1] and score > cur[0]):
                    best[k] = (score, is_final, row[0], blk.get("cat"))
        for (team, div), (score, _f, rank, c) in best.items():
            p = perf.setdefault((team, div), {"team": team, "division": div, "cat": c, "scores": []})
            p["scores"].append({"event": ev.get("name"), "event_id": ev.get("id"),
                                "date": (ev.get("start") or "")[:10], "score": score, "rank": rank})
    rows = []
    ql = (q or "").lower().strip()
    for p in perf.values():
        if ql and ql not in p["team"].lower() and ql not in p["division"].lower():
            continue
        sc = [s["score"] for s in p["scores"]]
        p["n"] = len(sc)
        p["avg"] = round(sum(sc) / len(sc), 3)
        p["high"] = max(sc)
        p["wins"] = sum(1 for s in p["scores"] if str(s["rank"]).strip() == "1")
        rows.append(p)
    key = (lambda r: (-r["high"], -r["avg"])) if rank_by == "high" else (lambda r: (-r["avg"], -r["high"]))
    rows.sort(key=key)
    return {"season": season, "events": len(evs),
            "events_with_results": sum(1 for e in evs if e.get("has_results")),
            "total": len(rows), "rows": rows[:limit],
            "divisions": sorted(d for d in divs if d), "cats": sorted({c for c in divs.values() if c})}


def events_list(db, season):
    evs = load_season(db, season)
    return [{"id": e.get("id"), "name": e.get("name"), "start": e.get("start"),
             "status": e.get("status"), "divisions": e.get("n_divisions"),
             "scores": e.get("n_scores")} for e in reversed(evs)]
