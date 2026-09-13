#!/usr/bin/env python3
"""Gemini SOCKET tape — the whole board on one websocket, persisted one changed quote per
symbol per minute. Replaces the 2-minute REST poll (gemini_tape.py keeps the pool census +
contract registry helpers; this process calls them on its refresh cycle).

Measured Sep 13 2026: one connection accepted 4,784 bookTicker subscriptions (the entire
sports board) with no cap; ~280 frames/s Sunday evening; a subscribe frame must stay under
the venue's message-size limit (200 symbols per request closed the socket with 1009; 25 is
safe). Every frame updates an in-memory quote; the minute flush writes only symbols whose
(bid, ask) differ from what was last written — the shared Postgres never sees per-frame rows.

FERRARI RULE: separate process, separate tables (gemini_*), no Polymarket calls, bounded
writes. If it misbehaves, `launchctl kickstart -k gui/$(id -u)/com.kahlahouse.gemini-wstape`
or kill it; nothing in the money daemon depends on it.

Run: .venv/bin/python kahla-scanner/scripts/gemini_ws_tape.py  (launchd: KeepAlive)
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import os
import signal
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "kahla-scanner" / "scripts"))
import websocket  # noqa: E402
import gemini_pm as g  # noqa: E402
import gemini_tape as rest_tape  # noqa: E402  (pool census + contract registry + DDL)

log = logging.getLogger("gemini_ws_tape")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ET = ZoneInfo("America/New_York")
SUB_BATCH = 25
FLUSH_S = int(os.getenv("GEMINI_TAPE_FLUSH_S", "60"))
REFRESH_S = int(os.getenv("GEMINI_TAPE_REFRESH_S", "600"))
CATEGORIES = [c for c in os.getenv("GEMINI_TAPE_CATEGORIES", "sports").split(",") if c]
STALE_CLOSE_S = 90     # no frame (not even a ping) for this long → reconnect


def _c(x):
    """Normalize a price string to 2-dp text ('0.4600' and '0.46' must compare equal); '' → None."""
    try:
        return None if x in (None, "") else f"{float(x):.2f}"
    except (TypeError, ValueError):
        return None


class Tape:
    def __init__(self):
        self.lock = threading.Lock()
        self.quotes: dict[str, tuple] = {}        # symbol → (bid, ask)
        self.event_of: dict[str, str] = {}        # symbol → event ticker
        self.canon: dict[str, str] = {}           # lowercase → canonical symbol
        self.written: dict[str, tuple] = {}       # symbol → last (bid, ask) persisted
        self.subscribed: set[str] = set()
        self.frames = 0
        self.last_rx = time.monotonic()
        self.ws: websocket.WebSocketApp | None = None
        self.stop = False

    # ---------------------------------------------------------------- symbols
    def refresh_symbols(self) -> tuple[set[str], set[str]]:
        """REST pass: contracts + pools + registry. Returns (new, gone) symbol sets."""
        active: dict[str, str] = {}
        for cat in CATEGORIES:
            for e in g.events_all(cat, "active"):
                tk = e.get("ticker")
                for c in e.get("contracts") or []:
                    s = c.get("instrumentSymbol")
                    if s:
                        active[s] = tk
        with self.lock:
            new = set(active) - self.subscribed
            gone = self.subscribed - set(active)
            self.event_of.update(active)
            for s in active:
                self.canon[s.lower()] = s
        try:
            r = rest_tape.tick(CATEGORIES)   # pools + registry (+ REST-side changed quotes)
            log.info("refresh: %s", r)
            with self.lock:                  # re-sync so the socket flush never re-writes what REST just wrote
                self.written.update({k: (_c(v[0]), _c(v[1])) for k, v in rest_tape.last_quotes().items()})
        except Exception as ex:
            log.warning("refresh tick failed: %s", str(ex)[:200])
        return new, gone

    # ---------------------------------------------------------------- socket
    def on_message(self, ws, m):
        self.last_rx = time.monotonic()
        try:
            d = json.loads(m)
        except Exception:
            return
        s = d.get("s")
        if s and "b" in d and "a" in d and "e" not in d:      # bookTicker
            sym = self.canon.get(s) or s.upper()
            bid = _c(d.get("b")); ask = _c(d.get("a"))
            with self.lock:
                self.quotes[sym] = (bid, ask)
                self.frames += 1
        elif d.get("id") and d.get("status") not in (None, 200):
            log.warning("ws error: %s", m[:200])

    def on_open(self, ws):
        log.info("socket open; subscribing %d symbols", len(self.pending_subs))
        self._send_subs(ws, sorted(self.pending_subs), "subscribe")
        with self.lock:
            self.subscribed |= self.pending_subs
        self.pending_subs = set()

    def _send_subs(self, ws, syms: list[str], method: str):
        for i in range(0, len(syms), SUB_BATCH):
            batch = syms[i:i + SUB_BATCH]
            try:
                ws.send(json.dumps({"id": f"{method[:3]}{i}", "method": method,
                                    "params": [f"{s.lower()}@bookTicker" for s in batch]}))
            except Exception as ex:
                log.warning("send %s failed: %s", method, ex)
                return
            time.sleep(0.03)

    def run_socket(self):
        backoff = 2
        while not self.stop:
            with self.lock:
                self.pending_subs = set(self.event_of)      # (re)subscribe everything we know
                self.subscribed = set()
            self.ws = websocket.WebSocketApp("wss://ws.gemini.com", on_open=self.on_open, on_message=self.on_message,
                                             on_error=lambda w, e: log.warning("ws error cb: %s", e),
                                             on_close=lambda w, a, b: log.info("ws closed %s %s", a, b))
            self.last_rx = time.monotonic()
            try:
                self.ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE}, ping_interval=0)
            except Exception as ex:
                log.warning("run_forever: %s", ex)
            if self.stop:
                break
            log.info("reconnecting in %ss", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    # ---------------------------------------------------------------- flush
    def flush(self):
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        rows = []
        with self.lock:
            for sym, q in self.quotes.items():
                if self.written.get(sym) != q:
                    rows.append([sym, self.event_of.get(sym, ""), q[0] or "", q[1] or "", now])
                    self.written[sym] = q
            fr, self.frames = self.frames, 0
        if rows:
            p = Path("/tmp/gem_ws_snaps.csv")
            with p.open("w", newline="") as f:
                csv.writer(f).writerows(rows)
            try:
                rest_tape._psql("", stdin=f"\\copy gemini_snapshots (symbol,event_ticker,bid,ask,captured_at) from '{p}' with (format csv, null '')\n")
            except Exception as ex:
                log.warning("flush write failed: %s", str(ex)[:200])
        log.info("flush: %d changed of %d quoted · %d frames/min · subs %d", len(rows), len(self.quotes), fr, len(self.subscribed))


def main():
    t = Tape()
    rest_tape._psql(rest_tape.DDL)
    # seed the 'written' map from the DB so a restart doesn't re-insert unchanged quotes
    t.written = {k: (_c(v[0]), _c(v[1])) for k, v in rest_tape.last_quotes().items()}
    new, _ = t.refresh_symbols()
    log.info("symbols known: %d", len(t.event_of))
    threading.Thread(target=t.run_socket, daemon=True).start()

    def _stop(*_):
        t.stop = True
        try:
            t.ws and t.ws.close()
        except Exception:
            pass
        t.flush()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    last_flush = last_refresh = time.monotonic()
    while True:
        time.sleep(1)
        now = time.monotonic()
        if now - last_flush >= FLUSH_S:
            t.flush(); last_flush = now
        if now - last_refresh >= REFRESH_S:
            try:
                new, gone = t.refresh_symbols()
                if t.ws and (new or gone):
                    if new:
                        t._send_subs(t.ws, sorted(new), "subscribe")
                        with t.lock:
                            t.subscribed |= new
                    if gone:
                        t._send_subs(t.ws, sorted(gone), "unsubscribe")
                        with t.lock:
                            t.subscribed -= gone
                            for s in gone:
                                t.quotes.pop(s, None)
                    log.info("subs +%d -%d", len(new), len(gone))
            except Exception as ex:
                log.warning("refresh failed: %s", str(ex)[:200])
            last_refresh = now
        if now - t.last_rx > STALE_CLOSE_S and t.ws:
            log.warning("no frames for %ss — forcing reconnect", STALE_CLOSE_S)
            try:
                t.ws.close()
            except Exception:
                pass
            t.last_rx = now


if __name__ == "__main__":
    main()
