-- Persisted football ladder STRUCTURE (Sep 6 2026): which rungs (slug /
-- side / line / synthetic) a game's spread/total/ML ladders hold, plus the
-- per-slug tick. Discovered once per game over REST (pmm_markets.lookup),
-- reloaded at daemon boot so the quote table is subscribed to the whole
-- board on the first lap instead of one REST discovery at a time.
create table if not exists ladder_cache (
  market_id   text primary key,
  sport       text,
  event_start timestamptz,
  struct      jsonb not null,
  ticks       jsonb,
  updated_at  timestamptz not null default now()
);
create index if not exists ladder_cache_start_idx on ladder_cache (event_start);
