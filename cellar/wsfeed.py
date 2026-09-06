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
# MEASURED, not vibes (ws_cap_probe, Sep 4 2026): 4,000 slugs accepted
# cleanly on one connection (~5.6k baseline frames in 8s, live rate
# ~190/s at Friday peak — parse pennies). The old 400 was one
# best-practice sentence. Above ~4k the venue FAILS SILENTLY (28,651
# subscribed OK, zero frames ever — likely the ~1MB subscribe message
# itself), so the budget stays at the measured-good size and groups
# keep every individual subscribe small.
MKTS_MAX_SLUGS = 4000
# REQUEST budget per connection (Sep 5 2026, found in the log, not the
# probe): the venue rejects the 13th+ live subscription REQUEST with
# "max subscriptions per connection reached" — the cap probe fired a few
# huge requests and never saw it, while group subscriptions (one request
# per football ladder) sailed past it and the rejected request at 10:09
# was g16-CORE, the group carrying our own order slugs. Core lost its
# quotes, the fill-status walk paid ~1,000 REST reads a lap. Budget 10;
# core is first-class (ladders are evicted to seat it) and is rebuilt as
# ONE request whenever it has accumulated deltas or dropped slugs.
MKTS_MAX_RIDS = 10
CORE_REBUILD_RIDS = 2
# PACKING (Sep 6 2026, Rob: "why the hell are we doing REST with a
# websocket"): the venue's scarce resource is subscription REQUESTS (~12
# per connection), not slugs (the probe accepted ~4,000 in one request).
# One request per football LADDER meant 8-10 ladders subscribed out of
# 100+ games we quote — every other game priced over REST, every lap.
# Ladders now pool into PACK requests of PACK_SLUGS rungs across games;
# adds batch for PACK_BATCH_S; when the pack budget is full the packs are
# REBUILT from the live ladder set (expired games fall out) at most every
# REPACK_MIN_S. Core (our own orders) keeps PACK_CORE_RESERVE requests.
PACK_SLUGS = 350
PACK_BATCH_S = 20.0
PACK_CORE_RESERVE = 3
REPACK_MIN_S = 600.0

RECV_TIMEOUT_S = 75.0    # heartbeat watchdog: silence this long = dead socket
WAKE_MIN_S = 15.0        # private-event wake throttle (repeg)
WAKE_MKTS_MIN_S = 10.0   # market-move wake throttle (outbid reaction)
WAKE_OPENER_MIN_S = 60.0  # position-event wake throttle (rebuy after exit)
BACKOFF_S = (2, 4, 8, 15, 30, 60)   # reconnect ladder, then stays at the cap
FAIL_STAMP_MIN_S = 3600  # at most one failure stamp per hour to exec_probe_runs

_SLUG_KEYS = ("market_slug", "marketSlug", "slug", "symbol",
              "market", "ticker", "market_ticker")
_UUID_RE = None


def _write_quote(slug: str, pv: dict) -> None:
    """One LITE frame → one quote-table row (app.WS_QUOTES[slug] =
    (bid_c, ask_c, monotonic_ts)). Values are immutable tuples and the
    write is a single GIL-atomic dict store — feed thread writes, lane
    threads read, no lock. Amounts arrive as {value, currency} dollars;
    stored as half-cent-rounded cents (the _pmm_book convention — the
    half-cent is load-bearing on game books). A side with no quote
    stores None; the timestamp still advances (an empty side is real
    news). Never raises — a bad frame is a skipped write, and the
    readers' staleness rule turns absence into a REST fallback."""
    try:
        import app as _app

        def _c(a):
            try:
                v = float((a or {}).get("value"))
            except (TypeError, ValueError, AttributeError):
                return None
            return round(v * 200) / 2.0 if 0 < v < 1 else None
        _app.WS_QUOTES[slug] = (_c(pv.get("bestBid")), _c(pv.get("bestAsk")),
                                time.monotonic())
        # presence freshness: the row belongs to THIS connection epoch
        _app.WS_QUOTE_EPOCH[slug] = _app.WS_MKTS_EPOCH
    except Exception:
        pass


