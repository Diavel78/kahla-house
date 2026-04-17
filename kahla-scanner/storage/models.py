"""Row dataclasses mirroring Supabase tables."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Market:
    sport: str
    event_name: str
    event_start: datetime
    poly_market_id: str | None = None
    dk_event_id: str | None = None
    fd_event_id: str | None = None
    kalshi_ticker: str | None = None
    status: str = "active"
    id: str | None = None

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["event_start"] = self.event_start.isoformat()
        d = {k: v for k, v in d.items() if v is not None}
        return d


@dataclass
class BookSnapshot:
    market_id: str
    book: str                    # 'DK','FD'
    market_type: str             # 'moneyline','spread','total'
    side: str                    # 'home','away','over','under'
    price_american: int
    line: float | None = None
    implied_prob: float | None = None

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class PolyTick:
    market_id: str
    outcome: str
    price: float
    size: float
    tick_ts: datetime
    side: str | None = None

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["tick_ts"] = self.tick_ts.isoformat()
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class Signal:
    market_id: str
    signal_type: str             # 'divergence','rlm','arb'
    fade_side: str
    public_prob: float
    sharp_prob: float
    edge_pct: float
    liquidity_usd: float | None = None
    notes: dict[str, Any] = field(default_factory=dict)
    status: str = "open"

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None or k == "notes"}


@dataclass
class Subscriber:
    telegram_chat_id: int
    handle: str | None = None
    display_name: str | None = None
    sports: list[str] = field(
        default_factory=lambda: ["NFL", "CBB", "MLB", "NBA", "NHL", "UFC"]
    )
    min_edge_pct: float = 3.0
    min_liquidity_usd: float = 500.0
    quiet_hours_start: int | None = None
    quiet_hours_end: int | None = None
    timezone: str = "America/Phoenix"
    active: bool = True
    id: str | None = None
