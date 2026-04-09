# Project: The Kahla House

## Domain Knowledge — Odds Board

### Splits vs Movement (Historical Line Data) — THESE ARE DIFFERENT THINGS
- **Splits**: Handle % and bets % data (Circa, DraftKings). Shows sharp money detection. Located in the game footer below the movement bar. SPLITS ARE FINE — do not touch unless explicitly asked.
- **Movement / Historical Line Data**: The movement bar (e.g. `PIN | ML CHI -126 ▼ -131 (-5) | SPR LOS -1.5 +132 | TOT O 7.5 -102`). Records the FIRST line seen (opener), then tracks if it moved up/down with point and price diffs. Can trigger RLM (Reverse Line Movement) flags. This is stored via the openers API in Firestore. Code: `computeMovement()`, `detectRLM()`, `renderMovement()` in odds.html. Backend: `/api/openers` in app.py. Do NOT confuse this with splits. Ever.
  - **Book priority**: Pinnacle first (sharpest book), then Circa. These are the ONLY two sharp books. Do NOT use any other books (Wynn, Westgate, DK, etc.) for openers, backfill, or RLM detection.
  - **Opener lock-in**: Once an opener is captured for a game ID, it's locked in PERMANENTLY — never overridden, never reset daily. Openers persist in Firestore by sport (not by date).
  - **ML openers**: Only from Pinnacle or Circa. Do NOT record ML from other books.
  - **Backfill**: If an opener was captured missing any market (ML, spread, or total), it gets backfilled from the source book on subsequent loads. ML backfill only from Pinnacle or Circa. This applies to ALL sports.
  - **RLM detection**: Uses Circa splits ONLY. Never use DraftKings splits for RLM. No splits is better than DK splits — DK data is unreliable.
  - **Movement display**: All three markets (ML, SPR, TOT) show opener → arrow → current with colored diffs. Green/red arrows for direction.
  - **Multiple bets per game**: A game can have multiple Polymarket bets. All show as separate tags in the header.
  - **API quirk**: Some books (Circa, South Point, Stations, Westgate) don't send ML (h2h) for MLB via OWLS API. Backfill handles this by falling through to Pinnacle. The market key can be `h2h` or `moneyline` — both are accepted.
  - **Debug**: Add `?debug=markets` to the odds URL to see which books send which market keys.

### Key terminology
- **ML** = Moneyline (a bet type), not Machine Learning
- **SPR** = Spread
- **TOT** = Total (Over/Under)
- **RLM** = Reverse Line Movement
- **CIR** = Circa (sportsbook)
- **PIN** = Pinnacle
- **DK** = DraftKings
- **FD** = FanDuel

## Deployment
- Always push to production (main) for Kahla House. Merge feature branches into main and push.
- Vercel deploys automatically from `main`.

## Tech Stack
- Backend: Flask (Python) on Vercel
- Frontend: Vanilla JS, Firebase Auth, Firestore
- Styling: Embedded CSS in HTML templates (no external CSS framework)
- Templates: `templates/odds.html`, `templates/dashboard.html`, `templates/budget.html`, `templates/index.html`
- Main backend: `app.py`

## Architecture — Key Files

### `app.py` — Backend
- **Page routes**: `/`, `/odds`, `/dashboard`, `/budget` — serve HTML templates
- **API routes**:
  - `GET /api/odds?sport=mlb` — fetches odds + splits + scores, merges, returns to frontend
  - `GET/POST /api/openers?sport=mlb` — load/save opening lines in Firestore (permanent per game ID, keyed by `openers:{sport}`)
  - `GET /api/my-bets` — fetches Polymarket positions
  - `GET /api/odds/debug-markets?sport=mlb` — shows market keys per book (requires auth)
  - `GET /api/odds/raw`, `/api/splits/raw`, `/api/scores/raw` — raw API responses (admin only)
- **Data flow**: OWLS Insight API → normalize → merge splits → merge scores → JSON response
- **Caching**: In-memory cache with 10s TTL for odds/splits, 30s for scores
- **Splits timestamp**: Tracks when splits data actually CHANGED (via hash comparison), not just when fetched

### `templates/odds.html` — Frontend (all-in-one)
- **CSS**: Lines 26–705, embedded `<style>` block. Mobile breakpoint at 768px, splits grid at 900px.
- **Key JS functions**:
  - `loadOdds()` — fetches from `/api/odds`, calls `captureOpeners()`, then `renderBoard()`
  - `captureOpeners()` — captures first-seen lines from PIN/CIR, backfills missing markets
  - `computeMovement()` — compares opener to current, includes JIT backfill safety net
  - `renderMovement()` — renders opener → arrow → current for ML/SPR/TOT
  - `detectRLM()` — detects reverse line movement using Circa splits only
  - `renderBoard()` — main render, double-buffered to prevent flash. Exposed to `window` for search.
  - `findMyBets()` — returns ALL matching Polymarket bets for a game (supports multiple)
  - `renderBetTag()` — renders individual bet tag with status coloring
  - `renderSplitsRow()` — renders handle%/bets% with sharp detection
- **Double-buffer rendering**: Two board divs swap IDs to prevent flash. `no-animate` class disables fadeUp animation after first load.
- **Refresh**: 15s interval for odds, 60s for bets. Only re-renders if game list changed.
- **Prefetch**: On first load, prefetches all sports to capture openers early.

### `templates/dashboard.html` — Polymarket P&L Dashboard
- Tracks trading positions, closed P&L, balances
- Separate from odds board

### `templates/budget.html` — Budget Tracker
- Personal finance tracking tool

## Mobile Layout
- `overflow-x: hidden` on html, body, and `#app` wrapper (iOS Safari fix)
- Top bar wraps on mobile, status/logout shrink
- Movement bar items wrap with `flex-wrap` so ML/SPR/TOT all show
- Odds table scrolls horizontally inside `overflow-x: auto` container
- Splits grid goes single-column below 900px
- Game card fadeUp animation only on first load (no flash on re-renders)

## Firestore Structure
- **Collection `openers`**: One document per sport (e.g. `openers:mlb`), contains `events` map of game IDs to opener data. Permanent — never reset daily.
- **Auth**: Firebase Auth with client-side SDK. Backend validates tokens via `firebase_auth_required` decorator.

## External APIs
- **OWLS Insight API**: Odds, splits, scores. See `OWLS_API.md` for full endpoint reference.
  - Base URL: `https://api.owlsinsight.com`
  - Auth: `Authorization: Bearer {OWLS_INSIGHT_API_KEY}`
  - Market keys: `h2h` or `moneyline` = ML, `spreads` = point spreads, `totals` = over/under
  - Splits sources: Circa Sports, DraftKings
  - Some books (Circa, South Point, Stations, Westgate) don't send `h2h` for MLB
- **Polymarket**: Bet positions fetched via OWLS API integration.
