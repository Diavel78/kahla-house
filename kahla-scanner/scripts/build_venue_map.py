#!/usr/bin/env python3
"""venue_contract_map — Polymarket rung ↔ Gemini contract, Polymarket-FIRST.

Rob, Sep 13 2026: "Polymarket picks the rent window, they are still the bible.
'Does Poly pay rent' is the starter. Without it on that list, it's dead."

So the universe is Polymarket's NFL slugs (rent-list harvest for spreads / totals /
team totals / ML; the prop tape for player props), each annotated with the venue's
per-market rent answer, and each joined to the Gemini contract that settles on the
same event. One row per (poly_slug, poly_side). Gemini has one contract per TEAM per
spread rung and both sides of every contract are native YES/NO, so:

    poly asc  pos-3pt5  (away +3.5)  YES  ⇔  gemini  S-<HOME>3   NO    ("home wins by >3.5" is false)
    poly asc  neg-3pt5  (away −3.5)  YES  ⇔  gemini  S-<AWAY>3   YES
    poly tsc  total-47pt5            YES  ⇔  gemini  T-O47       YES   (over)
    poly tsc  tt-<team>-24pt5        YES  ⇔  gemini  TT-<TEAM>O24 YES
    poly aec  (ML)  YES=<team>       YES  ⇔  gemini  M-<TEAM>    YES
    poly astatc  "Will X record N+ receiving yards?" YES ⇔ gemini PPRECY "X N+ Receiving Yards" YES
    (touchdowns → PPTD, rushing yards → PPRYDS, passing yards → PPYDS, passing touchdowns → PPPASSTD)
    the NO side of each maps to the same Gemini contract's NO.

Run from the repo root:  .venv/bin/python kahla-scanner/scripts/build_venue_map.py [--no-db] [--no-rent]
Writes table venue_contract_map (local Postgres via psql) and prints a coverage report.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")
import gemini_pm as g  # noqa: E402

PSQL = "/Applications/Postgres.app/Contents/Versions/latest/bin/psql"
DB = "kahla"
ET = ZoneInfo("America/New_York")
INCLUDE_PROPS = False   # Rob, Sep 13 2026: "no props" — ML / spread / O-U (incl. team totals) only

SEG_SKIP = re.compile(r"-(1h|2h|1q|2q|3q|4q|tt2h|tt1h|f5|ot)-")
ASC_RE = re.compile(r"^asc-nfl-([a-z]+)-([a-z]+)-(\d{4}-\d{2}-\d{2})-(pos|neg)-(\d+)(?:pt(\d+))?$")
TOT_RE = re.compile(r"^tsc-nfl-([a-z]+)-([a-z]+)-(\d{4}-\d{2}-\d{2})-total-(\d+)(?:pt(\d+))?$")
TT_RE = re.compile(r"^tsc-nfl-([a-z]+)-([a-z]+)-(\d{4}-\d{2}-\d{2})-tt-([a-z]+)-(\d+)(?:pt(\d+))?$")
AEC_RE = re.compile(r"^aec-nfl-([a-z]+)-([a-z]+)-(\d{4}-\d{2}-\d{2})$")
PROP_Q = [  # poly question → (gemini kind, stat)
    (re.compile(r"^Will (?P<name>.+?) record (?P<n>\d+)\+ receiving yards\?$", re.I), "PPRECY"),
    (re.compile(r"^Will (?P<name>.+?) record (?P<n>\d+)\+ rushing yards\?$", re.I), "PPRYDS"),
    (re.compile(r"^Will (?P<name>.+?) record (?P<n>\d+)\+ passing yards\?$", re.I), "PPYDS"),
    (re.compile(r"^Will (?P<name>.+?) record (?P<n>\d+)\+ passing touchdowns\?$", re.I), "PPPASSTD"),
    (re.compile(r"^Will (?P<name>.+?) record (?P<n>\d+)\+ touchdowns\?$", re.I), "PPTD"),
]
GEM_PROP_LABEL = re.compile(r"^(?P<name>.+?) (?P<n>\d+)\+ (Receiving Yards|Rushing Yards|Passing Yards|Passing Touchdowns|Touchdowns)$")


def _norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", s, flags=re.I)
    return re.sub(r"[^a-z]", "", s.lower())


def _line(n: str, frac: str | None) -> float:
    return float(n) + (float(frac) / 10.0 if frac else 0.0)


def _sql(q: str) -> list[list[str]]:
    out = subprocess.run([PSQL, DB, "-At", "-F", "\t", "-c", q], capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(out.stderr[:500])
    return [ln.split("\t") for ln in out.stdout.splitlines() if ln]


# ------------------------------------------------------------------ Polymarket side
def poly_universe(days_back: int = 1, days_fwd: int = 8) -> list[dict]:
    """Rent-list slugs (S/T/TT/ML) + prop-tape keys (player props), NFL, full game only."""
    rows = []
    lo = (dt.datetime.now(ET).date() - dt.timedelta(days=days_back)).isoformat()
    hi = (dt.datetime.now(ET).date() + dt.timedelta(days=days_fwd)).isoformat()
    for (slug,) in _sql("select slug from rent_list_slugs where slug like '%-nfl-%' "
                        "and last_seen > now() - interval '3 days'"):
        if SEG_SKIP.search(slug):
            continue
        m = ASC_RE.match(slug)
        if m:
            a, h, d, sgn, n, f = m.groups()
            if not (lo <= d <= hi):
                continue
            L = _line(n, f)
            rows.append(dict(slug=slug, away=a, home=h, date=d, mt="spread", line=L, sgn=sgn,
                             question=f"{a.upper()} {'+' if sgn=='pos' else '-'}{L:g}", src="rent_list"))
            continue
        m = TOT_RE.match(slug)
        if m:
            a, h, d, n, f = m.groups()
            if lo <= d <= hi:
                rows.append(dict(slug=slug, away=a, home=h, date=d, mt="total", line=_line(n, f), question=f"total over {_line(n,f):g}", src="rent_list"))
            continue
        m = TT_RE.match(slug)
        if m:
            a, h, d, team, n, f = m.groups()
            if lo <= d <= hi:
                rows.append(dict(slug=slug, away=a, home=h, date=d, mt="team_total", line=_line(n, f), team=team, question=f"{team.upper()} team total over {_line(n,f):g}", src="rent_list"))
            continue
        m = AEC_RE.match(slug)
        if m:
            a, h, d = m.groups()
            if lo <= d <= hi:
                rows.append(dict(slug=slug, away=a, home=h, date=d, mt="moneyline", line=None, question="ML", src="rent_list"))
    if not INCLUDE_PROPS:
        return rows
    # player props from the prop tape (latest question per key)
    q = ("select distinct on (prop_key) prop_key, question from prop_snapshots "
         "where prop_key like 'astatc-nfl-%%' and captured_at > now() - interval '7 days' "
         "order by prop_key, captured_at desc")
    for pk, question in _sql(q):
        m = re.match(r"^astatc-nfl-([a-z]+)-([a-z]+)-(\d{4}-\d{2}-\d{2})-", pk)
        if not m or not (lo <= m.group(3) <= hi):
            continue
        for rx, kind in PROP_Q:
            mm = rx.match(question or "")
            if mm:
                rows.append(dict(slug=pk, away=m.group(1), home=m.group(2), date=m.group(3), mt="prop",
                                 kind=kind, player=mm.group("name"), line=float(mm.group("n")) - 0.5,
                                 question=question, src="prop_tape"))
                break
    return rows


# ------------------------------------------------------------------ Gemini side
def gemini_index() -> tuple[dict, dict]:
    """(game_key → {kind → {rung_key → contract}}, pools by event ticker)."""
    pools = {p["event_ticker"]: p for p in g.liquidity_events()}
    idx: dict = {}
    for e in g.events_all("sports", "active"):
        parts = g.nfl_game_ticker_parts(e.get("ticker") or "")
        if not parts:
            continue
        code = parts["code"]
        utc = dt.datetime.strptime(code, "%y%m%d%H%M").replace(tzinfo=dt.timezone.utc)
        et_date = utc.astimezone(ET).date().isoformat()
        gk = f"{parts['t1'].lower()}-{parts['t2'].lower()}-{et_date}"
        kind = parts["kind"]
        pool = pools.get(e["ticker"]) or {}
        for c in e.get("contracts") or []:
            sym = c["instrumentSymbol"]; tail = sym.rsplit("-", 1)[-1]
            entry = dict(symbol=sym, label=c.get("label"), event=e["ticker"], kind=kind,
                         bid=g._f((c.get("prices") or {}).get("bestBid")), ask=g._f((c.get("prices") or {}).get("bestAsk")),
                         pool_usd=float(pool.get("daily_pool_usd") or 0), pool_makers=pool.get("qualifying_maker_count"),
                         start=e.get("startTime"))
            keys = []
            if kind == "S":
                m = re.match(r"^([A-Z]+?)(\d+)$", tail)
                if m: keys.append(("S", m.group(1).lower(), float(m.group(2)) + 0.5))
            elif kind == "T":
                m = re.match(r"^O(\d+)$", tail)
                if m: keys.append(("T", float(m.group(1)) + 0.5))
            elif kind == "TT":
                m = re.match(r"^([A-Z]+?)O(\d+)$", tail)
                if m: keys.append(("TT", m.group(1).lower(), float(m.group(2)) + 0.5))
            elif kind == "M":
                keys.append(("M", tail.lower()))
            elif kind.startswith("PP"):
                m = GEM_PROP_LABEL.match(c.get("label") or "")
                if m: keys.append((kind, _norm_name(m.group("name")), float(m.group("n")) - 0.5))
            for k in keys:
                idx.setdefault(gk, {})[k] = entry
    return idx, pools


# ------------------------------------------------------------------ Poly ML YES side
def poly_ml_yes_team(rows: list[dict]) -> dict[str, str]:
    """aec slug → team code whose win is YES, via the existing pmm lookup."""
    out: dict[str, str] = {}
    games = {(r["away"], r["home"], r["date"]) for r in rows if r["mt"] == "moneyline"}
    if not games:
        return out
    try:
        import pmm_markets
        from polymarket_us import PolymarketUS
        client = PolymarketUS(key_id=os.getenv("POLYMARKET_KEY_ID"), secret_key=os.getenv("POLYMARKET_SECRET_KEY"))
    except Exception as ex:
        print("ML side lookup unavailable:", ex); return out
    MASCOT = {"cardinals":"ari","falcons":"atl","ravens":"bal","bills":"buf","panthers":"car","bears":"chi","bengals":"cin",
              "browns":"cle","cowboys":"dal","broncos":"den","lions":"det","packers":"gb","texans":"hou","colts":"ind","jaguars":"jax",
              "chiefs":"kc","chargers":"lac","rams":"lar","raiders":"lv","dolphins":"mia","vikings":"min","patriots":"ne","saints":"no",
              "giants":"nyg","jets":"nyj","eagles":"phi","steelers":"pit","seahawks":"sea","49ers":"sf","buccaneers":"tb","titans":"ten","commanders":"was"}
    names: dict[str, str] = {}
    for ev, in _sql("select distinct event_name from markets where sport='NFL' and status='active' and event_name like '% @ %'"):
        for nm in [x.strip() for x in ev.split(" @ ", 1)]:
            code = MASCOT.get(nm.split()[-1].lower())
            if code: names[code] = nm
    print(f"  ML: {len(games)} games, {len(names)} team names resolved")
    for a, h, d in games:
        an, hn = names.get(a), names.get(h)
        if not an or not hn:
            print("  ML: no names for", a, h); continue
        try:
            start = dt.datetime.fromisoformat(d).replace(tzinfo=ET, hour=13).astimezone(dt.timezone.utc).isoformat()
            res = pmm_markets.lookup(client, "NFL", an, hn, start) or {}
            ml = res.get("ml") or []
            real = [e for e in ml if isinstance(e, dict) and not e.get("synthetic")]
            pick = real[0] if len(real) == 1 else None
            if pick is None:   # fall back: the entry whose title names the team first
                for e in ml:
                    t = (e.get("title") or "").lower()
                    if an.split()[-1].lower() in t and hn.split()[-1].lower() not in t: pick = {"side": "away"}; break
                    if hn.split()[-1].lower() in t and an.split()[-1].lower() not in t: pick = {"side": "home"}; break
            if pick:
                out[f"aec-nfl-{a}-{h}-{d}"] = a if pick.get("side") == "away" else h
            else:
                print("  ML: side unresolved", a, h, d, [ (e.get('side'), e.get('synthetic'), (e.get('title') or '')[:40]) for e in ml])
        except Exception as ex:
            print("  ml lookup failed", a, h, d, str(ex)[:80])
    return out


# ------------------------------------------------------------------ Poly rent, per market
def poly_rent_periods(slugs: list[str]) -> dict[str, list[str] | None]:
    """/v1/incentives?symbols= in batches. None = unreadable, [] = venue pays nothing."""
    out: dict[str, list[str] | None] = {}
    try:
        from polymarket_us import PolymarketUS
        client = PolymarketUS(key_id=os.getenv("POLYMARKET_KEY_ID"), secret_key=os.getenv("POLYMARKET_SECRET_KEY"))
    except Exception as ex:
        print("rent check unavailable:", ex); return out
    for i in range(0, len(slugs), 40):
        batch = slugs[i:i+40]
        try:
            r = client.get("/v1/incentives", query={"symbols": batch}, authenticated=True) or {}
        except Exception as ex:
            print("  incentives batch failed:", str(ex)[:100]); continue
        seen: dict[str, set] = {s: set() for s in batch}
        for m in (r.get("programs") or []):
            s = str(m.get("marketSlug") or "").strip()
            if s not in seen:
                continue
            for tp in (m.get("timePeriods") or []):
                if str(tp.get("status") or "").lower() == "active" and tp.get("period"):
                    seen[s].add(str(tp["period"]))
        for s in batch:
            out[s] = sorted(seen[s])
        time.sleep(0.25)
    return out


# ------------------------------------------------------------------ join
def build(no_rent: bool = False) -> list[dict]:
    poly = poly_universe()
    gidx, pools = gemini_index()
    ml_yes = poly_ml_yes_team(poly)
    rent = {} if no_rent else poly_rent_periods(sorted({r["slug"] for r in poly}))
    rows = []
    for r in poly:
        gk = f"{r['away']}-{r['home']}-{r['date']}"
        gm = gidx.get(gk) or {}
        pairs = []  # (poly_side, gemini_entry, gemini_outcome)
        if r["mt"] == "spread":
            if r["sgn"] == "pos":   # away +L YES  ⇔ home wins by > L : NO
                e = gm.get(("S", r["home"], r["line"]))
                if e: pairs = [("yes", e, "no"), ("no", e, "yes")]
            else:                   # away −L YES  ⇔ away wins by > L : YES
                e = gm.get(("S", r["away"], r["line"]))
                if e: pairs = [("yes", e, "yes"), ("no", e, "no")]
        elif r["mt"] == "total":
            e = gm.get(("T", r["line"]))
            if e: pairs = [("yes", e, "yes"), ("no", e, "no")]
        elif r["mt"] == "team_total":
            e = gm.get(("TT", r["team"], r["line"]))
            if e: pairs = [("yes", e, "yes"), ("no", e, "no")]
        elif r["mt"] == "moneyline":
            yt = ml_yes.get(r["slug"])
            if yt:
                other = r["home"] if yt == r["away"] else r["away"]
                ey, en = gm.get(("M", yt)), gm.get(("M", other))
                if ey: pairs.append(("yes", ey, "yes"))
                if en: pairs.append(("no", en, "yes"))
        elif r["mt"] == "prop":
            e = gm.get((r["kind"], _norm_name(r["player"]), r["line"]))
            if e: pairs = [("yes", e, "yes"), ("no", e, "no")]
        per = rent.get(r["slug"]) if not no_rent else None
        base = dict(game_key=gk, sport="NFL", market_type=r["mt"] if r["mt"] != "prop" else f"prop_{r['kind']}",
                    line=r.get("line"), poly_slug=r["slug"], poly_question=r.get("question"),
                    poly_src=r["src"], poly_rent_periods=None if per is None else ",".join(per),
                    poly_pays_now=(None if per is None else bool(per)))
        if pairs:
            for ps, e, go in pairs:
                rows.append({**base, "poly_side": ps, "gemini_symbol": e["symbol"], "gemini_outcome": go,
                             "gemini_label": e["label"], "gemini_event": e["event"], "gemini_bid": e["bid"], "gemini_ask": e["ask"],
                             "gemini_pool_usd": e["pool_usd"], "gemini_pool_makers": e["pool_makers"], "gemini_start": e["start"]})
        else:
            rows.append({**base, "poly_side": "yes", "gemini_symbol": None, "gemini_outcome": None, "gemini_label": None,
                         "gemini_event": None, "gemini_bid": None, "gemini_ask": None, "gemini_pool_usd": None,
                         "gemini_pool_makers": None, "gemini_start": None})
    return rows


DDL = """
create table if not exists venue_contract_map (
  poly_slug text not null, poly_side text not null, game_key text, sport text, market_type text,
  line numeric, poly_question text, poly_src text, poly_rent_periods text, poly_pays_now boolean,
  gemini_symbol text, gemini_outcome text, gemini_label text, gemini_event text,
  gemini_bid numeric, gemini_ask numeric, gemini_pool_usd numeric, gemini_pool_makers int, gemini_start timestamptz,
  built_at timestamptz not null default now(), primary key (poly_slug, poly_side));
