#!/usr/bin/env python3
"""Gemini tape — the whole prediction board's bid/ask, changed-only, plus the pool census.

One public request returns every active sports contract with bestBid/bestAsk, so a full
board snapshot costs nothing. Rows land in:

  gemini_snapshots (symbol, event_ticker, bid, ask, captured_at)   -- insert only when (bid,ask) changed
  gemini_pools     (event_ticker, day, pool_usd, makers, ends_at, category, title)  -- upsert per ET day

Why: the hedge-cost question ("what would the other leg have cost at fill time"), the
pool-scoring review (which rungs were empty when, who else was quoting), and Gemini's
listing lag vs Polymarket all need history this venue does not serve. Same posture as
pm_snapshots: a tape first, a lane later.

Run: .venv/bin/python kahla-scanner/scripts/gemini_tape.py [--categories sports,politics] [--once]
launchd: cellar/com.kahlahouse.gemini-tape.plist (every 120s).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import gemini_pm as g  # noqa: E402

PSQL = "/Applications/Postgres.app/Contents/Versions/latest/bin/psql"
DB = "kahla"
ET = ZoneInfo("America/New_York")

DDL = """
create table if not exists gemini_snapshots (
  id bigserial primary key, symbol text not null, event_ticker text, bid numeric, ask numeric,
  captured_at timestamptz not null default now());
create index if not exists gemini_snapshots_sym_ts on gemini_snapshots (symbol, captured_at desc);
create index if not exists gemini_snapshots_ts on gemini_snapshots (captured_at);
create table if not exists gemini_pools (
  event_ticker text not null, day date not null, pool_usd numeric, makers int, ends_at timestamptz,
  category text, title text, seen_at timestamptz not null default now(), primary key (event_ticker, day));
create table if not exists gemini_contracts (
  symbol text primary key, event_ticker text, label text, kind text, start_time timestamptz,
  expiry timestamptz, first_seen timestamptz not null default now(), last_seen timestamptz not null default now(),
  status text);
"""


def _psql(sql: str, stdin: str | None = None) -> str:
    out = subprocess.run([PSQL, DB, "-v", "ON_ERROR_STOP=1", "-At", "-F", "\t"] + (["-c", sql] if stdin is None else []),
                         input=stdin if stdin is not None else None, capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(out.stderr[:600])
    return out.stdout


def last_quotes() -> dict[str, tuple]:
    rows = _psql("select distinct on (symbol) symbol, bid, ask from gemini_snapshots "
                 "where captured_at > now() - interval '3 days' order by symbol, captured_at desc")
    out = {}
    for ln in rows.splitlines():
        s, b, a = ln.split("\t")
        out[s] = (b or None, a or None)
    return out


def _fmt(x):
    return "" if x is None else f"{float(x):.2f}"


def tick(categories: list[str]) -> dict:
    t0 = time.time()
    _psql(DDL)
    prev = last_quotes()
    snaps, contracts = [], []
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    n_ev = 0
    for cat in categories:
        for e in g.events_all(cat, "active"):
            n_ev += 1
            tk = e.get("ticker")
            for c in e.get("contracts") or []:
                sym = c.get("instrumentSymbol")
                if not sym:
                    continue
                pr = c.get("prices") or {}
                bid, ask = _fmt(g._f(pr.get("bestBid"))), _fmt(g._f(pr.get("bestAsk")))
                if prev.get(sym) != (bid or None, ask or None):
                    snaps.append([sym, tk, bid, ask, now])
                contracts.append([sym, tk, c.get("label") or "", (tk or "").split("-")[-1], e.get("startTime") or "",
                                  c.get("expiryDate") or "", now, c.get("status") or ""])
    # pools
    day = dt.datetime.now(ET).date().isoformat()
    pools = [[p["event_ticker"], day, p.get("daily_pool_usd") or "", p.get("qualifying_maker_count") if p.get("qualifying_maker_count") is not None else "",
              p.get("ends_at") or "", p.get("category") or "", (p.get("title") or "").replace("\t", " "), now] for p in g.liquidity_events()]
    # write
    def copy(table_cols: str, rows: list, tmp: str):
        p = Path(tmp)
        with p.open("w", newline="") as f:
            csv.writer(f).writerows(rows)
        return f"\\copy {table_cols} from '{p}' with (format csv, null '')\n"
    sql = "begin;\n"
    if snaps:
        sql += copy("gemini_snapshots (symbol,event_ticker,bid,ask,captured_at)", snaps, "/tmp/gem_snaps.csv")
    if contracts:
        sql += ("create temp table gc_in (symbol text, event_ticker text, label text, kind text, start_time text, expiry text, seen text, status text);\n"
                + copy("gc_in", contracts, "/tmp/gem_contracts.csv")
                + "insert into gemini_contracts (symbol,event_ticker,label,kind,start_time,expiry,first_seen,last_seen,status) "
                  "select symbol,event_ticker,label,kind,nullif(start_time,'')::timestamptz,nullif(expiry,'')::timestamptz,seen::timestamptz,seen::timestamptz,status from gc_in "
                  "on conflict (symbol) do update set last_seen=excluded.last_seen, status=excluded.status, label=excluded.label;\n")
    if pools:
        sql += ("create temp table gp_in (event_ticker text, day date, pool_usd numeric, makers int, ends_at text, category text, title text, seen text);\n"
                + copy("gp_in", pools, "/tmp/gem_pools.csv")
                + "insert into gemini_pools (event_ticker,day,pool_usd,makers,ends_at,category,title,seen_at) "
                  "select event_ticker,day,pool_usd,makers,nullif(ends_at,'')::timestamptz,category,title,seen::timestamptz from gp_in "
                  "on conflict (event_ticker,day) do update set pool_usd=excluded.pool_usd, makers=excluded.makers, seen_at=excluded.seen_at;\n")
    sql += "commit;\n"
    _psql("", stdin=sql)
    return {"events": n_ev, "contracts": len(contracts), "changed": len(snaps), "pools": len(pools), "ms": int((time.time() - t0) * 1000)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--categories", default="sports")
    a = ap.parse_args()
    r = tick([c.strip() for c in a.categories.split(",") if c.strip()])
    print(dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET"), r)


if __name__ == "__main__":
    main()
