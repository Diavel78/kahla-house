-- Sep 7 2026: PROPS ON THE SOCKET. Prop markets discovered once per game
-- (only the families we bet: NFL pass/rush/rec yards + receptions, MLB
-- outs/hits/walks), persisted so a restart re-subscribes the whole board;
-- the tape and the props passes then read socket frames, not REST payloads.
create table if not exists prop_catalog (
  slug        text primary key,
  market_id   text not null,
  sport       text,
  event_start timestamptz,
  question    text,
  fam         text,
  player      text,
  line        numeric,
  ptype       text,
  updated_at  timestamptz not null default now()
);
create index if not exists prop_catalog_start_idx on prop_catalog (event_start);
