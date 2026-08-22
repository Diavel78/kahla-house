-- FOOTBALL WEEKLY GAME SHEETS (Aug 2026) — see docs/football-sheet-runbook.md
--
-- football_sheets: one row per game per week. The Actions assembly job
-- (football-sheets-data.yml → scripts/football_sheet_data.py) writes
-- data_blob; the Monday generation session writes sheet_md (narrative
-- included); the Friday session writes friday_md from the diff the Friday
-- assembly run stamps into data_blob->'friday'.
--
-- week_key = the AZ date (YYYY-MM-DD) of the Monday the week's sheets are
-- built for. Friday updates reuse the same week_key.
--
-- Idempotent. Apply via kahla-scanner/scripts/run_sql.sh -f <this file>.

create table if not exists football_sheets (
    id            bigserial primary key,
    week_key      text        not null,
    sport         text        not null check (sport in ('NFL','NCAAF')),
    market_id     uuid,                 -- markets row when the spine has one
    espn_id       text,                 -- ESPN event id (stable join for diffs)
    event_name    text        not null, -- "Away Team @ Home Team"
    event_start   timestamptz not null,
    tier          text        not null default 'data' check (tier in ('deep','data')),
    data_blob     jsonb,                -- mechanical assembly (model/lines/splits/injuries/…)
    sheet_md      text,                 -- final per-game sheet markdown (Monday session)
    friday_md     text,                 -- Friday changes note for this game
    data_built_at timestamptz,
    published_at  timestamptz,
    friday_published_at timestamptz,
    created_at    timestamptz not null default now(),
    unique (week_key, sport, event_name)
);

create index if not exists football_sheets_week_idx
    on football_sheets (week_key, sport, event_start);

-- One row per (week, sport): the published artifacts + rollup stats.
create table if not exists football_sheet_weeks (
    id            bigserial primary key,
    week_key      text        not null,
    sport         text        not null check (sport in ('NFL','NCAAF')),
    games         integer,
    deep_games    integer,
    pdf_path      text,                 -- storage path of the Monday sheet pack
    friday_pdf_path text,               -- storage path of the Friday changes pack
    published_at  timestamptz,
    friday_published_at timestamptz,
    stats         jsonb,
    created_at    timestamptz not null default now(),
    unique (week_key, sport)
);
