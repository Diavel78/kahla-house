-- Polymarket fill-alert state table.
--
-- One row per Polymarket order we've ever seen, used by
-- /api/polymarket/check-fills to detect fill events between cron ticks.
-- The Polymarket US app has no native fill notifications (only the
-- international web app does), so we poll the SDK every minute via
-- cron-job.org pinging /api/polymarket/check-fills and diff cum_quantity
-- against this table to decide when to fire a Telegram alert.
--
-- Milestones tracked per order (in the `alerts_sent` jsonb array):
--   "25"  — partial fill crossed 25%
--   "50"  — partial fill crossed 50%
--   "75"  — partial fill crossed 75%
--   "100" — fully filled (state = ORDER_STATE_FILLED OR fill_pct >= 100)
--
-- Only the highest milestone newly crossed in any single tick fires a
-- Telegram message — a market order that fills 0 → 100% in one tick
-- sends one "FILLED" alert, not four (25/50/75/100).
--
-- `terminal=true` is a fast-skip flag: once an order is FILLED /
-- CANCELED / EXPIRED / REJECTED it never needs re-processing, so each
-- subsequent cron tick can skip it without rebuilding the row.
--
-- Run in Supabase SQL editor.

create table if not exists polymarket_fill_state (
  order_id           text primary key,
  market_name        text,
  pick               text,
  slug               text,
  intent             text,
  side_label         text,
  quantity           numeric,
  price              numeric,
  last_cum_quantity  numeric not null default 0,
  last_state         text,
  alerts_sent        jsonb not null default '[]'::jsonb,
  order_created_at   text,                            -- SDK createTime (ISO string)
  first_seen_at      timestamptz not null default now(),
  last_seen_at       timestamptz not null default now(),
  terminal           boolean not null default false
);

-- Active (non-terminal) orders are the only rows the cron tick needs to
-- inspect closely. Partial index keeps it tight.
create index if not exists polymarket_fill_state_active_idx
  on polymarket_fill_state (last_seen_at desc)
  where terminal = false;

-- Service-role only. Same posture as paper_bets / bot_picks — the
-- /api/polymarket/check-fills endpoint reads/writes via the Supabase
-- service key, no anon access needed.
alter table polymarket_fill_state enable row level security;
