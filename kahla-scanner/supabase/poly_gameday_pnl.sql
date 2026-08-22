-- VENUE TRUTH per Arizona GAME DAY — the function the operating rules
-- named as THE SOURCE and that never actually existed until Aug 21 2026,
-- the night the gap finally bit: a Supabase disconnect silently nulled
-- the bot_picks-side compute, the API fell back to the tick summary's
-- CREDIT-TIME number, and the "Last night" card showed ~$76 for a night
-- that finished -$4.93.
--
-- Method (the documented one, paths verified against a live payload):
--   * ACTIVITY_TYPE_POSITION_RESOLUTION rows only. afterPosition.realized
--     is the venue's CUMULATIVE P&L for that (market, leg) — cost, sells
--     and payout already netted; nothing for us to recompute.
--   * Dedup on (market slug, leg outcome, side) — the mirror stores ~90
--     identical copies of every resolution, and slug alone would collapse
--     a both-legs-held market into one side.
--   * Day = the AZ date of market.gameStartTime, NEVER the credit
--     timestamp (a Friday game resolving 12:20am is Friday's game).
--
-- Known gap, benign today: a position fully closed before resolution
-- emits no resolution row; with the harvest dead, everything rides to
-- resolution.
--
-- Apply: kahla-scanner/scripts/run_sql.sh -f kahla-scanner/supabase/poly_gameday_pnl.sql

create or replace function poly_gameday_pnl(p_days int default 3)
returns table (az_day date, realized_usd numeric, legs int)
language sql stable as $$
  with res as (
    select distinct on (
        payload->'positionResolution'->'market'->>'slug',
        payload->'positionResolution'->'afterPosition'
               ->'marketMetadata'->>'outcome',
        payload->'positionResolution'->>'side')
      (payload->'positionResolution'->'afterPosition'
              ->'realized'->>'value')::numeric as realized,
      (payload->'positionResolution'->'market'->>'gameStartTime') as gst
    from poly_activities
    where type = 'ACTIVITY_TYPE_POSITION_RESOLUTION'
      -- credits lag game start by hours, never days: a 2-day cushion on
      -- the credit clock covers every game day inside p_days.
      and at > now() - ((p_days + 2) || ' days')::interval
    order by payload->'positionResolution'->'market'->>'slug',
             payload->'positionResolution'->'afterPosition'
                    ->'marketMetadata'->>'outcome',
             payload->'positionResolution'->>'side',
             at desc
  )
  select (gst::timestamptz at time zone 'America/Phoenix')::date,
         round(sum(realized), 2),
         count(*)::int
  from res
  where gst is not null
  group by 1
  having (gst::timestamptz at time zone 'America/Phoenix')::date
         >= ((now() at time zone 'America/Phoenix')::date - p_days);
$$;
