-- Kahla Scanner — Supabase schema
-- Run in Supabase SQL editor on a fresh project.

create extension if not exists "pgcrypto";

-- Event/market linkage across venues
create table if not exists markets (
  id uuid primary key default gen_random_uuid(),
  sport text not null,                    -- 'NFL','CBB','MLB','UFC', etc.
  event_name text not null,               -- 'Chiefs vs Bills 2026-01-19'
  event_start timestamptz not null,
  poly_market_id text unique,             -- Polymarket market id
  dk_event_id text,
  fd_event_id text,
  kalshi_ticker text,
  status text default 'active',           -- 'active','settled','void'
  created_at timestamptz default now()
);

create index if not exists markets_event_start_idx on markets(event_start);
create index if not exists markets_sport_idx on markets(sport);

-- Sportsbook line snapshots (append-only)
create table if not exists book_snapshots (
  id bigserial primary key,
  market_id uuid references markets(id) on delete cascade,
  book text not null,                     -- 'DK','FD'
  market_type text not null,              -- 'moneyline','spread','total'
  side text not null,                     -- 'home','away','over','under'
  line numeric,                           -- spread/total value, null for ML
  price_american integer not null,        -- -110, +150, etc.
  implied_prob numeric,                   -- raw implied prob (with vig)
  captured_at timestamptz default now()
);

create index if not exists book_snap_market_idx
  on book_snapshots(market_id, captured_at desc);

-- Polymarket trade/tick log (append-only)
create table if not exists poly_ticks (
  id bigserial primary key,
  market_id uuid references markets(id) on delete cascade,
  outcome text not null,                  -- 'YES','NO' or team name
  price numeric not null,                 -- 0.0 to 1.0
  size numeric not null,                  -- USD size of fill
  side text,                              -- 'buy','sell'
  tick_ts timestamptz not null,           -- timestamp from Poly
  captured_at timestamptz default now()
);

create index if not exists poly_ticks_market_idx
  on poly_ticks(market_id, tick_ts desc);

-- Computed signals (when divergence clears threshold)
create table if not exists signals (
  id uuid primary key default gen_random_uuid(),
  market_id uuid references markets(id) on delete cascade,
  signal_type text not null,              -- 'divergence','rlm','arb'
  fade_side text not null,                -- which side to take
  public_prob numeric not null,           -- DK/FD implied prob (devig'd)
  sharp_prob numeric not null,            -- Polymarket implied prob
  edge_pct numeric not null,              -- sharp - public, in %
  liquidity_usd numeric,                  -- book depth at signal price
  triggered_at timestamptz default now(),
  status text default 'open',             -- 'open','expired','resolved'
  notes jsonb
);

create index if not exists signals_triggered_idx on signals(triggered_at desc);
create index if not exists signals_open_idx on signals(status) where status = 'open';

-- Telegram subscribers (multi-user)
create table if not exists subscribers (
  id uuid primary key default gen_random_uuid(),
  telegram_chat_id bigint unique not null,
  handle text,
  display_name text,
  sports text[] default array['NFL','CBB','MLB','NBA','NHL','UFC'],
  min_edge_pct numeric default 3.0,
  min_liquidity_usd numeric default 500,
  quiet_hours_start int,
  quiet_hours_end int,
  timezone text default 'America/Phoenix',
  active boolean default true,
  created_at timestamptz default now()
);

-- Alert dedup log
create table if not exists alerts_log (
  id bigserial primary key,
  signal_id uuid references signals(id) on delete cascade,
  subscriber_id uuid references subscribers(id) on delete cascade,
  sent_at timestamptz default now(),
  delivery_status text                    -- 'sent','failed'
);

create unique index if not exists alerts_dedup_idx
  on alerts_log(signal_id, subscriber_id);

-- Team name aliases for event matching
create table if not exists team_aliases (
  id bigserial primary key,
  sport text not null,
  canonical text not null,
  alias text not null,
  unique (sport, alias)
);

create index if not exists team_aliases_sport_idx on team_aliases(sport);

-- Settled market outcomes. Populated by a resolver job (Poly resolution
-- webhooks, scores API, or manual entry). Used by analytics/brier.py to
-- score how well each source predicted the actual outcome.
create table if not exists market_outcomes (
  market_id uuid primary key references markets(id) on delete cascade,
  winning_side text not null,             -- 'home','away','void'
  resolved_at timestamptz default now(),
  source text                             -- 'polymarket','manual','scores_api'
);

create index if not exists market_outcomes_resolved_idx
  on market_outcomes(resolved_at desc);

-- Unmatched markets (for manual review / alias tuning)
create table if not exists unmatched_markets (
  id bigserial primary key,
  source text not null,                   -- 'poly','dk','fd','kalshi'
  source_id text not null,
  sport text,
  event_name text,
  event_start timestamptz,
  payload jsonb,
  seen_at timestamptz default now(),
  resolved boolean default false,
  unique (source, source_id)
);

-- ============================================================================
-- Row Level Security
-- Service key bypasses RLS (used by scanner). Anon key is used by the dashboard;
-- policies below expose non-sensitive read data only.
-- ============================================================================

alter table markets enable row level security;
alter table book_snapshots enable row level security;
alter table poly_ticks enable row level security;
alter table signals enable row level security;
alter table subscribers enable row level security;
alter table alerts_log enable row level security;
alter table team_aliases enable row level security;
alter table unmatched_markets enable row level security;
alter table market_outcomes enable row level security;

-- Dashboard (anon) can read markets, snapshots, ticks, signals
drop policy if exists markets_read_anon on markets;
create policy markets_read_anon on markets for select to anon using (true);

drop policy if exists book_snap_read_anon on book_snapshots;
create policy book_snap_read_anon on book_snapshots for select to anon using (true);

drop policy if exists poly_ticks_read_anon on poly_ticks;
create policy poly_ticks_read_anon on poly_ticks for select to anon using (true);

drop policy if exists signals_read_anon on signals;
create policy signals_read_anon on signals for select to anon using (true);

drop policy if exists outcomes_read_anon on market_outcomes;
create policy outcomes_read_anon on market_outcomes for select to anon using (true);

-- subscribers + alerts_log are service-only (contain PII / Telegram IDs)
-- No anon policies added on purpose.
