-- odds_ingest_runs — per-sport cron heartbeat for adaptive cadence.
--
-- The Odds API ingester used to fire every sport every 30 min, blindly,
-- 24/7. That wastes credits on far-out games where the line barely moves.
-- The cadence is now driven by time-to-nearest-game per sport (see
-- scrapers/odds_api.py:_should_fire_now). To gate whether a given tick
-- should call the API for a given sport, we need to know when we last
-- successfully called for that sport — that's what this table tracks.
--
-- One row per (sport, attempted-fetch). `status` records the outcome:
--   'ok'                — API call succeeded, snapshots may have been written
--   'skipped:overnight' — 10pm-7am MT blackout
--   'skipped:offseason' — no upcoming events in the next 7 days
--   'skipped:cadence'   — last fetch was too recent for current window
--   'skipped:no-games'  — no upcoming events at all
--   'error'             — API call attempted but failed
--
-- Idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS odds_ingest_runs (
  id           BIGSERIAL PRIMARY KEY,
  sport        TEXT NOT NULL,
  fetched_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  status       TEXT NOT NULL,
  cadence_min  INT,                       -- chosen cadence at this tick
  next_game_h  NUMERIC,                   -- hours to nearest upcoming game
  events       INT,                       -- events returned by API (when status='ok')
  snapshots    INT,                       -- rows written to book_snapshots
  detail       TEXT                       -- optional free-form note
);

-- Index supports the "what was my last successful fire for this sport"
-- query that runs once per sport per cron tick. status='ok' filter is
-- the hot path — skipped rows are cheap heartbeat noise we never read
-- back beyond ad-hoc inspection.
CREATE INDEX IF NOT EXISTS idx_odds_ingest_runs_sport_ok
  ON odds_ingest_runs (sport, fetched_at DESC)
  WHERE status = 'ok';

CREATE INDEX IF NOT EXISTS idx_odds_ingest_runs_sport_time
  ON odds_ingest_runs (sport, fetched_at DESC);
