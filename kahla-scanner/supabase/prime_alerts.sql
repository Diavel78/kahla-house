-- prime_alerts — dedup state for Pick Bot "prime betting window" Telegram
-- alerts. One row per market we've already pinged about, so a game only
-- triggers a single "entering prime window" notification (the alert is
-- batched per-sport when a cluster of games crosses into the 90-120 min
-- pre-tip window). Written by /api/handicapper/prime-alert in app.py.
--
-- Idempotent. Run via kahla-scanner/scripts/run_sql.sh -f or the SQL editor.
create table if not exists prime_alerts (
    market_id   uuid primary key,
    sport       text,
    event_start timestamptz,
    alerted_at  timestamptz not null default now()
);

-- Cheap retention: nothing reads rows older than a day or two (the markets
-- are long over), so the endpoint opportunistically deletes stale rows.
-- Index keeps that delete + the freshness reads fast.
create index if not exists prime_alerts_alerted_at_idx on prime_alerts (alerted_at);
