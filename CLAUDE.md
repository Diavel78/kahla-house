# The Kahla House — Bet System

Multi-page sports betting platform deployed at **thekahlahouse.com**. Flask backend on Vercel, Firebase Auth + Firestore, vanilla JS frontend. This is the ONLY active codebase for the bet system. The "Poly-Tracker" repo is deprecated and not used.

**CRITICAL: This project lives at `/Users/robkahla/Documents/Kahla House/kahla-house/`. The domain is thekahlahouse.com. The Vercel project is `kahla-house`.**

> **PUSH RULE**: Every commit goes to `main`. Vercel auto-deploys from `main`. If you're working on a feature branch, finish the work, then merge to `main` and push `main` — without being asked. Don't leave changes sitting on a branch waiting for permission.
>
> **DOC RULE**: Whenever code or behavior changes, update this CLAUDE.md in the same commit. The project is too sprawling to navigate without an accurate map.

## Access Control (read this first)

Three roles in Firestore `users/{uid}.role`:
- **`admin`** — full access (Odds, Props, Dashboard, debug). Rob.
- **`viewer`** — Odds + Props only. Friends use this tier.
- **`pending`** — default for new signups. No access until an admin approves.

Approval flow:
- Sign-up creates a `pending` user doc with `approved: false`. The pending screen tells them to wait.
- Admins see pending users in the User Management panel on `/` with **Approve as Viewer** / **Approve as Admin** / **Reject** buttons.
- The **first** signup on an empty users collection auto-promotes to admin so the platform can bootstrap.
- Admin role dropdown can move users between `admin` / `viewer` / `pending` at any time.

Per-page gating (client-side via `/api/me` probe + server-side via decorators):
| Page / API | Roles allowed | Server gate |
|---|---|---|
| `/odds`, `/props` (pages) | admin, viewer | client probes `/api/me` and bounces unauthorized |
| `/api/odds`, `/api/props`, `/api/openers`, `/api/preferences`, `/api/splits-*` | any approved | `@firebase_auth_required` (rejects pending) |
| `/dashboard` (page) | admin | client probes `/api/me` and bounces non-admins |
| `/api/data`, `/api/my-bets`, `/api/debug-trades`, `/api/debug-deposits` | admin | `@admin_required` |
| All `/api/*/raw`, `/api/raw`, `/api/odds/debug-markets` | admin | `@admin_required` |

`@firebase_auth_required` itself rejects any user where `approved != true` (returns 403), so even API endpoints that don't need admin still keep `pending` users out.

## Pages & Routes

| Route | Template | Access | Purpose |
|---|---|---|---|
| `/` | `index.html` | public | Landing page (login/signup, pending screen, admin panel, app cards by role) |
| `/odds` | `odds.html` | admin + viewer | Odds Board — multi-book odds comparison, splits, movement, RLM, live scores, per-game line-movement chart |
| `/props` | `props.html` | admin + viewer | Player Props — game-grouped best-line comparison across books |
| `/dashboard` | `dashboard.html` | admin only | Polymarket P&L Dashboard — positions, closed trades, bet slip |

> **Odds-ingest cron (`kahla-scanner/`)**: a stripped-down Python subproject
> at `kahla-scanner/` runs `python -m scrapers.owls` every 5 min via GitHub
> Actions (`.github/workflows/scanner-poll.yml`), driven by an external
> cron-job.org trigger with a 30-min GitHub-native fallback. It writes
> deduplicated rows to Supabase `book_snapshots` for every (market, book,
> market_type, side) — these power the line-movement chart on the Odds Board.
> The legacy divergence/Brier/signals/Telegram pipeline has been retired;
> only the Owls ingest remains.

### API Routes

`Firebase` = `@firebase_auth_required` (any approved user). `Admin` = `@admin_required` (must also be role=admin).

