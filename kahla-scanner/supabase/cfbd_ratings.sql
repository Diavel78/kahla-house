-- CollegeFootballData ratings mirror (Rob, Sep 17 2026: "ChatGPT says shit's
-- already out there… we can stop guessing"). One row per (source, year,
-- team): SP+, FPI, Elo, SRS as the venue publishes them. Read by
-- app._cfbd_consensus (the college PRE-MARKET line — SP+/FPI carry a
-- preseason prior with the roster in it, which our two-game results
-- solve does not); written daily by scripts/ingest_cfbd_ratings.py.
-- Apply (box): psql kahla -f kahla-scanner/supabase/cfbd_ratings.sql
--              then: notify pgrst, 'reload schema';
create table if not exists cfbd_ratings (
  source      text not null,        -- sp | fpi | elo | srs
  year        int  not null,
  team        text not null,        -- CFBD school name ("Notre Dame")
  conference  text,
  rating      double precision,     -- points vs average (sp/fpi/srs) or Elo
  extra       jsonb,
  fetched_at  timestamptz not null default now(),
  primary key (source, year, team)
);
create index if not exists cfbd_ratings_year on cfbd_ratings (year, source);
notify pgrst, 'reload schema';
