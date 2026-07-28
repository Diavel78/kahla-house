-- poly_trades — the Polymarket TRADE TAPE (whale/order-flow signal, July 2026).
--
-- Origin: the user considered enviodev/track-poly-trades (an Envio HyperSync
-- demo streaming OrderFilled events off Polygon) and chose the cron-friendly
-- path instead: poll Polymarket's public data API trade tape from the
-- existing pm-snapshot tick — same information, zero blockchain infra.
--
-- One row per BIG taker fill (>= $500 notional) on a market belonging to one
-- of OUR upcoming games, matched by team names/tricodes. This is the GLOBAL
-- polymarket.com book's tape — a SIGNAL source (who is hammering a side
-- before the price moves), NOT a record of the user's own orders.
--
-- Earn-in posture: recorded + shown (dossier "Big Trades" card), NOT a
-- scoring/sizing input. Reviews join this table to pickbot_paperlog by
-- (market_id, time window) — no per-pick stamping needed.
--
-- Idempotent. Run via kahla-scanner/scripts/run_sql.sh -f or the SQL editor.

create table if not exists poly_trades (
    id           bigserial primary key,
    trade_key    text not null unique,   -- sha1(tx|asset|wallet|side|size|price|ts) dedup
    market_id    uuid not null,          -- our markets.id (the game)
    sport        text,
    title        text,                   -- the market question ("Padres to win…")
    slug         text,                   -- tape market slug
    event_slug   text,
    outcome      text,                   -- outcome name the trade bought/sold
    side         text,                   -- BUY | SELL (taker side)
    price_cents  numeric,                -- fill price, cents
    size         numeric,                -- shares
    notional_usd numeric,                -- size × price
    wallet       text,
    trader       text,                   -- display name / pseudonym when present
    tx_hash      text,
    traded_at    timestamptz,
    captured_at  timestamptz not null default now()
);

create index if not exists poly_trades_game_idx
    on poly_trades (market_id, traded_at desc);
create index if not exists poly_trades_captured_idx
    on poly_trades (captured_at);
