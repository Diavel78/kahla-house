# Kalshi Sports Execution Lane — Migration Spec

> Status: **SPEC ONLY — not approved, not started.** Written July 12 2026 after a
> live fee/mechanics investigation of Kalshi (perps + sports tickets, real
> receipts). This doc is self-contained: a fresh session should be able to build
> from it without the original conversation. The decision to migrate is the
> user's; the default until then is **sports execution stays on Polymarket**.

## 0. Why this exists (the decision ledger)

The user is considering moving his personal sports execution from Polymarket to
Kalshi. The money math is a verified **tie**; the soft factors lean Kalshi. All
numbers below were measured empirically July 11-12 2026 from the user's own
account receipts — not from docs.

### Fee facts (receipts, not blog posts)

| Venue | Sports maker | Sports taker | Notes |
|---|---|---|---|
| Polymarket | **EARNS rebate** `1.25¢·p(1−p)`/contract (~0.31¢ @50¢) | `5¢·p(1−p)` (~1.25¢ @50¢) | rebates paid on fill |
| Kalshi | **$0** (verified: 5-share YES 53¢ + NO 47¢ resting tickets showed no fee) | `7%·p·(1−p)·contracts`, rounded UP per order (verified: $0.09 on 5@54¢) | taker ~40% worse than Poly |

- Kalshi **perps** (separate exchange, separate schedule): maker 5 bps of
  leveraged notional, taker ~8-78 bps observed (receipts inconsistent with the
  published 12 bps tier-0 — app may quote worse; never take). Funding settles
  12 AM / 8 AM / 4 PM ET; **BTC funding measured ≈ $0** (two settlement windows,
  no ledger entries) → no harvestable funding yield at present. Perps are the
  user's manual entertainment; **no bot models perps direction** (decided).
- Kalshi pays **3.25% APY** on cash + open positions once monthly avg portfolio
  > $250. Polymarket pays 0 on USDC.
- User's June 2026 Polymarket volume (from `polymarket_fill_state`): **$704.59
  bet, 271 filled buy orders, 1,410 shares, avg entry 49.3¢, 100% maker** →
  est. rebates ~$4.27/mo. Kalshi interest on his ~$1,500 ≈ $4.06/mo.
  **Difference ≈ $0.34/mo. The math genuinely does not care.**
