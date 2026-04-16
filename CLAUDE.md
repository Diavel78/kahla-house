# The Kahla House — Bet System

Multi-page sports betting platform deployed at **thekahlahouse.com**. Flask backend on Vercel, Firebase Auth + Firestore, vanilla JS frontend. This is the ONLY active codebase for the bet system. The "Poly-Tracker" repo is deprecated and not used.

**CRITICAL: This project lives at `/Users/robkahla/Documents/Kahla House/kahla-house/`. The domain is thekahlahouse.com. The Vercel project is `kahla-house`. Always push to `main` — Vercel deploys automatically.**

## Pages & Routes

| Route | Template | Purpose |
|---|---|---|
| `/` | `index.html` | Landing page (login/signup, admin panel, app cards) |
| `/odds` | `odds.html` | Odds Board — multi-book odds comparison, splits, movement, RLM, live scores |
| `/props` | `props.html` | Player Props — game-grouped best-line comparison across books |
| `/dashboard` | `dashboard.html` | Polymarket P&L Dashboard — positions, closed trades, bet slip |
| `/budget` | `budget.html` | Budget Tracker (personal finance) |

### API Routes

| Route | Auth | Purpose |
|---|---|---|
| `GET /api/odds?sport=mlb` | Firebase | Odds + splits + scores merged JSON |
| `GET /api/props?sport=mlb` | Firebase | Player props grouped by game/player |
| `GET/POST /api/openers?sport=mlb` | Firebase | Opening lines (Firestore, permanent per game ID) |
| `GET /api/my-bets` | Firebase | Active Polymarket positions for odds board matching |
| `GET/POST /api/preferences` | Firebase | User settings (books, sport, order) in Firestore |
| `GET /api/data` | Firebase | Dashboard P&L data (positions, balances, trades) |
| `GET /api/odds/raw` | Admin | Debug: raw Owls Insight odds response |
| `GET /api/splits/raw` | Admin | Debug: raw splits response |
| `GET /api/props/raw` | Admin | Debug: raw props response |
| `GET /api/scores/raw` | Admin | Debug: raw live scores response |
| `GET /api/realtime/raw` | Admin | Debug: raw Pinnacle sharp odds |
| `GET /api/raw` | Admin | Debug: raw Polymarket SDK responses |
| `GET/POST /api/splits-openers?sport=mlb` | Firebase | First-seen splits (Firestore, permanent per game ID) |
| `GET /api/debug-trades` | Firebase | Debug: grouped trade details with before/after position data |
| `/debug?slug=X` | Firebase (page) | Debug page that calls debug-trades with auth |

## Tech Stack

- **Backend**: Flask (Python), single file `app.py`, Vercel serverless
- **Frontend**: Vanilla JS, embedded CSS in each HTML template (no framework)
- **Auth**: Firebase Auth (client SDK) + `firebase_auth_required` decorator (server validates tokens)
- **Database**: Firestore (user prefs, openers, user management)
- **APIs**: Owls Insight (odds, splits, scores, props), Polymarket US SDK (positions, P&L)
- **Fonts**: DM Sans + JetBrains Mono
- **Deployment**: Vercel via `vercel.json`, env vars in Vercel dashboard, auto-deploys from `main`

## Key Files

- `app.py` — All backend logic (~1500 lines)
- `templates/odds.html` — Odds board (~1640 lines)
- `templates/props.html` — Player props board (~560 lines)
- `templates/dashboard.html` — P&L dashboard (~1030 lines)
- `templates/index.html` — Landing page with auth + admin (~450 lines)
- `templates/budget.html` — Budget tracker
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
- **Line Movement**: Opener vs current with arrows and diffs
- **Reverse Line Movement (RLM)**: Pulsing red flag when line moves against sharp money
- **Polymarket Bet Indicators**: Multiple bets per game supported, with live status coloring
- **Auto-refresh**: 15 seconds (odds), 60 seconds (bets)
- **Opener Prefetch**: First visit prefetches ALL sports to capture opening lines
- **Double-buffer rendering**: Two board divs swap to prevent flash on re-render

### Key JS Functions (odds.html)
- `loadOdds()` — fetches `/api/odds`, calls `captureOpeners()`, then `renderBoard()`
- `captureOpeners()` — captures first-seen lines from PIN/CIR, backfills missing markets
- `computeMovement()` — compares opener to current, includes JIT backfill
- `renderMovement()` — renders opener → arrow → current for ML/SPR/TOT
- `detectRLM()` — reverse line movement using Circa splits ONLY
- `renderBoard()` — main render, double-buffered. Exposed to `window` for search
- `findMyBets()` — returns ALL matching Polymarket bets for a game (multiple per game)
- `renderBetTag()` — renders individual bet tag with status coloring
- `renderSplitsRow()` — renders handle%/bets% with sharp detection and movement diffs from splits openers
- `captureSplitsOpeners()` — captures first-seen Circa splits per game to Firestore (like `captureOpeners()`)
- `loadSplitsOpeners()` / `saveSplitsOpenersAPI()` — Firestore load/save for splits openers

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
- **Stats cards**: Balance, Open Positions, Portfolio Value, Today's P&L, Yesterday's P&L, Total P&L, Win Rate
- **Open Positions table**: Market, Pick, Qty, Entry, Current, P&L, Return %
- **Closed Positions tab**: Resolved bets + sold trades with Result (W/L/Sold) and P&L
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
- **Multiple bets per game**: A game can have multiple Polymarket bets. All show as separate tags
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

- **`users/{uid}`** — User profile (email, role, appAccess, preferences)
- **`openers/openers:{sport}`** — Opening lines per sport. `events` map of game IDs to opener data. Permanent — never reset daily
- **`openers/splits:{sport}`** — First-seen Circa splits per sport. `events` map of game IDs to handle/bets percentages. Permanent — never override
- **Preferences fields**: `odds_books`, `odds_book_order`, `odds_sport`, `props_sport`

## Firebase Auth
- Client-side SDK in every template (compat mode)
- `onAuthStateChanged` → show auth gate, then init app
- `authFetch()` — wrapper that adds Bearer token to every API call
- Backend: `@firebase_auth_required` decorator validates tokens, sets `g.uid`
- `@admin_required` — checks user role is "admin"
- First signup auto-promotes to admin

## Mobile Layout
- `overflow-x: hidden` on html, body, `#app` (iOS Safari fix)
- Top bar: nav links (Home, Odds, Props, Dashboard) on first row, status + logout on second row (prevents overlap with 4 nav items)
- Movement bar items wrap with `flex-wrap` so ML/SPR/TOT all show
- Odds table scrolls horizontally
- Splits grid single-column below 900px
- Game card fadeUp animation only on first load

## Deployment
- **Always push to `main`**. Vercel auto-deploys to thekahlahouse.com
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
