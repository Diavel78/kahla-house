-- THE PAIR MACHINE LOOKS FIRST (Rob, Sep 20 2026: "the Ferrari doesn't stop
-- seating football, it just doesn't get first pick… whatever it refuses goes
-- to the Ferrari"). One row per (game, market) the seeder looked at and did
-- NOT seat, with why. The football executor defers on a ladder until a row
-- exists here (or a pair owns it), then bets it exactly as before.
create table if not exists pair_declined (
  market_id   uuid not null,
  market_type text not null,
  reason      text,
  at          timestamptz not null default now(),
  primary key (market_id, market_type)
);
