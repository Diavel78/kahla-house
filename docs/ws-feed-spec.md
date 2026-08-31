# WS Feed — venue push as a lane wake-up (Aug 30 2026)

**Status: BUILT — v1 live pending box pull. Owner: the cellar daemon.**

## Why (the user's correction that caused this)

"Nothing pays us for speed" was wrong. Rent is a *time-weighted share*
accruing continuously, and the share depends on where the order rests
RIGHT NOW — Kalshi publishes the shape (each step off the touch HALVES
the score; Polymarket's dial is unpublished, same species). Every minute
between "my order filled / got killed / got outbid" and "the machine
noticed on its next 2-3 min lap" is degraded or zero accrual, across
60+ resting markets. The single biggest measured rent leak — the
ghost-order class, 20 of 26 football orders dead over a weekend — was
pure detection latency. Speed does not improve our entries; it improves
**presence uptime**, and presence uptime is the product.

## The doctrine: SOCKET AS HINT, NEVER AS TRUTH

The websocket does exactly one thing: tell the runner to run the `repeg`
lane NOW instead of at its next scheduled tick. That lane (which carries
fill-status, reconcile, and the scalp arm) then does what it always
does — fresh REST reads, venue truth, serial writes. **No engine ever
acts on socket state.** A stale/dropped/lying socket therefore degrades
to exactly today's behavior: the 120s lap. There is no mirror to
corrupt, no resync bug class, no second source of truth.

This is why the failure story is boring by construction:
- socket down → lanes run on schedule, same as before the feed existed
- socket up → the same lanes run *sooner* after real events
- messages missed → the next lap's full snapshot covers them anyway
- library not installed / creds missing → feed disables itself loudly
  at boot and the daemon runs exactly as it does today

## The venue's API (docs.polymarket.us/api-reference/websocket/overview,
fetched via /api/docs-fetch Aug 30 2026 — the `.md` suffix trick: the
Mintlify SPA shell has no content, `<path>.md` returns the markdown)

- `wss://api.polymarket.us/v1/ws/private` — ORDER (type 1), POSITION
  (type 3), ACCOUNT_BALANCE (type 4) updates. **Auth required.**
- `wss://api.polymarket.us/v1/ws/markets` — books/trades per
  `market_slugs`. (v2 material — outbid detection. NOT used in v1.)
- Handshake headers = the REST scheme exactly: `X-PM-Access-Key`,
  `X-PM-Timestamp` (ms), `X-PM-Signature` = base64(Ed25519 sig of
  `timestamp + "GET" + path`). **We import the SDK's own
  `polymarket_us.auth.create_auth_headers` — never re-implement.**
- Subscriptions answer with a SNAPSHOT first (`*_subscription_snapshot`
  with `eof: true` at the end), then stream updates. Server sends
  `{"heartbeat": {}}` periodically.

## v1 behavior (cellar/wsfeed.py)

- One daemon thread in the cellar process. Started by the runner iff
  `CELLAR_WS` (default ON) and the `repeg` lane is enabled.
- Connects to `/v1/ws/private`, subscribes ORDER + POSITION.
- Snapshot messages (until `eof`) are IGNORED — they describe standing
  state, not news.
- Any post-snapshot message that is not a heartbeat/error/ack →
  `runner.wake("repeg")` (sets the lane's next-due to now; the runner's
  existing skip-if-running guard prevents stacking). Wakes are throttled
  to one per `WAKE_MIN_S` (15s).
- Heartbeats are echoed back; a 75s receive silence = dead socket →
  reconnect with exponential backoff (2s → 60s cap).
- **Every (re)connect fires one wake** — we may have missed events while
  dark, so let the lap resync immediately.
- Message schema is deliberately treated as OPAQUE beyond the three
  shapes above (snapshot/heartbeat/error). We do not parse order fields;
  the wake is the entire output. First sighting of each unknown
  top-level key is logged once for future schema work.
- Observability: connect/disconnect transitions stamp
  `exec_probe_runs` (kind=ws_feed) rate-limited to one failure stamp per
  hour; counters (msgs, wakes, reconnects) ride the stamps.

## What v1 speeds up (all via the repeg lane running seconds after the event)

- **fill → scalp ask resting**: ~2-3 min → seconds (the "if it fills,
  instantly list it for sell" contract becomes literal)
- **venue kill → reconcile notices**: next 15-min reconcile → seconds
  (the ghost-order class)
- **partial fill → qty invariant top-up**: next lap → seconds

NOT in v1: outbid detection (needs the markets socket — v2), opener
wake on new listings (markets socket), acting on socket payloads
(never).

## Dependencies / install (the pull that enables this)

`websocket-client` (pure-python, sync, thread-friendly):

    python3 -m pip install websocket-client

Missing library or missing POLYMARKET_KEY_ID/POLYMARKET_SECRET_KEY →
the feed logs WHY and stays off; the daemon is otherwise unchanged.

## v2 — BUILT Aug 31 2026 (user: "anything that increases our speed
pays better money"; items 1+3 shipped together, parallel writes next)

1. **Markets socket (`MarketsFeed`) — the outbid hint. LIVE.**
   `wss://api.polymarket.us/v1/ws/markets`, same signed handshake (path
   `/v1/ws/markets`), subscription type 2 = MARKET_DATA_LITE. Watch
   list = slugs harvested from the private feed's ORDER snapshot ∪
   order events since (`_harvest_slugs`, tolerant snake/camel keys —
   zero extractable slugs self-reports, private feed unaffected);
   refreshed whole on every private reconnect, adds-only between, cap
   400 (stale extras cost a throttled hint, never a bet). Subscription
   rotates whole on change: subscribe new set under a fresh request id,
   THEN unsubscribe the old id (the venue unsubscribes by original
   request id); 15s recv timeout is the rotation latency bound, 75s
   silence = dead socket. Frames from a superseded request id are
   dropped. Post-snapshot news on a watched market → wake `repeg`
   (10s cooldown). Same doctrine: no price is ever read out of it.
   Stamps under **kind=ws_mkts** (NOT ws_feed — the dashboard footer
   reads the latest ws_feed stamp and must not see this socket's
   lifecycle). Flag `CELLAR_WS_MKTS` (default on) — a bad night is one
   env var, not a revert.

2. **Position event → opener wake — the rebuy hint. LIVE.** Any
   post-snapshot POSITION frame also wakes `opener` (60s cooldown,
   only when the opener lane is enabled on this side): a scalp exit is
   a position change, and the opener's dayof_wait rows are what
   rinse-repeat rides — turnaround drops from next-pass to seconds.
   No frame parsing to guess "flat"; the opener pass is idempotent and
   the 60-min no-rebuy rule stays the policy.

3. **Per-slug parallel writes — NEXT, its own commit.** The
   serial-writes invariant is PER-SLUG (cancel→verify→create must
   never interleave on one slug — the Aug 16 duplicate incident);
   global one-at-a-time is a convenience. Scale-up is per-slug work
   items + bounded concurrency (max ~3, the Cloudflare lesson) behind
   the shared snapshot. Money-write surface: reviewed alone, shipped
   alone.
