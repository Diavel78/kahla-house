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
-- Pinnacle's OWN two prices at the venue's rung (its alternate ladder), when it
-- lists that rung — the fair the chart draws without a push-rate conversion.
alter table line_tape add column if not exists rung numeric;     -- the Poly rung (home / over), else Kalshi's
alter table line_tape add column if not exists r_home numeric;   -- pin: home / over price AT the rung
alter table line_tape add column if not exists r_away numeric;   -- pin: away / under price at the mirror rung
create index if not exists line_tape_game_idx on line_tape (sport, event_name, at);
create index if not exists line_tape_at_idx on line_tape (at);
notify pgrst, 'reload schema';

-- THE LADDER TAPE (Oct 29 2026, Rob: "I need one chart… different lines in
-- different books"). Every rung each book lists near the line, one row per
-- rung per CHANGE (a rung the book stops listing writes a null row). Same
-- orientation rule: home / over. Pinnacle's alternate ladder every minute
-- (p_home / p_away American), Poly + Kalshi bid / ask cents on the touch
-- walk. The chart prices every book AT ONE LINE off this.
create table if not exists ladder_tape (
  id          bigserial primary key,
  at          timestamptz not null default now(),
  sport       text not null,
  event_name  text not null,
  src         text not null check (src in ('pin','poly','kalshi')),
  mt          text not null,          -- spread | puck | total
  line        numeric not null,       -- the rung, home / over orientation
  bid_c       numeric,
  ask_c       numeric,
  p_home      numeric,
  p_away      numeric
);
create index if not exists ladder_tape_game_idx on ladder_tape (sport, event_name, at);
create index if not exists ladder_tape_at_idx on ladder_tape (at);
notify pgrst, 'reload schema';
