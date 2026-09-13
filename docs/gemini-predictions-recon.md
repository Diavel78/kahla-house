# Gemini Predictions — venue recon (Sep 13 2026)

Read on: Rob got ~$5 back on ~$2 resting orders and asked what the machine is.
Everything below was read from the venue itself (public REST + a live
websocket probe) or its developer docs on Sep 13 2026. Re-measure before
believing any number here that is more than a week old — the program-table
rule applies to this venue exactly as it does to Polymarket.

## What it is

Gemini Predictions is the Winklevoss exchange's CFTC-regulated event-contract
venue (Gemini Titan DCM). Binary $1 contracts, YES/NO, 1¢ tick, 0.01-contract
minimum quantity. Sports, crypto, politics, weather, macro. In-play trading is
on (the MLB and NFL books were live mid-game during the probe).

## The three money programs

| program | pays for | rate / size | paid |
|---|---|---|---|
| Maker Rebate | FILLED maker orders, 20-80¢ band | `rebate = 0.30 × 0.07 × C × P(1−P)` (sports rate rule 0.50 expired Jul 9 2026; the 0.30 catch-all applies now), cap 5% of notional | daily 5pm ET, USD |
| Liquidity Rewards | RESTING orders, filled or not | per-EVENT daily USD pool, split by score (below) | daily 5:30pm ET, USD, min $1/day |
| Taker Rewards | taker volume ≥ $500k / 30d | irrelevant to us | — |

Maker FEE is `0.0175 × C × P(1−P)`, rounded UP to the cent. The rebate
(0.021) exceeds the fee (0.0175), so a filled maker order nets about
+0.0035·C·P(1−P): at 20 contracts and 50¢ that is roughly +1.5¢. Fills are
free at 20 contracts; the rent is the Liquidity Rewards pool. ⚠ At 5 contracts the fee ROUNDS UP
(2.19¢ → 3¢) while the rebate does not (2.6¢), so tiny lots net slightly negative — the
rebate paid on Rob's Sep 12 fills was exactly this: 2 maker fills ≈ 2.6¢ each → $0.05.
CORRECTION (venue per-fill ledger, same day): 34 of his 36 hand fills were MAKER (`/v1/mytrades`
`aggressor:false`), only 2 were taker (9¢ fee each). The Sep 12 5pm ET payout counted just the
LAST TWO maker fills before it (3:39pm + 3:58pm ET, $2.40 + $2.10 = $4.50 → 5¢); five earlier
same-day maker fills were NOT in it — the rebate window/lag is unresolved until the next payout
(Sep 13 5pm ET should carry ~27 maker fills ≈ $0.70 if it is a rolling 24h window).
**RESOLVED Sep 13 5pm ET: it is NOT a window.** That payout counted 6 fills / $13.89 / $0.15;
lifetime 8 of 34 maker fills are credited (~25%). The two Friday fills that paid were the LAST
two before the cutoff (3:39pm + 3:58pm ET) while five earlier same-day maker fills never paid
in either payout. Best reading: the rebate is a share of the taker fee ACTUALLY COLLECTED on
the fill, and most takers on this book (the contracted 250-lot maker re-centering, promo-cash
users) pay none — so ~3 in 4 maker fills earn $0. Treat the rebate as noise; the pool is the
only rent that matters. There is no per-fill rebate endpoint (payouts/{id}, /fills, summary/daily
all 404), so this can't be proven from the API.
**Maker fee actually charged: $0.00 on every one of the 34 maker fills** — the schedule says
0.0175 but the account is being charged nothing (promo or waiver); a maker fill is strictly
positive right now.

Per-fill truth endpoints (the spot API works on GEMI- symbols): `POST /v1/mytrades
{symbol, limit_trades}` → `aggressor`, `fee_amount`, `order_id`, `tid`; `POST /v1/tradevolume`
→ per-symbol per-day maker/taker counts + notional; `POST /v1/transfers` → the credit ledger
(`purpose: "Predictions maker rebate"`, deposits).

