# WS QUOTE TABLE — pricing the whole board without round trips

Status: **SPEC — written Sep 1 2026 (the night before Rob's trip), build
deferred to the first watched evening after the Postgres cutover.** The
user's framing, verbatim: *"Why can't that be websocket? Instead of round
trips to Polymarket?"* — and he's right: this is worth more than the
cutover. It waits only because it rewires the money path's data source and
shares machinery with the LIVE repeg wake feed, a surface that produced
two watched-live-only defect classes in its first 72 hours (the frame-
shape night, the baseline-flood overrun). Nothing here ships unwatched.

## What it replaces

Today every OMS executor visit to a market pays REST: pmm event lookup +
markets fetch (rent asks are already batched per ladder; ticks already
ride the lookup — Sep 1 fixes). After this, a market is discovered ONCE
(REST) and priced FOREVER from a live in-memory quote table fed by the
markets websocket. Recurring per-market venue cost → ~one batched rent
call per ladder per 10 min. Bonus: the repeg pegs against a live book
instead of a 2-minute-old one, and rent is scored per second — fresher
pegging is directly money.

## Established facts (SDK source read Sep 1 2026 — the wire truth rule:
## SDK source beats the prose docs, proven Aug 30)

- **The 400-slug cap is OURS**, not the venue's. `cellar/wsfeed.py:
  MKTS_MAX_SLUGS = 400` with a "best practice" comment; the SDK's
  `marketSlugs: list[str]` is unbounded and no venue limit is documented
  in the SDK. The real cap is UNKNOWN → probe procedure below.
- **Subscriptions are per-requestId and CONCURRENT.** `subscribe(rid,
  type, slugs)` / `unsubscribe(rid)` — a connection can hold many
  subscription groups at once, and unsubscribing one group does not
  disturb the others. This kills the current MarketsFeed's whole-set
  rotation (new rid → unsubscribe old rid → `_base_seen` reset → FULL
  baseline replay, the overrun class). **Group subscriptions are the
  design: add a group per new ladder, drop a group when its game starts;
  baselines replay only for the group being added.**
- **Errors are per-request and loud**: `{"error": ..., "requestId": ...}`
  — an oversized subscribe fails identifiably without touching other
  groups. That makes the cap probe safe and binary-searchable.
- **MARKET_DATA_LITE payload**: `{marketSlug, bestBid, bestAsk,
  lastTradePx}` — exactly the fields `_gridiron_try_bet` prices from
  (bid → peg, ask → join-guard). The full MARKET_DATA (book levels) is
  NOT needed for the executor; the repeg's depth reads can stay REST or
  upgrade later.
- **Auth**: the markets socket handshake uses the same Ed25519
  `create_auth_headers` as the private feed — never re-sign by hand,
  always through `polymarket_us.auth` (the Aug 30 lesson).
- Frame casing is camelCase with STRING enums (SDK), not the prose doc's
  snake_case/int — already burned once, do not relitigate.

## Design

### 1. The quote table
Module-level dict in the cellar process (feed thread writes, lane threads
read; GIL-atomic single-key ops, values are immutable tuples):

    QUOTES[slug] = (bid_c, ask_c, ts_monotonic)

Written from: (a) the baseline frame each new group subscription replays
— a free initial snapshot per slug; (b) every subsequent LITE frame.
Nothing else writes it; nothing persists it (a restart re-baselines on
resubscribe — the table is warm within seconds of connect).

### 2. The staleness doctrine (evolution of "socket as hint, never truth")
A quote is USABLE iff `now - ts <= QT_FRESH_S` (start 90s; heartbeats
prove liveness, a book that hasn't ticked in 90s on a subscribed slug is
either genuinely quiet or the socket is sick — either way REST decides).
`_gridiron_price_game` consults the table FIRST: if every rung of the
ladder it needs is usable, build `d` from the table, zero REST. Any gap →
full REST lookup exactly as today (which also re-baselines the table via
the lookup's own data). **A dead socket therefore degrades to today's
behavior, never to blindness — same shape as the wake-hint rule, one
level deeper.** The executor never knows which source priced it; the tick
journal stamps `oms_ws_hits`/`oms_rest_looks` so the hit rate is
measurable from day one.

### 3. Discovery stays REST, once per market
Slugs/lines/ticks don't stream; the FIRST executor visit to a market pays
the normal lookup (which now also seeds ticks + subscribes the ladder's
slugs as a new group). Discovery of NEW listings keeps its existing
cadence (opener pass + sheet harvest). Rent stays REST (no incentives
channel) — one batched call per ladder per 10 min (`_rent_prewarm_periods`).

### 4. Subscription budget & priority (works at ANY cap)
The board is ~300+ markets × 10-20 rungs ≈ 4,700 slugs. Whatever the
probed cap C is, subscribe in LADDER GROUPS (one rid per market, ~10-20
slugs) by priority until C is spent:
  1. placed picks' slugs (the repeg's chase set — already the watch list)
  2. due-soon pending rows (next executor visits — the drain set)
  3. nearest kickoffs first among the rest
Groups fall off at kickoff (game starts → unsubscribe rid). If C < the
whole board, the tail simply stays REST-priced — the table is an
accelerator, never a gate. If C ≥ ~5k, everything rides.

### 5. Baseline-flood budget
Adding a group replays one baseline frame per slug in THAT group only
(~10-20 frames). Adding the whole board on a cold connect = ~4,700
frames — fine as a one-time burst IF the venue sends them (probe
confirms); otherwise stagger group-adds a few per second. Reconnect =
cold connect. The `_base_seen` filter stays per-group.

### 6. What does NOT change
- Serial writes. The table feeds decisions; orders still place one at a
  time (the Cloudflare lesson).
- The private ORDER/POSITION feed and its wake semantics — untouched.
- pm_snapshot's tape logging — separate concern; it can adopt the table
  later for the same slugs, but that's not v1.

## The cap probe (watched evening, ~15 min, zero risk to live)
On the box, a STANDALONE script (never the daemon's connection): open a
fresh `MarketsWebSocket`, subscribe rid="probe1" with N slugs from the
live board (start N=500), await ack/error; binary-search N up/down
(500 → 1000 → 2000 → 4700...) until the error boundary or full board
accepted; count baseline frames received per subscribe to verify replay
behavior at size; then unsubscribe + close. Each attempt is its own rid;
errors are per-rid; the daemon's socket is a different TCP connection
entirely. Record the found cap + baseline timing in exec_probe_runs
(kind=ws_cap_probe) and set `MKTS_MAX_SLUGS` from measurement, not vibes.

## Build order (after cutover, watched)
1. Cap probe (above) → sets the budget.
2. QuoteTable + group-subscription rework of MarketsFeed (concurrent
   rids; kill whole-set rotation; per-group `_base_seen`).
3. `_gridiron_price_game` table-first path + `oms_ws_hits` journal stat.
4. Watch one evening: hit rate, staleness fallbacks, executor drain rate.
5. Then (separate decision): repeg pegging from the table; pm_snapshot
   adoption; MARKET_DATA full-book upgrade for depth reads.

## The docs answer (fetched Sep 1 2026 via /api/docs-fetch —
## api-reference/websocket/overview.md, result in exec_probe_runs)
**The venue documents NO numeric subscription limit.** The only guidance
is best-practice prose: "Limit subscriptions — only subscribe to markets
you need" — the sentence our 400 constant was born from. Everything else
matches the SDK reading: unsubscribe by ORIGINAL request_id, per-request
error field, heartbeats, exponential-backoff reconnect. (The page still
shows snake_case + integer enums — the known-wrong casing from Aug 30;
the SDK's camelCase + string enums remain the wire truth.) So the real
cap, if one exists, is empirical → the probe below is the decider, and
until it runs, sizing MKTS_MAX_SLUGS is guesswork either way.

## Open questions the probe answers
- Real per-connection slug cap (undocumented — see above); whether
  multiple connections are allowed (fallback if small: N connections ×
  C slugs).
- Baseline replay behavior at 1000s of slugs (burst size, ordering).
- LITE frame rate at board scale (CPU budget on the box — expect trivial;
  frames only arrive on book CHANGES plus baselines).
