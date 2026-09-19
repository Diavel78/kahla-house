-- THE MIDDLE PAIR (Rob, Sep 18 2026 — the Lambo's replacement: both legs on
-- Polymarket, opposite sides of two neighbouring rungs, a middle between them).
-- One row per pair; app._pair_tick owns every order on its two slugs, and every
-- other engine skips them (app._pair_slugs). `legs` is the static definition,
-- `state` the engine's per-leg memory (bid/ask prices it placed, lot cost, the
-- last sell price of the cycle) — persisted so a restart never re-costs a lot
-- off the venue's blended avgPx.
create table if not exists pair_hedges (
  id           bigserial primary key,
  game_prefix  text not null,              -- asc-nfl-gb-nyj-2026-09-20
  market_type  text not null default 'spread' check (market_type in ('spread','total')),
  event_name   text,
  kickoff      timestamptz not null,
  qty          integer not null default 15,
  cap_c        numeric not null default 110,   -- combined cents, both legs
  legs         jsonb not null,             -- [{key, slug, intent, label}] ×2
  state        jsonb not null default '{}'::jsonb,
  enabled      boolean not null default true,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create unique index if not exists pair_hedges_game_mt_uniq
  on pair_hedges (game_prefix, market_type) where enabled;
