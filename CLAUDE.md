# The Kahla House — Bet System

Multi-page sports betting platform deployed at **thekahlahouse.com**. Flask backend on Vercel, Firebase Auth + Firestore, vanilla JS frontend. This is the ONLY active codebase for the bet system. The "Poly-Tracker" repo is deprecated and not used.

**CRITICAL: This project lives at `/Users/robkahla/Documents/Kahla House/kahla-house/`. The domain is thekahlahouse.com. The Vercel project is `kahla-house`.**

> **PUSH RULE**: Every commit goes to `main`. Vercel auto-deploys from `main`. If you're working on a feature branch, finish the work, then merge to `main` and push `main` — without being asked. Don't leave changes sitting on a branch waiting for permission.
>
> **DOC RULE**: Whenever code or behavior changes, update this CLAUDE.md in the same commit. The project is too sprawling to navigate without an accurate map.

## Access Control (read this first)

Three roles in Firestore `users/{uid}.role`:
- **`admin`** — full access (Odds, Dashboard, debug). Rob.
- **`viewer`** — Odds only. Friends use this tier.
- **`pending`** — default for new signups. No access until an admin approves.

Approval flow:
- Sign-up creates a `pending` user doc with `approved: false`. The pending screen tells them to wait.
- Admins see pending users in the User Management panel on `/` with **Approve as Viewer** / **Approve as Admin** / **Reject** buttons.
- The **first** signup on an empty users collection auto-promotes to admin so the platform can bootstrap.
- Admin role dropdown can move users between `admin` / `viewer` / `pending` at any time.

Per-page gating (client-side via `/api/me` probe + server-side via decorators):
| Page / API | Roles allowed | Server gate |
|---|---|---|
| `/odds` (page) | admin, viewer | client probes `/api/me` and bounces unauthorized |
| `/api/odds`, `/api/odds/history`, `/api/odds/history-batch`, `/api/openers*`, `/api/preferences` | any approved | `@firebase_auth_required` (rejects pending) |
| `/dashboard` (page) | admin | client probes `/api/me` and bounces non-admins |
| `/api/data`, `/api/my-bets`, `/api/debug-trades`, `/api/debug-deposits`, `/api/debug-snap` | admin | `@admin_required` |
| `/api/raw` (Polymarket SDK debug) | admin | `@admin_required` |

`@firebase_auth_required` itself rejects any user where `approved != true` (returns 403), so even API endpoints that don't need admin still keep `pending` users out.

## Pages & Routes

| Route | Template | Access | Purpose |
|---|---|---|---|
| `/` | `index.html` | public | Landing page (login/signup, pending screen, admin panel, app cards by role) |
| `/odds` | `odds.html` | admin + viewer | Odds Board — multi-book odds comparison, opener-vs-current movement, per-game line-movement chart modal |
| `/dashboard` | `dashboard.html` | admin only | Polymarket P&L Dashboard — positions, closed trades, bet slip |

