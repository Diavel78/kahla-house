-- Phase 4 Sharp Bot — paper_bets table.
--
-- One row per logged paper bet. Three bots write here:
--   steam  — fired by scripts/sharp_alerts.py whenever a Telegram STEAM
--            alert fires. Same dedup window as the alert (sharp_alerts
--            table). Inherits the alert's market_id/market_type/side.
--   early  — scripts/paper_bets_picker.py --bot early. Runs 1×/day;
--            candidates have event_start 10–36h out. Top 5 across all
--            sports per run.
--   late   — scripts/paper_bets_picker.py --bot late. Runs every 30 min
--            via scanner-poll.yml; candidates have event_start <2h out.
--            Top 5 per run, max one row per (market_id, bot) ever (late
--            window is single per game).
--
-- Resolution (Stage 2): a separate resolver script reads ESPN final
-- scores and grades pending rows to won / lost / push, sets pnl_units
-- (flat 1u sizing: +price/100 win, -1.0 loss, 0 push), settled_at.
--
-- Run in Supabase SQL editor.

create table if not exists paper_bets (
  id            bigserial primary key,
  picked_at     timestamptz not null default now(),
  bot           text not null check (bot in ('steam','early','late')),
  market_id     uuid not null references markets(id) on delete cascade,
  sport         text not null,
  event_name    text not null,
  event_start   timestamptz not null,
  market_type   text not null check (market_type in ('moneyline','spread','total')),
  side          text not null check (side in ('home','away','over','under')),
  -- Entry: the actual price/book/line we'd hit. Locked at pick time.
  entry_book    text not null,
  entry_price   integer not null,            -- American odds
  entry_line    numeric,                     -- spread/total point (null for ML)
  -- Signal context (nullable so steam bets without a clean PIN devig
  -- still log — we still want hit-rate even without pre-bet edge).
  fair_prob     numeric,                     -- PIN devigged prob for our side
  edge_pp       numeric,                     -- (fair - implied_at_entry) × 100
  sharp_score   integer,                     -- 0-10 (PIN movement magnitude)
  signal_blob   jsonb,                       -- {opener_price, current_price, books, ...}
  -- Resolution. NULL until resolver grades the bet.
  status        text not null default 'pending'
                check (status in ('pending','won','lost','push','void')),
  result_score  jsonb,                       -- ESPN final {away, home, total}
  pnl_units     numeric,                     -- flat 1u sizing
  settled_at    timestamptz
);

-- Pickers query "is this market already picked by this bot recently?".
create index if not exists paper_bets_bot_market_idx
  on paper_bets (bot, market_id, picked_at desc);

-- Resolver scans pending rows whose game has finished.
create index if not exists paper_bets_pending_idx
  on paper_bets (status, event_start)
  where status = 'pending';

-- UI lists most-recent-first.
create index if not exists paper_bets_picked_idx
  on paper_bets (picked_at desc);

-- RLS: service-role only for now. The /api/sharp-bot endpoint reads via
-- the Supabase service key (same pattern as /api/odds), so anon access
-- isn't needed. Lock it down to be safe.
alter table paper_bets enable row level security;