create index if not exists venue_contract_map_gem on venue_contract_map (gemini_symbol);
"""
COLS = ["poly_slug","poly_side","game_key","sport","market_type","line","poly_question","poly_src","poly_rent_periods",
        "poly_pays_now","gemini_symbol","gemini_outcome","gemini_label","gemini_event","gemini_bid","gemini_ask",
        "gemini_pool_usd","gemini_pool_makers","gemini_start"]


def write_db(rows: list[dict]) -> None:
    tmp = Path("/tmp/venue_contract_map.csv")
    with tmp.open("w", newline="") as f:
        w = csv.writer(f)
        for r in rows:
            w.writerow(["" if r.get(c) is None else r.get(c) for c in COLS])
    sql = DDL + f"""
create temp table vcm_in (like venue_contract_map including defaults);
\\copy vcm_in ({",".join(COLS)}) from '{tmp}' with (format csv, null '')
insert into venue_contract_map ({",".join(COLS)}, built_at)
  select {",".join(COLS)}, now() from vcm_in
  on conflict (poly_slug, poly_side) do update set
  {", ".join(f"{c}=excluded.{c}" for c in COLS if c not in ("poly_slug","poly_side"))}, built_at=now();
notify pgrst, 'reload schema';
"""
    out = subprocess.run([PSQL, DB, "-v", "ON_ERROR_STOP=1"], input=sql, capture_output=True, text=True)
    print(out.stdout.strip()[-300:], out.stderr.strip()[-500:])


def report(rows: list[dict]) -> None:
    import collections
    by = collections.defaultdict(lambda: dict(slugs=set(), paying=set(), mapped=set(), paying_mapped=set(), pool=0.0))
    for r in rows:
        b = by[r["market_type"]]; s = r["poly_slug"]
        b["slugs"].add(s)
        if r["poly_pays_now"]: b["paying"].add(s)
        if r["gemini_symbol"]:
            b["mapped"].add(s)
            if r["poly_pays_now"]: b["paying_mapped"].add(s)
    print(f"\n{'market':<16}{'poly slugs':>11}{'poly paying':>13}{'gemini twin':>13}{'paying+twin':>13}")
    for k, b in sorted(by.items(), key=lambda kv: -len(kv[1]["slugs"])):
        print(f"{k:<16}{len(b['slugs']):>11}{len(b['paying']):>13}{len(b['mapped']):>13}{len(b['paying_mapped']):>13}")
    tot = lambda key: len({r['poly_slug'] for r in rows if (key(r))})
    print(f"{'TOTAL':<16}{tot(lambda r: True):>11}{tot(lambda r: r['poly_pays_now']):>13}{tot(lambda r: r['gemini_symbol']):>13}{tot(lambda r: r['poly_pays_now'] and r['gemini_symbol']):>13}")
    unread = tot(lambda r: r['poly_pays_now'] is None)
    if unread: print(f"(rent unreadable / skipped for {unread} slugs)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-db", action="store_true"); ap.add_argument("--no-rent", action="store_true")
    ap.add_argument("--dump", help="write rows as JSON here")
    a = ap.parse_args()
    t0 = time.time(); rows = build(no_rent=a.no_rent)
    print(f"built {len(rows)} rows in {time.time()-t0:.0f}s")
    report(rows)
    if a.dump: Path(a.dump).write_text(json.dumps(rows, indent=1, default=str))
    if not a.no_db: write_db(rows)


if __name__ == "__main__":
    main()
