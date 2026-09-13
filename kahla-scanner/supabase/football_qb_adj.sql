-- Football QB adjustment — one row per (sport, team): who throws the next
-- game vs who threw the games the rating was built on, in ANY/A, times a
-- fitted points-per-ANY/A. Read by app._gridiron_proj; written daily by
-- scripts/compute_football_qb.py on the batch lane after power_ratings.
-- Spec: docs/football-qb-adjust-spec.md
--
-- Apply (box): psql kahla -f kahla-scanner/supabase/football_qb_adj.sql
--              then: notify pgrst, 'reload schema';

create table if not exists football_qb_adj (
  sport         text not null,
  team          text not null,          -- game_results / power_ratings name
  starter_id    text,
  starter_name  text,
  starter_src   text,                   -- depth_chart | last_game | unknown
  starter_q     double precision,
  baseline_qb   text,                   -- top-weighted passer in the rated window
  baseline_q    double precision,
  adj_pts       double precision not null default 0,
  k             double precision,
  replacement_q double precision,
  detail        jsonb,
  computed_at   timestamptz not null default now(),
  primary key (sport, team)
);
