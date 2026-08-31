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
# ⚠ THE WIRE IS THE SDK'S, NOT THE PROSE DOC'S (Aug 31 2026, the user's
# catch after a night of silence: "READ THE INSTRUCTIONS"). The
# websocket overview page says snake_case fields and INTEGER
# subscription types — the vendor's own client
# (polymarket_us/websocket/{base,private,markets,types}.py) sends
# camelCase (`requestId`, `subscriptionType`, `marketSlugs`) and STRING
# enums, and parses camelCase response keys (orderSubscriptionSnapshot,
# positionSubscriptionUpdate, marketDataLite). Frames built from the
# prose doc were silently ignored — a "connected" socket that only ever
# heard heartbeats. When these disagree again, the SDK source wins.
SUB_ORDER = "SUBSCRIPTION_TYPE_ORDER"
SUB_POSITION = "SUBSCRIPTION_TYPE_POSITION"

# Markets socket (v2, Aug 30 2026 — user: "anything that increases our
# speed pays better money"). Same auth scheme, own path; subscription
# type 2 = MARKET_DATA_LITE (lightweight price data — the outbid hint;
# the full book would be truth we must not hold). Docs: websocket
# overview, fetched via /api/docs-fetch Aug 31 2026.
WS_MKTS_URL = "wss://api.polymarket.us/v1/ws/markets"
WS_MKTS_PATH = "/v1/ws/markets"
SUB_MARKET_LITE = "SUBSCRIPTION_TYPE_MARKET_DATA_LITE"
MKTS_MAX_SLUGS = 400     # venue best practice: only subscribe what you need

RECV_TIMEOUT_S = 75.0    # heartbeat watchdog: silence this long = dead socket
WAKE_MIN_S = 15.0        # private-event wake throttle (repeg)
WAKE_MKTS_MIN_S = 10.0   # market-move wake throttle (outbid reaction)
WAKE_OPENER_MIN_S = 60.0  # position-event wake throttle (rebuy after exit)
BACKOFF_S = (2, 4, 8, 15, 30, 60)   # reconnect ladder, then stays at the cap
FAIL_STAMP_MIN_S = 3600  # at most one failure stamp per hour to exec_probe_runs

_SLUG_KEYS = ("market_slug", "marketSlug", "slug", "symbol",
              "market", "ticker", "market_ticker")
_UUID_RE = None


def _slugish(v) -> bool:
    """A market slug, not a UUID and not a short token. UUIDs pass the
    dash test (36 hex chars, 4 dashes) and would subscribe to nothing —
    exclude them by shape."""
    global _UUID_RE
    if not (isinstance(v, str) and "-" in v and 8 < len(v) < 120):
        return False
    if _UUID_RE is None:
        import re
        _UUID_RE = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
    return not _UUID_RE.match(v)


def _harvest_slugs(obj, out: set, depth: int = 0) -> None:
    """Tolerantly pull market slugs out of an order frame. The venue's WS
    order shapes are documented loosely (snake_case per the overview, but
    the REST twin is camelCase) — try both, validate by shape, never
    raise. Used only to decide WHICH markets to watch: a miss costs a
    hint, never a bet."""
    if depth > 3:
        return
    if isinstance(obj, dict):
        for k in _SLUG_KEYS:
            v = obj.get(k)
            if _slugish(v):
                out.add(v)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _harvest_slugs(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:500]:
            _harvest_slugs(v, out, depth + 1)


