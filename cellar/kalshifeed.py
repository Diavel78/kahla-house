"""KALSHI ORDER-BOOK SOCKET (Oct 18 2026 — Rob: "Nothing functions on line
moves if you can't get the websockets and volume working").

One daemon thread on the box: an authenticated connection to Kalshi's
WebSocket (wss://external-api-ws.kalshi.com/trade-api/ws/v2, the REST
signing scheme over `GET /trade-api/ws/v2`), the `orderbook_delta` channel
for every Kalshi market a hand bet rests on or holds (`app.KALSHI_WATCH_TICKERS`,
refreshed by the strip publisher each tick), a local book per market built
from the snapshot + deltas, and ONE tape row per change into
`app.KALSHI_TAPE` through `app._kalshi_tape_record` — the same row shape
the Polymarket depth tape has, so the start rule, the line-move rule and
the thin-touch guard read a Kalshi bet exactly as they read a Polymarket
one. Presence rides `app.KALSHI_WS` so the REST sampler stands down while
the socket speaks.

DOCTRINE (the same as wsfeed): the socket is a TAPE, never a decision. A
dead socket degrades to the REST sample each handbets tick.

Resubscribe = reconnect: the watched set changes rarely (a placement, a
switch, a fill) and the venue replays a full snapshot per market on
subscribe, so a fresh connection is the simplest correct resync.
"""
from __future__ import annotations

import json
import logging
import threading
import time

log = logging.getLogger("cellar.kalshifeed")

WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
WS_URL_ALT = "wss://api.elections.kalshi.com/trade-api/ws/v2"
RECV_S = 15.0            # recv timeout; the loop re-checks the watch set between frames
SILENCE_S = 90.0         # no frame (ping included) this long = dead socket
BACKOFF_S = (2, 4, 8, 15, 30, 60)
FAIL_STAMP_MIN_S = 3600
WATCH_POLL_S = 2.0
RESUB_MIN_S = 5.0


