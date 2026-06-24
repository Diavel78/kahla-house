-- 012_closing_vsin.sql — per-pick CLOSING VSiN read.
--
-- The resolver stamps each graded pick with the last pre-first-pitch Circa +
-- DraftKings handle%/bets% (both sides of the pick's market) pulled from
-- vsin_snapshots — the same way clv_pp pulls the closing line. Paired with the
-- bet-time read (signal_blob.vsin) it shows whether sharp money hit Circa late
-- on the pick's side: the tuning signal "when are sharps hitting Circa?"
--
-- jsonb shape: {"circa": {"home": {"handle":54,"bets":48,"line":null}, ...},
--               "draftkings": {...}, "captured_at": "..."}
--
-- Applied to bot_picks AND pickbot_paperlog (both grade through the resolver).
-- Idempotent. Apply: run_sql.sh -f kahla-scanner/supabase/bot_picks_migrations/012_closing_vsin.sql
alter table bot_picks       add column if not exists closing_vsin jsonb;
alter table pickbot_paperlog add column if not exists closing_vsin jsonb;
