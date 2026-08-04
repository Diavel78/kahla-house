-- 017: venue money ledger gets its OWN column (Aug 3 2026).
-- poly_pnl lived in signal_blob for one evening; the fill tracker /
-- re-peg markers / autolog all do full-blob read-modify-write, so the
-- last writer silently dropped the ledger stamp (pending picks showed
-- null minutes after being stamped). A dedicated column has exactly one
-- writer (_poly_ledger_tick) and no race. Idempotent.
alter table bot_picks add column if not exists poly_pnl jsonb;
