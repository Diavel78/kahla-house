# FOOTBALL QB ADJUSTMENT — the roster the ratings never had (spec, Sep 13 2026)

## Why

Gridiron IQ rates a team as a bundle of past points (opponent-adjusted
off/def from `game_results`, 40-day half-life, fitted HFA, shrinkage fit
to a cover probability). It has no player table. Week 1 2026 it seated
real money on Arizona +10.5 (Kyler Murray released in March), Miami +3
(Tua gone), Atlanta +6.5 (Penix inactive, Cooper Rush started) — all
placed weeks early, all priced off last season's quarterback.

Rob, Sep 13: "roster news ain't hard… it needs looked at like once a
week. This is a rent machine, not a betting machine, but you have to get
in the ballpark." So: one number per team, refreshed daily, that moves the
projection by the one roster fact that dominates football — who is
throwing the passes — and nothing more elaborate until it has earned it.

## The number

Per quarterback, from `football_player_games` (NFL 2023→, NCAAF 2025→
after today's backfill):

    ANY/A_game = (pass_yds + 20·pass_td − 45·pass_int − sack_yds)
                 / (pass_att + sacks)              (sacks null → 0)

    quality(qb, as_of) = attempt-weighted, exp-decayed (half-life 400 d)
                         mean of ANY/A_game over games before as_of,
                         shrunk toward REPLACEMENT with a 200-attempt prior
    REPLACEMENT        = league attempt-weighted ANY/A (last 365 d) − 0.8
    no games on file   → quality = REPLACEMENT (a rookie prices as a backup)

Per team:

    baseline(team) = the quality of the passer who actually threw each of
                     the team's rated games, weighted exactly as the
                     ratings weight those games (0.5^(age/40d), 365-d
                     window). "The QB the rating was built on."
    starter(team)  = who throws the NEXT game (source order below)
    adj_pts(team)  = clamp(k · (quality(starter) − baseline), ±10)

`_gridiron_proj` adds `adj_pts` to that team's expected points before
margin/total. Every consumer inherits it: the executor's seat, the
recenter tick, the repeg's fresh fair, the ML rent lane, the opener
shadows. Nothing else in the model changes.

`k` (points per unit of ANY/A) is FIT, not guessed:
`scripts/backtest_football_qb.py` walks the seasons forward exactly as
the spread backtest does, regresses the residual (actual margin − shrunk
projection) on the QB delta, reports k with its standard error and the
Brier at the market-offset ladder with and without the term. The compute
script reads the fitted k from `machine_flags.football_qb`
(`{enabled, k_nfl, k_ncaaf, cap}`); the code constant is the fallback.

## Who is the starter (source order, per team)

NFL:
1. ESPN depth chart, core API
   `sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/{yr}/teams/{id}/depthcharts`
   — the offense formation's `qb` slot, rank 1. Skip an athlete whose
   roster `status` is not Active or whose `injuries[]` carries Out /
   Injured Reserve / Doubtful → rank 2.
2. The primary passer of the team's most recent game in the spine.
3. REPLACEMENT with `starter_src='unknown'`.

NCAAF (ESPN serves no college depth charts — 400):
1. The primary passer of the team's most recent game this season,
   if the roster lists him Active.
2. The next most-used passer this season who is Active.
3. REPLACEMENT, `starter_src='unknown'`.

Every row records `starter_name/id/src`, `baseline_qb` (the top-weighted
passer in the rated window), both qualities, and the adjustment. The
dashboard/dossier can print "priced on Brissett, rated on Brissett" or
"priced on Rush (backup), rated on Penix: −4.1".

## Freshness rule

`football_qb_adj.computed_at` older than 8 days ⇒ the pricer applies 0
and stamps `qb_adj_stale` — a dead batch job degrades to today's model,
never to a stale roster. Rows are upserted daily on the batch lane after
`power_ratings` (the ratings window and the baseline must agree).

## Table

`football_qb_adj (sport, team) pk`: starter_id, starter_name, starter_src,
starter_q, baseline_qb, baseline_q, adj_pts, k, replacement_q,
detail jsonb, computed_at. DDL `kahla-scanner/supabase/football_qb_adj.sql`.

## What this is NOT

- Not an injury model. RB1/WR1/OL/defense are not priced. QB is the
  ~80% term; the rest waits for evidence that it moves cover rates.
- Not a news feed. It reads ESPN's depth chart and roster status, which
  lag a beat-writer by hours, not weeks. That is the ballpark Rob asked
  for; a transaction feed can replace source #1 without touching the math.
- Not a veto. A team with an unknown starter still prices (at replacement),
  because rent is the reason the seat exists. The stamp says so.
