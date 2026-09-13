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
free; the rent is the Liquidity Rewards pool.

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
