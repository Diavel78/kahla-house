-- 002_bid_ask.sql — spread capture (Aug 2026, the adverse-selection review).
--
-- The mid alone cannot price the spread: a stored 46 is indistinguishable
-- from a 45/47 book or a 40/52 book, so nothing historical could answer
-- "would the model's edge survive the take?" (measured on the Aug 2-9
-- autobet tape: the spread ate 2-4pp of every claimed prop edge, and the
-- placed-bet gate at the make was inflated by ~half the spread). From this
-- migration on, every snapshot row that is already written (dedup is
-- UNCHANGED — still keyed on mid-cent change; these columns just ride
-- along) also records the row's own side's best bid/ask in integer cents
-- (same orientation as `cents` — synthetic inverse sides carry the
-- inverted book). Nullable — Kalshi rows and legacy rows stay null.
alter table pm_snapshots   add column if not exists bid_c integer;
alter table pm_snapshots   add column if not exists ask_c integer;
alter table prop_snapshots add column if not exists bid_c integer;
alter table prop_snapshots add column if not exists ask_c integer;
