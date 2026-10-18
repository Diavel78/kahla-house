# Dual-venue hand bets — Polymarket + Kalshi (spec, Oct 18 2026)

> Status: **PHASE 2 LIVE BEHIND `machine_flags hand_kalshi_enabled` (Oct 18
> 2026).** Phase 0 proved the write path end to end (fourth probe, 08:09 AZ:
> `write_scope_ok_no_funds` on the trade-scoped key — the venue accepted
> the order and refused only for balance). Phases 1-2 shipped the same
> morning (Rob: "Start on the rest, I want to use this for the 1 pm NFL
> games"): the quote prices both venues, the executor rests on the cheaper
> all-in seat, the strip / chase / start-cancel / take / cancel / re-peg /
> live carry a Kalshi bet. See §5 for what is and is not built.
> Rob, Oct 18 2026: "change my betting machine to automatically calculate
> the fees and figure out where to keep these bets while they're resting,
> move them back and forth… when we have to take a bet, with the new
> rules… would it be better to take on Kalshi versus Polymarket based on
> what the take line is at the 95% drop? Would it be better to rest on
> Kalshi on some bets if the market is lower? The machine would have to
> calculate the fees and determine what application should be used."

## 0. Rules Rob set (verbatim intent, do not re-litigate)

0. **EVERYTHING RUNS ON THE BOX. Vercel is the dashboard, nothing else**
   (Rob, Oct 18 2026: "Everything for Polymarket runs on the box. Nothing
   runs on Vercel except for the dashboard"). Every Kalshi read the machine
   acts on, every Kalshi write, the venue decision at placement / chase /
   take, and the Kalshi socket tape live in the box's `handbets` lane and
   `cellar/`. Vercel's only Kalshi job is to display what the box published
   (`lookup_cache`, the strip blob) and to enqueue what Rob tapped
   (`hand_orders`). `/api/kalshi/coverage` is a read-only DIAGNOSTIC for a
   sandbox that cannot reach Kalshi — not a production path. The slip Quote,
   which today reads Polymarket from Vercel, moves onto the box as a `quote`
   op in `hand_orders` when the dual-venue quote lands (the page polls the
   answer like a placed bet).

1. **Kalshi has coverage.** RULE 0.001 applies to Kalshi exactly as to
   Polymarket. A pick the Kalshi reader cannot place is a READER bug, never
   "Kalshi doesn't list it". It may be *listed differently* (title, suffix,
   series ticker) — probe it and read it, don't assume.
2. **Half points only.** No prediction market lists a whole-number spread or
   total — there is no book to push to. The slip's rung rule (exact, else
   the nearer MORE favorable, never worse) applies on both venues, unchanged.
3. **One resting order per bet, ever.** A venue switch is a VERIFIED cancel
   on one book, then a create on the other. Never two resting orders.
4. **The box decides the venue, every time it touches the bet.** Placement:
   quote both venues, add each venue's fee, rest on the cheaper all-in
   seat. The chase: when the touch moves and the box would re-peg, it
   re-asks "which venue is cheaper to join now?" — a re-peg may switch
   books. The take at the 95% drop: cheaper all-in ask wins.
5. **Dashboard before switch-on.** No Kalshi writes until the dashboard
   (strip, P&L, grading, sold/adopt) sees Kalshi bets — but that work comes
   AFTER Phase 0 proves the venue can be used at all.

## 1. Fees (the math is `kalshi_venue.fee_per_contract_c`, selftested)

| | Polymarket US | Kalshi |
|---|---|---|
| take, per contract | 5¢·p(1−p) | 7¢·p(1−p) × M; the ORDER's fee rounded UP to the cent |
| rest (maker), per contract | 1.25¢·p(1−p), charged at fill | 1.75¢·p(1−p) × M on series whose fee type carries maker fees, else $0 |
| tick | 0.25 / 0.5 / 1¢ by market | 1¢ |

- Polymarket numbers are calibrated on Rob's own tickets (`app._pmm_*_fee_cents`).
- Kalshi's maker fee is PER SERIES and the list moves (third-party readers
  counted 107 of 3,883 sports series with a maker fee on Oct 10 2026, incl.
  NFL/NBA/NHL/CFB games). `kalshi_venue.series_fee` reads it off
  `GET /series/{ticker}` live, 6h cache. **Never write the list down.**
