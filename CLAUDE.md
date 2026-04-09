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