def _recon_keys(obj, depth: int = 0) -> str | None:
    """Key names (never values) of the first order-shaped dict in a
    frame — the self-diagnosis for a harvest miss, so a field-name
    mismatch reports the REAL schema instead of just watching nothing.
    First live night proved the need: 130 resting orders, watched=0,
    and no way to see why from off the box."""
    if depth > 3:
        return None
    if isinstance(obj, dict):
        ks = [k for k in obj if isinstance(k, str)]
        if len(ks) >= 3 and not any(
                k.endswith("_subscription_snapshot") for k in ks):
            return ",".join(sorted(ks))[:280]
        for v in obj.values():
            r = _recon_keys(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj[:5]:
            r = _recon_keys(v, depth + 1)
            if r:
                return r
    return None


class WsFeed:
    """One daemon thread: connect, subscribe, wake on events, reconnect.

    v2 (Aug 31 2026): `wake_cb` takes the LANE name — private ORDER
    events wake repeg as before; POSITION events also wake the opener
    (the rebuy hint: a scalp exit is a position change, and the opener's
    dayof_wait rows are what rinse-repeat rides — the 60-min no-rebuy
    rule stays the policy, this only removes the waiting). Order frames
    additionally feed the MarketsFeed's watch list (which markets we are
    quoting) — hint infrastructure, not engine truth."""

    def __init__(self, wake_cb, sb=None, lanes=None):
        self.wake_cb = wake_cb
        self.sb = sb
        # Only lanes the runner actually enabled may be woken; a wake on
        # a lane this side doesn't own would just lose the lease race,
        # but not asking is cleaner than asking and being told no.
        self.lanes = set(lanes or ("repeg",))
        self.stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_wake: dict[str, float] = {}
        self._last_fail_stamp = 0.0
        self._was_connected = False
        # counters ride the transition stamps; approximate is fine
        self.msgs = 0
        self.wakes = 0
        self.reconnects = 0
        self._seen_keys: set[str] = set()
        self.mkts: "MarketsFeed | None" = None
        self._snap_slugs: set[str] = set()   # slugs seen inside a snapshot
        self._snap_keys: str | None = None   # first order's key names (recon)
        self._recon_done = False             # one recon stamp per process
        # DIRTY SET (Aug 31 2026, user: "if the websocket is telling you
        # what just moved, why the hell are you reading 130 slugs") —
        # market slugs named by socket events since the last drain. The
        # repeg lap reads books ONLY for these (plus a 10-min full-sweep
        # backstop on the app side). Still a HINT: it scopes which REST
        # reads happen sooner; no price ever leaves the socket.
        self.dirty: set[str] = set()
        self._dirty_lock = threading.Lock()

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
        # Markets socket (v2) — its own thread + connection; a failure
        # there can never take the private feed down. Flag-gated so a bad
        # first night is one env var, not a revert.
        try:
            from . import config as _cfg
            if getattr(_cfg, "WS_MKTS", True):
                self.mkts = MarketsFeed(self._wake, sb=self.sb,
                                        dirty_add=self.add_dirty)
                self.mkts.start()
        except Exception as e:
            log.error("markets feed failed to start (%s) — private feed "
                      "unaffected", e)
        return True

    def shutdown(self) -> None:
        self.stop.set()
        if self.mkts is not None:
            self.mkts.stop.set()

    # -- plumbing ------------------------------------------------------------

    def add_dirty(self, slugs: set) -> None:
        with self._dirty_lock:
            self.dirty |= slugs

    def drain_dirty(self):
        """Pop-and-return the moved-market set — or None when the markets
        socket can't vouch for completeness (never connected / currently
        down), which tells the caller to FULL-sweep instead. A dead
        socket therefore degrades to today's behavior, never to
        blindness."""
        if not (self.mkts and self.mkts._was_connected):
            return None
        with self._dirty_lock:
            d, self.dirty = self.dirty, set()
        return d

    def _wake(self, lane: str, why: str, min_s: float = WAKE_MIN_S) -> None:
        if lane not in self.lanes:
            return
        now = time.time()
        if now - self._last_wake.get(lane, 0.0) < min_s:
            return
        self._last_wake[lane] = now
        self.wakes += 1
        log.info("ws wake -> %s (%s)", lane, why)
        try:
            self.wake_cb(lane)
        except Exception as e:      # a wake must never kill the feed
            log.warning("wake callback failed: %s", e)

    def _recon(self) -> None:
        """One self-documentation stamp per process, fired the moment the
        snapshot phase settles (or the first news frame, for a venue that
        never lets snap_open empty): rid present or not, every frame key
        seen, the first order's field names, slugs harvested. Exists
        because the first live night was UNDIAGNOSABLE from off the box —
        watched=0 and total silence."""
        if self._recon_done:
            return
        self._recon_done = True
        try:                       # NEVER let recon kill the live socket
            watched = (len(getattr(self.mkts, "_slugs", ()))
                       if self.mkts else -1)
            self._stamp("recon",
                        err=(f"keys={sorted(self._seen_keys)[:14]} "
                             f"snap_keys={self._snap_keys or '-'} "
                             f"watched={watched}")[:290])
        except Exception:
            pass

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
                "requestId": rid, "subscriptionType": sub}}))
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
                self._wake("repeg", "reconnect-resync")
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
                    rid = msg.get("requestId") or msg.get("request_id")
                    # Schema recon on EVERY frame kind (key names only).
                    for k in msg:
                        if (k not in self._seen_keys
                                and k not in ("requestId", "request_id")):
                            self._seen_keys.add(k)
                            log.info("ws first sighting of key %r", k)
                    # Snapshot keys per the SDK: orderSubscriptionSnapshot /
                    # positionSubscriptionSnapshot (alt: ordersSnapshot /
                    # positionsSnapshot). endswith covers all four.
                    snap = [k for k in msg if k.endswith("Snapshot")
                            or k.endswith("_subscription_snapshot")]
                    if snap:
                        # CHANNEL FROM THE KEY NAME, rid secondary — the
                        # snapshot key itself says which channel this is
                        # and cannot be absent.
                        _kl = snap[0].lower()
                        chan = ("cellar-order" if _kl.startswith("order")
                                else "cellar-position"
                                if _kl.startswith("position")
                                else (rid or snap[0]))
                        body = msg.get(snap[0]) or {}
                        # The ORDER snapshot is the complete standing
                        # order book — the markets watch list, for free,
                        # refreshed on every reconnect.
                        if chan == "cellar-order":
                            _harvest_slugs(body, self._snap_slugs)
                            if self._snap_keys is None:
                                self._snap_keys = _recon_keys(body)
                        if isinstance(body, dict) and body.get("eof"):
                            snap_open.discard(chan)
                            snap_open.discard(rid)
                            log.info("ws snapshot complete: %s", chan)
                            if chan == "cellar-order" and self.mkts:
                                if (not self._snap_slugs
                                        and self._snap_keys):
                                    # Orders exist, zero slugs found —
                                    # the field-name miss. Report the
                                    # REAL keys so the fix is one word.
                                    self.mkts._stamp(
                                        "no_slug_field",
                                        err="order keys: "
                                            + self._snap_keys)
                                self.mkts.set_slugs(self._snap_slugs,
                                                    replace=True)
                                self._snap_slugs = set()
                            if not snap_open:
                                self._recon()
                        continue            # standing state — never a wake
                    if rid is not None and rid in snap_open:
                        continue            # still inside that snapshot
                    self._recon()           # news before snapshots settle →
                                            # recon fires anyway (rid-less
                                            # venues never empty snap_open)
                    # Anything else after the snapshots is NEWS: an order
                    # changed, a position changed. That is the whole hint.
                    kset = set(msg) - {"requestId", "request_id",
                                       "subscriptionType"}
                    why = "event:" + ",".join(kset)[:60]
                    # An order/position event names its market — that
                    # market's book is worth a fresh read.
                    _ev_slugs: set[str] = set()
                    _harvest_slugs(msg, _ev_slugs)
                    if _ev_slugs:
                        self.add_dirty(_ev_slugs)
                    self._wake("repeg", why)
                    if (rid == "cellar-position"
                            or any("position" in k for k in kset)):
                        # A position changed — a fill or an exit. The
                        # opener's dayof_wait rows are what rinse-repeat
                        # rides; waking it turns the rebuy lag from
                        # next-pass into seconds. The 60-min no-rebuy
                        # rule stays the policy — this is only speed.
                        self._wake("opener", why, WAKE_OPENER_MIN_S)
                    elif (rid == "cellar-order"
                          or any("order" in k for k in kset)) and self.mkts:
                        # New/changed order → make sure its market is
                        # watched. Adds only; removals ride the next
                        # snapshot / REST push (a stale watch costs a
                        # hint, nothing more; the cooldown absorbs it).
                        if _ev_slugs:
                            self.mkts.set_slugs(_ev_slugs, replace=False)
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


