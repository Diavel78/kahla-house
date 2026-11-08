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

THE MARKET LIST CHANGES ON THE LIVE CONNECTION (Nov 7 2026, BYU +6.5 on
Kalshi: the chase rejoined at 49¢ off a book that still showed pre-play
bids while Kalshi refused 52¢ and 50¢ as crossing the real asks; Rob:
"You have a WebSocket… why can't you read the fucking WebSocket?"). Until
then every change to the watched set — a placement, a fill, a pre-subscribe
expiring — HUNG UP and reconnected (84 times in one morning), and between
the reconnect and each market's new snapshot the app kept serving that
market's book from the OLD connection as fresh. Now a change is an
`update_subscription` (add_markets / delete_markets) on the same socket,
an added market is untrusted until ITS snapshot lands (`get_snapshot` if
it doesn't come, reconnect as the last resort), and every book carries the
connection epoch that built it (`app.KALSHI_SNAP_EPOCH`) — `_kalshi_ws_fresh`
only vouches for a book snapshotted on the live connection.
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
SNAP_WAIT_S = 5.0        # an added market with no snapshot this long: ask for one (get_snapshot)
SNAP_GIVEUP_S = 15.0     # still none: reconnect (full snapshots) — the old path, last resort


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
        self._seq: dict = {}             # sid → last seq seen (reset per connection)
        self.seq_gaps = 0
        self.suspect = 0                 # snapshots whose shape we could not read
        self.snapshots = 0
        self.epoch = 0                   # one per connection; a book is trusted only on its own epoch
        self.sid = None                  # the orderbook_delta subscription id (update_subscription needs it)
        self._cmd_id = 1
        self._pending: dict = {}         # ticker → (monotonic sent, asked_snapshot) awaiting a snapshot
        self.updates = 0                 # market-list changes made on the live connection
        self.update_fail = 0

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
                           "reconnects": self.reconnects, "snapshots": self.snapshots,
                           "seq_gaps": self.seq_gaps, "shape_unknown": self.suspect,
                           "epoch": self.epoch, "updates": self.updates, "update_fail": self.update_fail,
                           "tickers": sorted(self.subscribed)[:40]},
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
            p["epoch"] = self.epoch
            p["updates"] = self.updates
            p["msgs"] = self.msgs
            p["snapshots"] = self.snapshots
            p["seq_gaps"] = self.seq_gaps
            p["shape_unknown"] = self.suspect
        except Exception:
            pass

    # -- the watched set -------------------------------------------------
    @staticmethod
    def _want() -> set:
        try:
            import app as _app
            # a Gemini contract in Kalshi's slot (`GEMI-…`, Nov 8 2026) is never sent to Kalshi —
            # Gemini's own socket (app._gem_ws_run) watches it
            return set(x for x in (_app.KALSHI_WATCH_TICKERS or set()) if x and not str(x).upper().startswith("GEMI-"))
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
        if t in ("orderbook_snapshot", "orderbook_delta"):
            # THE SEQUENCE IS THE CONTRACT: a gap means a frame was lost and
            # the local book is no longer the venue's — resync (reconnect =
            # fresh snapshots). The venue's own doc says to check it.
            if not kv.ws_seq_ok(self._seq, msg.get("sid"), msg.get("seq")):
                self.seq_gaps += 1
                raise ConnectionError(f"seq gap sid={msg.get('sid')} seq={msg.get('seq')}")
        if t == "orderbook_snapshot":
            tk = body.get("market_ticker")
            if tk:
                st = kv.ws_snapshot(body)
                self.books[tk] = st
                self._pending.pop(tk, None)
                if kv.ws_snapshot_suspect(body, st):
                    # a renamed wire: the body carries levels we did not read.
                    # Never a quiet empty book again (Oct 18 2026 — `yes_dollars_fp`).
                    self.suspect += 1
                    if self.suspect <= 3:
                        log.error("kalshi ws SNAPSHOT SHAPE UNKNOWN %s keys=%s", tk, st.get("keys"))
                        self._stamp("snapshot_shape_unknown", f"{tk} keys={st.get('keys')}")
                    self.books.pop(tk, None)         # no book is better than a wrong one
                    return
                self.snapshots += 1
                try:
                    import app as _app
                    _app.KALSHI_SNAP_EPOCH[tk] = self.epoch      # this connection built this book
                except Exception:
                    pass
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
            if msg.get("id") is not None and int(msg.get("id") or 0) > 1:
                # a market-list change the venue refused: resync the old way
                self.update_fail += 1
                raise ConnectionError(f"update_subscription refused: {str(body or msg)[:120]}")
        elif t == "subscribed":
            log.info("kalshi ws subscribed: %s", str(msg)[:200])
            if body.get("channel") in (None, "orderbook_delta") and body.get("sid") is not None:
                try:
                    self.sid = int(body.get("sid"))
                except (TypeError, ValueError):
                    self.sid = None

    def _send(self, ws, action: str, tickers) -> None:
        self._cmd_id += 1
        ws.send(json.dumps({"id": self._cmd_id, "cmd": "update_subscription",
                            "params": {"sids": [self.sid], "market_tickers": sorted(tickers),
                                       "action": action}}))

    def _update_markets(self, ws, new: set) -> None:
        """Pure-ish: the watched set changed — add/remove markets on the live
        connection. An added market's book is untrusted until its snapshot."""
        add, rem = new - self.subscribed, self.subscribed - new
        import app as _app
        if rem:
            self._send(ws, "delete_markets", rem)
            for tk in rem:
                self.books.pop(tk, None)
                self._pending.pop(tk, None)
                _app.KALSHI_SNAP_EPOCH.pop(tk, None)
        if add:
            self._send(ws, "add_markets", add)
            now_m = time.monotonic()
            for tk in add:
                self.books.pop(tk, None)
                _app.KALSHI_SNAP_EPOCH.pop(tk, None)
                self._pending[tk] = (now_m, False)
        self.subscribed = set(new)
        self.updates += 1
        self._presence(True)

    def _snap_watch(self, ws) -> None:
        """Added markets whose snapshot never came: ask once, then give up
        (reconnect = full snapshots). Plus any ticker the app distrusts
        (a refused order proved its copy wrong): re-snapshot it."""
        import app as _app
        now_m = time.monotonic()
        ask = set()
        try:
            want = set(getattr(_app, "KALSHI_RESNAP", set()) or set())
            if want:
                _app.KALSHI_RESNAP.difference_update(want)
                for tk in want & self.subscribed:
                    _app.KALSHI_SNAP_EPOCH.pop(tk, None)
                    self._pending[tk] = (now_m, True)
                    ask.add(tk)
        except Exception:
            pass
        for tk, (at, asked) in list(self._pending.items()):
            if tk not in self.subscribed:
                self._pending.pop(tk, None)
            elif not asked and now_m - at >= SNAP_WAIT_S:
                self._pending[tk] = (at, True)
                ask.add(tk)
            elif asked and now_m - at >= SNAP_GIVEUP_S:
                raise ConnectionError(f"no snapshot for {tk} in {SNAP_GIVEUP_S:.0f}s")
        if ask and self.sid is not None:
            self._send(ws, "get_snapshot", ask)

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
                self.epoch += 1               # every book from the last connection is now untrusted
                self.sid = None
                self._pending = {}
                self._cmd_id = 1
                ws = self._connect(want)
                self.subscribed = set(want)
                self.books = {}
                self._seq = {}
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
                        if not new:
                            break
                        if new != self.subscribed:
                            if self.sid is not None:
                                log.info("kalshi ws watch set changed (%d → %d) — updating on the live connection",
                                         len(self.subscribed), len(new))
                                self._update_markets(ws, new)
                            elif now_m - (getattr(self, "_sub_at", 0.0)) > RESUB_MIN_S:
                                self._sub_at = now_m
                                log.info("kalshi ws watch set changed (%d → %d), no sid yet — resubscribing",
                                         len(self.subscribed), len(new))
                                break            # reconnect with the new set (full snapshots)
                        self._snap_watch(ws)
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
