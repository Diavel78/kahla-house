-- 018: one pending machine pick per Polymarket slug — FOOTBALL lanes.
--
-- The tenst-ga twin-pick incident (Sep 2-3 2026): _autobet_execute's
-- pick-insert retry (added for the wa-gercol-gte2 orphan) re-inserts when
-- the first insert times out client-side but actually committed — twin
-- rows 0.8s apart carrying the SAME order_id. The scalp arm then placed
-- one ask PER PICK ROW per lap (its one-sell-per-slug check reads the
-- start-of-tick orders snapshot), stacking 4 identical 20-lot sells on a
-- 20-contract position — the over-sell / naked-short class.
--
-- The retry's own comment says it is safe because a unique index makes a
-- double-commit impossible; bot_picks_machine_slug_uniq covers
-- autobet/whiff_autobet/ou_trader/pmm_autolog but NOT the football
-- sources. This closes the gap: gridiron_autobet + fbprop_autobet,
-- pending-only, keyed on the slug alone (a slug is one market — two
-- machine picks on it are never legitimate; the reconcile/autolog DELETE
-- rows rather than settle them mid-flight, which frees the key for
-- legitimate re-bets).
--
-- Replaces bot_picks_gridiron_slug_uniq (created ad hoc Sep 3 during the
-- incident; this widens it to fbprop before that lane's first bet).
-- Idempotent. Applied to the live DB Sep 3 2026 via run_sql.sh.

drop index if exists bot_picks_gridiron_slug_uniq;
drop index if exists bot_picks_football_slug_uniq;
create unique index bot_picks_football_slug_uniq
    on bot_picks ((signal_blob->>'pmm_slug'))
    where status = 'pending'
      and signal_blob->>'source' in ('gridiron_autobet', 'fbprop_autobet');
