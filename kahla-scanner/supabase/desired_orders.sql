-- PHASE 1 OMS (Aug 31 2026): desired-state betting for the rent list.
-- A row = "this game+market should carry a resting order". Producers
-- (the rent list, later every lane) write desire — milliseconds, so
-- coverage of a 400-game board is instant and "the sweep didn't reach
-- it" stops existing as a state. The executor converges pending rows
-- via the proven placement path; a row can only be pending, placed,
-- or blocked WITH A REASON — never silently missed.
create table if not exists desired_orders (
  id           bigserial primary key,
  market_id    uuid not null,
  market_type  text not null check (market_type in ('spread','total','moneyline')),
  lane         text not null default 'rentlist',
  state        text not null default 'pending'
               check (state in ('pending','placed','blocked','retired')),
  detail       text,
  tries        int not null default 0,
  event_start  timestamptz,
  next_try_at  timestamptz not null default now(),
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  unique (market_id, market_type, lane)
);
create index if not exists desired_orders_pending_idx
  on desired_orders (state, next_try_at);
