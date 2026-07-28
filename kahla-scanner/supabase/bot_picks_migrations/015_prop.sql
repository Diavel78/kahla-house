-- 015_prop.sql — allow generic exchange PROP picks (July 2026 props
-- pipeline). market_type='prop' with side yes/no; the prop's identity
-- (question / venue / PMM slug / line) travels in signal_blob.prop and
-- query_text carries the question for display. Props stay pending in the
-- resolver (manual settle from the page/analytics), same posture as the
-- UFC method lane — exchange-resolution auto-grade is a later phase.
-- Idempotent.
ALTER TABLE bot_picks DROP CONSTRAINT IF EXISTS bot_picks_market_type_check;
ALTER TABLE bot_picks ADD CONSTRAINT bot_picks_market_type_check
  CHECK (market_type = ANY (ARRAY['moneyline','spread','total','nrfi','distance','prop']));