def _forget_quotes(slugs) -> None:
    """Slugs we no longer subscribe to can go quiet without meaning
    'unchanged' — drop their epoch so the readers fall back to the age
    rule (the row itself stays; a <90s row is still fresh)."""
    try:
        import app as _app
        for sl in slugs or ():
            _app.WS_QUOTE_EPOCH.pop(sl, None)
    except Exception:
        pass


def _mkts_presence(up: bool | None = None, rx: bool = False) -> None:
    """Feed-thread-only writes of the socket's liveness for _ws_quote."""
    try:
        import app as _app
        if rx:
            _app.WS_MKTS_LAST_RX = time.monotonic()
        if up is True:
            _app.WS_MKTS_EPOCH += 1
            _app.WS_MKTS_LAST_RX = time.monotonic()
            _app.WS_MKTS_UP = True
        elif up is False:
            _app.WS_MKTS_UP = False
    except Exception:
        pass


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
        # GROUP SUBSCRIPTIONS (Sep 4 2026 — the cap probe's real find):
        # the venue tracks subscribed slugs PER CONNECTION and REJECTS
        # any subscribe that overlaps one ("slug already subscribed").
        # The old whole-set rotation (subscribe new rid, then drop the
        # old) therefore FAILED on every watch-list change — the new
        # subscribe bounced off its own predecessor, the unsubscribe
        # then landed, and the feed sat on DEAD AIR until the next
        # rotation. Groups keep membership disjoint by construction:
        # one rid per group, adds deduped against the covered set,
        # drops by original rid.
        self._groups: dict[str, tuple[str, frozenset]] = {}  # gid->(rid,slugs)
        self._rid_slugs: dict[str, frozenset] = {}   # rid -> the slugs IT carries
        self._covered: set[str] = set()      # union of all group slugs
        self._ops: list = []                 # queued (verb, gid, slugs, exp)
        self._expiry: dict[str, float] = {}  # gid -> epoch expiry (eviction)
        self._gseq = 0
        self._base_seen: set[str] = set()   # slugs whose baseline frame
                                            # arrived since THEIR group's
                                            # subscription (burst filter —
                                            # per-group now, never a global
                                            # reset: the full-board replay
                                            # drain was the overrun class)
        self._no_slugs_logged = False
        self._last_fail_stamp = 0.0
        self._was_connected = False
        self.msgs = 0
        self.reconnects = 0
        # PACKING state: desired ladders (gid -> (slugs, expiry)), the slugs
        # desired-but-not-yet-covered (slug -> first-seen ts), repack clock.
        self._ladders: dict[str, tuple[frozenset, float | None]] = {}
        self._pending: dict[str, float] = {}
        self._last_repack = 0.0
        self._last_pack_build = 0.0
        self._pack_hold_until = 0.0

    # ── public API (lane threads queue; the feed thread executes) ──
    def set_slugs(self, slugs: set, replace: bool) -> None:
        """Back-compat entry for the repeg lap's watch-list push: the
        whole set lands as the reserved 'core' group (replace = re-add;
        dedup against ladder groups happens at execution)."""
        self.add_group("core", set(slugs), None)

    def add_group(self, gid: str, slugs: set, expire_ts=None) -> None:
        with self._lock:
            self._ops.append(("add", str(gid), set(slugs), expire_ts))

    def drop_group(self, gid: str) -> None:
        with self._lock:
            self._ops.append(("drop", str(gid), None, None))

    def _take_ops(self) -> list:
        with self._lock:
            ops, self._ops = self._ops, []
        # collapse repeated adds of the same gid (the core push comes
        # every lap; only the newest membership matters)
        seen: dict = {}
        out = []
        for op in reversed(ops):
            key = (op[0], op[1])
            if op[0] == "add" and key in seen:
                continue
            seen[key] = True
            out.append(op)
        out.reverse()
        return out

    @property
    def _slugs(self) -> set:
        return self._covered

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

    # ── group bookkeeping (feed thread only) ──────────────────────────
    # A "group" is one logical membership (the repeg watch list = 'core',
    # a ladder = its market id) carried by one or more rids: adds are
    # DELTA-subscribed (new slugs only — the venue rejects overlap), so
    # a changing group accumulates rids; stale extras just cost frames.
    # When a group's rid count passes _GROUP_REBUILD_RIDS, it is torn
    # down (unsubscribe every rid) and re-queued as a pure add with a
    # not-before delay so the venue finishes processing the drops first
    # (the overlap-rejection lesson, applied to ourselves).
    _GROUP_REBUILD_RIDS = 20

    def _evict_one(self, ws, protect: str) -> bool:
        """Drop the non-core ladder group with the FARTHEST expiry (least
        urgent for the pricer; REST covers its rungs). True if one went."""
        cands = [(g, self._expiry.get(g)) for g in self._groups
                 if g not in ("core", protect)]
        if not cands:
            return False
        cands.sort(key=lambda kv: (kv[1] is None, kv[1] or 0), reverse=True)
        g2 = cands[0][0]
        r2, o2 = self._groups.pop(g2, (None, frozenset()))
        for r in (r2 or []):
            try:
                ws.send(json.dumps({"unsubscribe": {"requestId": r}}))
            except Exception:
                pass
            self._rid_slugs.pop(r, None)
        self._covered -= o2
        self._base_seen -= o2
        self._expiry.pop(g2, None)
        _forget_quotes(o2)
        nowt = time.time()
        for s in o2:                      # still desired → next pack
            self._pending.setdefault(s, nowt)
        log.info("ws mkts evicted pack %s (%d slugs) to seat %s",
                 g2, len(o2), protect)
        return True

    def _on_rejected(self, rid: str) -> None:
        """The venue refused subscription request `rid`: its slugs are NOT
        covered, whatever we recorded. Un-cover them so a later add can
        retry; if it was core, re-queue core right away (the request
        budget path evicts a ladder for it)."""
        gone = set(self._rid_slugs.pop(rid, frozenset()))
        gid = None
        for g, (rids, sl) in list(self._groups.items()):
            if rid in rids:
                gid = g
                rids2 = [r for r in rids if r != rid]
                keep = frozenset(set(sl) - gone)
                if rids2:
                    self._groups[g] = (rids2, keep)
                else:
                    self._groups.pop(g, None)
                    self._expiry.pop(g, None)
                break
        self._covered -= gone
        self._base_seen -= gone
        _forget_quotes(gone)
        if gid == "core" and gone:
            with self._lock:
                self._ops.append(("add", "core", gone, None,
                                  time.time() + 1.0))
        elif gone:
            nowt = time.time()
            for s in gone:
                self._pending.setdefault(s, nowt)
        log.warning("ws mkts subscription %s REJECTED (%s, %d slugs)%s",
                    rid, gid, len(gone),
                    " — core re-queued" if gid == "core" else "")

    def _apply_ops(self, ws) -> None:
        ripe = []
        requeue = []
        nowt = time.time()
        for op in self._take_ops():
            (requeue if len(op) > 4 and op[4] and op[4] > nowt
             else ripe).append(op)
        if requeue:
            with self._lock:
                self._ops = requeue + self._ops
        changed = False
        for op in ripe:
            verb, gid, slugs = op[0], op[1], op[2]
            exp = op[3] if len(op) > 3 else None
            if verb == "drop" and gid != "core" and gid not in self._groups:
                sl, _e = self._ladders.pop(gid, (frozenset(), None))
                for s in sl:
                    self._pending.pop(s, None)
                continue                  # subscribed rungs leave at repack
            if verb == "drop":
                rids, old = self._groups.pop(gid, (None, frozenset()))
                for r in (rids or []):
                    ws.send(json.dumps({"unsubscribe": {"requestId": r}}))
                    self._rid_slugs.pop(r, None)
                self._covered -= old
                self._base_seen -= old
                self._expiry.pop(gid, None)
                _forget_quotes(old)
                changed = changed or bool(old)
                continue
            # CORE HYGIENE: the repeg lap pushes the WHOLE current order
            # set. Slugs that fell off (cancels/fills) and accumulated
            # delta-requests both mean: tear core down and re-add it as
            # one clean request (2s later, after the unsubscribes land).
            if gid == "core" and gid in self._groups:
                c_rids, c_old = self._groups[gid]
                stale = set(c_old) - set(slugs)
                # a few fallen-off slugs cost nothing subscribed; rebuild
                # on real drift or on accumulated delta requests
                if len(stale) > 20 or len(c_rids) >= CORE_REBUILD_RIDS:
                    for r in c_rids:
                        ws.send(json.dumps({"unsubscribe": {"requestId": r}}))
                        self._rid_slugs.pop(r, None)
                    self._groups.pop(gid, None)
                    self._covered -= set(c_old)
                    self._base_seen -= set(c_old)
                    _forget_quotes(stale)
                    with self._lock:
                        self._ops.append(("add", gid, set(slugs), exp,
                                          time.time() + 2.0))
                    changed = True
                    continue
            if gid != "core":
                # LADDER → PACK LAYER: record the desire; _pack_flush
                # subscribes rungs in batched cross-game packs.
                self._ladders[gid] = (frozenset(slugs), exp)
                for s in set(slugs) - self._covered:
                    self._pending.setdefault(s, nowt)
                continue
            # core add: delta only — the venue rejects overlapping subscribes
            new = set(slugs) - self._covered
            if exp is not None:
                self._expiry[gid] = exp
            if not new:
                continue
            # REQUEST budget: core evicts ladders to seat itself; a ladder
            # that finds no room is skipped (its rungs price via REST).
            while (sum(len(r) for r, _s in self._groups.values())
                   >= MKTS_MAX_RIDS):
                if gid != "core" or not self._evict_one(ws, protect=gid):
                    break
            if (sum(len(r) for r, _s in self._groups.values())
                    >= MKTS_MAX_RIDS):
                if gid != "core":
                    log.info("ws mkts request budget full — ladder %s "
                             "not subscribed (REST prices it)", gid)
                    continue
            # budget: evict expired ladder groups first, then refuse
            if len(self._covered) + len(new) > MKTS_MAX_SLUGS:
                for g2, e2 in sorted(self._expiry.items(),
                                     key=lambda kv: kv[1] or 0):
                    if g2 == gid or g2 == "core":
                        continue
                    if len(self._covered) + len(new) <= MKTS_MAX_SLUGS:
                        break
                    if e2 and e2 < nowt:
                        r2, o2 = self._groups.pop(g2, (None, frozenset()))
                        for r in (r2 or []):
                            ws.send(json.dumps(
                                {"unsubscribe": {"requestId": r}}))
                        self._covered -= o2
                        self._base_seen -= o2
                        self._expiry.pop(g2, None)
                        _forget_quotes(o2)
                if len(self._covered) + len(new) > MKTS_MAX_SLUGS:
                    log.warning("ws mkts budget full (%d) — group %s not "
                                "subscribed", len(self._covered), gid)
                    continue
            self._gseq += 1
            rid = f"g{self._gseq}-{gid[:32]}"
            ws.send(json.dumps({"subscribe": {
                "requestId": rid,
                "subscriptionType": SUB_MARKET_LITE,
                "marketSlugs": sorted(new)}}))
            rids, old = self._groups.get(gid, ([], frozenset()))
            rids = list(rids) + [rid]
            self._groups[gid] = (rids, frozenset(old | new))
            self._rid_slugs[rid] = frozenset(new)
            self._covered |= new
            self._base_seen -= new       # their next frame = the baseline
            changed = True
            if len(rids) > self._GROUP_REBUILD_RIDS:
                # hygiene: too many delta-rids → tear down + re-add clean
                cur = set(self._groups[gid][1])
                for r in rids:
                    ws.send(json.dumps({"unsubscribe": {"requestId": r}}))
                    self._rid_slugs.pop(r, None)
                self._groups.pop(gid, None)
                self._covered -= cur
                self._base_seen -= cur
                with self._lock:
                    self._ops.append(("add", gid, cur,
                                      self._expiry.get(gid),
                                      time.time() + 2.0))
        if self._pack_flush(ws, nowt):
            changed = True
        if changed:
            log.info("ws mkts watching %d markets in %d groups",
                     len(self._covered), len(self._groups))

    # ── PACK LAYER ──
    def _pack_budget_used(self) -> int:
        return sum(len(r) for g, (r, _s) in self._groups.items()
                   if g != "core")

    def _live_rungs(self, nowt: float) -> dict:
        """slug -> nearest expiry over the desired (unexpired) ladders;
        prunes ladders an hour past kickoff."""
        for g, (sl, e) in list(self._ladders.items()):
            if e is not None and e < nowt - 3600.0:
                self._ladders.pop(g, None)
        live: dict = {}
        for g, (sl, e) in self._ladders.items():
            ev = e if e is not None else 1e18
            for s in sl:
                if ev < live.get(s, 1e18):
                    live[s] = ev
        return live

    def _send_pack(self, ws, slugs: list, live: dict) -> None:
        self._gseq += 1
        rid = f"g{self._gseq}-pack"
        gid = f"pack{self._gseq}"
        ws.send(json.dumps({"subscribe": {
            "requestId": rid,
            "subscriptionType": SUB_MARKET_LITE,
            "marketSlugs": sorted(slugs)}}))
        fs = frozenset(slugs)
        self._groups[gid] = ([rid], fs)
        self._rid_slugs[rid] = fs
        self._covered |= fs
        self._base_seen -= fs
        self._expiry[gid] = max((live.get(s, 1e18) for s in slugs),
                                default=None)
        self._last_pack_build = time.time()
        for s in slugs:
            self._pending.pop(s, None)

    def _pack_flush(self, ws, nowt=None, force: bool = False) -> bool:
        """Subscribe pending rungs as cross-game packs. Batches for
        PACK_BATCH_S; pack budget full → REPACK from the live ladder set
        (rate-limited), leftovers stay pending (REST prices them)."""
        nowt = nowt if nowt is not None else time.time()
        if nowt < self._pack_hold_until:
            return False
        live = self._live_rungs(nowt)
        for s in list(self._pending):
            if s not in live or s in self._covered:
                self._pending.pop(s, None)
        if not self._pending:
            return False
        oldest = min(self._pending.values())
        if (not force and len(self._pending) < PACK_SLUGS
                and nowt - oldest < PACK_BATCH_S):
            return False
        budget = MKTS_MAX_RIDS - PACK_CORE_RESERVE
        sent = False
        while self._pending and self._pack_budget_used() < budget:
            batch = sorted(self._pending,
                           key=lambda s: live.get(s, 1e18))[:PACK_SLUGS]
            self._send_pack(ws, batch, live)
            sent = True
        if not self._pending or self._pack_budget_used() < budget:
            return sent
        # budget full with rungs still wanting → repack from the live set,
        # but ONLY when it frees something: packs carrying DEAD weight
        # (rungs no longer desired — kicked-off games), or a pending rung
        # that expires sooner than one already packed. Never within
        # REPACK_MIN_S of the last build (a repack is 2s of dead air).
        if (nowt - self._last_repack < REPACK_MIN_S
                or nowt - self._last_pack_build < REPACK_MIN_S):
            return sent
        packed = set()
        for g, (_r, sl) in self._groups.items():
            if g != "core":
                packed |= set(sl)
        dead = packed - set(live)
        far_packed = max((live.get(s, 1e18) for s in packed), default=0)
        near_pending = min((live.get(s, 1e18) for s in self._pending),
                           default=1e18)
        if not dead and near_pending >= far_packed:
            return sent
        self._last_repack = nowt
        for g in [g for g in self._groups if g != "core"]:
            rids, old = self._groups.pop(g, ([], frozenset()))
            for r in rids:
                try:
                    ws.send(json.dumps({"unsubscribe": {"requestId": r}}))
                except Exception:
                    pass
                self._rid_slugs.pop(r, None)
            self._covered -= old
            self._base_seen -= old
            self._expiry.pop(g, None)
            _forget_quotes(old)
        core_cov = self._groups.get("core", ([], frozenset()))[1]
        for s in live:
            if s not in core_cov:
                self._pending.setdefault(s, nowt)
        # the venue must see the unsubscribes land before the re-adds
        self._pack_hold_until = nowt + 2.0
        log.info("ws mkts REPACK — %d live rungs across %d ladders queued "
                 "into packs of %d", len(self._pending), len(self._ladders),
                 PACK_SLUGS)
        return True

    def _run(self) -> None:
        import websocket
        backoff_i = 0
        while not self.stop.is_set():
            # Nothing to watch → nothing to connect to. Poll cheaply
            # until someone queues a group.
            with self._lock:
                have = bool(self._covered) or bool(self._ops)
            if not have:
                self.stop.wait(5.0)
                continue
            ws = None
            try:
                ws = self._connect()
                backoff_i = 0
                self.reconnects += 1
                snap_open: set = set()
                last_rx = time.time()
                # Fresh connection = the venue holds no subscriptions:
                # re-queue every group we believe in as a clean add.
                with self._lock:
                    prev = [("add", g, set(s), self._expiry.get(g))
                            for g, (r, s) in self._groups.items()]
                    self._groups = {}
                    self._rid_slugs = {}
                    self._covered = set()
                    self._base_seen = set()
                    self._ops = prev + self._ops
                _mkts_presence(up=True)      # new epoch: old rows age out
                if not self._was_connected:
                    self._was_connected = True
                    self._stamp("connected")
                    log.info("ws mkts connected")
                while not self.stop.is_set():
                    self._apply_ops(ws)
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        if time.time() - last_rx > RECV_TIMEOUT_S:
                            raise ConnectionError("silent socket")
                        continue             # just a rotation-check beat
                    if raw is None or raw == "":
                        raise ConnectionError("empty frame")
                    last_rx = time.time()
                    _mkts_presence(rx=True)      # any frame = socket alive
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
                        _erid = msg.get("requestId") or msg.get("request_id")
                        if _erid and "subscription" in str(msg.get("error", "")).lower():
                            self._on_rejected(str(_erid))
                        else:
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
                    # (no per-rid tail filter anymore: many rids live at
                    # once under group subscriptions; a frame from a rid
                    # we just dropped is still a real quote — write it)
                    # NEWS on a market we quote (marketDataLite / trade):
                    # the book moved. Whether we were outbid is the lap's
                    # question, not ours — but WHICH market moved scopes
                    # the lap's reads (the dirty set).
                    _sl = None
                    for _pk in ("marketDataLite", "marketData", "trade"):
                        _pv = msg.get(_pk)
                        if isinstance(_pv, dict):
                            _sl = _pv.get("marketSlug")
                            break
                    if isinstance(_sl, str) and _sl:
                        # THE QUOTE TABLE (docs/ws-quote-table-spec.md):
                        # every LITE frame — baselines INCLUDED, they are
                        # a free snapshot — lands bid/ask in app.WS_QUOTES
                        # so the money lanes can price without a REST
                        # round trip. Write-only from here; freshness and
                        # fallback live with the readers (_ws_quote).
                        if _pk == "marketDataLite":
                            _write_quote(_sl, _pv)
                        if _sl not in self._base_seen:
                            # First frame for this market since the
                            # subscription = the baseline replay. State,
                            # not news: no dirty, no wake. Cost of being
                            # wrong (a real move as first frame): one
                            # missed hint, caught by the 10-min sweep.
                            self._base_seen.add(_sl)
                            continue
                        if self._dirty_add is not None:
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
                _mkts_presence(up=False)     # readers fall back to the age rule
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