| Route | Auth | Purpose |
|---|---|---|
| `GET /api/me` | Firebase | Lightweight role probe — returns `{uid, role, approved, displayName, email}`. Used by every sub-page to gate UI before loading data. |
| `GET /api/odds?sport=mlb` | Firebase | Odds + splits + scores merged JSON |
| `GET /api/odds/history` | Firebase | Line-movement history for one event from Supabase `book_snapshots`. Params: `sport`, `home`, `away`, `commence` (ISO), `market` (ml/spread/total), `since` (15m/30m/1h/6h/12h/24h/all). Returns step-function-ready data per book per side. Books: PIN/CIR/DK/FD/MGM/CAE/HR/NVG (POLY excluded). Powers the chart modal. |
| `GET /api/props?sport=mlb` | Firebase | Player props grouped by game/player |
| `GET/POST /api/openers?sport=mlb` | Firebase | Legacy Firestore openers (kept as fallback for games predating the scanner cron). Permanent per game ID. |
| `GET /api/openers/scanner?sport=mlb` | Firebase | **Primary opener source.** Earliest PIN/CIR snapshot per (market_type, side) from Supabase `book_snapshots`. Returned as `[{home, away, commence, opener: {ml, spread, total, src}}]` — client matches against current Owls events by team + commence_time within ±30 min and merges over Firestore openers. |
| `GET/POST /api/preferences` | Firebase | User settings (books, sport, order) in Firestore |
| `GET/POST /api/splits-openers?sport=mlb` | Firebase | First-seen splits (Firestore, permanent per game ID) |
| `GET/POST /api/splits-last-changed?sport=mlb` | Firebase | Per-game ts of last actual Circa handle/bets % change. Server-authoritative diff |
| `GET /api/my-bets` | **Admin** | Active Polymarket positions (Dashboard only — no longer used by Odds Board) |
| `GET /api/data` | **Admin** | Dashboard P&L data (positions, balances, trades) |
| `GET /api/odds/raw` | Admin | Debug: raw Owls Insight odds response |
| `GET /api/splits/raw` | Admin | Debug: raw splits response |
| `GET /api/props/raw` | Admin | Debug: raw props response |
| `GET /api/scores/raw` | Admin | Debug: raw live scores response |
| `GET /api/realtime/raw` | Admin | Debug: raw Pinnacle sharp odds |
| `GET /api/raw` | Admin | Debug: raw Polymarket SDK responses |
| `GET /api/debug-trades` | **Admin** | Debug: grouped trade details with before/after position data |
| `GET /api/debug-deposits` | **Admin** | Debug: all balance changes with types and reasons |
| `GET /api/odds/debug-markets` | Firebase | Debug: market keys per book for a sport |
| `/debug?slug=X` | Firebase (page) | Debug page that calls debug-trades with auth (server-side admin gate on the API call) |
| `/debug-deposits` | Firebase (page) | Debug page showing all balance changes (server-side admin gate on the API call) |
| `/odds?debug=markets` | Firebase (page) | Overlay showing market keys per book |

## Tech Stack

- **Backend**: Flask (Python), single file `app.py`, Vercel serverless
- **Frontend**: Vanilla JS, embedded CSS in each HTML template (no framework)
- **Auth**: Firebase Auth (client SDK) + `firebase_auth_required` decorator (server validates tokens)
- **Database**: Firestore (user prefs, openers, user management)
- **APIs**: Owls Insight (odds, splits, scores, props), Polymarket US SDK (positions, P&L)
- **Fonts**: DM Sans + JetBrains Mono
- **Deployment**: Vercel via `vercel.json`, env vars in Vercel dashboard, auto-deploys from `main`

## Key Files

- `app.py` — All backend logic (~1900 lines)
- `templates/odds.html` — Odds board (~1770 lines)
- `templates/props.html` — Player props board (~940 lines)
- `templates/dashboard.html` — P&L dashboard (~1130 lines)
- `templates/index.html` — Landing page with auth + admin + role-based app cards (~440 lines)
- `firestore.rules` — Firestore security rules (admin/approved helpers)
- `vercel.json` — Vercel deployment config
- `requirements.txt` — Python deps (flask, polymarket-us, requests, python-dotenv, firebase-admin)
- `.env` — Local env vars (DO NOT COMMIT — contains API keys)

## Environment Variables (in Vercel)

| Variable | Purpose |
|---|---|
| `OWLS_INSIGHT_API_KEY` | Owls Insight API key (MVP+ plan) |
| `POLYMARKET_KEY_ID` | Polymarket US API key ID |
| `POLYMARKET_SECRET_KEY` | Polymarket US API secret |
| `FIREBASE_SERVICE_ACCOUNT` | Firebase Admin SDK service account JSON |
| `FLASK_SECRET_KEY` | Flask session secret |
| `SUPABASE_URL` | Supabase Postgres URL — read by Flask for the line-movement chart |
| `SUPABASE_SERVICE_KEY` | Supabase service key — same |

---

## Odds Board (`/odds`)