- Unreadable fee terms default to CONSERVATIVE (maker charged, M=1).
- The fee gap at 50¢ is ~0.5¢ on the take and ~0.13¢ on the rest. The
  PRICE gap between venues decides the route far more often than the fee.

## 2. The compare (pure, `kalshi_venue.compare_rest` / `compare_take`)

- **rest:** all-in = our-side touch + maker fee per contract (order fee ÷
  qty). Lower wins. Tie, or a gain under `min_gain_c`, stays on the current
  venue — no churn on noise. The threshold is a machine_flag when built
  (`hand_venue_min_gain_c`, default 0.5¢ = one Poly tick).
- **take:** all-in = ask + taker fee per contract. Cheaper wins, but the
  winner's RAW ask must sit within Rob's cap (1¢, `hand_start_take_c`) of
  the resting price — the cap is on the line, the fee is the cost of
  crossing. Both past the cap → the old cancel.

## 3. Phases

**Phase 0 — can we?** (this commit)
- `kalshi_venue.py`: paged public reads, catalog discovery of series per
  (sport, market type), team-NAME event matching (no code tables), market
  classifier (ML / spread / total off title + sub-titles + strike), the
  slip's rung rule, per-series fee read, the pure compare.
- `/api/kalshi/coverage?sport=&game=&n=&creds=1` (shared-secret, site-curl):
  today's sheet picks resolved on BOTH venues with touch/ask/sizes, fee
  terms, all-in rest + take, which venue wins each. `creds=1` reports
  whether the Kalshi key is configured and whether a signed balance read is
  accepted (no values echoed).
