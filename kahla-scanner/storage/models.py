"""Row dataclasses mirroring Supabase tables.

Trimmed to only the rows scrapers/odds_api.py writes: Market and
BookSnapshot. Cross-venue linkage fields (poly_market_id, dk_event_id,
fd_event_id, kalshi_ticker) are gone — they were used by the divergence/
Brier pipeline that's been retired. The columns still exist on the
`markets` Postgres table (nullable, no harm) but the Python dataclass
no longer carries them so we don't accidentally rely on them.
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
    status: str = "active"
    id: str | None = None

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["event_start"] = self.event_start.isoformat()
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class BookSnapshot:
    market_id: str
    book: str                    # 'PIN','DK','FD','MGM','CAE','HR','BOL'
    market_type: str             # 'moneyline','spread','total'
    side: str                    # 'home','away','over','under'
    price_american: int
    line: float | None = None
    implied_prob: float | None = None

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}
