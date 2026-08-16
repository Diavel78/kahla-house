-- Polymarket liquidity-incentive (maker reward) mirror.
--
-- Aug 16 2026. Until now every reward figure in this system was inferred
-- from the activities feed, which shows only PAID lump credits 5-7 business
-- days after the fact. The venue publishes the truth at
-- /v1/incentives/earnings (per market, per ET day, with PENDING accrual)
-- and /v1/incentives (per-market program config). These two tables mirror
-- both so reward-per-bet is a JOIN, not an inference.
--
-- Apply:  kahla-scanner/scripts/run_sql.sh -f kahla-scanner/supabase/poly_incentives.sql

-- ---------------------------------------------------------------- earnings
-- One row per (market, ET day, program type). status is PAID / PENDING /
-- SKIPPED, where SKIPPED means the day never reached the $1.00 minimum
-- payout and the money was forfeited.
--
-- earn_date is EASTERN TIME per the venue's own definition. It is NOT the
-- Arizona day this project reports in. Convert deliberately at read time;
-- do not assume they line up.
create table if not exists poly_incentive_earnings (
    market_slug   text        not null,
    earn_date     date        not null,
    program_type  text        not null default 'liquidityProgram',
    status        text        not null,
    reward        numeric(12,4) not null default 0,
    -- set when the venue returned >1 record for this key and the sync
    -- SUMMED them; a silent collapse here would undercount real money
    merged_rows   int         not null default 1,
    first_seen_at timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    primary key (market_slug, earn_date, program_type)
);

create index if not exists poly_inc_earn_date_idx
    on poly_incentive_earnings (earn_date desc);
create index if not exists poly_inc_earn_status_idx
    on poly_incentive_earnings (status);

-- ---------------------------------------------------------------- programs
-- Per-market program configuration: the reward pool, the discount factor,
-- and TARGET SIZE — the aggregate resting size (across ALL participants)
-- a side needs before anyone on it scores. The exchange walks from the best
-- price outward accumulating orders until target_size is met; everything
-- past that point scores zero no matter how close to the touch it sits.
create table if not exists poly_incentive_programs (
    market_slug     text        not null,
    program_id      text        not null,
    program_type    text,
    period          text,          -- pre_day / day_of / live / daily_event
    starts_at       timestamptz,
    ends_at         timestamptz,
    reward_pool     numeric(12,4),
    discount_factor numeric(8,4),
    target_size     numeric(14,2),
    status          text,          -- active / closed
    category        text,
    subcategory     text,
    event_start     timestamptz,
    synced_at       timestamptz not null default now(),
    primary key (market_slug, program_id)
);

create index if not exists poly_inc_prog_status_idx
    on poly_incentive_programs (status);
create index if not exists poly_inc_prog_slug_idx
    on poly_incentive_programs (market_slug);

-- sync heartbeat, so a dead feed is visible rather than silently stale
create table if not exists poly_incentive_sync (
    id          int primary key default 1,
    ran_at      timestamptz,
    earnings_rows int,
    program_rows  int,
    note        text
);