### Features
- **Best Odds Column** (left, always visible): Best ML, Spread, Total across all enabled books
- **Multi-Book Columns** (scrollable right): Individual book odds side by side
- **Sport Tabs**: MLB, NBA, NHL, NFL, NCAAB, MMA, Soccer, Tennis
- **Search**: Filter by team name (client-side, instant)
- **Book Selector**: Dropdown with checkboxes + up/down arrows to reorder. Saved to Firestore
- **Live Scores**: Green LIVE badge with score between team names
- **Circa Splits**: Handle % vs Ticket % per market. SHARP tags when divergence >= 15%. Shows movement from first-seen values (e.g. `44% (-3)`) — stored in Firestore like openers
- **Splits Last-Changed Timestamp**: Splits header shows `updated Xm ago` per game. Server-authoritative — only bumps `ts` when Circa handle/bets % values actually differ from stored (Circa feed updates ~15-30 min, so this reveals real movement vs stale polls). Stored in Firestore doc `openers/splits_changed:{sport}`
- **Line Movement**: Opener vs current with arrows and diffs
- **Reverse Line Movement (RLM)**: Pulsing red flag when line moves against sharp money
- **Line-Movement Chart**: Each game header has a small graph icon. Click → modal with a step-function chart (Chart.js) of historical odds from Supabase `book_snapshots`. Defaults to PIN/CIR/DK on the ML market over the last 24h; toggle other books (FD/MGM/CAE/HR/NVG) and switch market (ML/Spread/Total) or range (15m/30m/1H/6H/12H/24H/All). POLY excluded — its prices are 0-1 probability, not American odds. Data only exists for games the 5-min Owls ingest cron has captured.
- **Auto-refresh**: 15 seconds (odds)
- _Note: Polymarket "my bets" indicators were removed from the Odds Board so it can be shared with friends (viewer role). The Dashboard still shows P&L for active positions._
- **Genuine first-seen openers**: `/api/openers/scanner` returns the actual earliest PIN/CIR snapshot per market from Supabase. The client merges this on top of legacy Firestore openers — scanner data wins, Firestore fills any gaps for games predating the cron. The old "prefetch all sports on first page load to seed openers" hack has been removed.
- **Double-buffer rendering**: Two board divs swap to prevent flash on re-render

### Key JS Functions (odds.html)
- `loadOdds()` — fetches `/api/odds`, calls `captureOpeners()`, then `mergeScannerOpenersInto()`, then `renderBoard()`
- `captureOpeners()` — legacy Firestore opener capture from current PIN/CIR data
- `loadScannerOpeners()` / `mergeScannerOpenersInto()` — pulls scanner-backed openers via `/api/openers/scanner` and merges them over `currentOpeners`. Scanner values win
- `computeMovement()` — compares opener to current, includes JIT backfill
- `renderMovement()` — renders opener → arrow → current for ML/SPR/TOT
- `detectRLM()` — reverse line movement using Circa splits ONLY
- `renderBoard()` — main render, double-buffered. Exposed to `window` for search
- `renderSplitsRow()` — renders handle%/bets% with sharp detection and movement diffs from splits openers
- `captureSplitsOpeners()` — captures first-seen Circa splits per game to Firestore (like `captureOpeners()`)
- `loadSplitsOpeners()` / `saveSplitsOpenersAPI()` — Firestore load/save for splits openers
- `buildSplitsSnapshot()` — builds per-game `{ml, spread, total}` snapshot of current Circa handle/bets % for diff POST
- `syncSplitsLastChanged()` — POSTs snapshot to `/api/splits-last-changed`; server decides which games actually changed and returns fresh `ts` map. Triggers `renderBoard()` on change
- `loadSplitsLastChanged()` — GETs per-game last-changed map on sport switch / app boot
- `fmtTsAgo(ts)` — formats unix-ms timestamp as `just now` / `Xs/m/h/d ago`

---

## Player Props (`/props`)

### Features
- **Game-grouped layout**: Props organized by matchup, each game collapsible (click header to expand/collapse)
- **Best Line Comparison**: Best over/under price across all enabled books with book attribution
- **Expandable Detail**: Click any prop row to see all books' lines with deep links to sportsbook pages
- **Sport Tabs**: Same as Odds Board (MLB, NBA, NHL, NFL, NCAAB, MMA, Soccer, Tennis)
- **Search**: Filter by player name or team name (client-side, instant). Auto-expands matching games. Clear button (X) in search box
- **Book Selector**: Shared with Odds Board (same Firestore preferences — `odds_books`, `odds_book_order`)
- **Sport preference**: Saved separately as `props_sport` in Firestore
- **Auto-refresh**: 120 seconds (prop lines move slowly — saves API budget)

