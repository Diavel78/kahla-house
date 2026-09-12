-- lookup_cache (Sep 12 2026, tape-process split step 2): the newest
-- pmm_markets.lookup result per event (ml/spread/total/nrfi — props dropped),
-- written by whichever process fetched it, read by the money process's
-- pricer when younger than _LOOKUP_REUSE_S. Rows older than a day are junk;
-- the writer prunes opportunistically. Applied to the box's local PG Sep 12.
create table if not exists lookup_cache (
  key        text primary key,
  sport      text,
  fetched_at timestamptz not null default now(),
  payload    jsonb not null
);
create index if not exists lookup_cache_fetched_idx on lookup_cache (fetched_at);
notify pgrst, 'reload schema';
