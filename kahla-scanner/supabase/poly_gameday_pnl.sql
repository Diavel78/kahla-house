-- VENUE TRUTH per Arizona GAME DAY — the function the operating rules
-- named as THE SOURCE and that never actually existed until Aug 21 2026,
-- the night the gap finally bit: a Supabase disconnect silently nulled
-- the bot_picks-side compute, the API fell back to the tick summary's
-- CREDIT-TIME number, and the "Last night" card showed ~$76 for a night
-- that finished -$4.93.
--
-- Method (the documented one, paths verified against live payloads):
--   * ACTIVITY_TYPE_POSITION_RESOLUTION rows: afterPosition.realized is
--     the venue's CUMULATIVE P&L for that (market, leg) — cost, sells
--     and payout already netted; nothing for us to recompute.
--   * Dedup on (market slug, leg outcome, side) — the mirror stores ~90
--     identical copies of every resolution, and slug alone would collapse
--     a both-legs-held market into one side.
--   * SCALP EXITS (added Aug 27 2026, the night the scalp arm armed —
--     user, reading the day card: "Dashboard better learn how to track
--     sells, since bets to the end are going to be a thing of the past"):
--     a position fully closed by a sell emits NO resolution row, so a
--     scalped round trip vanished from the day math. Our-side SELL trades
--     now count as realized = trade.cost − trade.costBasis (both venue-
--     stamped real dollars — never trust trade price/realizedPnl on
--     shorts, the complement-pricing gotcha). Our side of a trade is
--     passive when isAggressor=false else aggressor, classified by
--     INTENT (SELL_LONG/SELL_SHORT), never by ORDER_SIDE — a BUY_SHORT
--     shows side=SELL (the Aug 26 tape gotcha).
--     DOUBLE-COUNT GUARD: sells on a (slug, outcome) that ALSO has a
--     resolution row are EXCLUDED — the resolution's cumulative realized
--     already nets them (the harvest-era behavior, preserved).
--   * Day = the AZ date of market.gameStartTime, NEVER the credit/trade
--     timestamp (a Friday game resolving 12:20am is Friday's game).
--
-- Apply: kahla-scanner/scripts/run_sql.sh -f kahla-scanner/supabase/poly_gameday_pnl.sql

create or replace function poly_gameday_pnl(p_days int default 3)
returns table (az_day date, realized_usd numeric, legs int)
language sql stable as $$
  with res0 as (
    select distinct on (
        payload->'positionResolution'->'market'->>'slug',
        payload->'positionResolution'->'afterPosition'
               ->'marketMetadata'->>'outcome',
        payload->'positionResolution'->>'side')
      (payload->'positionResolution'->'afterPosition'
              ->'realized'->>'value')::numeric as realized,
      (payload->'positionResolution'->'market'->>'gameStartTime') as gst,
      payload->'positionResolution'->'market'->>'slug' as slug,
      payload->'positionResolution'->'afterPosition'
             ->'marketMetadata'->>'outcome' as outcome,
      payload->'positionResolution'->>'side' as res_side
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
  ),
  -- ⚠ THE UNDEFINED-SIDE TWIN (caught by the user Aug 27 night, re-adding
  -- the day's legs by hand: "Check this again"): some settlements emit a
  -- SECOND resolution row with side=POSITION_RESOLUTION_SIDE_UNDEFINED
  -- carrying the SAME cumulative realized as the real LONG/SHORT row.
  -- With side in the dedup key it counted as a second leg — the Aug 27
  -- YRFIs each double-counted. Keep the UNDEFINED row ONLY when it is
  -- the sole row for its (slug, outcome).
  res as (
    select realized, gst, slug, outcome from res0 r0
    where r0.res_side <> 'POSITION_RESOLUTION_SIDE_UNDEFINED'
       or not exists (
         select 1 from res0 r1
         where r1.slug = r0.slug
           and r1.outcome is not distinct from r0.outcome
           and r1.res_side <> 'POSITION_RESOLUTION_SIDE_UNDEFINED')
  ),
  tr as (
    select distinct on (payload->'trade'->>'id')
      payload->'trade' as t,
      case when coalesce((payload->'trade'->>'isAggressor')::boolean, false)
           then payload->'trade'->'aggressor'
           else payload->'trade'->'passive' end as myord
    from poly_activities
    where type = 'ACTIVITY_TYPE_TRADE'
      and at > now() - ((p_days + 2) || ' days')::interval
    order by payload->'trade'->>'id', at desc
  ),
  scalp as (
    select
      ((t->'cost'->>'value')::numeric
       - (t->'costBasis'->>'value')::numeric) as realized,
      t->'market'->>'gameStartTime' as gst,
      t->>'marketSlug' as slug,
      myord->'marketMetadata'->>'outcome' as outcome
    from tr
    where myord->>'intent' like 'ORDER_INTENT_SELL%'
      and t->'costBasis'->>'value' is not null
      and t->'cost'->>'value' is not null
  ),
  scalp_only as (
    -- a leg that later resolves is fully covered by its resolution row
    select sc.realized, sc.gst from scalp sc
    where not exists (
      select 1 from res r
      where r.slug = sc.slug and r.outcome = sc.outcome)
  ),
  allrows as (
    select realized, gst from res
    union all
    select realized, gst from scalp_only
  )
  select (gst::timestamptz at time zone 'America/Phoenix')::date,
         round(sum(realized), 2),
         count(*)::int
  from allrows
  where gst is not null
  group by 1
  having (gst::timestamptz at time zone 'America/Phoenix')::date
         >= ((now() at time zone 'America/Phoenix')::date - p_days);
$$;
