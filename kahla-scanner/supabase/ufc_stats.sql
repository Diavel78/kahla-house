-- UFC data spine (Fight IQ model — Phase 1, July 2026).
-- Populated by kahla-scanner/scripts/ingest_ufc_stats.py from UFCStats.com
-- (complete bout history + per-fighter career stats). Mirrors the
-- game_results -> power_ratings pattern: these are the raw materials the
-- P2 FightElo engine solves from; the model snapshot table (ufc_model)
-- arrives with P2. Idempotent — safe to re-run.

create table if not exists ufc_fighters (
  id                text primary key,          -- UFCStats fighter id (URL tail)
  name              text not null,
  height_in         numeric,                   -- inches
  reach_in          numeric,                   -- inches
  stance            text,                      -- Orthodox / Southpaw / Switch
  dob               date,
  -- career per-15-min rates from the fighter page
  slpm              numeric,                   -- sig. strikes landed / min
  str_acc           numeric,                   -- striking accuracy (0-1)
  sapm              numeric,                   -- sig. strikes absorbed / min
  str_def           numeric,                   -- striking defense (0-1)
  td_avg            numeric,                   -- takedowns / 15 min
  td_acc            numeric,
  td_def            numeric,
  sub_avg           numeric,                   -- sub attempts / 15 min
  wins              int,
  losses            int,
  draws             int,
  detail_scraped_at timestamptz,               -- null = roster-only row
  updated_at        timestamptz not null default now()
);

create table if not exists ufc_fights (
  id            text primary key,              -- UFCStats fight id
  event_id      text not null,
  event_name    text,
  event_date    date,
  -- fighter1 is UFCStats' first-listed fighter (the winner on decided
  -- bouts); winner_id is authoritative, null on draw/NC.
  fighter1_id   text,
  fighter1_name text,
  fighter2_id   text,
  fighter2_name text,
  winner_id     text,
  result        text,                          -- win / draw / nc
  weight_class  text,                          -- as printed (carries 'Title' when shown)
  method        text,                          -- KO/TKO, SUB, U-DEC, S-DEC, ...
  round         int,
  fight_time    text,                          -- mm:ss within the round
  created_at    timestamptz not null default now()
);

create index if not exists ufc_fights_event_date_idx on ufc_fights (event_date);
create index if not exists ufc_fights_f1_idx on ufc_fights (fighter1_id);
create index if not exists ufc_fights_f2_idx on ufc_fights (fighter2_id);
create index if not exists ufc_fighters_scraped_idx on ufc_fighters (detail_scraped_at);
