-- hedge_pairs (Sep 14 2026): written by the Lambo (kahla-scanner/scripts/gemini_hedge.py) whenever a
-- Poly slug has a HELD Gemini hedge leg; read by the Ferrari's scalp arm (app._hedged_ask_off) so that
-- inside T-60 a paired slug carries NO ask — Rob: "they both cancel sell, maintain hedge."
create table if not exists hedge_pairs (
  poly_slug text primary key,
  gemini_symbol text not null,
  paired_qty numeric not null default 0,
  poly_filled numeric, gemini_held numeric,
  kickoff timestamptz,
  updated_at timestamptz not null default now()
);

-- Sep 15 2026 (the GB–NYJ re-rung): the row now LIVES while the Lambo holds its Gemini leg (paired
-- or not) and carries the leg's geometry so the Ferrari's seat rule can enforce Rob's re-rung law —
-- dog only up, favorite only down (app._hedge_rung_ok), the 100.5 pair cap on a re-rung
-- (app._hedge_cap_c), and a HELD split-rung pair (middle) rides with NO ask on either venue.
alter table hedge_pairs
  add column if not exists game_prefix text,      -- 'asc-nfl-gb-nyj-2026-09-20'
  add column if not exists gem_side text,         -- side the Gemini leg is long, in Poly's frame: 'away'|'home'
  add column if not exists gem_rv numeric,        -- that leg's rung as the HOME line (GB −5.5 → 5.5)
  add column if not exists gem_cost numeric,      -- Gemini avg cost of the held leg
  add column if not exists poly_rv numeric,       -- the Poly leg's rung (home line), null when Poly has no leg
  add column if not exists middle boolean not null default false;   -- paired AND rungs differ → ride, no sells
