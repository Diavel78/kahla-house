# Owls Insight API — Complete Reference
# https://owlsinsight.com | https://owlsinsight.com/docs

## Quick Start

Base URL: https://api.owlsinsight.com
Auth: Authorization: Bearer YOUR_API_KEY

## Sports

nba, ncaab, nfl, nhl, ncaaf, mlb, mma, soccer, tennis, cs2, valorant, lol

## Sportsbooks

pinnacle, fanduel, draftkings, betmgm, bet365, caesars, kalshi, polymarket, novig, 1xbet, betonline, circa, south_point, westgate, wynn, stations, hardrock

## REST Endpoints

### Odds (all tiers)
GET /api/v1/{sport}/odds          — All odds (spreads, moneylines, totals) for a sport
GET /api/v1/{sport}/moneyline     — Moneyline odds only
GET /api/v1/{sport}/spreads       — Point spread odds only
GET /api/v1/{sport}/totals        — Over/under totals only

Query parameters:
  ?books=pinnacle,fanduel         — Filter by sportsbook (comma-separated)
  ?alternates=true                — Include alternate lines (Rookie+ tiers)

### Events
GET /api/v1/{sport}/events        — List upcoming events for a sport

### Live Scores (all tiers)
GET /api/v1/scores/live                — All live scores across all sports
GET /api/v1/{sport}/scores/live        — Scores for a specific sport (sport in path, NOT query param)

### Player Props (Rookie+ tiers)
GET /api/v1/{sport}/props                        — All props from all books
GET /api/v1/{sport}/props?books=fanduel           — Filter by book
GET /api/v1/{sport}/props?player=LeBron           — Filter by player name (partial match)
GET /api/v1/{sport}/props?category=points         — Filter by prop category
GET /api/v1/{sport}/props/{book}                  — Props from a specific book

Available books for props: pinnacle, fanduel, draftkings, caesars, bet365, betmgm, kalshi, novig, polymarket

Exchange/prediction market props (kalshi, novig, polymarket) include a `liquidity` object.

Prop categories by sport:
  hockey: goals, hockey_assists, hockey_points, shots_on_goal
  soccer: shots_on_target, tackles, passes, fouls_committed, corners_taken
  baseball: hits, runs, rbis, home_runs, stolen_bases, total_bases, strikeouts_pitcher, strikeouts_batter, walks, earned_runs, outs_recorded, hits_allowed
  football: passing_yards, passing_tds, rushing_yards, rushing_tds, receiving_yards, receptions, touchdowns
  basketball: points, rebounds, assists, steals, blocks, threes_made, pts_rebs_asts, pts_rebs, pts_asts, rebs_asts, double_double, triple_double

### Betting Splits (Rookie+ tiers)
GET /api/v1/{sport}/splits                   — Public betting splits (handle %, ticket %)
  Sources: Circa Sports, DraftKings

### Team Normalization
GET /api/v1/normalize?name=Man%20Utd&sport=soccer — Normalize a team name

### Real-Time Sharp Odds (MVP+ tiers, Beta)
GET /api/v1/{sport}/realtime                 — Sub-second Pinnacle odds via MQTT/PS3838
GET /api/v1/{sport}/ps3838-realtime          — PS3838 feed specifically

### Prediction Markets (all tiers)
GET /api/v1/kalshi/{sport}                   — Kalshi prediction markets
GET /api/v1/kalshi/{sport}?league=nba        — Filter by league
GET /api/v1/kalshi/series                    — List all Kalshi series
GET /api/v1/kalshi/series/{ticker}           — Markets for a specific series ticker
GET /api/v1/polymarket/{sport}/markets       — Polymarket decentralized markets

Note: Novig odds are included in the main /odds endpoint as a bookmaker, NOT a separate route.

### Player Stats (Rookie+ tiers)
GET /api/v1/{sport}/stats                    — Box scores for recent/live games
GET /api/v1/{sport}/stats/averages?playerName=LeBron James — Rolling averages (L5, L10, L20, season)
GET /api/v1/{sport}/stats/match?eventId=ID   — Match stats (possession, shots, etc.)
GET /api/v1/{sport}/stats/h2h?team1=X&team2=Y — Head-to-head history

### Historical Data (MVP+ tiers)
GET /api/v1/history/games?sport=nba          — List of archived games
GET /api/v1/history/odds?eventId=EVENT_ID    — Historical odds snapshots
GET /api/v1/history/props?eventId=EVENT_ID   — Historical props snapshots
GET /api/v1/history/stats?eventId=EVENT_ID   — Game stats (lineups, incidents)
GET /api/v1/history/player-props?player=LeBron&sport=nba — Historical closing player prop lines
GET /api/v1/history/closing-odds?sport=nba&startDate=2026-03-01 — Closing odds
GET /api/v1/history/public-betting?sport=nba&startDate=2026-03-01 — Public betting

### Props History & Stats
GET /api/v1/{sport}/props/history             — Historical props data
GET /api/v1/{sport}/props/{book}/history      — Historical props per book
GET /api/v1/props/stats                       — Props cache stats

## Response Format (Odds)

```json
{
  "success": true,
  "data": {
    "pinnacle": [
      {
        "id": "event-uuid",
        "sport_key": "basketball_nba",
        "commence_time": "2026-03-27T23:10:00Z",
        "home_team": "Los Angeles Lakers",
        "away_team": "Boston Celtics",
        "bookmakers": [
          {
            "key": "pinnacle",
            "title": "Pinnacle",
            "last_update": "2026-03-27T23:00:00Z",
            "markets": [
              {
                "key": "h2h",
                "outcomes": [
                  { "name": "Los Angeles Lakers", "price": 150 },
                  { "name": "Boston Celtics", "price": -175 }
                ]
              },
              {
                "key": "spreads",
                "outcomes": [
                  { "name": "Los Angeles Lakers", "price": -110, "point": 4.5 },
                  { "name": "Boston Celtics", "price": -110, "point": -4.5 }
                ]
              },
              {
                "key": "totals",
                "outcomes": [
                  { "name": "Over", "price": -110, "point": 220.5 },
                  { "name": "Under", "price": -110, "point": 220.5 }
                ]
              }
            ]
          }
        ]
      }
    ]
  }
}
```

Market keys: `h2h` = moneyline, `spreads` = point spreads, `totals` = over/under

## Deep Links

Odds and props responses include an `event_link` field that opens the game directly on the sportsbook's website.

## Rate Limits

- Bench ($9.99/mo): 10,000 req/mo, 20 req/min
- Rookie ($24.99/mo): 75,000 req/mo, 120 req/min
- MVP ($49.99/mo): 300,000 req/mo, 400 req/min
- Hall of Fame ($200/mo): Unlimited req/mo, 1000 req/min

Rate limit headers: X-RateLimit-Remaining-Minute, X-RateLimit-Remaining-Month

## WebSocket

URL: https://api.owlsinsight.com
Auth: query string `?apiKey=YOUR_API_KEY` (NOT headers)
Transport: websocket only

Events:
- odds-update: Latest odds, pushed on change
- player-props-update: Pinnacle player props
- fanduel-props-update, draftkings-props-update, bet365-props-update, betmgm-props-update, caesars-props-update
- esports-update: CS2, Valorant, LoL odds
- pinnacle-realtime / ps3838-realtime: Sub-second sharp odds

## Error Codes

- 401: Missing or invalid API key
- 403: Feature not available on your tier
- 404: Endpoint or resource not found
- 429: Rate limit exceeded (check Retry-After header)
- 500: Server error, retry with backoff
