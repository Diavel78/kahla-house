-- THE LINE TAPE (Oct 29 2026, Rob: "let's read the market, like a stock
-- broker"). Price history for every upcoming sheet game, one row per CHANGE:
--   src 'pin'    — Pinnacle's main line + both prices (American), read every
--                  minute off Pinnacle's own feed (free).
--   src 'poly' / 'kalshi' — the venue bid/ask at the sheet's rung, in cents,
--                  read by the box's sheet-touch walk (30 min far out, 10 min
--                  inside 24h, 3 min inside 3h).
-- Every row is stored in ONE orientation per market: the HOME side for
-- ml / spread / puck, the OVER for a total (an away / under read is mirrored:
-- bid = 100 − ask, line negated on a spread). The page mirrors back.
-- The box applies this file itself (psql) once per daemon boot.
create table if not exists line_tape (
  id          bigserial primary key,
  at          timestamptz not null default now(),
  sport       text not null,
  event_name  text not null,
  market_id   text,
  event_start timestamptz,
  src         text not null check (src in ('pin','poly','kalshi')),
  mt          text not null,          -- ml | spread | puck | total (the sheet's pick key)
  line        numeric,                -- home spread / over total; null on ml
  bid_c       numeric,                -- venues: home/over bid, cents
  ask_c       numeric,                -- venues: home/over ask, cents
  p_home      numeric,                -- pin: home / over price, American
  p_away      numeric                 -- pin: away / under price, American
);
create index if not exists line_tape_game_idx on line_tape (sport, event_name, at);
create index if not exists line_tape_at_idx on line_tape (at);
notify pgrst, 'reload schema';
