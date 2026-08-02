# Re-Peg Bot — Out-of-Touch Maker Order Manager (NRFI/YRFI)

> Status: **KILLED Aug 2 2026 after the first live session** (`REPEG_ENABLED
> =False`). The user waived the probe/shadow phases ("go live"); the first
> two live amends (Phillies/Pirates YRFI, 9:19 AM) **canceled the resting
> orders without landing a replacement** — `orders.modify` returned
> success, Telegram pinged RE-PEGGED, but `orders.list` and the app showed
> nothing and the autolog removed both picks as sold. No money lost (no
> fills); the user re-placed by hand. **Root cause (suspected): Polymarket's
> modify is FIX-style cancel/replace, and a replace leg missing
> quantity/type dies AFTER the cancel leg executes.** This is precisely
> what Phase 0 existed to catch — the waived probe ran itself on real
> orders.
>
> **Hardened same day (dark, awaiting re-enable):** modify now sends the
> FULL replace (quantity included), and after EVERY amend — success or
> error — `_repeg_verify_or_recreate` treats `orders.list` as the only
> truth: order survives → verified ping; venue killed it → re-create at
> the amend price (full create params, master-rule qty) → "re-placed
> fresh" ping; both gone → **🚨 ORDER LOST — RE-BID BY HAND NOW** ping;
> verify read failed → never recreate blind (dup risk), ⚠ CHECK THE APP
> ping.
>
> **RE-ENABLE GATE (non-negotiable now): run `GET
> /api/polymarket/probe-exec?slug=...&place=1` and confirm the modify step
> leaves an open order on the book (retrieve_after_modify shows an open
> state with the new price). Only then flip `REPEG_ENABLED=True`.** Engine = `_repeg_tick`
> riding the paperlog tick (every 2nd minute, blackout-gated, before the
> outbid ping so a live amend will clear the outbid). Phase-0 probe =
> `GET /api/polymarket/probe-exec` (admin; DRY/preview by default,
> `&place=1` runs create→modify→cancel on a deep off-touch order). **SDK
> introspection (polymarket-us 0.1.2) resolved all four §6 open questions
> at build time:**
> 1. There is no "Start of game" TIF literal — the app's picker builds
>    **`TIME_IN_FORCE_GOOD_TILL_DATE` + `goodTillTime`**. The engine
>    passes the order's existing GTD through on amends; probe confirms the
>    timestamp format against reality.
> 2. App-placed orders are amendable/cancelable via API (same account) —
>    `orders.modify(order_id)` / `orders.cancel(order_id, {marketSlug})`;
>    probe verifies live.
> 3. Order states distinguish FILLED / REPLACED / REJECTED / EXPIRED; the
>    race rule is implemented as retrieve-after-failed-modify → state
>    FILLED or leaves 0 ⇒ treat as fill, never re-place.
> 4. **`orders.modify` EXISTS** — re-peg is one atomic amend, no
>    cancel+place unquoted window. Bonus: **`participateDontInitiate=True`
>    (post-only)** is set on every amend — the venue itself rejects any
>    move that would cross the spread, a hard maker-only guarantee.
>
> Go-live checklist: (a) hit the probe dry, then `&place=1`, confirm the
> §6 shapes; (b) review shadow pings over a slate or two; (c) flip
> `REPEG_ENABLED=True` and deploy. Original spec below, unchanged.

## 0. What it is (one paragraph)

The user bets Y/NRFI as a maker (rest the bid, log the bid). When the market
moves away, the resting order goes "out of touch" — outbid — and today the
fill tracker only *detects* that (`outbid` status chip + the Filled Bot
Telegram ping) and the user re-pegs by hand. The bot closes that loop: when a
logged, pending Y/NRFI pick's resting Polymarket order is outbid, it cancels
and re-rests at the current best bid (join the touch), within hard limits.
Detection, matching, and bookkeeping all already exist; the only new
capability is **place/cancel via the Polymarket US API**.

## 1. THE MASTER RULE (Rule 1 — cannot be broken)

**No order the bot places or replaces may cost more than $6.00 on any
event** (`contracts × price ≤ $6.00`). Enforced in code as a hard reject
immediately before EVERY place/replace call — belt-and-suspenders on top of
every other limit, and the reason the whole experiment is safe. A rejected
order Telegrams the user and does nothing.

## 2. Locked dials (user's answers, Aug 2 2026)

