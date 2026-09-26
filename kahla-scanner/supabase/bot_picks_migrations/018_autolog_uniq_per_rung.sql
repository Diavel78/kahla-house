-- 018 — the autolog's anti-double-log index is PER RUNG, not per side (Sep 25 2026)
--
-- 016 keyed bot_picks_autolog_uniq on (asked_by, market_id, market_type, side)
-- for autolog-created rows. That blocks the race it was built for (the same
-- bet logged twice 47ms apart) — but it ALSO blocks booking a second lot on
-- the same side at a DIFFERENT rung, which is exactly the same-side-double
-- inventory the machine most needs to see: six 17-20 contract lots from
-- Sep 14-15 (COL@BAY, OKL@GA, WF@LOU, CIN@PIT, HOU@IND) sat with no pick
-- because an autolog pick already existed on that game/side at another
-- rung; every adoption attempt died 'duplicate key' at DEBUG level.
-- The slug (the rung) joins the key. Same slug twice is still refused.
DROP INDEX IF EXISTS bot_picks_autolog_uniq;
CREATE UNIQUE INDEX bot_picks_autolog_uniq
    ON public.bot_picks (asked_by, market_id, market_type, side, (signal_blob->>'pmm_slug'))
    WHERE (signal_blob->>'source') IN ('pmm_autolog', 'kalshi_autolog');
