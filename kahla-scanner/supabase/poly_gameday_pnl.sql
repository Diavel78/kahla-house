-- RESOLUTION P&L per Arizona GAME DAY — the set-based half of the day math.
--
-- ⚠ REWRITTEN Aug 28 2026, after the day card read +$8.21 for a night the
-- dashboard's own Closed Positions table totalled +$4.39.
--
-- THE BUG THIS REPLACES: every prior version summed
-- `afterPosition.realized`, believing it to be the venue's cumulative P&L
-- per leg. It is populated LATE. On Aug 27 it sat at 0.0000 for 7 of 11
-- legs — $33.10 of settled cost reported as nothing — while the same legs
-- were −$5.20, −$5.00, −$3.80 … in Closed Positions. Aug 20-26 looked fine
-- only because a backwards backfill had re-read those rows; the newest day,
-- the one anybody actually looks at, was always the broken one.
--
-- THE MATH IS NOW THE ONE `app.py:parse_activities` HAS USED SINCE THE
-- DASHBOARD WAS BUILT — computed from beforePosition, never read off a
-- venue P&L field:
--     qty  = |beforePosition.netPosition|
--     cost =  beforePosition.cost
--     won  = (held long AND side LONG) OR (held short AND side SHORT)
--     pnl  = qty - cost   if won   else   -cost
-- Verified to the cent against 11 of 11 Aug 27 legs.
--
-- ⚠ NEVER trust `afterPosition.realized`, `trade.costBasis` or
-- `trade.realizedPnl` (gotchas #7/#8). All three are complement-poisoned on
-- SHORT positions or filled in late; each has now produced a wrong number
-- on this dashboard.
--
-- SELLS ARE NOT HERE. A scalp exit's P&L needs a running average cost that
-- is order-dependent (a sell leaves the average alone; a later buy re-blends
-- it against the reduced quantity), which no window function expresses.
-- `app.py:_venue_day_map` adds the sell half in Python and is the only
-- thing that should call this. Resolutions bucket on GAME day; sells bucket
-- on TRADE day — see that function for why each needs its own clock.
--
-- Apply: kahla-scanner/scripts/run_sql.sh -f kahla-scanner/supabase/poly_gameday_pnl.sql

create or replace function poly_gameday_pnl(p_days int default 3)
returns table (az_day date, realized_usd numeric, legs int)
language sql stable as $$
  with r0 as (
    -- The mirror keeps ~19 copies of every resolution: they carry no id, so
    -- `_act_key` hashes the whole activity and a nested market `updatedAt`
    -- mints a fresh row on every sync pass. The live feed returns one.
    select distinct on (
        payload->'positionResolution'->'market'->>'slug',
        payload->'positionResolution'->>'side',
        payload->'positionResolution'->'beforePosition'->>'netPosition',
        payload->'positionResolution'->'beforePosition'->'cost'->>'value')
      payload->'positionResolution'->'market'->>'slug' as slug,
      payload->'positionResolution'->>'side' as side,
      (payload->'positionResolution'->'beforePosition'
              ->>'netPosition')::numeric as net,
      (payload->'positionResolution'->'beforePosition'
              ->'cost'->>'value')::numeric as cost,
      coalesce(payload->'positionResolution'->'market'->>'gameStartTime',
               payload->'positionResolution'->>'updateTime') as gst
    from poly_activities
    where type = 'ACTIVITY_TYPE_POSITION_RESOLUTION'
      -- credits lag game start by hours, never days
      and at > now() - ((p_days + 2) || ' days')::interval
    order by payload->'positionResolution'->'market'->>'slug',
             payload->'positionResolution'->>'side',
             payload->'positionResolution'->'beforePosition'->>'netPosition',
             payload->'positionResolution'->'beforePosition'->'cost'->>'value',
             at desc
  ),
  -- ⚠ THE UNDEFINED-SIDE TWIN: some settlements emit a SECOND row carrying
  -- the same leg (same slug/qty/cost) with side=UNDEFINED. It doubles the
  -- day, and because UNDEFINED satisfies neither win test it books a
  -- WINNING leg as a full loss. Keep it only when it is the sole record.
  r as (
    select * from r0 a
    where a.side <> 'POSITION_RESOLUTION_SIDE_UNDEFINED'
       or not exists (
         select 1 from r0 b
         where b.slug = a.slug and b.net = a.net and b.cost = a.cost
           and b.side <> 'POSITION_RESOLUTION_SIDE_UNDEFINED')
  )
  select (gst::timestamptz at time zone 'America/Phoenix')::date,
         round(sum(
           case when (net > 0 and a.side in ('POSITION_RESOLUTION_SIDE_LONG',
                                             'POSITION_RESOLUTION_SIDE_YES'))
                  or (net < 0 and a.side in ('POSITION_RESOLUTION_SIDE_SHORT',
                                             'POSITION_RESOLUTION_SIDE_NO'))
                then abs(net) - cost
                else -cost end), 2),
         count(*)::int
  from r a
  where gst is not null
  group by 1
  having (gst::timestamptz at time zone 'America/Phoenix')::date
         >= ((now() at time zone 'America/Phoenix')::date - p_days);
$$;
