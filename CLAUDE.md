# Project: The Kahla House

## Domain Knowledge — Odds Board

### Splits vs Historical Data — THESE ARE DIFFERENT THINGS
- **Splits**: Handle % and bets % data (Circa, DraftKings). Shows sharp money detection. Located in the game footer. SPLITS ARE FINE — do not touch unless explicitly asked.
- **Historical Data**: This is NOT splits. Historical data refers to past game/matchup data, trends, or records. Do not confuse the two. Ever.

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
