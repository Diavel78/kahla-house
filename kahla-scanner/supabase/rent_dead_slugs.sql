-- THE RENT CULL (Sep 5 2026). One row per market slug the cull judged
-- DEAD: enrolled by the venue but earning (near) nothing on our resting
-- order. `_rent_dead(slug)` vetoes these slugs in every gridiron
-- candidate filter so a culled rung STAYS dead across re-entries; the
-- judge (`_rent_cull_tick`, repeg lane, hourly) writes them. Un-dead a
-- slug by deleting its row (+ flip its desired_orders row to pending).
create table if not exists rent_dead_slugs (
  slug         text primary key,
  sport        text,
  market_type  text,
  event_name   text,
  event_start  timestamptz,
  pick_id      bigint,
  contracts    numeric,
  entry_c      numeric,
  capital_usd  numeric,
  rent_usd     numeric,
  days         numeric,
  usd_per_day  numeric,
  reason       text,
  dry          boolean default false,
  killed_at    timestamptz not null default now()
);
create index if not exists rent_dead_slugs_killed_idx on rent_dead_slugs (killed_at desc);
-- PostgREST caches the schema: a new table is invisible to the daemon
-- until it reloads.
notify pgrst, 'reload schema';
