"""Row dataclasses mirroring Supabase tables.

Trimmed to only the rows owls.py writes: Market and BookSnapshot.
PolyTick / Signal / Subscriber dataclasses were removed when the
divergence/Brier pipeline was retired — the underlying tables are no
longer written to. The tables themselves still exist in Supabase for
historical inspection but can be dropped manually if you want.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
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
    book: str                    # 'PIN','CIR','DK','FD','MGM','CAE','HR','NVG','POLY'
    market_type: str             # 'moneyline','spread','total'
    side: str                    # 'home','away','over','under'
    price_american: int
    line: float | None = None
    implied_prob: float | None = None

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}