class KalshiFeed:
    def __init__(self, sb=None):
        self.sb = sb
        self.stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.msgs = 0
        self.reconnects = 0
        self._last_fail_stamp = 0.0
        self._was_connected = False
        self.books: dict = {}            # ticker → {"yes": {c: q}, "no": {c: q}}
        self.subscribed: set = set()
        self._url_i = 0

    def start(self) -> bool:
        try:
            import websocket  # noqa: F401
        except Exception:
            log.error("kalshi ws DISABLED: websocket-client not installed")
            self._stamp("disabled:no_lib")
            return False
        try:
            import kalshi_venue as kv
            if kv.ws_auth_headers() is None:
                log.error("kalshi ws DISABLED: no Kalshi creds (kalshi_state / env)")
                self._stamp("disabled:no_creds")
                return False
        except Exception as e:
            log.error("kalshi ws DISABLED: %s", e)
            self._stamp("disabled:creds_error")
            return False
        self._thread = threading.Thread(target=self._run, name="cellar-kalshi-ws", daemon=True)
        self._thread.start()
        self._stamp("started")
        log.info("kalshi ws feed started (%s)", WS_URL)
        return True

    def shutdown(self) -> None:
        self.stop.set()

    # -- stamps ----------------------------------------------------------
    def _stamp(self, state: str, err: str | None = None) -> None:
        if self.sb is None:
            return
        now = time.time()
        if state in ("disconnected", "connect_failed"):
            if now - self._last_fail_stamp < FAIL_STAMP_MIN_S:
                return
            self._last_fail_stamp = now
        try:
            self.sb.table("exec_probe_runs").insert({
                "params": {"kind": "kalshi_ws"},
                "result": {"kalshi_ws": state, "state": state, "err": (err or "")[:300], "msgs": self.msgs,
                           "reconnects": self.reconnects, "tickers": sorted(self.subscribed)[:40]},
            }).execute()
        except Exception:
            pass

    def _presence(self, up: bool, rx: bool = False) -> None:
        try:
            import app as _app
            p = _app.KALSHI_WS
            p["up"] = up
            if rx:
                p["rx"] = time.time()
            p["tickers"] = set(self.subscribed)
            p["msgs"] = self.msgs
        except Exception:
            pass

    # -- the watched set -------------------------------------------------
    @staticmethod
    def _want() -> set:
        try:
            import app as _app
            return set(x for x in (_app.KALSHI_WATCH_TICKERS or set()) if x)
        except Exception:
            return set()

    # -- connection -----------------------------------------------------
    def _connect(self, tickers: set):
        import websocket
        import kalshi_venue as kv
        hdrs = kv.ws_auth_headers()
        if hdrs is None:
            raise ConnectionError("no creds")
        sslopt = None
        try:
            import certifi
            sslopt = {"ca_certs": certifi.where()}
        except Exception:
            pass
        url = (WS_URL, WS_URL_ALT)[self._url_i % 2]
        ws = websocket.create_connection(url, timeout=RECV_S, sslopt=sslopt, header=hdrs)
        if tickers:
            ws.send(json.dumps({"id": 1, "cmd": "subscribe",
                                "params": {"channels": ["orderbook_delta"],
                                           "market_tickers": sorted(tickers)}}))
        return ws

    def _on_frame(self, msg: dict) -> None:
        import kalshi_venue as kv
        t = msg.get("type")
        body = msg.get("msg") if isinstance(msg.get("msg"), dict) else {}
        if t == "orderbook_snapshot":
            tk = body.get("market_ticker")
            if tk:
                self.books[tk] = kv.ws_snapshot(body)
                self._record(tk)
        elif t == "orderbook_delta":
            tk = body.get("market_ticker")
            if tk:
                st = self.books.get(tk)
                if st is None:
                    return                       # a delta before its snapshot: wait for the snapshot
                kv.ws_delta(st, body)
                self._record(tk)
        elif t == "error":
            log.warning("kalshi ws error frame: %s", str(msg)[:300])
        elif t == "subscribed":
            log.info("kalshi ws subscribed: %s", str(msg)[:200])

    def _record(self, tk: str) -> None:
        try:
            import app as _app
            import kalshi_venue as kv
            _app._kalshi_tape_record(tk, kv.ws_to_ob(self.books[tk]))
        except Exception as e:
            log.debug("kalshi tape record %s: %s", tk, e)

    def _run(self) -> None:
        import websocket
        backoff_i = 0
        while not self.stop.is_set():
            want = self._want()
            if not want:
                self._presence(False)
                self.stop.wait(WATCH_POLL_S)
                continue
            ws = None
            try:
                ws = self._connect(want)
                self.subscribed = set(want)
                self.books = {}
                self.reconnects += 1
                backoff_i = 0
                if not self._was_connected:
                    self._was_connected = True
                    self._stamp("connected")
                self._presence(True, rx=True)
                log.info("kalshi ws connected: %d market(s)", len(want))
                last_rx = time.monotonic()
                last_check = time.monotonic()
                while not self.stop.is_set():
                    try:
                        op, frame = ws.recv_data(control_frame=True)
                    except websocket.WebSocketTimeoutException:
                        op, frame = None, None
                    now_m = time.monotonic()
                    if op is not None:
                        last_rx = now_m
                        self._presence(True, rx=True)
                        if op == websocket.ABNF.OPCODE_PING:
                            try:
                                ws.pong(frame)
                            except Exception:
                                raise ConnectionError("pong failed")
                        elif op == websocket.ABNF.OPCODE_TEXT:
                            self.msgs += 1
                            try:
                                msg = json.loads(frame.decode("utf-8") if isinstance(frame, bytes) else frame)
                            except Exception:
                                msg = None
                            if isinstance(msg, dict):
                                self._on_frame(msg)
                        elif op == websocket.ABNF.OPCODE_CLOSE:
                            raise ConnectionError("closed by venue")
                    if now_m - last_rx > SILENCE_S:
                        raise ConnectionError(f"silent socket {SILENCE_S:.0f}s")
                    if now_m - last_check >= WATCH_POLL_S:
                        last_check = now_m
                        new = self._want()
                        if new != self.subscribed and now_m - (getattr(self, "_sub_at", 0.0)) > RESUB_MIN_S:
                            self._sub_at = now_m
                            log.info("kalshi ws watch set changed (%d → %d) — resubscribing",
                                     len(self.subscribed), len(new))
                            break                # reconnect with the new set (full snapshots)
                        if not new:
                            break
                try:
                    ws.close()
                except Exception:
                    pass
                self._presence(False)
                if self.stop.is_set():
                    break
                continue                          # the resubscribe path: no backoff
            except Exception as e:
                self._presence(False)
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                self._url_i += 1                  # alternate hosts on failure
                wait = BACKOFF_S[min(backoff_i, len(BACKOFF_S) - 1)]
                backoff_i += 1
                log.warning("kalshi ws down (%s: %s) — reconnect in %ss", type(e).__name__, str(e)[:120], wait)
                self._stamp("disconnected" if self._was_connected else "connect_failed", str(e)[:200])
                self.stop.wait(wait)
        self._presence(False)