- **The write client + THE PROOF (same day, Rob: "I'll put money in Kalshi
  if you can prove this thing works").** `kalshi_venue.signed_request`
  (ts + METHOD + full path, RSA-PSS — the reader's scheme), `balance`,
  `my_orders/positions/fills`, `create_order` (post-only GTC by default;
  IOC for the take), `cancel_order`, `amend_order`, `order_resting` (the
  orders LIST is the truthful read, never the create's echo). **THE V2
  ORDER API, read off the venue's own docs after the first live probe got
  `410 deprecated_v1_order_endpoint` on `/portfolio/orders` (Oct 18
  2026):** create = `POST /portfolio/events/orders`; the side is the YES
  leg's BOOK side — `bid` buys YES, `ask` sells YES, so BUYING NO at p is an
  `ask` at 1 − p (`yes_price_c`); `count` = fixed-point contracts
  ("1.00"), `price` = fixed-point dollars ("0.6100"); `time_in_force` ∈
  fill_or_kill | good_till_canceled | immediate_or_cancel, GTC +
  `expiration_time` (unix SECONDS) = an expiring order, IOC carries none;
  `self_trade_prevention_type` REQUIRED (`taker_at_cross` — the new order
  dies rather than hit our own resting one); 201 → {order_id,
  client_order_id, fill_count, remaining_count, average_fill_price?,
  average_fee_paid?, ts_ms} with NO status field. Cancel = `DELETE
  /portfolio/events/orders/{id}?market_ticker=` (the ticker routes the
  cancel to its shard — "an order_id alone cannot identify the exchange
  shard") → {order_id, reduced_by}. Amend = `POST …/{id}/amend` with
  `count` = filled + desired remaining (the TOTAL — Polymarket's modify
  semantics) — Phase 3's in-place chase on Kalshi. Reads
  (`/portfolio/balance`, `/portfolio/orders`) still answer on the old
  paths. Rate limit: a create costs 10 tokens, a cancel 2. The legacy
  hosts (`external-api`, `trading-api`) answer every signed call with a
  401 "API has been moved" — `signed_request` treats ANY HTTP reply from a
  host as final and only tries the next host on a connection failure
  (the first probe lost the venue's real answer to that fallback).
  `kalshi_venue.probe` = creds → balance →
  orders read → a liquid market → [place] ONE post-only 1-lot 10¢ under
  the bid → verify resting → cancel → verify gone, with a `verdict`:
  `read_only_ok` · `write_ok` · `write_scope_ok_no_funds` (an empty
  account still proves trade scope — the venue says insufficient balance,
  not 401/403) · `write_denied` · `create_rejected` (read `create.data`).
  **THIRD LIVE PROBE (Oct 18 2026, 19:08 AZ): `403 insufficient_scope:
  write::trade required` on the V2 path with a fully accepted body —
  KALSHI API KEYS ARE SCOPED, and the key in the box's `kalshi_state` row
  is read-only (it has answered balance/orders/fills reads since July;
  it has never placed anything).** The fix is a key with the trade scope
  from the Kalshi app's API-key settings, written into `kalshi_state`
  (`key_id`, `private_key`, row id=1) with psql on the box — never through
  the site — then the probe again. Every other link in the chain is
  proven: creds load, RSA-PSS signing, the elections host, the V2 path,
  the body shape, market selection, the orders read for verification.
  Runs on the box two ways: `python3 kahla-scanner/scripts/kalshi_probe.py
  [--place]`, or `machine_flags kalshi_probe = {"place": true}` (via
  `/api/cellar/flag`) which the next handbets tick runs ONCE
  (`app._kalshi_probe_tick` clears the flag BEFORE placing) and stamps
  `exec_probe_runs kind=kalshi_probe` (read:
  `/api/bet-sheets/picks?stamps=kalshi_probe`). This is the ONLY Kalshi
  write surface until `hand_kalshi_enabled` exists.
- **Kalshi WebSocket**: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`,
  same RSA headers; channels `orderbook_delta` (snapshot + deltas, sizes),
  `ticker`, `trade` (public), `fill`, `market_positions` (private). The 95%
  drop, thin-touch and line-move rules need SIZES, so a bet resting on
  Kalshi needs its own tape — `cellar/kalshifeed.py`, the wsfeed shape.
  Build after the coverage probe prints green.

**Phase 1 — quote both, write nothing new on Kalshi.** The slip Quote becomes
a `hand_orders` op the BOX answers: both venues' all-in rest and the box's
pick. Place still goes to Polymarket.

**Phase 2 — Kalshi placement path.** `_hand_order_execute` grows a venue:
post-only `good_till_canceled` create (dollar-string schema), cancel, the
IOC take. `bot_picks.signal_blob.venue` + `kalshi_ticker` + `buy_yes`. The
strip publisher reads the Kalshi orders/positions (REST first, the socket
mirror when the feed lands). Behind `machine_flags hand_kalshi_enabled`.

**Phase 3 — the chase across venues.** `_hand_chase_plan` gets both touches
and both fees; a move whose cheaper seat is on the OTHER venue is cancel →
verify → create there (never an amend across venues). Thin-touch, line-move
and the drop read the venue's own tape where the bet sits; the drop signal
reads both tapes (the makers leave both books).

**Phase 4 — the take at the drop across venues.** `_hand_start_tick` calls
`compare_take` on both asks.

**Phase 5 — dashboard parity**, then the flag flips: strip, fill verdict,
sold detection, adoption of Kalshi app bets, dashboard P&L, resolver
grading off Kalshi settlement.

## 4. Landmines carried in from the existing Kalshi code

- Prices are dollar STRINGS (`yes_bid_dollars`), counts are `*_fp` strings;
  the integer-cents schema 400s on the current API.
- `expiration_time` (not `expiration_ts`) with `good_till_canceled`; IOC
  cannot carry an expiration.
- Rate limit, basic tier: 20 reads / 10 writes per second. The box's
  existing venue read gate (3/s) is already under it.
- Kalshi has NO rent on game markets (CLAUDE.md, Aug 26 2026). Irrelevant
  now (rent is dead), noted so nobody re-asks.
- The hand-fill verdict (`_hand_fill_verdict`) reads Polymarket's trade
  tape; a Kalshi fill needs the fills endpoint (`/portfolio/fills`) or the
  `fill` channel.


## 5. What shipped Oct 18 2026 (Phase 1 + 2), and what did not

**Built (app.py, the `KALSHI AS A SECOND HAND-BET VENUE` block before
`_kalshi_probe_tick`; `kalshi_venue.py` reads at the end of the module):**

- **Quote** (`/api/bet-sheets/place preview=true`): when the flag is on and
  the item's `venue` is not `poly`, `_bet_sheet_resolve_kalshi` prices the
  Kalshi seat (same seat rule as Polymarket — `_hand_seat_price` on the
  Kalshi book), `_hand_venue_pick` → `compare_rest` picks; the slip shows
  `all-in Poly X¢ vs Kalshi Y¢` and the venue. Quote still runs on Vercel
  (public reads only); moving it onto the box as a `quote` op is open.
- **Place** (`_hand_order_execute` create): both venues resolved, venue
  picked (`auto` / `poly` / `kalshi` from the slip's Venue selector), Kalshi
  → `_kalshi_order_place` (post-only GTC, whole cents, 1-99¢, 1-250, ≤$500,
  never crossing the ask, pre-game, verified on the orders LIST) →
  `_bet_sheet_log_pick` with the Kalshi identity + the Polymarket twin.
- **Strip** (`_hand_status_publish` → `_hand_status_kalshi`): one orders
  read + one positions read a tick, the book per Kalshi pick (≤12 reads a
  tick, which also samples `KALSHI_TAPE`), fill stamp, OUTBID. Vercel's
  `/api/bet-sheets/mine` falls back to the same reads when the box is not
  publishing; every row carries `venue`.
- **Chase** (`_hand_chase_kalshi`): join the touch, Rob's leash from the
  anchor, the thin-touch guard, moves by `amend_order` (the venue may hand
  back a NEW order id — the verified id is kept).
- **Game start** (`_hand_start_kalshi`): `_hand_start_plan` on the Kalshi
  tape (REST-sampled, so ~10s frames; the 90s stale rule holds), the twin's
  Polymarket tape as the fallback baseline; the take compares both venues'
  asks + taker fees under Rob's 1¢ cap, cancels the resting Kalshi order
  (verified) BEFORE the IOC, and a Polymarket take stamps `pmm_slug` so the
  lot is tracked there. Unfilled → pick deleted (the missed list reads the
  stamp, `venue: KALSHI`).
- **Slip ops**: cancel / re-peg / move-to-live have Kalshi branches
  (`_kalshi_cancel_exec`, `_kalshi_repeg_exec`, `_kalshi_live_exec`).
- **Grading**: the resolver's ESPN pass (entry_book KALSHI, market_id /
  side / entry_line as always). Dollars on the page = filled_qty × price,
  fees not netted (same as Polymarket).

- **The venue switch** (`_hand_switch_to_poly` / `_hand_switch_to_kalshi`,
  same morning — Rob: "If it repegs, the machine determines if we are
  switching books… I shouldn't even know what you're doing"): every chase
  tick an UNFILLED bet re-runs `compare_rest` against the other venue's
  seat (the Polymarket twin rides the socket watch set; ≤4 Kalshi book
  reads a tick for Polymarket bets); the destination must sit inside Rob's
  leash; verified cancel on the old book, then the create on the new one;
  a failed create re-places on the venue it left, and a second failure
  reads OFF THE BOOK (red). A bet with any fill never switches (its lot
  would split across venues). Stamp `exec_probe_runs hand_venue_switch`.
  There is NO venue knob on the page — the chip says which book the bet is
  on (POLY / KALSHI) and nothing else.

**Not built (say so, don't assume):** the line-move sit-out / vanish drop on Kalshi (no Kalshi depth
socket — `cellar/kalshifeed.py` is the next piece); adopting bets placed in
the Kalshi app; sold-in-the-Kalshi-app detection; venue-truth grading off
Kalshi settlement; re-rung on Kalshi; dashboard P&L from Kalshi fills.

**Operating it:** flip `machine_flags hand_kalshi_enabled=true` via
`/api/cellar/flag` (site-curl); the box must be on this commit (pull +
kick the money daemon); fund the Kalshi account. The slip's Venue
selector defaults to best venue. Watch `exec_probe_runs` kinds
`kalshi_order`, `hand_chase` (venue KALSHI), `hand_start_cancel` (venue
KALSHI) — `/api/bet-sheets/picks?stamps=kalshi_order`.
