-- Cross-confirm trigger state (June 2026). Step 3 of the PMM+Kalshi
-- detector: when Polymarket AND Kalshi both drift >=1c the same direction
-- vs a per-game baseline, fire an on-demand paid Pinnacle (Odds API) pull
-- instead of polling far-out games on a blind clock.
--
-- Two tables:
--   xconfirm_state      — per-game baseline cents (home side) + last trigger
--   odds_pull_requests  — Flask->cron signal (one row per sport): a fresh
--                         requested_at the cron consumes on its next tick
--                         (if >=5min since the last pull, >3h out, not blackout)
--
-- Idempotent. Run via kahla-scanner/scripts/run_sql.sh -f or the SQL editor.

create table if not exists xconfirm_state (
    market_id       uuid primary key,
    sport           text,
    pmm_base        integer,        -- home-side mid cents at baseline
    kalshi_base     integer,
    baseline_at     timestamptz,
    last_trigger_at timestamptz
);

create table if not exists odds_pull_requests (
    sport        text primary key,
    requested_at timestamptz not null default now(),
    consumed_at  timestamptz,
    reason       text
);
