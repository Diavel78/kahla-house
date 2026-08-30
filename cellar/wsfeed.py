"""THE CELLAR — websocket wake feed (v1, Aug 30 2026).

Spec: docs/ws-feed-spec.md. Read it before touching this.

DOCTRINE: SOCKET AS HINT, NEVER AS TRUTH. This module's entire output is
`wake_cb()` — "run the repeg lane now instead of at its next tick". No
engine ever reads socket state; the woken lane does its own fresh REST
reads exactly as it always has. A dead/stale/lying socket therefore
degrades to today's behavior (the scheduled lap), and there is no local
mirror to corrupt and no resync bug class to own.

Why it exists (the user's correction): rent is a time-weighted share
that accrues per-second and depends on where the order rests RIGHT NOW.
The lap bounds every reaction at 2-3 minutes; the private socket makes
fill→scalp-ask and kill→reconcile take seconds. Presence uptime is the
product.

Auth: the SDK's OWN `polymarket_us.auth.create_auth_headers` (Ed25519
over `timestamp + "GET" + path`, same headers as REST). Never
re-implement signing — drift here is an outage that looks like a venue
problem.

Failure posture: every missing prerequisite (websocket-client lib, API
creds) DISABLES the feed loudly at boot and changes nothing else. The
daemon must run identically on a box that can't run this.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

log = logging.getLogger("cellar.wsfeed")

WS_URL = "wss://api.polymarket.us/v1/ws/private"
WS_PATH = "/v1/ws/private"
SUB_ORDER = 1        # private-channel subscription types, from the venue's
SUB_POSITION = 3     # websocket overview doc (fetched Aug 30 2026)

RECV_TIMEOUT_S = 75.0    # heartbeat watchdog: silence this long = dead socket
WAKE_MIN_S = 15.0        # wake throttle; runner also skips if lane is running
BACKOFF_S = (2, 4, 8, 15, 30, 60)   # reconnect ladder, then stays at the cap
FAIL_STAMP_MIN_S = 3600  # at most one failure stamp per hour to exec_probe_runs


class WsFeed:
    """One daemon thread: connect, subscribe, wake on events, reconnect."""

    def __init__(self, wake_cb, sb=None):
        self.wake_cb = wake_cb
        self.sb = sb
        self.stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_wake = 0.0
        self._last_fail_stamp = 0.0
        self._was_connected = False
        # counters ride the transition stamps; approximate is fine
        self.msgs = 0
        self.wakes = 0
        self.reconnects = 0
        self._seen_keys: set[str] = set()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> bool:
        """Spawn the feed thread. False (with a LOUD log) when a
        prerequisite is missing — the daemon then runs exactly as it does
        without this module."""
        try:
            import websocket  # noqa: F401  (websocket-client)
        except Exception:
            log.error("ws feed DISABLED: websocket-client not installed "
                      "(python3 -m pip install websocket-client). "
                      "Lanes run on schedule as before.")
            self._stamp("disabled:no_lib")
            return False
        if not (os.environ.get("POLYMARKET_KEY_ID")
                and os.environ.get("POLYMARKET_SECRET_KEY")):
            log.error("ws feed DISABLED: POLYMARKET_KEY_ID/SECRET_KEY not in "
                      "env. Lanes run on schedule as before.")
            self._stamp("disabled:no_creds")
            return False
        self._thread = threading.Thread(target=self._run, name="cellar-ws",
                                        daemon=True)
        self._thread.start()
        log.info("ws feed started (%s)", WS_URL)
        self._stamp("started")
        return True

    def shutdown(self) -> None:
        self.stop.set()

    # -- plumbing ------------------------------------------------------------

    def _wake(self, why: str) -> None:
        now = time.time()
        if now - self._last_wake < WAKE_MIN_S:
            return
        self._last_wake = now
        self.wakes += 1
        log.info("ws wake -> repeg (%s)", why)
        try:
            self.wake_cb()
        except Exception as e:      # a wake must never kill the feed
            log.warning("wake callback failed: %s", e)

    def _stamp(self, state: str, err: str | None = None) -> None:
        """Connect/disconnect transitions to exec_probe_runs (kind=ws_feed)
        so socket health is visible OFF the box. Best-effort; failure
        stamps rate-limited so a flapping network can't flood the table."""
        if self.sb is None:
            return
        now = time.time()
        # Rate-limit only the FAILURE states — one-shot lifecycle stamps
        # (started / disabled:*) and the connected transition must always
        # land, and must not consume the failure budget (a "started" stamp
        # eating the only disconnected stamp of the hour hid the very
        # signal this table exists for).
        if state in ("disconnected", "connect_failed"):
            if now - self._last_fail_stamp < FAIL_STAMP_MIN_S:
                return
            self._last_fail_stamp = now
        try:
            self.sb.table("exec_probe_runs").insert({
                "params": {"kind": "ws_feed"},
                "result": {"state": state, "err": (err or "")[:300],
                           "msgs": self.msgs, "wakes": self.wakes,
                           "reconnects": self.reconnects},
            }).execute()
        except Exception:
            pass

    def _connect(self):
        """One signed connect + both subscriptions. Returns the socket."""
        import websocket
        from polymarket_us.auth import create_auth_headers
        hdrs = create_auth_headers(
            os.environ["POLYMARKET_KEY_ID"].strip(),
            os.environ["POLYMARKET_SECRET_KEY"].strip(),
            "GET", WS_PATH)
        # macOS venv pythons ship a bare ssl module with NO CA roots (the
        # REST side never sees this — requests/httpx carry certifi
        # themselves). First live connect died exactly there:
        # CERTIFICATE_VERIFY_FAILED, unable to get local issuer. Hand the
        # socket the same certifi bundle; NEVER disable verification.
        sslopt = None
        try:
            import certifi
            sslopt = {"ca_certs": certifi.where()}
        except Exception:
            pass
        ws = websocket.create_connection(
            WS_URL, timeout=RECV_TIMEOUT_S, sslopt=sslopt,
            header=[f"{k}: {v}" for k, v in hdrs.items()])
        for rid, sub in (("cellar-order", SUB_ORDER),
                         ("cellar-position", SUB_POSITION)):
            ws.send(json.dumps({"subscribe": {
                "request_id": rid, "subscription_type": sub}}))
        return ws

    # -- the loop ------------------------------------------------------------

    def _run(self) -> None:
        backoff_i = 0
        while not self.stop.is_set():
            ws = None
            try:
                ws = self._connect()
                backoff_i = 0
                self.reconnects += 1
                if not self._was_connected:
                    self._was_connected = True
                    self._stamp("connected")
                log.info("ws connected + subscribed (order, position)")
                # We may have been dark — one wake lets the lap resync now.
                self._wake("reconnect-resync")
                # Snapshot phase per subscription: messages before eof
                # describe STANDING state, not news — never wake on them.
                snap_open = {"cellar-order", "cellar-position"}
                while not self.stop.is_set():
                    raw = ws.recv()
                    if raw is None or raw == "":
                        raise ConnectionError("empty frame")
                    self.msgs += 1
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue            # opaque frame — not our problem
                    if not isinstance(msg, dict):
                        continue
                    if "heartbeat" in msg:
                        # Doc: "respond to heartbeats or implement your own
                        # keep-alive". Echoing is the cheapest compliance.
                        try:
                            ws.send(json.dumps({"heartbeat": {}}))
                        except Exception:
                            raise ConnectionError("heartbeat echo failed")
                        continue
                    if "error" in msg:
                        log.warning("ws error frame: %s", str(msg)[:300])
                        continue
                    rid = msg.get("request_id")
                    snap = [k for k in msg
                            if k.endswith("_subscription_snapshot")]
                    if snap:
                        body = msg.get(snap[0]) or {}
                        if isinstance(body, dict) and body.get("eof"):
                            snap_open.discard(rid)
                            log.info("ws snapshot complete: %s", rid)
                        continue            # standing state — never a wake
                    if rid in snap_open:
                        continue            # still inside that snapshot
                    # Log each new message SHAPE once — free schema recon
                    # for v2 without parsing anything now.
                    for k in msg:
                        if k not in self._seen_keys and k != "request_id":
                            self._seen_keys.add(k)
                            log.info("ws first sighting of key %r", k)
                    # Anything else after the snapshots is NEWS: an order
                    # changed, a position changed. That is the whole hint.
                    self._wake("event:" + ",".join(
                        k for k in msg if k != "request_id")[:60])
            except Exception as e:
                if self.stop.is_set():
                    break
                if self._was_connected:
                    self._was_connected = False
                    self._stamp("disconnected", err=repr(e))
                else:
                    # NEVER-CONNECTED failures must stamp too (v1 only
                    # stamped transitions, so a feed that never got its
                    # first connection wrote NOTHING — indistinguishable
                    # from healthy silence, the exact hole this table
                    # exists to close). Rate-limited like disconnects.
                    self._stamp("connect_failed", err=repr(e))
                wait = BACKOFF_S[min(backoff_i, len(BACKOFF_S) - 1)]
                backoff_i += 1
                log.warning("ws down (%s) — reconnect in %ss", e, wait)
                self.stop.wait(wait)
            finally:
                try:
                    if ws is not None:
                        ws.close()
                except Exception:
                    pass
        log.info("ws feed stopped")