> **Odds-ingest cron (`kahla-scanner/`)**: minimal Python subproject at
> `kahla-scanner/` runs `python -m scrapers.odds_api` every 30 min via
> GitHub Actions (`.github/workflows/scanner-poll.yml`). Triggered ONLY
> by an external cron-job.org workflow_dispatch — the GitHub-native
> schedule was killed because both firing every 30 min was queueing
> back-to-back via the concurrency group, doubling credit burn.
> `cancel-in-progress: true` so any retry/manual-overlap kills the
> in-flight run; each run is idempotent (dedup) so partial runs lose
> nothing.
>
> The cron hits The Odds API (https://the-odds-api.com) for
> `/v4/sports/{sport_key}/odds` with `markets=h2h,spreads,totals` and
> `regions=us,eu` (EU required for Pinnacle — NOT in the US region).
> Writes deduped rows to Supabase `book_snapshots` for every (market,
> book, market_type, side). Powers the Odds Board, the line-movement
> chart modal, AND the inline 3-row sparkline per game card — all reads,
> no live odds-vendor API calls from Flask.
>
> Cost: 6 credits/call × 7 sports × 2 calls/hr × 24h × 30d = ~60K
> credits/mo on the $59/100K-credit tier.
>
> A second workflow `.github/workflows/snapshot-cleanup.yml` runs nightly
> at 09:00 UTC and deletes `book_snapshots` rows older than 15 days —
> chart range maxes out at "All" but games are over after a few hours,
> so retention beyond ~2 weeks just bloats Supabase.
>
> Owls Insight was the prior provider; retired April 2026 due to
> coverage gaps (only 7 of 15 MLB games returned on a typical Saturday).
> Brier/signals/Telegram pipeline was retired earlier in the same spring
> cleanup. Player Props page, Owls live scores, and Circa betting splits
> were removed when Owls was cancelled — props weren't being used,
> live scores got reimplemented via free ESPN scoreboard JSON, and
> **Circa is not in The Odds API at any region** (known data gap).

### API Routes

`Firebase` = `@firebase_auth_required` (any approved user). `Admin` = `@admin_required` (must also be role=admin).

| Route | Auth | Purpose |
|---|---|---|
| `GET /api/me` | Firebase | Lightweight role probe — returns `{uid, role, approved, displayName, email}`. Used by every sub-page to gate UI before loading data. |
| `GET /api/odds?sport=mlb` | Firebase | Odds Board JSON — built from latest `book_snapshots` per (market, book, market_type, side) in Supabase. Cron-only; no live Odds API call here. Includes anchor sweep so books that haven't priced inside the freshness window still show their last value. Merges ESPN scoreboard data per event for live scores. Returns `last_data_iso` so the page can show "last odds update Nm ago" instead of a wall clock. |
| `GET /api/odds/history` | Firebase | Line-movement history for one event from Supabase `book_snapshots`. Params: `sport`, `home`, `away`, `commence` (ISO), `market` (ml/spread/total), `since` (15m/30m/1h/6h/12h/24h/all). Returns step-function-ready data per book per side. Books: 14-book allowlist (see _ALLOWED_BOOKS). Chart modal defaults to PIN only at 12H. NO live-game freeze on this endpoint — full history including post-start movement. |
| `GET /api/odds/history-batch?sport=mlb` | Firebase | 6-hour PIN history for ALL active games in the sport, batched in one response. Three series per game: ML home, Spread home, Total over. Powers the inline sparklines in each game card footer. Live-game freeze applied — same as the board cells. |
| `GET /api/openers?sport=mlb` (also POST) | Firebase | Legacy Firestore openers (fallback for games predating the cron). Permanent per game ID. |
| `GET /api/openers/scanner?sport=mlb` | Firebase | **Primary opener source.** Earliest PIN snapshot per (market_type, side) from Supabase `book_snapshots`. Client matches against current events by team + commence_time within ±30 min and merges over Firestore openers. (PIN-only post-Owls; Circa not in The Odds API.) |
| `GET /api/splits?sport=mlb` | Firebase | Public ML betting splits (% bets, % money) per game. Three-layer fetch: (1) Action Network's JSON API at `api.actionnetwork.com/web/v2/scoreboard/{league}` is the primary source (today's scheduled games + live %s), (2) `<script id="__NEXT_DATA__">` JSON in the SSR HTML page as backup, (3) HTML table parser as last resort (legacy, only catches yesterday's finals). Cached 30 min server-side per (sport, date). Successful parses cache; failures don't, so the next hit retries fresh. |
| `GET/POST /api/preferences` | Firebase | User settings (books, sport, order) in Firestore |
| `GET /api/my-bets` | **Admin** | Active Polymarket positions (Dashboard only) |
| `GET /api/my-orders` | **Admin** | Open / unfilled Polymarket limit orders (CLOB working orders). Filtered to NEW / PENDING_NEW / PENDING_REPLACE / PARTIALLY_FILLED states — filled, canceled, expired, rejected excluded. Powers the **Open Orders** section of the dashboard betslip so planned bets can be shared with friends before they fill. 30s server cache. |
| `GET /api/clv` | **Admin** | Closing Line Value per open Polymarket position whose underlying game has started (so PIN has a closing line). Matches each Polymarket bet to our `markets` table (sport prefix + commence date + fuzzy team name), pulls PIN's last pre-`event_start` snapshots on both sides, devigs the pair, computes `(close_devig_prob − entry_implied_prob) × 100`. Positive = sharp entry, negative = got picked off. Returns per-bet records + `avg_clv_pp` rolling average across matched bets. 60s server cache. v1 covers open positions only — closed/settled bet history + 30-day rolling per-signal hit-rate is Phase 4 (Sharp Bot). |
| `GET /api/data` | **Admin** | Dashboard P&L data (positions, balances, trades) |
| `GET /api/raw` | Admin | Debug: raw Polymarket SDK responses |
| `GET /api/debug-trades` | **Admin** | Debug: grouped trade details with before/after position data |
| `GET /api/debug-deposits` | **Admin** | Debug: all balance changes with types and reasons |
| `GET /api/debug-snap` | **Admin** | Debug: Supabase row counts + sample markets/snapshots + what `_fetch_odds_from_snapshots` returns. JSON. |
| `/debug?slug=X` | Firebase (page) | Debug page that calls debug-trades with auth |
| `/debug-deposits` | Firebase (page) | Debug page showing all balance changes |
| `/debug-snap?sport=mlb` | Firebase (page) | Browser-friendly wrapper for `/api/debug-snap` |
| `/debug-splits?sport=mlb` | Firebase (page) | Browser-friendly view of `/api/splits` for the splits scraper. Shows raw events, source (`json_api` / `next_data` / `table`), `failed_samples` for unmatched rows, `next_debug` (sample top keys + first candidate field shape), and `api_debug` (URL, status, game count, splits paths seen). Built specifically to iterate on Action Network's shape changes without hitting their site directly from curl. |

## Tech Stack

- **Backend**: Flask (Python), single file `app.py`, Vercel serverless
- **Frontend**: Vanilla JS, embedded CSS in each HTML template (no framework)
- **Auth**: Firebase Auth (client SDK) + `firebase_auth_required` decorator (server validates tokens)
- **Databases**:
  - **Firestore** — user prefs, openers (legacy), user management
  - **Supabase** (Postgres) — `markets` + `book_snapshots`. Sole source of truth for the Odds Board AND the line-movement chart. Written by the kahla-scanner cron, read by Flask.
- **External APIs**:
  - **The Odds API** (`https://api.the-odds-api.com/v4`) — every 30 min via cron-job.org → GitHub Actions; only the cron talks to it
  - **ESPN free public scoreboard** (`https://site.api.espn.com/apis/site/v2/sports/...`) — 30s server cache; called from Flask `/api/odds` to merge live scores onto live games. No auth, no rate-limit issues at our volume.
  - **Action Network** (`https://api.actionnetwork.com/web/v2/scoreboard/{league}` + `https://www.actionnetwork.com/{sport}/public-betting`) — public betting splits (% bets / % money). 30-min server cache per (sport, date). No auth on the JSON API (browser UA + Referer header is enough). Falls back to scraping the SSR HTML's `__NEXT_DATA__` JSON or the rendered HTML table if the API misbehaves. Used by `/api/splits`.
  - **Polymarket US SDK** — Dashboard positions/P&L
- **Fonts**: DM Sans + JetBrains Mono
- **Deployment**: Vercel via `vercel.json`, env vars in Vercel dashboard, auto-deploys from `main`

## Key Files

- `app.py` — All backend logic (~2100 lines, includes splits scraper + JSON API client)
- `templates/odds.html` — Odds board (~2230 lines, includes splits row + sparklines)
- `templates/dashboard.html` — P&L dashboard (~1130 lines)
- `templates/index.html` — Landing page with auth + admin + role-based app cards (~440 lines)
- `kahla-scanner/scrapers/odds_api.py` — The Odds API ingester (cron entry point)
- `kahla-scanner/scripts/cleanup_snapshots.py` — nightly book_snapshots > 15d delete
- `kahla-scanner/scripts/sharp_alerts.py` — Telegram steam + Sharp 7+ alerts (runs after each scanner-poll ingest). Also logs paper bets for the **steam bot** (Phase 4) — every Telegram steam fire writes a row to `paper_bets`.
- `kahla-scanner/scripts/paper_bets_picker.py` — Phase 4 Early/Late EV pickers. `--bot early` runs 1×/day via `paper-bets-early.yml`; `--bot late` runs every 30 min appended to `scanner-poll.yml`.
- `kahla-scanner/_lib/sharp.py` — sharp-score math + sharp-side detection. Used by the paper-bet picker. (`sharp_alerts.py` still has local copies of these helpers — DRY violation kept on purpose to minimise blast radius on the live alert pipeline; both implementations are bytewise identical and any future fix lands in both.)
- `kahla-scanner/_lib/paper_bets.py` — paper-bet shared helpers: PIN devig, best-entry finder, score formula, dedup check, insert helper, snapshot loaders.
- `kahla-scanner/supabase/paper_bets.sql` — `paper_bets` table DDL. Run manually in Supabase SQL editor.
- `kahla-scanner/storage/{models,supabase_client}.py` — slim Supabase wrapper
- `kahla-scanner/_lib/{matcher,normalize}.py` — team-name fuzzy match + odds math
- `firestore.rules` — Firestore security rules (admin/approved helpers)
- `vercel.json` — Vercel deployment config
- `requirements.txt` — Python deps (flask, polymarket-us, requests, python-dotenv, firebase-admin, supabase, **beautifulsoup4**, **lxml**)
- `.env` — Local env vars (DO NOT COMMIT — contains API keys)

## Environment Variables

| Variable | Where | Purpose |
|---|---|---|
| `ODDS_API_KEY` | GitHub Actions secret + Vercel env | The Odds API key (100K-credit tier, $59/mo) |
| `POLYMARKET_KEY_ID` | Vercel env | Polymarket US API key ID |
| `POLYMARKET_SECRET_KEY` | Vercel env | Polymarket US API secret |
| `FIREBASE_SERVICE_ACCOUNT` | Vercel env | Firebase Admin SDK service account JSON |
| `FLASK_SECRET_KEY` | Vercel env | Flask session secret |
| `SUPABASE_URL` | GitHub Actions secret + Vercel env | Supabase Postgres URL |
| `SUPABASE_SERVICE_KEY` | GitHub Actions secret + Vercel env | Supabase service key |
| `TELEGRAM_BOT_TOKEN` | GitHub Actions secret | Bot token from @BotFather. Used by `scripts/sharp_alerts.py`. Optional — alert step is a no-op when missing. |
| `TELEGRAM_CHAT_ID` | GitHub Actions secret | Your Telegram user id (from `getUpdates`). Optional. |

---

## Odds Board (`/odds`)

### Features
- **Best Odds Column** (left, always visible): Best ML, Spread, Total across all enabled books, with devigged **fair line** as a subscript under each price (e.g. `-120 BR / fair -125`). Fair = no-vig American odds derived from the best-of-each-side pair: `_devigPair(awayPrice, homePrice)` normalizes the two implied probs to sum to 1.0. Polymarket-friendly — limit-order at the fair price. SPR/TOT only show fair when the best home/away points match (devigging across different lines is meaningless).
- **Multi-Book Columns** (scrollable right): Individual book odds side by side
- **Sport Tabs**: MLB, NBA, NHL, NFL, NCAAB, NCAAF, MMA. Soccer / Tennis intentionally not listed (cron doesn't ingest them).
- **Search**: Filter by team name (client-side, instant)
- **Book Selector**: Dropdown with checkboxes + up/down arrows to reorder. Saved to Firestore. Hard-filtered through `ALL_KNOWN_BOOKS` allowlist so stale Owls-era preferences don't pollute the dropdown.
- **Live-game freeze**: once `commence_time` passes, the board displays the closing line (last pre-start snapshot per book) and stops showing post-start retail twitches. Server-side filter — see `_fetch_odds_from_snapshots` in `app.py`.
- **Live game header**: green pulsing `LIVE` badge for in-progress games (ESPN `state: "in"`), grey `FINAL` badge once ESPN reports `state: "post"`. Score inline (Away N – N Home), period/clock from ESPN. `closing line` tag next to teams whenever the line is frozen.
- **Line Movement Bar**: Opener vs current with arrows + diffs (per game footer). Driven by PIN-only openers from `/api/openers/scanner`. Each row (ML / SPR / TOT) is split: existing values left, **Sharp Score chip** on the right (`[SIDE] SHARP N`, 1-10 scale, color-tiered low/mid/strong/elite). Score = pure PIN movement magnitude since opener (`|cents|` for ML, `|point_diff|×10` OR `|price_cents|` for SPR/TOT — never additive). Side = the team/over/under whose bet got HARDER. See "Sharp Score" section below for full rule + edge cases.
- **Inline 6H Sparklines** (in the spot where Circa splits used to live): three small Chart.js sparklines stacked per game card — PIN ML home, PIN Spread home, PIN Total Over. Each plots ODDS (price) — line value (e.g. `-1.5`, `8.5`) shown in the row label and tooltip. Y-axis labels in American odds (right side, small). Live-game freeze applied — sparkline stops at event_start. Uses `/api/odds/history-batch`.
- **Public Splits Row** (under the sparklines): horizontal `% bets` / `% money` bar — away% on the left, home% on the right, color-coded blue (bets) / orange (money). Source: Action Network's public-betting JSON API. Optional `SHARP +N%` tag in the header when |money% − bets%| ≥ 10 (sharp-money fingerprint). Hidden by default (`display:none`) — only shows when a match is found and at least one of bets/money has data. Drawn by `drawSplitsRows()` which matches each game card to a splits event by team-name substring containment in either direction.
- **Click-through Chart Modal**: graph icon next to each game header opens a full-screen modal with toggleable books / markets / ranges. PIN-only by default at 12H. Chart modal does NOT freeze on live — full pre+post-start history visible there if you want to see mid-game movement.
- **Status text**: "X games · last odds update Nm ago" (NOT a wall clock — relative to most-recent cron snapshot in the response, so the user knows actual data freshness).
- **Adaptive polling**: 30s when any visible event is live (for ESPN score updates), 90s otherwise. Both poll only Supabase + ESPN — never The Odds API directly. Splits also re-fetched on each poll (cheap; cached 30 min server-side).
- **Double-buffer rendering**: Two board divs swap to prevent flash on re-render.

### Removed (when Owls retired in spring 2026)
- **Owls live scores** — replaced with free ESPN scoreboard JSON merged in `_merge_espn_scores`.
- **Circa betting splits + SHARP/RLM detection** — Circa isn't in The Odds API at any region. Replaced spring 2026 by Action Network public betting splits (free scrape via their JSON API, see `/api/splits` and the "Public Splits" section below).
- **Player Props page** — `/props` route, `templates/props.html`, all `/api/props*` endpoints, props JS. Not being used; props in The Odds API are per-event (more credits per call).
- **Splits-related JS in odds.html** (`renderSplitsRow`, `captureSplitsOpeners`, `loadSplitsOpeners`, `saveSplitsOpenersAPI`, `buildSplitsSnapshot`, `syncSplitsLastChanged`, `loadSplitsLastChanged`, `fmtTsAgo`, `detectRLM`) — fully deleted.
- **All Owls Flask endpoints**: `/api/odds/raw`, `/api/events/raw`, `/api/odds/debug-markets`, `/api/splits/raw`, `/api/props/raw`, `/api/scores/raw`, `/api/realtime/raw`, `/api/splits-openers`, `/api/splits-last-changed`, `/api/props`, `/scanner`, `/debug-odds`. Gone.

### Key JS Functions (odds.html)
- `loadOdds()` — fetches `/api/odds`, calls `captureOpeners()`, then `mergeScannerOpenersInto()`, then `renderBoard()`
- `captureOpeners()` — legacy Firestore opener capture from current PIN data (mostly dormant now)
- `loadScannerOpeners()` / `mergeScannerOpenersInto()` — pulls scanner-backed openers via `/api/openers/scanner` and merges them over `currentOpeners`. Scanner values win.
- `computeMovement()` — PIN-only; compares opener to current, includes JIT backfill
- `renderMovement()` — renders opener → arrow → current for ML/SPR/TOT
- `renderBoard()` — main render, double-buffered. Exposed to `window` for search. Inserts `<div class="splits-row js-splits">` placeholder under the spark-wrap; populated post-render by `drawSplitsRows()`.
- `renderSparkRow()` / `fetchSparklineBatch()` / `drawSparklines()` — inline 6h sparklines (3 per card)
- `fetchSplitsBatch()` / `_matchSplitsEvent()` / `drawSplitsRows()` — public ML splits row. `fetchSplitsBatch` hits `/api/splits` per-sport, `_matchSplitsEvent` resolves Action Network's short names ("Mariners") to our full names ("Seattle Mariners") via two-way substring containment, `drawSplitsRows` populates the bars (% bets always, % money + SHARP tag when present).
- `_amerToProb()` / `_probToAmer()` / `_fmtAmer()` / `_fmtPoint()` / `_fmtDataAge()` — small numeric formatters
- `scheduleNextLoad()` — adaptive setTimeout chain replacing setInterval
- Chart modal: see the IIFE block at the bottom of `odds.html`. Chart.js v4 + date-fns adapter via CDN.

---

## Dashboard (`/dashboard`)

### Features
- **Stats cards**: Balance, Open Positions, Portfolio Value, Today's P&L, Yesterday's P&L, Maker Rewards, Total P&L, Win Rate
- **Open Positions table**: Market, Pick, Qty, Entry, Current, P&L, Return %
- **Closed Positions tab**: Resolved bets + sold trades + maker rewards with Result (W/L/Sold/Maker) and P&L
- **Maker Rewards**: `ACTIVITY_TYPE_TRANSFER` = maker rewards (income, counted in P&L). `ACTIVITY_TYPE_ACCOUNT_DEPOSIT` = user deposits (NOT P&L). `ACTIVITY_TYPE_ACCOUNT_WITHDRAWAL` = withdrawals (NOT P&L). Maker rewards show as a separate stat card and appear in closed positions with "Maker" badge.
- **Bet Slip modal**: Shareable sportsbook-ticket format. Three sections in display order: **Open Orders** (unfilled limit orders from `/api/my-orders` — forward-looking "here's what I'm trying to get into"; shows fill progress like `1/100` for partials), **Pending** (held positions awaiting outcome — from `/api/data`), **Settled Today** (resolved-today bets with W/L/Sold/Maker badges). Orders intentionally don't show in the Open Positions or Closed Positions tabs — they're only on the betslip because they represent intent, not active risk. **Share button** (top-right of header) rasterizes the entire slip — including off-viewport content — to PNG via [html2canvas](https://html2canvas.hertzen.com/) (CDN); on mobile it hands the image to `navigator.share()` (pops the iMessage / share sheet) with the auto-text "Another day of heartbreak and losses queued up!", on desktop it downloads as `kahla-house-betslip-YYYY-MM-DD.png`. Capture forces `max-height: none` on `.betslip.capturing` so the image grabs the full content even if the on-screen modal is scrolled.
- **CLV column on Open Positions** + **Avg CLV stat card**: per-position Closing Line Value (vs PIN's devigged closing line). Bets whose game hasn't started yet show `--` (no closing line yet). Stat card averages all matched positions; rolls in/out as games start/finish. Bets we can't match (non-sport markets, slug parse failures) just don't appear in the rolled-up average — silent skip. See `/api/clv` route + `_clv_extract_match_info` / `_clv_find_market` / `_clv_pin_close_pair` helpers in `app.py`.
- **Auto-refresh**: 60 seconds (loads `/api/data`, `/api/my-orders`, and `/api/clv` in parallel)

### P&L Computation — CRITICAL NOTES
- **Do NOT trust SDK's `price` field** — it returns the COMPLEMENT (YES price when trading NO, vice versa). Always use `cost / qty` for actual per-share price paid or received
- **Do NOT trust SDK's `realizedPnl` value** — it uses complement pricing. Only use non-null as a sell indicator
- **Sell detection**: `realizedPnl is not None` (primary) or `beforePosition.netPosition > afterPosition.netPosition` (fallback)
- **Trade P&L formula**: `(sell_cost/sell_qty - avg_buy_cost_per_share) * sell_qty`
- Self-tracking average cost: accumulate buy `cost` values per slug (NOT `price`), compute avg cost per share
- Both "Position Resolution" AND closed trades count toward realized P&L, win rate, daily P&L
- Activity cutoff: filters out activity before `2026-03-01`
- **SDK fields on trades**:
  - `price` — COMPLEMENT, do not use for P&L (e.g., reports 0.76 when you paid 0.25/share)
  - `cost` — actual dollars spent (buy) or received (sell). Use `cost.value / qty` for real per-share price
  - `qty` — number of shares
  - `realizedPnl` — unreliable value, but non-null = sell indicator
  - `costBasis` — original cost basis (on sells)
  - `originalPrice` — original entry price (on sells)
  - `beforePosition` / `afterPosition` — position state before/after trade (netPosition, cost fields may be null)

---

## Domain Knowledge — Movement

- **Movement / Historical Line Data**: The per-game footer movement bar + the inline sparklines + the click-through chart. All driven by Supabase `book_snapshots`.
  - **Primary opener source**: scanner-backed openers from `/api/openers/scanner` — earliest PIN snapshot per (market, side). PIN-only post-Owls.
  - **Fallback**: legacy Firestore openers in `openers/openers:{sport}` for games predating the cron's history.

### Movement Rules
- **Sharp source**: Pinnacle only. Circa was the historical fallback when PIN dropped lines, but Circa isn't in The Odds API at any region.
- **Opener lock-in**: Once captured for a game ID, PERMANENTLY locked. Never overridden, never reset daily.

### Key Terminology
- **ML** = Moneyline (NOT Machine Learning)
- **SPR** = Spread
- **TOT** = Total (Over/Under)
- **PIN** = Pinnacle (sharp), **DK** = DraftKings, **FD** = FanDuel, **MGM** = BetMGM, **CAE** = Caesars, **HR** = HardRock, **BR** = BetRivers, **BOL** = BetOnline, **LV** = LowVig, **BVD** = Bovada, **ESPN** = ESPN BET, **FAN** = Fanatics, **MB** = MyBookie, **BET365** = Bet365 (US)

---

## The Odds API (`https://api.the-odds-api.com/v4`)

**Auth**: `?api_key=...` query param (NOT a Bearer header — common gotcha when copying patterns from Owls/etc.)
**Plan**: $59/mo, 100K credits/mo, "All sports / All bookmakers / All markets"
**Cost formula**: each call to `/odds` costs `markets × regions` credits. We send `markets=h2h,spreads,totals` (3) and `regions=us,eu` (2) → **6 credits per call**. With 7 sports × 2 calls/hr × 24h × 30d = 60,480 credits/mo, fits in the 100K budget.

> **Region gotcha**: Pinnacle is in the `eu` region, NOT `us`. Without `eu` in the regions param we'd get zero PIN data — defeating the whole sharp-tracking purpose. Adding the second region doubled the per-call credit cost, which is why the cron is at 30 min cadence (not 15).

### Endpoint Used

`GET /v4/sports/{sport_key}/odds?regions=us,eu&markets=h2h,spreads,totals&oddsFormat=american&dateFormat=iso&api_key=KEY`

Response: a top-level JSON array of events, each with a `bookmakers` list, each with a `markets` list (`h2h`/`spreads`/`totals`), each with an `outcomes` list. See `kahla-scanner/scrapers/odds_api.py` for the parse logic.

### Sport Keys
| Scanner code | Odds API sport_key |
|---|---|
| MLB   | `baseball_mlb` |
| NBA   | `basketball_nba` |
| NHL   | `icehockey_nhl` |
| NFL   | `americanfootball_nfl` |
| CBB   | `basketball_ncaab` |
| NCAAF | `americanfootball_ncaaf` |
| UFC   | `mma_mixed_martial_arts` |

### Books Allowlist
The cron + Flask both filter to a 14-book allowlist. Anything else returned by The Odds API (Euro books from EU region — `winamax_fr`, `tipico_de`, `betsson`, `unibet_se`, `marathonbet`, etc.) is silently dropped at ingest. Allowlist must stay in sync between three places:

| File | Symbol |
|---|---|
| `kahla-scanner/scrapers/odds_api.py` | `BOOK_CODES` (Odds API key → short code) + `ALLOWED_BOOKS` (set of allowed short codes) |
| `app.py` | `_SHORT_TO_DISPLAY_KEY` (short code → frontend display key) + `_ALLOWED_BOOKS` (same set) |
| `templates/odds.html` | `BL` + `BL_FULL` + `ALL_KNOWN_BOOKS` |

Allowed short codes (14): `PIN, DK, FD, MGM, CAE, HR, BET365, BR, BOL, LV, BVD, ESPN, FAN, MB`.

### Rate-Limit Headers
- `x-requests-used` / `x-requests-remaining` — logged on every cron run so credit burn is visible in workflow logs.

## Sharp Score (per-market 1-10)

Per-market signal-strength rating shown on each game card's movement bar. Scale of 1-10 where 10 = aggressive sharp signal.

**The unified rule across ML / SPR / TOT:** _sharp side = the side whose bet got HARDER to win._ Books move odds to balance action — whichever side they made worse is where money is flowing. Two distinct sharp signals: a **line move**, OR a **vig-only move** (line flat). Vig drift that comes WITH a line shift is rebalance, NOT a separate signal.

Score is the PIN movement magnitude, full stop. Splits divergence and PIN-vs-retail divergence are NOT folded into the headline number — they're already visible on the card (splits row, per-book odds table) and blending them just dilutes the score when public action happens to be balanced.

Computed JS-side in `computeSharpScore()` (`templates/odds.html`) and Python-side in `_sharp_for_ml/_sharp_for_spread/_sharp_for_total` + `_move_score_ml/_move_score_spr_tot` (`kahla-scanner/scripts/sharp_alerts.py`). Both implementations follow the same rule so the on-card chip and the Telegram alert always agree.

### Score (magnitude)

- **ML**: `|cent_distance(opener, current)|` capped 10. `_amerToCents()` handles the ±100 boundary so a flip from −110 to +110 reads as a 20-cent move, not 0. 1 cent = 1 score, "1 is 1, 5 is 5, 10 is 10".
- **SPR / TOT**: TWO distinct signals, **never additive**.
  - LINE moved (≥0.5pt) → score = `|point_diff| × 10` capped 10. Any juice drift that came along is rebalance, IGNORED.
  - LINE flat → score = `|price_diff_cents|` capped 10. Pure juice move.

### Side detection (which side is sharp?)

| Market | Rule |
|---|---|
| ML | Team whose American odds got more negative (= more expensive to bet = harder = sharp). |
| SPR | PRIMARY: side whose line moved against them (`point_diff < 0` → harder spread to cover). FALLBACK: line flat → side whose price decreased. |
| TOT | Line raised → over needs more = sharp OVER. Line lowered → under has less room = sharp UNDER. Line flat → vig direction. |

Chip prints `[SIDE] SHARP N`. Side label is the team's `truncTeam()` abbreviation for ML/SPR, "OVER"/"UNDER" for TOT.

**Edge case — only one side observed:** if PIN snapshot exists for only one side of a market, we use the available side's direction directly: if it got more favored (negative diff) we fire with that side; if it got less favored we'd be naming the wrong team and don't have the right team's prices to print, so we **skip** the alert/chip rather than label the wrong side. (Old behaviour was an `Infinity` fallback that always picked the available side regardless of direction — that bug is gone.)

The `_splitsSubScore` and `_divergenceSubScore` helpers are kept in the file (Phase 4 Sharp Bot will use them for paper-bet selection logic, where weighted blending across signals makes sense). They just don't feed the on-card display number.

### UI tiers (CSS color-coded chips)

- **0-3** — `tier-low` (gray, muted)
- **4-6** — `tier-mid` (orange)
- **7-9** — `tier-strong` (green)
- **10**  — `tier-elite` (gold gradient)

### Telegram alerts (Phase 3 — live)

`kahla-scanner/scripts/sharp_alerts.py` runs immediately after each ingest cycle (appended step in `.github/workflows/scanner-poll.yml` — same 30-min cadence, no second cron registration). Sends two kinds of messages to Telegram:

- **🚨 STEAM** — for each book on each (market_type, raw_side), computes the implied sharp side from THAT book's move via `_move_sharp_side()` (line direction first for SPR/TOT, vig fallback). Groups books by `(market_type, sharp_side)`; fires when ≥`STEAM_BOOK_COUNT` (5) books point at the same sharp side. Single book counted once per market regardless of which raw side reported the move. Indicates institutional-flow synchronization.
- **⚡ SHARP N** — fires when any (market, market_type) crosses Sharp Score ≥`SHARP_THRESHOLD` (7). Score formula mirrors the on-card chip in `templates/odds.html` exactly so the Telegram alert matches what the user sees: `_amer_to_cents()` + `_move_score_ml()` + `_move_score_spr_tot()` are Python ports of the JS helpers.

Pre-game only: `ACTIVE_WINDOW` runs from `now − LIVE_BUFFER_MIN (5min)` to `now + ACTIVE_WINDOW_HOURS (24h)`. Alerts on already-live games would be useless — line is no longer pre-game and you can't act on it. Time formatting: `_fmt_local()` formats to America/Denver with day+date prefix (`Sun Apr 26 · 5:00 PM MT`) so a Saturday-night alert about Sunday's game can't be mistaken for in-progress one.

STEAM message renders the SHARP side's prices (not the raw_side that triggered detection) so an alert that says "sharp HOUSTON ROCKETS" lists Houston prices, not Lakers prices. SPR/TOT samples include the line value (`+7.0 -112 → +6.5 -119`), ML is price-only.

Dedupe via the `sharp_alerts` Supabase table — duplicate (market_id, market_type, alert_type, side) within `DEDUPE_HOURS` (6) is suppressed. Required schema:

```sql
CREATE TABLE IF NOT EXISTS sharp_alerts (
  id          BIGSERIAL PRIMARY KEY,
  market_id   UUID NOT NULL,
  market_type TEXT NOT NULL,
  alert_type  TEXT NOT NULL,           -- 'steam' or 'sharp7'
  side        TEXT,                     -- home/away/over/under
  sent_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  payload     JSONB
);
CREATE INDEX IF NOT EXISTS idx_sharp_alerts_dedup
  ON sharp_alerts (market_id, market_type, alert_type, side, sent_at DESC);
```

Setup: BotFather → `/newbot` → token; message bot anything; visit `https://api.telegram.org/bot<TOKEN>/getUpdates` → grab `chat.id`. Add as GitHub secrets `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`. Alert step skips silently when either is missing, so the workflow doesn't break if you tear down the bot.

## Phase 4 — Sharp Bot (paper bets)

Three independent paper-bet bots writing to a single Supabase table (`paper_bets`). Each bot represents a distinct thesis about when sharp signals are actionable; logging them separately is the only way to know which strategy actually wins money.

All three bots ride the existing `scanner-poll.yml` 30-min cron — no extra workflows or cron-job.org entries. Per-bot dedup (`(market_id, bot)` × 7-day lookback) means a game gets picked the first time it qualifies in each bot's window and is skipped on later cycles.

| Bot | Trigger / window | Cap | Where |
|---|---|---|---|
| **steam** | A Telegram STEAM alert fires (5+ books moved same direction in last ~30 min) | uncapped (steam is rare; ~0-3/day in practice) | Logged from `scripts/sharp_alerts.py` after a successful Telegram send + dedup pass. |
| **early** | Cumulative PIN movement on games starting in 12–18h | top 5 per run | Appended step in `scanner-poll.yml`. |
| **late** | Cumulative PIN movement on games starting in 0–2h | top 5 per run | Appended step in `scanner-poll.yml`. |

### Stage 1 — live (this commit)

Schema (`kahla-scanner/supabase/paper_bets.sql`): one row per logged bet with `bot ∈ {steam,early,late}`, locked entry book/price/line, signal context (`fair_prob`, `edge_pp`, `sharp_score`, `signal_blob`), and resolution fields (`status`, `pnl_units`, `result_score`, `settled_at`) populated later by Stage 2.

**Picker selection logic for early/late** (`scripts/paper_bets_picker.py`):
1. Fetch markets where `event_start` is inside the bot's window.
2. Determine sharp side per market_type via `_lib/sharp.py` — same logic as the on-card chip + Telegram alerts.
3. Filter to markets where `sharp_score ≥ 4`.
4. Devig PIN's two-way market for that side → `fair_prob`. Skip if either PIN side is missing or (for SPR/TOT) the home/away or over/under lines don't match.
5. Find the best non-PIN entry price for that side. **Line gate for SPR/TOT**: entry book must be quoting at PIN's current line, otherwise the devigged fair doesn't apply. ML has no line so any non-PIN book qualifies.
6. `edge_pp = (fair_prob − implied_at_entry) × 100`. Filter `edge_pp ≥ 1.0pp`.
7. `combined_score = 0.6 × sharp_score/10 + 0.4 × min(edge_pp/10, 1.0)`. Edge component capped at 10pp so a freak data error doesn't drown out genuine sharp signals.
8. Sort candidates desc by `combined_score`, dedup by `market_id` (one bet per game per bot), skip any market already picked by this bot in the last 7 days, insert top 5.

**Steam paper bet logic** (`_log_steam_paper_bet` in `sharp_alerts.py`):
1. Triggered after a successful Telegram steam send (so dedup gating is the existing `sharp_alerts` table — same 6h window).
2. Entry = best price on the sharp side among the steaming books that are in the entry-book allowlist (`pb.ENTRY_BOOKS` = 14-book allowlist minus PIN). Entry line for SPR/TOT comes from that book's snapshot, NOT PIN's.
3. `fair_prob` / `edge_pp` are computed when PIN devig is possible, otherwise null. Steam can fire without clean PIN data — we still want hit-rate tracking even without pre-bet edge.
4. `sharp_score` is null for steam (the trigger is the burst event, not cumulative movement magnitude).
5. Per-`(market_id, bot=steam)` dedup via `pb.already_picked()` — 7-day lookback. Avoids double-logging if a steam re-fires after the 6h alert dedup window expires but the game hasn't started yet.

**Constants in `_lib/paper_bets.py`** (tune as we get hit-rate data):
- `SHARP_SCORE_MIN = 4`
- `EDGE_PP_MIN = 1.0`
- `SHARP_WEIGHT = 0.6`, `EDGE_WEIGHT = 0.4`
- `MAX_PICKS_PER_RUN = 5`
- `ENTRY_BOOKS = {DK, FD, MGM, CAE, HR, BET365, BR, BOL, LV, BVD, ESPN, FAN, MB}` — same 14-book allowlist as `_ALLOWED_BOOKS` minus PIN.

### Latent bug fixed in this stage

`scripts/sharp_alerts.py` previously wrote `_record_alert(... payload={"books": ..., "direction": ..., "raw_side": ...})` after a successful steam send, but `_detect_steam` never put `direction` or `raw_side` keys into its alert dict — so any real steam fire would `KeyError` before `_record_alert` was called, leaving the dedup row unwritten and the next cycle re-firing the same alert. Now the payload is `{"books", "samples"}` (keys that actually exist in `_detect_steam` output).

### Stage 2 — resolver (live)

`scripts/paper_bets_resolver.py`, appended step in `scanner-poll.yml` (every 30 min). For each `paper_bets` row with `status='pending'` and `event_start < now - 4h`:
1. Look up ESPN scoreboard for the bet's sport on the bet's US/Eastern date (per-run in-memory cache so 5-15 bets on the same night = 1 ESPN call).
2. Match by lowercase team-name substring (two-way) + commence_time within ±90 min — same logic as Flask's `_merge_espn_scores`.
3. Skip if `state != 'post'` (game still in-progress / postponed — try again next cycle).
4. Grade:
   - **ML**: side wins iff their score > opponent's. Tie → push.
   - **SPR**: `(side_score − opp_score) + entry_line` > 0 = won, < 0 = lost, == 0 = push.
   - **TOT**: total vs `entry_line` (over wins on >, under on <, push on ==).
5. `pnl_units` = flat 1u sizing: win @ +N → `+N/100`, win @ −N → `+100/N`, loss → `−1.0`, push/void → `0`.
6. Update `status`, `pnl_units`, `result_score` (`{home, away, total}`), `settled_at`.

UFC bets stay pending — ESPN has no consolidated MMA scoreboard endpoint. Manual resolution for now (low volume). Postponed games (`PPD` / `state` stuck at `pre`/`in` past expected end) also stay pending until ESPN's state flips to `post`.

### Stage 3 — admin UI (after Stage 2)

`/sharp-bot` admin-only page + `/api/sharp-bot/*` routes. Three columns (Steam / Early / Late), each showing today's picks, last 30 days' P&L, hit rate, ROI. Per-bot stat cards roll up across all sports.

### Stage 4 — self-tuning (deferred until ~14d of resolved data)

Rolling 30-day per-signal hit-rate fed back into `combined_score` weights. `_splitsSubScore` and `_divergenceSubScore` (currently dormant in `templates/odds.html`) come into play here — the picker can blend additional signals once we have outcome data to grade their contribution. Also: CLV closed/settled history rollup (Phase 4 per the CLV section above).

## Action Network — Public Betting Splits

Free public-betting source replacing Circa splits (which we lost when Owls was retired and Circa turned out to not exist in The Odds API at any region). Powers the % bets / % money bar under each game card on `/odds` and the optional `SHARP +N%` tag when money diverges from bets.

### Data sources (in fallback order)

`_fetch_action_splits(sport)` in `app.py` tries three paths and uses the first that returns events:

1. **JSON API (primary)** — `_fetch_action_api()`:
   ```
   GET https://api.actionnetwork.com/web/v2/scoreboard/{league}?period=game&date=YYYYMMDD
   ```
   Headers: `User-Agent` (browser-like), `Origin: https://www.actionnetwork.com`, `Referer: https://www.actionnetwork.com/`. No auth — Cloudflare/WAF passes the request through with a real-looking UA + referer.

   This is the same endpoint Action Network's own browser UI calls via XHR after page hydration, so it's the most complete and current data path. Returns today's scheduled games + live games + completed games for the date, all with their public betting %s. Sport keys map via `_ACTION_API_LEAGUE` (mlb/nba/nhl/nfl/ncaab/ncaaf — same path codes as our internal sport keys).

2. **`__NEXT_DATA__` JSON in the SSR HTML page** — `_parse_action_splits_next_data()`:
   `<script id="__NEXT_DATA__" type="application/json">…</script>` — Next.js apps embed their full hydration tree here. Walk it heuristically looking for game-shaped objects (`home_team_id` + `away_team_id` + `start_time`), then per-game subtree-walk for `*_percent` keys matching bet/ticket/money/handle × away/home.
   - **Caveat learned the hard way**: Action Network's `__NEXT_DATA__` for the public-betting page does NOT carry split percentages on the game object — only odds, scores, and per-book market prices. Today's scheduled games are also frequently missing from `__NEXT_DATA__` (rendered client-side from the JSON API). So this path basically only works as a backup for cached completed-game data; the JSON API is the real answer.

3. **HTML table parser (legacy fallback)** — `_parse_action_splits_html()`:
   BeautifulSoup over `<table>` rows with cell layout: `[status+teams, open odds, current odds, % bets, % money, money-vs-bets diff, ticket count]`. Status prefix (`Final`, `Final - OT`, `PPD`, `1ST 18:42`, `7:05 PM`, etc.) gets stripped before the team-name regex via `status_prefix_re`. Team regex allows 1-4 digit game IDs (NHL uses 1-2 digit: `CAR 7`, MLB uses 3: `SEA 925`).
   - Only useful for yesterday's finals — Action Network's SSR table doesn't include today's scheduled games regardless of `?date=` URL param.

### URL we hit
- API: `https://api.actionnetwork.com/web/v2/scoreboard/{league}?period=game&date=YYYYMMDD` (today in US/Eastern, via `zoneinfo.ZoneInfo("America/New_York")`)
- HTML page (only for `__NEXT_DATA__` + table fallback): `https://www.actionnetwork.com/{sport}/public-betting?date=YYYYMMDD`

### Caching
- Server-side `_cache` dict (same one used for ESPN cache). Key: `splits:{sport}`. TTL: **30 min**.
- **Successful parses cache, failures don't** — so if the JSON API rejects us or our walker misses everything, the next user hit retries fresh instead of being pinned to a broken response for half an hour.

### Diagnostics — `/debug-splits?sport=X`
Browser-friendly view of `/api/splits` that shows:
- `source`: which path won (`json_api` / `next_data` / `table`)
- `events`: parsed event list with `away_team`, `home_team`, `ml: {away_bets, home_bets, away_money, home_money}`, `sharp_diff`, `status`
- `failed_samples`: up to 5 raw cell strings the table parser couldn't match (helps spot new status patterns)
- `next_debug`: `__NEXT_DATA__` walker diagnostics — `candidate_count`, `sample_top_keys`, `splits_paths_seen`, and `candidate_shape` (deep field-name dump of the first game when extraction fails — types/keys, no raw values)
- `api_debug`: JSON API diagnostics — `url`, `status`, `top_keys`, `game_count`, `events_extracted`, `splits_paths_seen`, `game_shape` (when 0 events extracted from games)

The `*_shape` dumps are how we iterate on Action Network's frequently-changing JSON shapes without fetching the URL ourselves from a sandbox that blocks external network. Whenever the splits row stops rendering for a sport, hit `/debug-splits?sport=X` first.

### Frontend wiring (`odds.html`)
- `fetchSplitsBatch()` calls `/api/splits?sport={activeSport}`, stashes into `_splitsData`, then runs `drawSplitsRows()`.
- `_matchSplitsEvent(splitsEvents, ourAway, ourHome)` matches by team-name **substring containment in either direction** — Action Network uses short names ("Mariners"), we have full names ("Seattle Mariners"), so `seattle_mariners.includes(mariners) || mariners.includes(seattle_mariners)` resolves both. Lowercased before comparing.
- `drawSplitsRows()` runs after each `renderBoard()` swap (inside the same `requestAnimationFrame` as `drawSparklines()`). For each `.js-splits` placeholder div, finds the matching event and either populates with `% bets` / `% money` bars + `.has-data` class (which un-hides via CSS) or leaves it empty.
- Polling: re-fetched on every `scheduleNextLoad()` tick (30s/90s). Cheap because of the server-side 30-min cache — most ticks are no-ops.

### Sport coverage
- Supported: MLB, NBA, NHL, NFL, NCAAB, NCAAF (= `_ACTION_SPORTS`)
- NOT supported: MMA, soccer, tennis (Action Network doesn't have public-betting pages for these). Splits row just stays hidden for those sports.

## ESPN Scoreboard

Used for live game scores on the Odds Board. Free, public, no auth.

`GET https://site.api.espn.com/apis/site/v2/sports/{sport_group}/{league}/scoreboard`

Sport group / league mapping in `app.py:_ESPN_PATH`:
| Sport (Flask path) | sport_group | league |
|---|---|---|
| mlb   | baseball       | mlb |
| nba   | basketball     | nba |
| nhl   | hockey         | nhl |
| nfl   | football       | nfl |
| ncaab | basketball     | mens-college-basketball |
| ncaaf | football       | college-football |

MMA intentionally not mapped — ESPN doesn't have a single consolidated MMA scoreboard endpoint.

Server-cached 30s in `_ESPN_CACHE`. `_merge_espn_scores` matches each Odds API event to an ESPN game by lowercase team-name substring + commence_time within ±90 min, then attaches a `score` object to the event. Failures are silenced — board renders without scores rather than 500s.

---

## Firestore Structure

- **`users/{uid}`** — User profile: `email`, `displayName`, `role` (`admin` / `viewer` / `pending`), `approved` (bool), `preferences`, `createdAt`. Access determined entirely by `role`.
- **`openers/openers:{sport}`** — Legacy opening lines per sport. `events` map of game IDs to opener data. Fallback only — scanner-backed openers from Supabase win.
- **Preferences fields**: `odds_books`, `odds_book_order`, `odds_sport`
- _Stale: `openers/splits:{sport}` and `openers/splits_changed:{sport}` were used by the retired splits feature — safe to delete the docs in Firestore manually if you want, or leave them; nothing reads them anymore._

`firestore.rules` exposes two helpers: `isApproved()` (any approved role) and `isAdmin()` (admin + approved). The `openers` collection is gated by `isApproved()`. The `users` collection allows self-create (signup), self-read, and admin read/update/delete.

## Firebase Auth
- Client-side SDK in every template (compat mode)
- `onAuthStateChanged` → probe `/api/me` → bounce unauthorized → init app
- `authFetch()` — wrapper that adds Bearer token to every API call
- Backend: `@firebase_auth_required` validates tokens, sets `g.uid` and `g.user_data`, rejects users where `approved != true`
- `@admin_required` — additionally checks `g.user_data.role == 'admin'`
- First signup on an empty users collection auto-promotes to admin (bootstrap)
- All other signups stay `pending` until an admin clicks **Approve as Viewer** or **Approve as Admin** in the User Management panel on `/`

## Mobile Layout
- `overflow-x: hidden` on html, body, `#app` (iOS Safari fix)
- Top bar: nav links (Home, Odds, Dashboard) on first row, status + logout on second row. Dashboard link only renders for admins.
- Movement bar items wrap with `flex-wrap` so ML/SPR/TOT all show
- Odds table scrolls horizontally
- Game card fadeUp animation only on first load

## Deployment
- **Every commit goes to `main`**. Vercel auto-deploys to thekahlahouse.com on push to `main`. Don't leave changes on a feature branch.
- If you're handed a feature branch (e.g. `claude/...`), finish the work, merge into `main`, push `main`. Don't wait to be told.
- GitHub repo: `Diavel78/kahla-house`
- Vercel project: `kahla-house` (team: `diavel78s-projects`)
- Domain: `thekahlahouse.com` + `www.thekahlahouse.com`

## Known Issues & Gotchas
1. **The Odds API auth is `?api_key=` query param** — NOT a Bearer header. Easy to copy from one provider's pattern (Owls used Bearer) and break.
2. **The Odds API credit cost = `markets × regions`** per `/odds` call. We use `h2h,spreads,totals` × `us,eu` = 6 credits. Don't add markets/regions casually — costs scale linearly. Adding `us2` (ESPN BET, Fanatics) would bump to 9 credits/call.
3. **Pinnacle is in the EU region**, NOT US. If you ever drop `eu` from the regions param, PIN data stops flowing — and PIN is the entire sharp angle for openers/movement.
4. **Cron is cron-job.org ONLY** — the GitHub-native `*/30 * * * *` schedule on `scanner-poll.yml` was killed because it double-fired with cron-job.org and burned 2x credits via the concurrency queue. If cron-job.org dies, the "last odds update Nm ago" indicator on `/odds` will surface it within minutes; manually trigger from the Actions tab as recovery.
5. **`cancel-in-progress: true`** on the scanner-poll concurrency group — any retry/manual-overlap kills the in-flight run instead of queueing. Each run is idempotent (dedup logic) so partial runs lose nothing.
6. **`_cache` (Polymarket dashboard cache)** resets on Vercel cold start. Used by `api_my_bets` and `api_data` only. Odds/openers/snapshots safe in Supabase + Firestore.
7. **SDK `price` field is the COMPLEMENT** — NEVER use for P&L *or for displaying order limit prices*. For positions/trades use `cost.value / qty` for real per-share price. For unfilled orders use `1 - price` (no cost field exists yet — they haven't filled). The `price` field returns the opposite side's price (YES when trading NO). Symptom: a +150 limit order shows up as -150ish in the betslip.
8. **SDK `realizedPnl` unreliable** — Only use non-null as sell indicator, not the value.
9. **SDK trade fields are nested objects** — `price`, `cost`, `realizedPnl`, `costBasis` are all `{currency, value}` dicts, not plain numbers. `_safe_float()` handles this by extracting `.value`.
10. **`book_snapshots` is deduplicated** — a new row is only written when a (market, book, market_type, side)'s price or line actually changes since the last stored value (`_latest_snapshot_map` + `_dedup_unchanged` in `kahla-scanner/scrapers/odds_api.py`). Retail books (MGM, CAE) re-price often; sharp books (PIN) post a line and sit — their last row can be hours old. The Flask `/api/odds`, `/api/odds/history`, AND `/api/odds/history-batch` all use anchor queries (latest pre-window row per book) so stale-but-current sharp lines still render.
11. **Live-game freeze applies to** the board cells, the inline sparklines, AND `/api/openers/scanner` — same `_post_start` filter pattern. The click-through chart modal (`/api/odds/history`) deliberately does NOT freeze, so users can see post-start movement there.
12. **Markets table never marks rows `closed`** — the Flask query filters by `event_start` window so stale markets don't render, but the table grows unboundedly. Low-priority cleanup; would need a small extension to the snapshot-cleanup workflow.
13. **`book_snapshots` retention is 15 days** — `.github/workflows/snapshot-cleanup.yml` deletes older rows nightly. Chart "All" range is bounded by this.
14. **Splits scraper is undocumented territory** — Action Network's JSON shape changes between builds (snake_case ↔ camelCase, fields move, things rename). Whenever the splits row stops rendering, hit `/debug-splits?sport=X` and inspect `next_debug.candidate_shape` / `api_debug.game_shape` — they dump field names to make tuning the extractor a one-round-trip iteration. Don't try to debug via curl — the Vercel runtime CAN reach Action Network from US east edge nodes, but local curls + browsers from random IPs often get 403'd by Cloudflare.
15. **Action Network team names are short** ("Mariners", "Red Sox") where ours from The Odds API are full ("Seattle Mariners", "Boston Red Sox"). The frontend matches with **two-way substring containment** in `_matchSplitsEvent()` (`a.includes(b) || b.includes(a)`). Don't switch to exact match — it'll silently break splits across all sports.
16. **Splits status prefix regex** in `_parse_action_splits_html()` is the brittle part of the legacy table parser. New live-game status strings from Action Network's UI ("END 2ND PER", "INT 1", a different separator like "Final/2OT") will land in `failed_samples` if not covered. Add new patterns to `status_prefix_re`. Smoke-test with the inline test in commit `3ef01aa`'s message before pushing.
17. **NHL game IDs are 1-2 digits** (`CAR 7`, `OTT 8`) where MLB uses 3-digit (`SEA 925`). `team_re` in the legacy table parser uses `\d{1,4}` to handle both. Do NOT tighten this back to `\d{3}`; NHL will silently break.
18. **Action Network `?date=YYYYMMDD` URL param doesn't actually bust their SSR cache** — bare URL and dated URL return identical SSR HTML for several hours into the day. We pass it anyway (cheap, helps cache key separation), but the JSON API is the only path that respects the date param.
19. **Sharp-side rule across ALL markets: side whose bet got HARDER = sharp.** Books move odds to balance action; the side they made worse to bet is where money is flowing. ML = side whose American odds got more negative. SPR = side whose line moved against them (line is primary; vig drift after a line move is rebalance noise). TOT = total raised → harder for over → sharp OVER; total lowered → harder for under → sharp UNDER. Don't try to be clever with composite/symmetric formulas — the rule is asymmetric (raising a TOT makes both sides "move +1 direction" by old composite logic but only OVER is sharp), and clever formulas have repeatedly missed this.
20. **Sharp Score is line OR vig, NEVER additive.** For SPR/TOT: if the line moved, score = `|point_diff| × 10` and vig drift is ignored (rebalance). If the line stayed flat, score = `|price_diff_cents|`. Adding them double-counts when books re-juice a new line.
21. **One-sided PIN snapshots: skip rather than guess.** When only one side of a market has a PIN snapshot in `book_snapshots`, use that side's direction directly: if it got more favored (negative diff), sharp = that side and we fire. If it got less favored, the actually-sharp side is the OTHER one but we don't have its prices to render — bail. Old `Infinity`-fallback heuristic always picked the available side regardless of direction; that bug is gone in both `_sharpSide()` (JS chip) and `_sharp_for_ml/spread/total()` (Python alert).
22. **GitHub secrets often have trailing whitespace from copy-paste.** A trailing newline in `TELEGRAM_BOT_TOKEN` blew up `urllib` with `InvalidURL: URL can't contain control characters`. `sharp_alerts.py` now `.strip()`s both Telegram env vars at read time. If you add new secret-driven scripts, do the same defensively.
23. **Polymarket `intent` flips price meaning on orders.** For `BUY_LONG`/`SELL_LONG` (buying/selling YES), the SDK `price` field is what the user pays/receives directly. For `BUY_SHORT`/`SELL_SHORT` (NO side), the SDK reports the YES-canonical price; real per-share price = `1 − price`. `/api/my-orders` flips only on `*_SHORT` intents — verified empirically against the Polymarket app.
