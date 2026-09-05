"""WS CAP PROBE — measure the markets socket's real subscription limit.

Spec: docs/ws-quote-table-spec.md ("The cap probe"). The 400-slug cap in
wsfeed.py is OUR constant born from one best-practice sentence; the venue
documents no numeric limit and the SDK's marketSlugs list is unbounded.
Before the quote table can carry the whole board (~4,700 slugs), someone
has to ask the venue how many it will actually take. This script asks.

STANDALONE by design: its own TCP connection, its own request ids —
errors are per-rid, so nothing here can disturb the daemon's sockets
(which may be off anyway; the probe does not care).

Run ON THE BOX from the repo root (env comes from app's own loading):

    python3 -m cellar.ws_cap_probe            # ladder to full board
    python3 -m cellar.ws_cap_probe 800        # single attempt at N=800

Output: one line per attempt (n, ok/error, baseline frames, seconds),
then a steady-state frame-rate read at the largest accepted size, then
a summary stamped to exec_probe_runs (kind=ws_cap_probe) so the result
survives the terminal. Set wsfeed.MKTS_MAX_SLUGS from the measurement,
not vibes.
"""
from __future__ import annotations

import json
import sys
import time

from .wsfeed import WS_MKTS_URL, WS_MKTS_PATH, SUB_MARKET_LITE

LISTEN_S = 8.0        # per-attempt frame collection window
RATE_S = 12.0         # steady-state frame-rate window after the ladder
LADDER = (500, 1000, 2000, 4000)   # then the full pool as the last rung


def _connect():
    import os
    import websocket
    from polymarket_us.auth import create_auth_headers
    hdrs = create_auth_headers(
        os.environ["POLYMARKET_KEY_ID"].strip(),
        os.environ["POLYMARKET_SECRET_KEY"].strip(),
        "GET", WS_MKTS_PATH)
    sslopt = None
    try:
        import certifi
        sslopt = {"ca_certs": certifi.where()}
    except Exception:
        pass
    return websocket.create_connection(
        WS_MKTS_URL, timeout=5.0, sslopt=sslopt,
        header=[f"{k}: {v}" for k, v in hdrs.items()])


def _slug_pool(sb) -> list:
    """Real, live slugs at board scale: the venue's own enrolled-market
    mirror (catalog ∪ rewards page, 48h) plus the slugs of our own
    pending picks. Order: picks first (certainly-live markets), then
    enrolled football, then whatever else — the probe subscribes real
    markets so baseline-replay behavior is the real thing."""
    pool: list = []
    seen: set = set()

    def add(s):
        if s and isinstance(s, str) and s not in seen:
            seen.add(s)
            pool.append(s)

    from datetime import datetime, timedelta, timezone
    cut = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    try:
        rows = (sb.table("bot_picks").select("signal_blob")
                .eq("status", "pending").limit(400).execute().data) or []
        for r in rows:
            b = r.get("signal_blob") if isinstance(r.get("signal_blob"),
                                                   dict) else {}
            add(b.get("pmm_slug"))
    except Exception:
        pass
    for tbl, col, tcol in (("rent_list_slugs", "slug", "last_seen"),
                           ("poly_incentive_programs", "market_slug",
                            "synced_at")):
        try:
            pg = 0
            while True:      # gotcha #40 — page, never trust one response
                page = (sb.table(tbl).select(col).gte(tcol, cut)
                        .range(pg * 1000, pg * 1000 + 999)
                        .execute().data) or []
                for r in page:
                    add(r.get(col))
                if len(page) < 1000:
                    break
                pg += 1
        except Exception:
            pass
    return pool


def _drain(ws, rid: str, secs: float) -> dict:
    """Collect frames for `secs`; count LITE frames + catch a per-rid
    error. Heartbeats echoed (silence = venue closes the socket)."""
    t0 = time.time()
    lite = 0
    err = None
    while time.time() - t0 < secs:
        try:
            raw = ws.recv()
        except Exception:
            continue
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        if not isinstance(msg, dict):
            continue
        if "heartbeat" in msg:
            try:
                ws.send(json.dumps({"heartbeat": {}}))
            except Exception:
                pass
            continue
        if "error" in msg and (msg.get("requestId") in (None, rid)):
            err = str(msg.get("error"))[:200]
            break
        if "marketDataLite" in msg:
            lite += 1
    return {"lite": lite, "err": err, "secs": round(time.time() - t0, 1)}


def main() -> int:
    import app as _app     # env loading + get_supabase, the daemon's own
    sb = _app.get_supabase()
    pool = _slug_pool(sb)
    print(f"slug pool: {len(pool)} live slugs")
    if len(pool) < 50:
        print("pool too small to probe — is the DB reachable?")
        return 2
    one_n = None
    if len(sys.argv) > 1:
        one_n = max(1, int(sys.argv[1]))
    sizes = [one_n] if one_n else \
        [n for n in LADDER if n < len(pool)] + [len(pool)]

    attempts = []
    best_ok = 0
    ws = _connect()
    print("connected", WS_MKTS_URL)
    try:
        prev_rid = None
        for n in sizes:
            rid = f"capprobe-{n}"
            ws.send(json.dumps({"subscribe": {
                "requestId": rid,
                "subscriptionType": SUB_MARKET_LITE,
                "marketSlugs": pool[:n]}}))
            if prev_rid:
                ws.send(json.dumps({"unsubscribe": {"requestId": prev_rid}}))
            prev_rid = rid
            r = _drain(ws, rid, LISTEN_S)
            r["n"] = n
            attempts.append(r)
            ok = r["err"] is None
            print(f"  n={n:>5}  {'OK ' if ok else 'ERR'}  "
                  f"baselines={r['lite']:>5} in {r['secs']}s"
                  + (f"  err={r['err']}" if r["err"] else ""))
            if ok:
                best_ok = n
            else:
                break
        rate = None
        if best_ok:
            r2 = _drain(ws, prev_rid, RATE_S)
            rate = round(r2["lite"] / max(r2["secs"], 0.1), 1)
            print(f"steady-state at n={best_ok}: {r2['lite']} frames "
                  f"in {r2['secs']}s = {rate}/s")
        try:
            ws.send(json.dumps({"unsubscribe": {"requestId": prev_rid}}))
        except Exception:
            pass
    finally:
        try:
            ws.close()
        except Exception:
            pass

    res = {"kind": "ws_cap_probe", "pool": len(pool), "best_ok": best_ok,
           "attempts": attempts, "steady_fps": rate}
    try:
        sb.table("exec_probe_runs").insert(
            {"params": {"kind": "ws_cap_probe"}, "result": res}).execute()
        print("stamped exec_probe_runs kind=ws_cap_probe")
    except Exception as e:
        print("stamp failed:", e)
    print(json.dumps(res)[:600])
    return 0


if __name__ == "__main__":
    sys.exit(main())