## Liquidity Rewards — the rent machine

Source: developer.gemini.com/prediction-markets/liquidity-rewards-program and
`GET /v1/prediction-markets/liquidity-rewards/{config,events}` (public).

Scoring, verbatim shape:

```
snapshot score (every minute, per maker, per contract)
  = spread_weight × size × two_sided_multiplier
daily payout = pool × (your daily score / total qualifying score)
```

| rule | value |
|---|---|
| max distance from midpoint | 10¢ (`max_spread_cents: 10`) |
| minimum order size | 10 contracts |
| size cap | 250 contracts |
| two-sided multiplier | 1.5× at any snapshot with a qualifying bid AND ask on the same contract |
| uptime threshold | ≥ 50% of eligible snapshots, else the whole day scores ZERO |
| spread weight | quadratic in distance from mid; on a ONE-SIDED book the best price on your own side is the reference |
| minimum payout | $1.00/day, rounded down to the cent |
| excluded | contracts in post-only mode, house/test accounts |

The pool is per EVENT (a whole spread ladder or a whole prop family is one
event); every contract in the event scores into the same pool.

Pool census on Sep 13 2026 (620 pools, $209,450/day program-wide):

| category | pools | $/day | note |
|---|---|---|---|
| Midterms (House/Senate/Gov) | 197 | $98,500 | |
| Weather (daily temperature) | 98 | $49,000 | |
| **Pro Football** | **183** | **$36,600** | **$200/day on EVERY NFL game market**: ML, spread, total, team total, and each of 5 player-prop families; 107 pools on today's slate alone; qualifying makers per pool min 2 / median 4 / max 14 |
| Crypto (BTC/ETH/XRP/SOL/ZEC) | 91 | $10,800 | 5-min / 15-min price events |
| Commodities | 18 | $9,000 | |
| Pro Baseball | 2 | $300 | FUTURES ONLY — no MLB game pools |
| College Football | 1 | $150 | playoff futures only — no CFB game pools |
| NBA / NHL / UFC | 0 | 0 | none listed today |

Pool `ends_at` for a 1pm NFL game is 11:00Z the next morning, so the pool
covers pre-game and in-play.

Empty-rung census across today's NFL ladders (bestBid/bestAsk from the
events list): spreads 21 rungs with no book at all + 23 with no bid; totals
18 + 7; player-prop rungs are MOSTLY empty (receiving yards: 419 rungs with
no book, 383 more with no bid). On a one-sided book your own price is the
reference, so a lone 10-lot bid on an empty rung scores at full spread
weight. That is the alone-in-the-window shape we measured on Polymarket,
priced 100× higher per event.

Incumbents on the main lines rest 250-lot two-sided quotes 1-3¢ wide
(CHI-CAR spread rungs: 277 × 188, 256 × 181). Share math at 20 one-sided
contracts against four such makers is ~1.3% of a $200 pool ≈ $2.60/pool/day;
across 107 game pools ≈ $280/day. At the 250 cap two-sided it is ~20% per
pool. Capital, not rent, is the constraint.

## API — everything needed to build

REST base `https://api.gemini.com` (sandbox `api.sandbox.gemini.com` has ZERO
prediction events — useless for this).

Public, no auth (all verified live):
- `GET /v1/prediction-markets/events?category=sports&status=active&limit=500` — returns `{data:[...], pagination}` (key is `data`, not `events`); each event carries `contracts[]` with `instrumentSymbol`, `prices{buy{yes,no},sell{yes,no},bestBid,bestAsk,lastTradePrice}`, `strike{type,value}`, `priceIncrement`, `quantityMinimum`, `expiryDate`; event carries `startTime`, `isLive`, `gameId`, `sportsMarket{sport,type,subject,scope,metric,team/player}`.
- `GET /v1/prediction-markets/events/{eventTicker}` — same, no depth (`contractOrderbooks` is null).
- **`GET /v1/book/{instrumentSymbol}`** — full L2 with sizes (`bids/asks[{price,amount,timestamp}]`). The old spot endpoint works on GEMI- symbols.
- `GET /v2/ticker/{symbol}`, `GET /v1/trades/{symbol}?limit_trades=N` — 24h candles-ish + trade tape.
- `GET /v1/prediction-markets/liquidity-rewards/config|events`, `/maker-rebate/rates`.

