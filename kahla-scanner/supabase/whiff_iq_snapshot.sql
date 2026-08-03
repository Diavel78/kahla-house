-- Whiff IQ snapshot — the serialized pitcher-K model state Flask prices
-- live K props against (the diamond_iq_snapshot pattern). One row, id=1.
-- state jsonb: {"lg": {k_bf, bf, bf_sd},
--               "pitchers": {name: {"team": tricode,
--                                   "hist": [[iso_date, bf, k], ...]}},
--               "teams": {tricode: [[iso_date, pa, k], ...]}}
-- Written daily by scripts/compute_whiff_iq.py (whiff-iq-compute.yml).
create table if not exists whiff_iq_snapshot (
  id        integer primary key default 1,
  built_at  timestamptz not null default now(),
  engine    text not null,
  state     jsonb not null
);
