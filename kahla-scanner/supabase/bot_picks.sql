-- Handicapper Bot — bot_picks table.
--
-- Every pick the in-chat handicapper makes lands here, auto-graded by
-- scripts/bot_picks_resolver.py against ESPN finals (same pattern as
-- paper_bets). Drives the /handicapper admin page and the rolling
-- hit-rate / units / ROI stats.
--
-- Different from paper_bets in three ways:
--   1. Sized 1/3/5 units (confidence-tiered) instead of flat 1u.
--   2. Carries the user's question + the analyst write-up so each pick
--      is a self-contained record we can review later.
--   3. Markets limited to ML / SPR / TOT — no props.
--
-- Run in Supabase SQL editor.

create table if not exists bot_picks (
  id            bigserial primary key,
  picked_at     timestamptz not null default now(),
  -- Provenance.
  asked_by      text,                          -- Firebase UID, or 'admin' for CLI
  query_text    text,                          -- "Toronto vs Angels today, thoughts?"
  -- Game / market identity.
  market_id     uuid not null references markets(id) on delete cascade,
  sport         text not null,
  event_name    text not null,
  event_start   timestamptz not null,
  market_type   text not null check (market_type in ('moneyline','spread','total')),
  side          text not null check (side in ('home','away','over','under')),
  -- Entry: locked at pick time so resolver grades against the actual
  -- price/line the bot recommended.
  entry_book    text not null,
  entry_price   integer not null,              -- American odds
  entry_line    numeric,                       -- spread/total point (null for ML)
  -- Sizing + confidence. units ∈ {1,3,5}. confidence is the human-readable
  -- chip so the page can color-code without re-deriving.
  units         integer not null check (units in (1,3,5)),
  confidence    text not null check (confidence in ('low','medium','high','max')),
  -- Edge math. Nullable when PIN devig isn't possible (one-sided market,
  -- mismatched lines) — we still log the pick.
  fair_prob     numeric,
  edge_pp       numeric,
  sharp_score   integer,
  -- Full analyst write-up (markdown). Rendered on the /handicapper page.
  analysis_md   text,
  -- Bullet reasons (for compact list views) + the raw dossier the analyst
  -- worked from. signal_blob lets us re-grade past picks if we change
  -- scoring later, without re-fetching historical odds.
  reasons       jsonb,                         -- ["bullet 1", "bullet 2", ...]
  signal_blob   jsonb,
  -- Resolution. NULL until resolver grades the pick.
  status        text not null default 'pending'
                check (status in ('pending','won','lost','push','void')),
  result_score  jsonb,                          -- {away, home, total}
  pnl_units     numeric,                        -- units × payout (signed)
  settled_at    timestamptz
);

-- Resolver scans pending rows whose game has finished.
create index if not exists bot_picks_pending_idx
  on bot_picks (status, event_start)
  where status = 'pending';

-- Page lists most-recent-first.
create index if not exists bot_picks_picked_idx
  on bot_picks (picked_at desc);

-- Per-(market_id) lookback: caller can check "did I already pick this
-- market in the last 7d?" to avoid double-logging when the user asks
-- about the same game twice in a session.
create index if not exists bot_picks_market_idx
  on bot_picks (market_id, picked_at desc);

-- RLS: service-role only. The /api/handicapper endpoint reads via the
-- Supabase service key (same pattern as /api/sharp-bot). Lock anon out.
alter table bot_picks enable row level security;