Private (HMAC): `X-GEMINI-APIKEY`, `X-GEMINI-PAYLOAD` = base64(JSON with
`"request": "<path>"`, `"nonce"`, params), `X-GEMINI-SIGNATURE` =
hex(HMAC-SHA384(payload, secret)); empty body, `Content-Type: text/plain`.
Account-scoped key (`account-…` prefix) with the time-based nonce option
(epoch SECONDS, ±30s) and Trading permission. First call:
`GET /v1/prediction-markets/terms/status` → `POST …/terms/accept` or every
order is refused.

Orders: `POST /v1/prediction-markets/order` `{symbol, orderType:"limit",
side:"buy|sell", outcome:"yes|no", quantity, price, timeInForce,
makerOrCancel:true, clientOrderId}`; batch of 1-20; cancel by `orderId`.
Positions/orders/fills: `POST /v1/prediction-markets/orders/active`,
`/orders/history`, `/positions`, `/positions/settled`. Rewards per account:
`/liquidity-rewards/summary/daily?dateFrom&dateTo` (per-event score
breakdown), `/maker-rebate/payouts`.

Rate limits (from the socket's `conninfo`): request weight 7,000 per 10s,
orders 3,500 per 10s, 10,000,000 per day, 300 connection attempts per 5 min.

WebSocket `wss://ws.gemini.com?snapshot=-1` (verified live):
- request shape `{"id":"1","method":"SUBSCRIBE","params":["<symbol>@<stream>"]}` — **params is a plain ARRAY of strings**; the `{streams:[...]}` object form is rejected with -1013.
- valid public streams: `bookTicker`, `depth` (differential, `depthUpdate` frames with `[price,qty]` deltas, qty 0 = remove), `depth@100ms`, `depth5|10|20`, `depth5@100ms`, `depth10@100ms`, `trade`. Symbols echo back lowercase.
- `contractStatus` (no symbol) streams every contract lifecycle change venue-wide — new listings arrive live (`Awaiting Approval → Approved → Active`), which is the rent-list feed.
- private (auth at handshake, same HMAC headers, cannot auth after connect): `orders@account`, `balances@account`, position updates; WS trading methods `order.place / order.cancel / order.cancel_all / order.cancel_session`; `?cancelOnDisconnect=true`.
- add `snapshot=-1` to get a full book on subscribe.
- trading methods (playground, verified Sep 13): `order.place` `{symbol, side: BUY|SELL, type: LIMIT|MARKET, timeInForce: GTC|IOC|FOK|MOC, price, quantity, clientOrderId, eventOutcome: YES|NO}` — **`MOC` (maker-or-cancel) is post-only on the socket**; `order.cancel {orderId}`, `order.cancel_all`, `order.cancel_session`; `depth {symbol, limit≤5000}` returns an on-demand L2 snapshot. Method names are accepted lowercase.
- REST vs socket body shape: the generic Gemini rule is an empty body with everything in `X-GEMINI-PAYLOAD`; the prediction-markets place-order example sends the JSON body as well. `gemini_pm.detect_body_mode()` tries the header-only shape on the read-only `orders/active` POST first and remembers what worked, so a mutating call is never retried in a second shape.

**Official code exists — read it before guessing (Sep 13 2026):**
`github.com/gemini/developer-platform` carries Python/TS/Go samples (`samples/python/pm_order.py`
= a prediction-market order over the socket, `balances.py` = REST HMAC), an MCP server with
prediction-market tools (`packages/mcp-server/src/auth/signer.ts` is the authoritative REST
signing recipe: nonce SECONDS, `{request, nonce, ...fields}` payload, header-only POST,
`text/plain`), and a TypeScript SDK `@gemini-markets/sdk`. No official Python SDK.
`gemini_pm.py` mirrors the signer exactly. The MCP client parses `orderId` with a big-int
parser because the venue emits 17-18 digit ids that JavaScript truncates — irrelevant in
Python, fatal in the browser.

## Landmines found

1. **No good-till-date.** `timeInForce` is GTC / IOC / FOK only. A resting bid lives through kickoff and the whole game unless we cancel it. Kickoff cancel is OUR job (or `cancelOnDisconnect` on the socket).
2. **Uptime ≥50% or ZERO.** Snapshots are per minute across the pool day; a quote placed late in the day can fail the threshold and earn nothing for that event, however tight. Quote from listing / from the start of the pool day, not from T-6h.
3. **NO is a native side** (`outcome:"no"` priced in NO terms — the events list shows `buy.no = 1 − sell.yes`), NOT Polymarket's yes-canonical `BUY_SHORT`. Verify on the first live order before any peg math trusts it.
4. **Fractional contracts** (`quantityMinimum 0.01`) — dust lots exist here too.
5. The `events` list key is `data`; `sportsMarket` is null on non-game events; `limit` max 500.
6. Sandbox lists no prediction events; there is no dry-run venue. First orders are real, at 10 contracts.
7. Rebate/reward payout timestamps are ET; pool days are ET. Reconcile in ET, report in AZ.

## "Promo cash returned" — what the ~$5 probably was

Both reward programs pay USD, labelled as rewards, at 5:00/5:30pm ET. The
venue's legal terms use "promo cash" only for promotional credits (the
Aug 17–Sep 30 deposit/trade rewards, the Sep 2–15 NFL 50% trade match) and
state that promo cash applied to a voided/cancelled/refunded trade is
credited back. So a line literally reading "promo cash returned" is most
likely a promo credit coming back off a cancelled or unfilled order, not
rent. The rent, if any, is a separate USD credit. Settle it from the API:
`liquidity-rewards/summary/daily` and `maker-rebate/payouts` list exactly
what the two programs paid per day and per event.

