-- pickbot_paperlog — the Pick Bot's COMPLETE suggestion log. Every
-- gate-cleared pick the bot would make for an upcoming game, captured
-- automatically over the 5h -> 1min pre-game window by /api/handicapper/
-- paperlog (cron-pinged), whether or not the user actually bets it.
--
-- Purpose: a clean 2-week dataset to review which suggestions / timing
-- windows actually win — separate from bot_picks (the user's REAL logged
-- picks) and from the retired Sharp Bot paper_bets. Mirrors bot_picks'
-- fields so the same resolver grades it and the buckets line up.
--
-- "Log on bet CHANGE": one row per (market_id, market_type) each time the
-- bot's suggested (side, units) changes — so a flip (Reds ML -> Reds -1.5)
-- or an upgrade (1u -> 3u) lands a NEW row, the old kept. Unchanged-bet
-- price drift is NOT a row (that lives in book_snapshots / pm_snapshots).
--
-- Idempotent. Run via kahla-scanner/scripts/run_sql.sh -f or the SQL editor.
create table if not exists pickbot_paperlog (
    id            bigserial primary key,
    market_id     uuid        not null,
    event_name    text,
    event_start   timestamptz,
    sport         text,
    market_type   text        not null,   -- moneyline/spread/total/nrfi
    side          text        not null,
    line          numeric,
    entry_price   integer,                -- american (PMM maker bid, else fair)
    units         numeric,
    confidence    text,
    timing_window text,                   -- late/prime/far
    prime_core    boolean,                -- 90-120 hammer sub-bucket
    starts_in_min integer,                -- exact minutes-to-tip at log time
    sharp_score   numeric,
    edge_pp       numeric,
    fair_american integer,
    gates_cleared boolean      default true,
    signal_blob   jsonb,
    logged_at     timestamptz not null default now(),
    -- grading (resolver fills, same to-WIN math as bot_picks)
    status        text        not null default 'pending',  -- pending/won/lost/push/void
    pnl_units     numeric,
    result_score  text,
    settled_at    timestamptz,
    clv_pp        numeric
);

-- Resolver scans pending by event_start.
create index if not exists pickbot_paperlog_pending_idx
    on pickbot_paperlog (status, event_start);
-- Dedup + per-game evolution reads.
create index if not exists pickbot_paperlog_dedup_idx
    on pickbot_paperlog (market_id, market_type, logged_at desc);