### Caching
- Props: 120 second TTL server-side (vs 10s for odds)
- Uses same `_owls_cache` dict

### Owls Insight Props API Response Format
The `/props` endpoint returns a **different format** than `/odds`:
```json
{
  "data": [
    {
      "gameId": "mlb:Colorado Rockies@San Diego Padres-20260410",
      "sport": "mlb",
      "homeTeam": "San Diego Padres",
      "awayTeam": "Colorado Rockies",
      "commenceTime": "2026-04-10T01:41:00.000Z",
      "isLive": false,
      "books": [
        {
          "key": "fanduel",
          "title": "FanDuel",
          "props": [
            {
              "playerName": "Fernando Tatis Jr.",
              "category": "runs",
              "line": 0.5,
              "overPrice": 210,
              "underPrice": null,
              "event_link": "https://sportsbook.fanduel.com/..."
            }
          ]
        }
      ]
    }
  ]
}
```
**Key differences from odds endpoint**: Uses `gameId`/`homeTeam`/`awayTeam`/`commenceTime` (camelCase, not snake_case). Props are flat under `books[].props[]` with `playerName`, `category`, `line`, `overPrice`, `underPrice` — NOT the nested `bookmakers[].markets[].outcomes[]` structure.

### Props Normalization (`app.py`)
- `_fetch_props(sport)` — fetches `/{sport}/props` with 120s cache
- `_normalize_props()` — parses the flat `data[]` list into game → player → prop structure
- `_prop_market_label(category)` — maps category strings (`runs`, `strikeouts`, `hits`, `points`, `rebounds`, etc.) to human labels. Categories are simple strings, NOT prefixed with `player_`

### Key JS Functions (props.html)
- `renderBoard()` — main render, exposed to `window` for search input binding
- `findBestLine(prop, side, books)` — finds highest price across enabled books for over/under
- `toggleGame(eid)` — expand/collapse game card
- `toggleDetail(rowId)` — expand/collapse individual prop row to show all books
- `loadProps()` — fetches `/api/props`, re-renders board
- `loadAndStart()` — loads Firestore prefs, then starts app

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

## Domain Knowledge — Splits vs Movement

### THESE ARE DIFFERENT THINGS
- **Splits**: Handle % and bets % data (Circa only — DK is worthless). Shows sharp money detection. Located in the game footer below the movement bar. First-seen splits stored in Firestore (`splits:{sport}`) — shows diffs when handle % changes (e.g. `1% (+1)` in green, `44% (-3)` in red). Same persistence pattern as line openers — survives cold starts.
- **Movement / Historical Line Data**: The movement bar. Records the FIRST line seen (opener), then tracks if it moved up/down. Stored via `/api/openers` in Firestore. Do NOT confuse with splits. Ever.

### Movement Rules
- **Book priority**: Pinnacle first, then Circa. ONLY two sharp books. Do NOT use Wynn, Westgate, DK, etc. for openers, backfill, or RLM
- **Opener lock-in**: Once captured for a game ID, PERMANENTLY locked. Never overridden, never reset daily
- **ML openers**: Only from Pinnacle or Circa
- **Backfill**: Missing markets get backfilled from source book on subsequent loads
- **RLM detection**: Circa splits ONLY. Never DraftKings. No splits > DK splits
- **API quirk**: Some books don't send ML (h2h) for MLB. Market key can be `h2h` or `moneyline`
- **Debug**: Add `?debug=markets` to odds URL to see market keys per book

### Key Terminology
- **ML** = Moneyline (NOT Machine Learning)
- **SPR** = Spread
- **TOT** = Total (Over/Under)
- **RLM** = Reverse Line Movement
- **CIR** = Circa, **PIN** = Pinnacle, **DK** = DraftKings, **FD** = FanDuel

---

## Owls Insight API

**Base URL**: `https://api.owlsinsight.com/api/v1`
**Auth**: `Authorization: Bearer {OWLS_INSIGHT_API_KEY}`
**Plan**: MVP+ — 300K req/month, 400/min, real-time sharp odds, full props

### Endpoints Used

