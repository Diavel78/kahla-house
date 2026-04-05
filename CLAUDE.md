# Project: The Kahla House

## Domain Knowledge — Odds Board

### Splits vs Movement (Historical Line Data) — THESE ARE DIFFERENT THINGS
- **Splits**: Handle % and bets % data (Circa, DraftKings). Shows sharp money detection. Located in the game footer below the movement bar. SPLITS ARE FINE — do not touch unless explicitly asked.
- **Movement / Historical Line Data**: The movement bar (e.g. `CIR | SPR LOS -1.5 -175 | TOT O 9.5 -110`). Records the FIRST Circa line seen (opener), then tracks if it moved up/down with point and price diffs. Can trigger RLM (Reverse Line Movement) flags. This is stored via the openers API in Firestore. Code: `computeMovement()`, `detectRLM()`, `renderMovement()` in odds.html. Backend: `/api/openers` in app.py. Do NOT confuse this with splits. Ever.

### Key terminology
- **ML** = Moneyline (a bet type), not Machine Learning
- **SPR** = Spread
- **TOT** = Total (Over/Under)
- **RLM** = Reverse Line Movement
- **CIR** = Circa (sportsbook)
- **PIN** = Pinnacle
- **DK** = DraftKings
- **FD** = FanDuel

## Tech Stack
- Backend: Flask (Python) on Vercel
- Frontend: Vanilla JS, Firebase Auth, Firestore
- Styling: Embedded CSS in HTML templates (no external CSS framework)
- Templates: `templates/odds.html`, `templates/dashboard.html`, `templates/budget.html`, `templates/index.html`
- Main backend: `app.py`
