-- 014_ufc_distance.sql — allow the Fight IQ duration family (UFC) to log
-- real picks. The "goes the distance" prop uses market_type='distance'
-- (side yes/no); UFC round over/unders ride the existing 'total' type.
-- UFC non-ML picks stay pending in the resolver (manual settle), so no
-- auto-grade path is needed. Idempotent.
ALTER TABLE bot_picks DROP CONSTRAINT IF EXISTS bot_picks_market_type_check;
ALTER TABLE bot_picks ADD CONSTRAINT bot_picks_market_type_check
  CHECK (market_type = ANY (ARRAY['moneyline','spread','total','nrfi','distance']));
