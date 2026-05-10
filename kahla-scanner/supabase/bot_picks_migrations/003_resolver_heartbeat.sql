-- bot_picks migration 003 — resolver heartbeat table.
--
-- bot_picks_resolver runs every 30 min via scanner-poll.yml with
-- continue-on-error: true, which means any crash (bad env var, ESPN
-- downtime, supabase rejecting an update) is INVISIBLE — the workflow
-- shows green and we have no idea grading isn't happening.
--
-- This table is the heartbeat. Resolver writes one row per run with
-- the counts and (if it crashed) the error. /api/handicapper reads
-- the latest row so the page can show "last graded run Nm ago" + the
-- breakdown. If runs stop appearing, we know the script is dead.
--
-- Idempotent: `if not exists` on table + indexes.

create table if not exists resolver_runs (
  id          bigserial primary key,
  run_at      timestamptz not null default now(),
  kind        text not null,        -- 'bot_picks' | 'paper_bets'
  picks_seen  integer,
  won         integer,
  lost        integer,
  push        integer,
  unmatched   integer,
  not_final   integer,
  unsupported integer,
  took_ms     integer,
  error       text                  -- null on success, full traceback on crash
);

create index if not exists idx_resolver_runs_kind_run_at
  on resolver_runs (kind, run_at desc);