class MarketsFeed:
    """The OUTBID hint (v2): MARKET_DATA_LITE on every market we quote.

    Same doctrine as the private feed — its entire output is a wake on
    the repeg lane; no price, book, or touch is ever read out of it. A
    book move on a watched market MAY mean we lost the front of the
    queue; the woken lap reads venue truth and decides, exactly as a
    scheduled lap would. Rent is a per-second df^ticks share, so the
    minutes this removes are the product.

    Watch list comes from the private feed (ORDER snapshot ∪ order
    events since — see WsFeed). Subscription is rotated whole: when the
    set changes, subscribe the new set under a fresh request id, then
    unsubscribe the old id (the venue unsubscribes by ORIGINAL request
    id). Stale extras cost a throttled hint, never a bet.

    Stamps under kind=ws_mkts — a SEPARATE kind, so the dashboard's
    `ws ✓` footer (which reads the latest ws_feed stamp) can't have its
    state confused by this socket's lifecycle."""

    RECV_S = 15.0            # short recv timeout = subscription-rotation
                             # latency bound; silence watchdog is separate

    def __init__(self, wake, sb=None, dirty_add=None):
        self._wake = wake                    # WsFeed._wake (lane, why, min_s)
        self._dirty_add = dirty_add          # WsFeed.add_dirty — moved slugs
        self.sb = sb
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self._slugs: set[str] = set()
        self._dirty = False
        self._no_slugs_logged = False
        self._last_fail_stamp = 0.0
        self._was_connected = False
        self.msgs = 0
        self.reconnects = 0

    def set_slugs(self, slugs: set, replace: bool) -> None:
        with self._lock:
            new = set(list(slugs)[:MKTS_MAX_SLUGS]) if replace \
                else (self._slugs | slugs)
            if len(new) > MKTS_MAX_SLUGS:
                new = set(sorted(new)[:MKTS_MAX_SLUGS])
            if new != self._slugs:
                self._slugs = new
                self._dirty = True

    def start(self) -> None:
        threading.Thread(target=self._run, name="cellar-ws-mkts",
                         daemon=True).start()
        log.info("markets feed started (%s)", WS_MKTS_URL)
        self._stamp("started")

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
                "params": {"kind": "ws_mkts"},
                "result": {"state": state, "err": (err or "")[:300],
                           "msgs": self.msgs, "watched": len(self._slugs),
                           "reconnects": self.reconnects},
            }).execute()
        except Exception:
            pass

    def _connect(self):
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
        ws = websocket.create_connection(
            WS_MKTS_URL, timeout=self.RECV_S, sslopt=sslopt,
            header=[f"{k}: {v}" for k, v in hdrs.items()])
        return ws

    def _run(self) -> None:
        import websocket
        backoff_i = 0
        sub_n = 0
        while not self.stop.is_set():
            # Nothing to watch → nothing to connect to. Poll cheaply
            # until the private feed hands us a watch list.
            with self._lock:
                have = bool(self._slugs)
            if not have:
                self.stop.wait(5.0)
                continue
            ws = None
            try:
                ws = self._connect()
                backoff_i = 0
                self.reconnects += 1
                cur_rid = None
                snap_open: set = set()
                last_rx = time.time()
                with self._lock:
                    self._dirty = True       # (re)subscribe on every connect
                if not self._was_connected:
                    self._was_connected = True
                    self._stamp("connected")
                    log.info("ws mkts connected")
                while not self.stop.is_set():
                    # Rotate the subscription when the watch list moved.
                    with self._lock:
                        dirty, slugs = self._dirty, sorted(self._slugs)
                        self._dirty = False
                    if dirty and slugs:
                        sub_n += 1
                        rid = f"cellar-mkts-{sub_n}"
                        # Wire per the SDK (camelCase, STRING enum) — the
                        # prose doc's snake_case/int frames are ignored.
                        ws.send(json.dumps({"subscribe": {
                            "requestId": rid,
                            "subscriptionType": SUB_MARKET_LITE,
                            "marketSlugs": slugs}}))
                        # NO snap_open.add here: the SDK's markets socket
                        # has no snapshot phase — pre-adding the rid meant
                        # nothing ever cleared it and every frame was
                        # suppressed as "inside the snapshot" (caught by
                        # the scripted test, would have been live bug #5).
                        if cur_rid:
                            ws.send(json.dumps({"unsubscribe": {
                                "requestId": cur_rid}}))
                        cur_rid = rid
                        log.info("ws mkts watching %d markets", len(slugs))
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        if time.time() - last_rx > RECV_TIMEOUT_S:
                            raise ConnectionError("silent socket")
                        continue             # just a rotation-check beat
                    if raw is None or raw == "":
                        raise ConnectionError("empty frame")
                    last_rx = time.time()
                    self.msgs += 1
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
                            raise ConnectionError("heartbeat echo failed")
                        continue
                    if "error" in msg:
                        log.warning("ws mkts error frame: %s", str(msg)[:300])
                        continue
                    rid = msg.get("requestId") or msg.get("request_id")
                    # The SDK's markets handler has NO snapshot branch —
                    # market subs stream directly. Tolerate one anyway.
                    snap = [k for k in msg if k.endswith("Snapshot")
                            or k.endswith("_subscription_snapshot")]
                    if snap:
                        body = msg.get(snap[0]) or {}
                        if isinstance(body, dict) and body.get("eof"):
                            snap_open.discard(rid)
                        continue             # standing state — never a wake
                    if rid is not None and rid in snap_open:
                        continue
                    if rid is not None and rid != cur_rid:
                        continue             # tail of an unsubscribed batch
                    # NEWS on a market we quote (marketDataLite / trade):
                    # the book moved. Whether we were outbid is the lap's
                    # question, not ours — but WHICH market moved scopes
                    # the lap's reads (the dirty set).
                    if self._dirty_add is not None:
                        _sl = None
                        for _pk in ("marketDataLite", "marketData", "trade"):
                            _pv = msg.get(_pk)
                            if isinstance(_pv, dict):
                                _sl = _pv.get("marketSlug")
                                break
                        if isinstance(_sl, str) and _sl:
                            try:
                                self._dirty_add({_sl})
                            except Exception:
                                pass
                    self._wake("repeg", "mkts:" + ",".join(
                        k for k in msg
                        if k not in ("requestId", "request_id",
                                     "subscriptionType"))[:50],
                        WAKE_MKTS_MIN_S)
            except Exception as e:
                if self.stop.is_set():
                    break
                if self._was_connected:
                    self._was_connected = False
                    self._stamp("disconnected", err=repr(e))
                else:
                    self._stamp("connect_failed", err=repr(e))
                wait = BACKOFF_S[min(backoff_i, len(BACKOFF_S) - 1)]
                backoff_i += 1
                log.warning("ws mkts down (%s) — reconnect in %ss", e, wait)
                self.stop.wait(wait)
            finally:
                try:
                    if ws is not None:
                        ws.close()
                except Exception:
                    pass
        log.info("markets feed stopped")