- Prices are identical across venues: our own `pm_snapshots` validation found
  14/15 games within 0-2¢ (it's the basis of the cross-confirm steam signal).

### Soft factors (what actually decides)

Kalshi: **native fill notifications** (retires the Filled Bot polling system
entirely), interest scales with bankroll, parlays (user fun — the bot never
routes parlays), perps + sports + cash in one app, USD not USDC.
Polymarket: execution stack already built and battle-tested (inertia, not
superiority), rebates scale with volume.

## 1. Scope

**In scope:** the admin's execution/tracking path — suggestion entry pricing,
make/take verdict, pick logging (`entry_book`), fill detection + entry
auto-sync, fill-status chips, outbid warning.
**Out of scope / unchanged:** ESPN schedule spine, `pm_snapshots` logger
(already logs Kalshi cents), exchange sharp score, CLV (resolver already grades
ML vs **Kalshi mid** close — `_exch_close_pair`), dossier data, per-user Pick
Bot logging semantics, the Dashboard (`/api/data` etc. stays Polymarket until
the Poly book winds down — separate later decision), the unlogged-BET Telegram
alert (`_bet_alerts` — keeps `FILLED_BOT_TOKEN`; only the fill-notification
role of that bot retires).

**Non-goals:** no parlay routing, no perps trading/logic, no multi-venue
best-price router (Kalshi replaces Poly for the admin's entries; Poly path kept
dormant, mirroring how Kalshi's readers are dormant today).

## 2. What we already have (dormant, in-repo)

- `app.py:_fetch_kalshi_markets` — public market reader (dollar-string price
  landmine: use `_kalshi_cents()`; `yes_bid`/`last_price` are null).
- `_kalshi_side_book` / `_kalshi_line_book` / `_kalshi_nearest` — order-book
  readers, series-disambiguated (team codes alone are ambiguous during an MLB
  series). Built when Kalshi was briefly a routed venue (June–July 2026),
  de-venued July 5 on user order; **the re-venue hooks are intact**.
- `_cross_book_signal(kbook=None)` — the make/take engine already accepts a
  Kalshi book; `_KALSHI_ROUTE_HURDLE_C` constant dormant.
- `_KALSHI_SERIES` / `_TEAM_TO_KALSHI` maps (MLB/NBA/NHL/NFL; NFL codes need
  live verify via `/debug-kalshi?sport=nfl`), `KXUFCFIGHT` (verified),
  `_KALSHI_LINE_SERIES` (MLB totals/spreads suffix encoding, verified).
- `/debug-kalshi`, `/debug-kalshi-discover` — shape probes.

Key structural advantage vs PMM: Kalshi lists **two rows per game** (one per
team) — each side is a real market. **No synthetic-side inversion** (the PMM
landmine class — `_invert_book`, `inverse=1`, `*_SHORT` flips — disappears).

## 3. New surface: authenticated trading API

- Base: `https://api.elections.kalshi.com/trade-api/v2` (demo:
  `https://demo-api.kalshi.co/trade-api/v2` — use for Phase 0 smoke tests).
- Auth: API key pair — `KALSHI-ACCESS-KEY` (key id), `KALSHI-ACCESS-TIMESTAMP`,
  `KALSHI-ACCESS-SIGNATURE` = **RSA-PSS(SHA256)** signature of
  `timestamp + METHOD + path`. Key generated in Kalshi account settings; store
  `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY` (PEM) in Vercel env. No SDK
  dependency needed — ~40 lines with `cryptography` (already a transitive dep;
  VERIFY it imports on Vercel before committing to no-SDK).
- Endpoints (VERIFY exact shapes in Phase 0 against demo + docs.kalshi.com):
  - `POST /portfolio/orders` — {ticker, action: buy/sell, side: yes/no, count,
    type: limit, yes_price (cents), expiration_ts, client_order_id}. Check for
    a `post_only` flag — if it exists, use it on every maker order (guarantees
    never paying taker; the perps receipts showed the app happily crossing).
  - `DELETE /portfolio/orders/{id}` — cancel.
  - `GET /portfolio/orders` / `GET /portfolio/fills` (since-timestamp cursor) /
    `GET /portfolio/positions` / `GET /portfolio/balance`.
  - WebSocket `fill` channel exists but is NOT needed — REST fills polling on
    the existing 1-min cron slot is plenty (and Vercel can't hold sockets).
- **The fills API is the big simplification:** Polymarket's SDK only returns
  currently-open orders, which forced the Filled Bot's two-path
  disappeared-order forensics. Kalshi hands us the fill history directly —
  the whole detection layer becomes "query fills since last cursor".

## 4. Build plan (phased, each phase shippable + reversible)

### Phase 0 — creds + probe (half a session)
1. User creates Kalshi API key; env vars into Vercel (+ demo key optional).
2. `/api/kalshi/probe` (admin-gated): balance + open orders + last 5 fills,
   raw shapes logged. Verify RSA signing, order create/cancel **on demo**.
3. Verify `post_only` existence; verify fee lines on a 1-lot real maker order
   (expect $0) and the fill notification arriving natively in the app.

### Phase 1 — entry + make/take re-venue (1 session)
Feature flag `KALSHI_EXECUTION=1` (env). When on, for the ADMIN's surfaces:
1. Suggestion entry: `entry_book='KALSHI'`, `entry_price` = Kalshi maker bid
   (American conversion identical; cents are cents). `handicapper_web` already
   attaches Kalshi cents via `exch_current` — thread the side's bid into the
   suggestion the way `pmm_bid_american` is today.
2. Make/take: re-enable the Kalshi leg of `_cross_book_signal` as the PRIMARY
   (single-venue again, just Kalshi this time): fee model taker
   `7·p·(1−p)`¢/contract rounded up **per order**, maker 0, **no rebate term**.
   MAKE+ tick size on Kalshi is 1¢ (no half-cents — verify).
3. Log-pick POST accepts `entry_book='KALSHI'` (validation list + UI label).
   Non-admin users: unchanged (their books are notional anyway; entry price
   source follows whatever the dossier quotes — decide in Phase 1 whether
   viewers' quoted entries flip with the flag or stay PMM; simplest: flip
   globally with the flag, the venues price within 2¢).

### Phase 2 — fills: auto-sync + status chips, retire the Filled Bot (1 session)
1. `kalshi_fill_state` table (thin: order_id, ticker, pick metadata,
   client_order_id, cum filled, last_cursor) — or just a cursor row; fills API
   is authoritative, unlike Poly where we had to reconstruct.
2. Port `/api/handicapper/fill-status`: match pending admin `bot_picks` to open
   Kalshi orders/fills by `client_order_id` (stamp it at log time =
   the exact-match discipline the PMM slug stamping bought us — do this from
   day one, no heuristic era). Statuses: resting/partial/filled/none/warn +
   **outbid** (port `_fs_outbid_info` — read `_kalshi_side_book`, compare our
   resting price to best bid; no synthetic inversion needed).
3. Port `_fs_auto_sync_entry`: restamp `entry_price` from real fills
   (weighted avg, fee = $0 maker / 7% formula if a fill was taker — fills API
   reports taker flag? VERIFY; if not, infer from order type/price vs book).
   UNITS ARE NEVER TOUCHED (standing user rule — dollars ≠ units).
4. Retire: `/api/polymarket/check-fills` cron curl, `polymarket_fill_state`
   writes, the fill-milestone Telegram messages. KEEP `_bet_alerts` (different
   feature, same bot token). Keep the Poly code paths dormant until the Poly
   book is empty, then delete in a cleanup pass (CLAUDE.md deletion-pass rule:
   run surviving consumers after deleting — the WC kill lesson).

### Phase 3 — burn-in + cleanup (background)
- Parallel-run rule: user funds Kalshi sports with a small slice first; 2 weeks
  green (fills sync correctly, CLV sane, no orphaned picks) before moving the
  main bankroll. Poly rebates keep accruing on whatever stays.
- CLAUDE.md updates: Access-control table rows for new endpoints, retire the
  Polymarket Fill Alerts section to a stub, entry-price rules in the Pick Bot
  section, env var table (+`KALSHI_API_KEY_ID`/`KALSHI_PRIVATE_KEY`, note
  `FILLED_BOT_*` survives for bet alerts only).
- Dashboard stays Poly P&L; a Kalshi P&L/dashboard is a separate future project
  (balance + fills give everything needed; `/api/clv` close-pair source is
  already exchange-based for the Pick Bot).

## 5. Risks / open questions

1. **`post_only` support** — if absent, maker discipline relies on price checks
   at submit time (compare vs live book, refuse to cross) — build that guard
   regardless; the app's own defaults happily cross (observed twice).
2. **Fee-schedule drift** — July 7 2026 schedule added maker fees to SOME
   series (25% of taker). Sports verified $0 July 12; re-verify per new sport
   series at Phase 1 (one 1-lot resting ticket = the test).
3. **Order minimums/tick** — sports tick is 1¢; verify no per-order minimums
   that break 1u sizing.
4. **Rate limits** — fills polling + book reads on the 1-min cron; Kalshi
   public limits are generous but the authed tier needs a Phase-0 check.
5. **Multi-user semantics** — only the ADMIN executes real money; other
   `bot_access` users' picks are notional logs. Their quoted entries follow the
   dossier's venue flag (within 2¢ either way). No per-user creds, ever.
6. **What if the user changes his mind again** — everything lands behind
   `KALSHI_EXECUTION`; flipping the flag restores Poly execution wholesale.
   (Kalshi was venued once already and de-venued July 5; design for whiplash.)

## 6. Effort estimate

Phase 0: ~half a session. Phase 1: ~1 session. Phase 2: ~1 session.
Phase 3: background over 2 weeks. Deletions at the end are net-negative code:
the Filled Bot system (~2 detection paths, milestone dedup, first-sight rules)
comes out entirely.
