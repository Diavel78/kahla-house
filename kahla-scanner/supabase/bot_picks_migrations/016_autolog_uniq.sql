-- 016_autolog_uniq.sql — kill the autolog double-insert race (July 26 2026,
-- the "Padres bet doubled" incident). The autolog runs from TWO paths (the
-- cron reconcile route AND the fill-status compute), and its dedup is a
-- check-then-insert: two concurrent invocations both SELECTed no existing
-- pick for the same resting order and both inserted, 47ms apart. A partial
-- UNIQUE index makes the losing racer's insert fail (the autolog already
-- try/excepts inserts, so it degrades to a logged skip).
--
-- Scoped to AUTOLOG-created rows only — user-logged picks legitimately
-- repeat on (asked_by, market_id, market_type, side): the upgrade flow
-- logs DELTA rows and prop logs share ('prop', side) across different
-- props, so a blanket constraint would break them.
--
-- Requires existing autolog dups to be deleted first (done live before
-- this was applied). Idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS bot_picks_autolog_uniq
  ON bot_picks (asked_by, market_id, market_type, side)
  WHERE (signal_blob->>'source') IN ('pmm_autolog', 'kalshi_autolog');
