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
