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
| `/api/odds`, `/api/openers*`, `/api/preferences` | any approved | `@firebase_auth_required` (rejects pending) |
| `/dashboard` (page) | admin | client probes `/api/me` and bounces non-admins |
| `/api/data`, `/api/my-bets`, `/api/debug-trades`, `/api/debug-deposits` | admin | `@admin_required` |
| `/api/raw` (Polymarket SDK debug) | admin | `@admin_required` |

`@firebase_auth_required` itself rejects any user where `approved != true` (returns 403), so even API endpoints that don't need admin still keep `pending` users out.

## Pages & Routes

| Route | Template | Access | Purpose |
|---|---|---|---|
| `/` | `index.html` | public | Landing page (login/signup, pending screen, admin panel, app cards by role) |
| `/odds` | `odds.html` | admin + viewer | Odds Board — multi-book odds comparison, opener-vs-current movement, per-game line-movement chart modal |
| `/dashboard` | `dashboard.html` | admin only | Polymarket P&L Dashboard — positions, closed trades, bet slip |

> **Odds-ingest cron (`kahla-scanner/`)**: minimal Python subproject at
> `kahla-scanner/` runs `python -m scrapers.odds_api` every 30 min via GitHub
> Actions (`.github/workflows/scanner-poll.yml`), driven by an external
> cron-job.org trigger with a 30-min GitHub-native fallback. It hits The
> Odds API (https://the-odds-api.com) for `/v4/sports/{sport_key}/odds`
> with `markets=h2h,spreads,totals` and `regions=us,eu` (EU required for
> Pinnacle), writes deduped rows to Supabase `book_snapshots` for every
> (market, book, market_type, side). Powers BOTH the live Odds Board AND
> the line-movement chart modal — Flask reads from Supabase, no live
> odds-vendor API calls from `/api/odds`.
>
> Cost: 6 credits/call × 7 sports × 2 calls/hr × 24h × 30d = 60K credits/mo
> on the $59/100K-credit tier.
>
> Owls Insight was the prior provider; retired April 2026 due to coverage
> gaps. Brier/signals/Telegram pipeline was retired earlier in the same
> spring cleanup. Player Props, live scores, and Circa betting splits
> were removed at the same time as Owls — none of them came in The Odds
> API equivalent and the user opted to drop the features rather than pay
> a second provider. **Circa is not available in The Odds API at all** —
> known data gap; Circa was a unique Owls feature.

### API Routes

`Firebase` = `@firebase_auth_required` (any approved user). `Admin` = `@admin_required` (must also be role=admin).

| Route | Auth | Purpose |
|---|---|---|
| `GET /api/me` | Firebase | Lightweight role probe — returns `{uid, role, approved, displayName, email}`. Used by every sub-page to gate UI before loading data. |
| `GET /api/odds?sport=mlb` | Firebase | Odds Board JSON — built from latest `book_snapshots` per (market, book, market_type, side) in Supabase. Cron-only; no live Odds API call here. Includes anchor sweep so books that haven't priced inside the freshness window still show their last value. |
| `GET /api/odds/history` | Firebase | Line-movement history for one event from Supabase `book_snapshots`. Params: `sport`, `home`, `away`, `commence` (ISO), `market` (ml/spread/total), `since` (15m/30m/1h/6h/12h/24h/all). Returns step-function-ready data per book per side. Books: PIN/DK/FD/MGM/CAE/HR/BOL. Chart modal defaults to PIN only at 12H. |
| `GET/POST /api/openers?sport=mlb` | Firebase | Legacy Firestore openers (fallback for games predating the cron). Permanent per game ID. |
| `GET /api/openers/scanner?sport=mlb` | Firebase | **Primary opener source.** Earliest PIN snapshot per (market_type, side) from Supabase `book_snapshots`. Client matches against current events by team + commence_time within ±30 min and merges over Firestore openers. (Circa was the historical fallback but isn't in The Odds API — PIN-only now.) |
| `GET/POST /api/preferences` | Firebase | User settings (books, sport, order) in Firestore |
| `GET /api/my-bets` | **Admin** | Active Polymarket positions (Dashboard only) |
| `GET /api/data` | **Admin** | Dashboard P&L data (positions, balances, trades) |
| `GET /api/raw` | Admin | Debug: raw Polymarket SDK responses |
| `GET /api/debug-trades` | **Admin** | Debug: grouped trade details with before/after position data |
| `GET /api/debug-deposits` | **Admin** | Debug: all balance changes with types and reasons |
| `/debug?slug=X` | Firebase (page) | Debug page that calls debug-trades with auth |
| `/debug-deposits` | Firebase (page) | Debug page showing all balance changes |

## Tech Stack

- **Backend**: Flask (Python), single file `app.py`, Vercel serverless
- **Frontend**: Vanilla JS, embedded CSS in each HTML template (no framework)
- **Auth**: Firebase Auth (client SDK) + `firebase_auth_required` decorator (server validates tokens)
- **Databases**:
  - **Firestore** — user prefs, openers (legacy), user management
  - **Supabase** (Postgres) — `markets` + `book_snapshots`. Sole source of truth for the Odds Board AND the line-movement chart. Written by the kahla-scanner cron, read by Flask.
- **External APIs**:
  - **The Odds API** (`https://api.the-odds-api.com/v4`) — every 15 min via cron
  - **Polymarket US SDK** — Dashboard positions/P&L
- **Fonts**: DM Sans + JetBrains Mono
- **Deployment**: Vercel via `vercel.json`, env vars in Vercel dashboard, auto-deploys from `main`

## Key Files

- `app.py` — All backend logic (~1700 lines)
- `templates/odds.html` — Odds board (~2230 lines)
- `templates/dashboard.html` — P&L dashboard (~1130 lines)
- `templates/index.html` — Landing page with auth + admin + role-based app cards (~440 lines)
- `kahla-scanner/scrapers/odds_api.py` — The Odds API ingester (cron entry point)
- `kahla-scanner/scripts/cleanup_snapshots.py` — nightly book_snapshots > 15d delete
- `kahla-scanner/storage/{models,supabase_client}.py` — slim Supabase wrapper
- `kahla-scanner/_lib/{matcher,normalize}.py` — team-name fuzzy match + odds math
- `firestore.rules` — Firestore security rules (admin/approved helpers)
- `vercel.json` — Vercel deployment config
- `requirements.txt` — Python deps (flask, polymarket-us, requests, python-dotenv, firebase-admin, supabase)
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

---

## Odds Board (`/odds`)

### Features
- **Best Odds Column** (left, always visible): Best ML, Spread, Total across all enabled books
- **Multi-Book Columns** (scrollable right): Individual book odds side by side
- **Sport Tabs**: MLB, NBA, NHL, NFL, NCAAB, NCAAF, MMA. Soccer + Tennis tabs are still in the UI but the cron doesn't ingest them — those tabs will read empty.
- **Search**: Filter by team name (client-side, instant)
- **Book Selector**: Dropdown with checkboxes + up/down arrows to reorder. Saved to Firestore.
- **Line Movement Bar**: Opener vs current with arrows and diffs (per game footer)
- **Line-Movement Chart**: Each game header has a small graph icon. Click → modal with a step-function chart (Chart.js) of historical odds from Supabase `book_snapshots`. Defaults to PIN/CIR/DK on the ML market over the last 24h; toggle other books (FD/MGM/CAE/HR) and switch market (ML/Spread/Total) or range (15m/30m/1H/6H/12H/24H/All).
- **Genuine first-seen openers**: `/api/openers/scanner` returns the actual earliest PIN/CIR snapshot per market from Supabase. The client merges this on top of legacy Firestore openers — scanner data wins, Firestore fills any gaps for games predating the cron.
- **Live-game freeze**: once an event's `commence_time` passes, the board displays the closing line (last pre-start snapshot per book) and stops showing post-start retail twitches. Live games render a green `LIVE` badge in the header and a `closing line` tag next to the teams. The chart modal still shows full pre-and-post-start history if you want to see live movement.
- **Auto-refresh**: 90 seconds (the page reads from cached Supabase data; the cron writes every 30 min, so polling faster is just rerendering the same numbers)
- **Double-buffer rendering**: Two board divs swap to prevent flash on re-render

### Removed (when Owls retired in spring 2026)
- **Live scores** — The Odds API `/scores` is a separate per-credit endpoint. If wanted back, hook free ESPN scoreboard JSON instead.
- **Circa betting splits + SHARP/RLM detection** — Circa splits aren't in The Odds API. Sports Insights at $35/mo was an option but Rob opted to drop the feature.
- **Player Props page** — `/props` route, `templates/props.html`, all `/api/props*` endpoints, all props JS. Props in The Odds API are per-event (more credits per call) and Rob isn't using props enough to justify the budget.
- **User-side `setInterval(loadOdds, 15000)`** — replaced with 90s refresh since cron is the source of truth.

### Key JS Functions (odds.html)
- `loadOdds()` — fetches `/api/odds`, calls `captureOpeners()`, then `mergeScannerOpenersInto()`, then `renderBoard()`
- `captureOpeners()` — legacy Firestore opener capture from current PIN/CIR data (now mostly dormant — scanner-backed openers usually have everything)
- `loadScannerOpeners()` / `mergeScannerOpenersInto()` — pulls scanner-backed openers via `/api/openers/scanner` and merges them over `currentOpeners`. Scanner values win.
- `computeMovement()` — compares opener to current, includes JIT backfill
- `renderMovement()` — renders opener → arrow → current for ML/SPR/TOT
- `renderBoard()` — main render, double-buffered. Exposed to `window` for search.
- Chart modal: see the IIFE block at the bottom of `odds.html`. Uses Chart.js v4 + date-fns adapter via CDN.

> **Note**: dead splits-related JS functions (`renderSplitsRow`, `captureSplitsOpeners`,
> `loadSplitsOpeners`, `saveSplitsOpenersAPI`, `buildSplitsSnapshot`,
> `syncSplitsLastChanged`, `loadSplitsLastChanged`, `fmtTsAgo`, `detectRLM`)
> are still defined in odds.html but never called. Safe to delete in a future cleanup pass.

---

## Dashboard (`/dashboard`)

### Features
- **Stats cards**: Balance, Open Positions, Portfolio Value, Today's P&L, Yesterday's P&L, Maker Rewards, Total P&L, Win Rate
- **Open Positions table**: Market, Pick, Qty, Entry, Current, P&L, Return %
- **Closed Positions tab**: Resolved bets + sold trades + maker rewards with Result (W/L/Sold/Maker) and P&L
- **Maker Rewards**: `ACTIVITY_TYPE_TRANSFER` = maker rewards (income, counted in P&L). `ACTIVITY_TYPE_ACCOUNT_DEPOSIT` = user deposits (NOT P&L). `ACTIVITY_TYPE_ACCOUNT_WITHDRAWAL` = withdrawals (NOT P&L). Maker rewards show as a separate stat card and appear in closed positions with "Maker" badge.
- **Bet Slip modal**: Shareable sportsbook-ticket format
- **Auto-refresh**: 60 seconds

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

- **Movement / Historical Line Data**: The per-game footer movement bar. Records the FIRST line seen (opener) and renders it next to the current line with an arrow + diff. Two opener sources (merged on the client):
  - **Primary**: scanner-backed openers from `/api/openers/scanner` — earliest PIN/CIR snapshot per (market, side) in Supabase `book_snapshots`. This IS the genuine first-seen line.
  - **Fallback**: legacy Firestore openers in the `openers/openers:{sport}` doc — captured client-side for games predating the cron's history.

### Movement Rules
- **Book priority**: Pinnacle first, then Circa. ONLY two sharp books for openers
- **Opener lock-in**: Once captured for a game ID, PERMANENTLY locked. Never overridden, never reset daily

### Key Terminology
- **ML** = Moneyline (NOT Machine Learning)
- **SPR** = Spread
- **TOT** = Total (Over/Under)
- **PIN** = Pinnacle, **CIR** = Circa, **DK** = DraftKings, **FD** = FanDuel, **MGM** = BetMGM, **CAE** = Caesars, **HR** = HardRock

---

## The Odds API (`https://api.the-odds-api.com/v4`)

**Auth**: `?api_key=...` query param (NOT a Bearer header — common gotcha)
**Plan**: $59/mo, 100K credits/mo, "All sports / All bookmakers / All markets"
**Cost formula**: each call to `/odds` costs `markets × regions` credits. We send `markets=h2h,spreads,totals` (3) and `regions=us,eu` (2) → **6 credits per call**. With 7 sports × 2 calls/hr × 24h × 30d = 60,480 credits/mo, fits in the 100K budget.

> **Region gotcha**: Pinnacle is in the `eu` region, NOT `us`. Without `eu` in the regions param we'd get zero PIN data — defeating the whole sharp-tracking purpose. The doubled credit cost is why the cron runs every 30 min instead of every 15.

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

### Bookmaker Key Mapping
Odds API returns lowercase keys; we normalize to short codes for Supabase. Mapping in `kahla-scanner/scrapers/odds_api.py:BOOK_CODES`. Unmapped keys pass through uppercased (no data loss).

### Rate-Limit Headers
- `x-requests-used` / `x-requests-remaining` — logged on every cron run so credit burn is visible in workflow logs.

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
1. **The Odds API auth is `?api_key=` query param** — NOT a Bearer header. Easy to copy from one provider's pattern and break.
2. **The Odds API credit cost = `markets × regions`** per `/odds` call. We use `h2h,spreads,totals` × `us` = 3 credits. Don't add markets/regions casually — costs scale linearly.
3. **Vercel cold starts** — `_owls_cache` (the in-memory dict, kept around as a generic Polymarket dashboard cache) resets. Odds/openers safe in Supabase + Firestore.
4. **SDK `price` field is the COMPLEMENT** — NEVER use for P&L. Always use `cost.value / qty` for real per-share price. The `price` field returns the opposite side's price (YES when trading NO).
5. **SDK `realizedPnl` unreliable** — Only use non-null as sell indicator, not the value.
6. **SDK trade fields are nested objects** — `price`, `cost`, `realizedPnl`, `costBasis` are all `{currency, value}` dicts, not plain numbers. `_safe_float()` handles this by extracting `.value`.
7. **`book_snapshots` is deduplicated** — a new row is only written when a (market, book, market_type, side)'s price or line actually changes since the last stored value (`_latest_snapshot_map` + `_dedup_unchanged` in `kahla-scanner/scrapers/odds_api.py`). Retail books (MGM, CAE) re-price often; sharp books (PIN, CIR) post a line and sit — their last row can be hours old. The Flask `/api/odds` and `/api/odds/history` both use anchor queries (latest pre-window row per book) so stale-but-current sharp lines still render.
8. **Cron timing** — GitHub-native cron drifts (5-15 min variance). Primary trigger is cron-job.org calling `workflow_dispatch` every 15 min. GitHub fallback is `*/15 * * * *`.
9. **Soccer + Tennis** — UI tabs exist on the Odds Board but the cron doesn't ingest them (not in `SPORTS_ENABLED`). Those tabs will read empty until added to the workflow.