## Fit with the machine (recommendation, not a build)

- The rent list here is `liquidity-rewards/events`. Today it is NFL only
  for sports. MLB game markets exist (23 ML + 16 spread + 16 total + 22
  props today) but pay NOTHING — per the rent rule they are not bettable.
- The lane shape is the football executor's: for each NFL pool event,
  seat ≥10 contracts within 10¢ of mid on the model's side of every rung,
  prefer rungs with no bid (one-sided reference = full weight), hold ≥50%
  uptime, cancel at kickoff unless we want in-play exposure, scalp fills
  at cost (fills are net-positive after rebate).
- Two-sided is 1.5× and the incumbents do it; a two-sided seat on a binary
  means a YES bid + a YES ask (short = NO collateral). Decide inventory
  policy before enabling it.
- Public data (events, book, socket) needs no key; a rent tape can start
  today. Orders need an account-scoped key + terms acceptance.

## Live venue truth (first authenticated read, Sep 13 2026 ~11:15 AZ)

- Key works (account-scoped, Trader, time-based nonce). Terms already accepted. Body mode
  detected = `header` (empty body), exactly the official signer.
- **Liquidity Rewards lifetime: $0.00. Maker rebate lifetime: $0.05** (2 fills, $4.50
  volume, paid Sep 12 5pm ET). The ~$5 "promo cash returned" was a promo credit, not rent.
- Balance frame: `available` is the spendable cash; `amount`/`c` read NEGATIVE (−39.87 with
  60.13 available) — the venue nets open positions into it. Use `available`.
- **Position sign: a NO holding is a NEGATIVE quantity** (`positionReport` … `"v":"-5"`,
  REST `outcome:"no"`), the Kalshi/Poly short convention.