| Dial | Setting |
|---|---|
| Markets | **Y/NRFI only** (MLB first-inning) |
| Whose picks | Admin's own logged pending picks only (it's their creds) |
| Venue | Polymarket US only |
| Size | Flat 1u, **max 10 contracts** (~$5 at typical 45-55¢ NRFI prices) |
| Cost ceiling | **$6.00/event — the Master Rule** |
| Time-in-force | **"Start of game"** expiration on every order (native Polymarket TIF, confirmed in the app's order ticket) — unfilled orders die at tip venue-side, no bot logic, no in-play fill risk |
| Re-peg trigger | Outbid only. **Downward re-pegs are impossible** (user's catch): our resting bid IS the book's best bid — the market can't move below us without filling us |
| Re-peg action | Cancel + re-rest at the **current best bid** (join the touch; no MAKE+ front-stepping in v1). Poly tick = 0.5¢ |
| Chase limit | **Max 2 re-pegs per bet, lifetime** — after 2, the order sits at its last price until fill or Start-of-game expiry |
| Edge gate | **Move 1 is UNCONDITIONAL** (user dial, revised first live night: "I bet the game, I want it bet — chase 1 repeg, then you can reevaluate"; months of hand-chasing these profitably). **Move 2** must clear the NRFI edge gate at the NEW price (pick's `fair_prob`, else the latest paperlog `p_nrfi` — the autolog fallback). Gate fails → do NOT re-peg; leave the order resting at its old (better) price and ping Telegram |
| Race rule | Cancel returns already-filled → **treat as fill, never re-place** filled qty |
| Partial fills | Cancel-replace the unfilled remainder only; the existing qty-weighted entry auto-sync blends the average |
| Kill switch | `REPEG_ENABLED = False` module constant (`KALSHI_EXECUTION` pattern) — ships dark |

Note the edge-gate direction: the market moving away from a Y/NRFI bid
*shrinks* the edge (fair − price), so the gate naturally stops chases that
approach fair — and a violent news-driven jump blows straight through the
band, so the gate doubles as the adverse-selection guard. No separate
velocity rule needed in v1.

## 3. What already exists (reuse, don't rebuild)

- **Outbid detection** — `_compute_fill_status(sb, uid)` in `app.py` already
  computes `outbid` per pending pick (my resting price vs the public book's
  best bid, our-side oriented, with `ahead_qty`), every paperlog tick
  (~1 min). The bot is a new consumer of the same computation — it acts
  where `_outbid_alerts` today only pings.
- **Order↔pick matching** — `_pmm_fill_entry` / `_pmm_open_orders_raw`
  already matches the admin's resting CLOB orders to logged picks (side =
  BUY_LONG/YES, or BUY_SHORT for synthetic NO). Works for orders placed in
  the app by hand — which is the point: the bot manages the user's manual
  orders too, not just its own.
- **Entry bookkeeping** — on re-peg, update the pick's `entry_price` to the
  new resting bid (the standing logged-price convention: rest the bid, log
  the bid — CLV and to-WIN key off it), and append to `signal_blob.repeg`
  (array of `{from_c, to_c, at}`) for review. On fill, the existing
  auto-sync corrects to the actual qty-weighted average as it always has.
- **Telegram** — `_send_fill_telegram` (Filled Bot). Bot pings on every
  action taken or refused (re-pegged / gate-stopped / master-rule reject),
  so the user always sees it working.

## 4. Economics note (why the failure mode is benign)

Cancels are free. The Polymarket maker fee (1.25¢·p(1−p)/share — the Aug 1
2026 rebate→fee flip, proven on the user's own NRFI ticket) is charged only
at fill, once, on filled quantity — re-pegging never adds fees. The bot's
worst case is "missed a fill" or "filled at a price that still cleared the
edge gate"; it cannot run up a bill, and the Master Rule caps any single
event's exposure at $6.

## 5. Build plan (phased, each shippable + reversible)

### Phase 0 — write-capability probe (half a session)
`/api/polymarket/probe-exec` (`@admin_required`): verify with the existing
`POLYMARKET_KEY_ID`/`POLYMARKET_SECRET_KEY` creds from Vercel that the
`polymarket_us` SDK can (a) place a limit order **with Start-of-game TIF**,
(b) cancel it (including one placed by hand in the app), (c) report raw
order/response shapes. Probe order must itself respect the Master Rule
(1 contract, deep off-touch price, canceled immediately). **Hit this first —
the SDK's TIF parameter surface is the one genuinely unverified assumption**
(the app exposes Good-'til-canceled / Immediate-or-cancel / 12am PT /
Start-of-game; the API should match). If the API lacks Start-of-game TIF,
fall back to GTC + a bot-side cancel at the existing take-warning check
(T−5m) — worse, so prefer native.

### Phase 1 — shadow mode ("play it by ear" duration)
Bot computes every action on the live tick but only LOGS it
(`signal_blob.repeg_shadow`) — no orders touched. Even 1-2 slates of review
(would-have-repegged vs what the user did by hand vs what filled) is enough
given the Master Rule; user decides when to flip.

### Phase 2 — live
Flip `REPEG_ENABLED`. NRFI-only, admin-only, Master Rule enforced, Telegram
on every action. Store the CLOB order id the bot places in
`signal_blob.order_id` so cancels target exactly the right order (manual
orders keep matching heuristically via the existing fill-status machinery).

## 6. Open questions for Phase 0 to answer

1. Does the API expose Start-of-game TIF? (Fallback defined above.)
2. Can the API cancel an order placed by hand in the app? (Expected yes —
   same account/CLOB; verify.)
3. Exact place/cancel response shapes — especially how "cancel raced a fill"
   is reported (error code vs filled-status response). The race rule in §2
   depends on reading this correctly.
4. Does a re-priced order need cancel+place, or is there an amend/replace
   endpoint? (Cancel+place assumed; amend would shrink the unquoted window.)