| Endpoint | Purpose |
|---|---|
| `GET /{sport}/odds` | All odds (spreads, moneylines, totals) from all books |
| `GET /{sport}/props` | Player props from all books |
| `GET /{sport}/splits` | Circa + DK betting splits |
| `GET /{sport}/scores/live` | Live scores |
| `GET /{sport}/realtime` | Real-time Pinnacle sharp odds |

### Sports Keys
`mlb`, `nba`, `nhl`, `nfl`, `ncaab`, `ncaaf`, `mma`, `soccer`, `tennis`

### Sportsbook Keys
`pinnacle`, `fanduel`, `draftkings`, `betmgm`, `caesars`, `bet365`, `circa`, `south_point`, `westgate`, `wynn`, `stations`, `hardrock`, `betonline`, `1xbet`, `polymarket`, `kalshi`, `novig`

### Caching (server-side, in-memory)
- Odds: 10s TTL
- Splits: 10s TTL
- Props: 120s TTL (2 minutes)
- Scores: 30s TTL
- My-bets: 60s TTL
- **Vercel cold starts reset all caches**

---

## Firestore Structure

- **`users/{uid}`** — User profile: `email`, `displayName`, `role` (`admin` / `viewer` / `pending`), `approved` (bool), `preferences`, `createdAt`. There is NO `appAccess` field anymore — access is determined entirely by `role`.
- **`openers/openers:{sport}`** — Opening lines per sport. `events` map of game IDs to opener data. Permanent — never reset daily
- **`openers/splits:{sport}`** — First-seen Circa splits per sport. `events` map of game IDs to handle/bets percentages. Permanent — never override
- **`openers/splits_changed:{sport}`** — Last-changed Circa splits per game. `events` map: `{eid: {ml, spread, total, ts}}`. `ts` (unix ms) bumps only when values actually differ from stored. Server-authoritative diff in `/api/splits-last-changed` POST
- **Preferences fields**: `odds_books`, `odds_book_order`, `odds_sport`, `props_sport`

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
- Top bar: nav links (Home, Odds, Props, Dashboard) on first row, status + logout on second row. Dashboard link only renders for admins.
- Movement bar items wrap with `flex-wrap` so ML/SPR/TOT all show
- Odds table scrolls horizontally
- Splits grid single-column below 900px
- Game card fadeUp animation only on first load

## Deployment
- **Every commit goes to `main`**. Vercel auto-deploys to thekahlahouse.com on push to `main`. Don't leave changes on a feature branch.
- If you're handed a feature branch (e.g. `claude/...`), finish the work, merge into `main`, push `main`. Don't wait to be told.
- GitHub repo: `Diavel78/kahla-house`
- Vercel project: `kahla-house` (team: `diavel78s-projects`)
- Domain: `thekahlahouse.com` + `www.thekahlahouse.com`

## Known Issues & Gotchas
1. **Pinnacle feed drops randomly** — Circa is the reliable fallback for openers
2. **Some books don't send ML for MLB** — Backfill handles this
3. **Vercel cold starts** — In-memory cache resets. Openers + splits openers safe in Firestore
4. **SDK `price` field is the COMPLEMENT** — NEVER use for P&L. Always use `cost.value / qty` for real per-share price. The `price` field returns the opposite side's price (YES when trading NO). This was the root cause of sell P&L showing as losses — now fixed
5. **SDK `realizedPnl` unreliable** — Only use non-null as sell indicator, not the value
6. **Splits duplicates** — API returns today + tomorrow entries. Must prefer Circa-containing entries
7. **MMA odds sparse** — Only BetOnline returns MMA data through Owls Insight. No FanDuel/DraftKings/Pinnacle/Circa MMA coverage. User must enable BetOnline (BOL) in Books to see MMA fights
8. **SDK trade fields are nested objects** — `price`, `cost`, `realizedPnl`, `costBasis` are all `{currency, value}` dicts, not plain numbers. `_safe_float()` handles this by extracting `.value`
9. **`book_snapshots` is deduplicated** — a new row is only written when a (market, book, market_type, side)'s price or line actually changes since the last stored value (`_latest_snapshot_map` + `_dedup_unchanged` in `kahla-scanner/scrapers/owls.py`). Retail books (MGM, CAE) re-price constantly and get fresh rows every 5-min cycle; sharp books (PIN, CIR) post a line and sit — their last row can be hours old. This is correct for step-function chart rendering: the chart carries the last value forward visually until the next sample.