- Order life on the socket: REST create → `orderUpdate X=NEW` (fields `i` id, `c` client id,
  `S` BUY, `O` YES, `p` price, `q` qty, `z` remaining) + a `balanceUpdate` + a
  `depthUpdate` showing our level, all within ~1s. Book read confirms the same second.
- `orders@account` snapshot arrives as `{"e":"orderSnapshot","orders":[...]}`.

### Depth census, every NFL pool rung (3,795 rungs, 125 events)

1,374 rungs have a two-sided book. Median spread 8¢ (q1 4, q3 12). Median 1 bid level,
2 ask levels. Dollars resting within 10¢ of mid across the whole NFL board ≈ $409k.

| kind | rungs | two-sided | med spread | med bid in-window | med ask in-window | med $ in-window per event |
|---|---|---|---|---|---|---|
| M (moneyline) | 28 | 26 | 3¢ | 4,557 | 4,557 | $5,289 |
| S (spread) | 354 | 266 | 4¢ | 1,056 | 381 | $5,604 |
| T (total) | 266 | 203 | 4¢ | 493 | 527 | $4,253 |
| **TT (team total)** | 355 | 112 | 11¢ | **10** | **10** | $821 |
| PPTD | 601 | 169 | 8¢ | 250 | 45 | $1,405 |
| PPRECY | 1,157 | 334 | 12¢ | 25 | 35 | $1,088 |
| PPRYDS | 667 | 152 | 12¢ | 12 | 35 | $182 |
| PPYDS | 252 | 78 | 12¢ | 36 | 35 | $682 |
| PPPASSTD | 115 | 34 | 11¢ | 35 | 35 | $228 |

Reading: **the main lines are NOT tiny** — a professional maker rests 250-lot two-sided
quotes 3-4¢ wide, thousands of contracts inside the window, and the pool is the same $200.
**Team totals and props ARE tiny** — the typical rung carries exactly one 10-lot bid and
one 10-lot ask 11-12¢ wide (the program minimum, quoted by 2-4 makers), so a 10-lot seat
is a meaningful fraction of the same $200 pool. That is where the experiment sits.

### The $10 experiment (resting now; ledger `~/.kahla/gemini_orders.json`, 12 orders)

| group | seats | what | pool-day uptime expected |
|---|---|---|---|
| 4:25pm ET games (today) | 8 × 10 @ 5¢ virgin rungs | TT / prop rungs with no book | ~2h before the kickoff cancel → likely fails the 50% rule |
| SNF DAL–NYG | 2 × 10 @ 5¢ virgin | PPTD, TT | ~6h |
| MNF DEN–KC | 1 × 10 @ 5¢ virgin (`DENO3`) + **1 × 10 @ 45¢ in-window** (`DENO20`, bid+1 tick, 4.5¢ from mid, against a single 10-lot two-sided incumbent) | full Monday pool day |

Read the answer with `gemini_probe.py status`: `liquidity-rewards/summary/daily` breaks
the payout down per event. Sunday's pools pay Monday 5:30pm ET (2:30pm AZ); Monday's pay
Tuesday. Kickoff cancel: launchd `com.kahlahouse.gemini-cancel` (plist in `cellar/`,
every 300s, log `~/.kahla/logs/gemini-cancel.log`) — cancels only `kh-seed-*` orders on
games whose start has passed; hand-placed orders are never touched.

## Kickoff rule (Rob, Sep 13 2026)

**Props: every order, bid OR ask, cancels at kickoff** — they resolve yes/no, nothing to
manage in-play. **ML / spread / O-U asks on held positions should stay live in-play, at
cost, exactly like Polymarket — but ONLY once a repeg exists on this venue.** With no
repeg (today) everything cancels at kickoff; a stale in-play ask with nothing chasing it is
the risk. `gemini_probe.py cancel-started` implements "everything cancels" for now.
