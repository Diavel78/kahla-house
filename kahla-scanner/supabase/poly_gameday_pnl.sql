-- THE VENUE IS THE SCOREBOARD.
--
-- "it's graded when Poly pays me."  Polymarket's own POSITION_RESOLUTION
-- activity is the settlement statement: afterPosition.realized is the
-- CUMULATIVE realized P&L for that (market, leg) — cost paid, harvest
-- sells, and the resolution payout, already netted by the venue. There is
-- nothing for us to recompute and nothing of ours to wait on.
--
-- Why this is a database function and not a Python loop: the activities
-- mirror re-reports every resolution on every feed walk (~90 identical
-- copies per position, each with its own row key), so a naive read of four
-- days is ~7,600 rows of ~8 KB payload. Deduping in Postgres and returning
-- one row per day costs a few KB. Egress from this table has taken the
-- site down once already.
--
-- DEDUP KEY is (marketSlug, outcome). The slug alone is wrong: holding both
-- legs of one market is a real state (the LAD@CHC flip), and collapsing
-- them loses a side.
--
-- DAY is the ARIZONA date of gameStartTime — the CLOCK RULE. Never the
-- payout timestamp: a Friday night game resolving at 12:20am is Friday's
-- result, and bucketing on credit time once put +$7.22 on a Saturday
-- morning with nothing yet played. eventSlug's date is the fallback; on
-- 217 markets the two never disagreed.
--
-- KNOWN GAP: a position fully closed before the market resolves emits no
-- resolution row, so its P&L would be invisible here. Today that cannot
-- happen — the harvest always rides at least one contract (min_held), and
-- 109 of 109 recent resolutions were still long at settlement. If a lane
-- ever sells a position to zero, this function needs the sell side too.
create or replace function poly_gameday_pnl(p_days integer default 8)
returns table (game_day date, legs integer, pnl numeric)
language sql stable as $$
  with res as (
    select distinct on (
             payload->'positionResolution'->>'marketSlug',
             payload->'positionResolution'->'beforePosition'
                    ->'marketMetadata'->>'outcome')
           coalesce(
             ((payload->'positionResolution'->'market'->>'gameStartTime')
                ::timestamptz at time zone 'America/Phoenix')::date,
             substring(payload->'positionResolution'->'afterPosition'
                              ->'marketMetadata'->>'eventSlug'
                       from '(\d{4}-\d{2}-\d{2})')::date
           ) as d,
           (payload->'positionResolution'->'afterPosition'
                   ->'realized'->>'value')::numeric as realized
      from poly_activities
     where type = 'ACTIVITY_TYPE_POSITION_RESOLUTION'
       -- +3 days of slack: `at` is when the venue credited, which trails
       -- the game day it belongs to.
       and at > now() - make_interval(days => greatest(p_days, 1) + 3)
     order by payload->'positionResolution'->>'marketSlug',
              payload->'positionResolution'->'beforePosition'
                     ->'marketMetadata'->>'outcome',
              at desc)
  select d, count(*)::integer, round(sum(realized), 2)
    from res
   where d is not null
   group by d
   order by d desc;
$$;
