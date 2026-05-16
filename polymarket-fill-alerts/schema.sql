-- Polymarket fill-alert state table.
--
-- One row per Polymarket order we've ever seen. The check-fills
-- endpoint diffs the SDK response against this table every minute to
-- decide when to fire a Telegram alert.
--
-- Milestones tracked per order (in the `alerts_sent` jsonb array):
--   "25"  — partial fill crossed 25%
--   "50"  — partial fill crossed 50%
--   "75"  — partial fill crossed 75%
--   "100" — fully filled
--
-- Run once in your Supabase project's SQL Editor.

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
  order_created_at   text,
  first_seen_at      timestamptz not null default now(),
  last_seen_at       timestamptz not null default now(),
  terminal           boolean not null default false
);

-- Partial index so the per-tick "what's still open?" lookup stays
-- tight even as historical rows accumulate.
create index if not exists polymarket_fill_state_active_idx
  on polymarket_fill_state (last_seen_at desc)
  where terminal = false;

-- Lock down anon access — only the service-role key (used by the
-- Flask app via SUPABASE_SERVICE_KEY) can read/write.
alter table polymarket_fill_state enable row level security;
