-- THE HAND-ORDER QUEUE (Oct 3 2026 — Rob: "They SHOULD be routed through
-- the box… everything runs on the box"). Bet Sheets on Vercel writes one
-- row per order (create / cancel / repeg); the box's `handbets` lane claims
-- it atomically and places it from the house IP — Polymarket's order
-- endpoint geo-checks the sender and flags some Vercel egress IPs as a
-- "VPN or proxy" (one create in six, the Troy −9.5 night). Vercel only
-- places itself when no box claims the row within a few seconds (the
-- fallback), and it claims first, so a row can never run twice.
-- The box lane applies this file itself (psql) once per daemon boot.
create table if not exists hand_orders (
  id          bigserial primary key,
  created_at  timestamptz not null default now(),
  state       text not null default 'pending'
              check (state in ('pending','claimed','done','failed')),
  op          text not null,
  slug        text,
  synthetic   boolean not null default false,
  price_c     numeric,            -- null = join the touch at placement time
  contracts   integer,
  event_start timestamptz,
  order_id    text,
  pick_id     bigint,
  payload     jsonb,              -- the slip item + resolve + asked_by (the pick row is logged from it)
  result      jsonb,
  error       text,
  worker      text,               -- 'box' | 'vercel'
  claimed_at  timestamptz,
  done_at     timestamptz
);
create index if not exists hand_orders_state_idx on hand_orders (state, created_at);
-- Every op the executor runs (app._hand_order_execute) must be allowed here;
-- a new op the table refuses fails the slip at the queue write (Oct 7 2026:
-- 'rerung' shipped without it — "violates check constraint hand_orders_op_check").
-- Idempotent: the box re-applies this file once per daemon boot.
alter table hand_orders drop constraint if exists hand_orders_op_check;
alter table hand_orders add constraint hand_orders_op_check
  check (op in ('create','cancel','repeg','rerung'));
-- PostgREST caches the schema: a new table is invisible until it reloads.
notify pgrst, 'reload schema';
