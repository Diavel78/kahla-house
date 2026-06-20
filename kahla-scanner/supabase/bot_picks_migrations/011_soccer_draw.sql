-- 011: World Cup / soccer support — add 'draw' as a valid pick side (3-way
-- 1-X-2 markets: home win / draw / away win). market_type stays 'moneyline'
-- (the 3-outcome match-result market reuses it with three sides). Idempotent.

alter table bot_picks drop constraint if exists bot_picks_side_check;
alter table bot_picks add constraint bot_picks_side_check
  check (side = any (array['home','away','over','under','yes','no','draw']));
