"""Round 2: dump real markets/events/sports payloads so we can design the
SDK-based discover path.

Read-only. Run from kahla-scanner/ with the venv activated:

    source venv/bin/activate
    python scripts/probe_markets.py

Prints:
  - sports.list()                    -> what sport keys exist
  - markets.list() unfiltered        -> count + first 3 market shapes
  - markets.list(sport=<each sport>) -> count per sport
  - events.list() unfiltered         -> count + first 3 event shapes
  - MarketsListParams field names    -> what filters the SDK accepts
"""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import config  # noqa: E402


def _safe_dump(obj, limit: int = 1500) -> str:
    try:
        # pydantic v2 models
        if hasattr(obj, "model_dump"):
            obj = obj.model_dump()
        s = json.dumps(obj, indent=2, default=str)
    except Exception as e:
        s = f"<unserializable: {e}>"
    return s if len(s) <= limit else s[:limit] + "\n...(truncated)"


def _shape(obj) -> str:
    if isinstance(obj, dict):
        return f"dict(keys={sorted(obj.keys())})"
    if isinstance(obj, list):
        return f"list(len={len(obj)})"
    if hasattr(obj, "model_dump"):
        d = obj.model_dump()
        if isinstance(d, dict):
            return f"{type(obj).__name__}(keys={sorted(d.keys())})"
    return type(obj).__name__


def main() -> int:
    from polymarket_us import PolymarketUS
    from polymarket_us.types.markets import MarketsListParams
    from polymarket_us.types.events import EventsListParams

    c = PolymarketUS(key_id=config.poly_api_key_id, secret_key=config.poly_api_secret)

    print("== MarketsListParams fields ==")
    try:
        print(sorted(MarketsListParams.model_fields.keys()))
    except Exception as e:
        print(f"(couldn't introspect: {e})")
        print(inspect.getsource(MarketsListParams))

    print("\n== EventsListParams fields ==")
    try:
        print(sorted(EventsListParams.model_fields.keys()))
    except Exception as e:
        print(f"(couldn't introspect: {e})")

    print("\n== sports.list() ==")
    try:
        sresp = c.sports.list()
        print(f"shape: {_shape(sresp)}")
        print(_safe_dump(sresp))
    except Exception as e:
        print(f"failed: {e}")
        sresp = None

    # Extract usable sport keys for per-sport filtering.
    sport_keys: list[str] = []
    try:
        if hasattr(sresp, "model_dump"):
            sresp = sresp.model_dump()
        if isinstance(sresp, dict):
            sports_list = sresp.get("sports") or []
            for s in sports_list:
                for k in ("key", "slug", "id", "name"):
                    if k in s:
                        sport_keys.append(str(s[k]))
                        break
    except Exception:
        pass
    print(f"\nExtracted sport keys: {sport_keys}")

    print("\n== markets.list() (unfiltered) ==")
    try:
        mresp = c.markets.list()
        data = mresp.model_dump() if hasattr(mresp, "model_dump") else mresp
        markets = data.get("markets") if isinstance(data, dict) else None
        print(f"shape: {_shape(mresp)}  markets count: {len(markets) if markets else 0}")
        if markets:
            print("\nFirst market (full dump):")
            print(_safe_dump(markets[0]))
            print("\nAll top-level keys across first 3 markets:")
            for m in markets[:3]:
                print(f"  {sorted(m.keys())}")
            print("\nSample slugs + sports:")
            for m in markets[:12]:
                sport = m.get("sport") or m.get("category") or m.get("league") or "?"
                print(f"  {sport:>12}  {m.get('slug', '?')}   {m.get('status', '')}")
    except Exception as e:
        print(f"failed: {e}")

    print("\n== markets.list(sport=<each>) — counts ==")
    for key in sport_keys or ["mlb", "nba", "nhl", "nfl", "cbb", "ncaaf"]:
        for field in ("sport", "sport_key", "sportKey", "league"):
            try:
                mresp = c.markets.list(MarketsListParams(**{field: key}))
                data = mresp.model_dump() if hasattr(mresp, "model_dump") else mresp
                n = len((data or {}).get("markets") or [])
                print(f"  sport={key!r:12} via {field!r:10}: {n} markets")
                break  # stop trying other fields once one works
            except TypeError:
                continue
            except Exception as e:
                print(f"  sport={key!r:12} via {field!r:10}: {type(e).__name__} {e}")
                break

    print("\n== events.list() (unfiltered) ==")
    try:
        eresp = c.events.list()
        data = eresp.model_dump() if hasattr(eresp, "model_dump") else eresp
        events = data.get("events") if isinstance(data, dict) else None
        print(f"shape: {_shape(eresp)}  events count: {len(events) if events else 0}")
        if events:
            print("\nFirst event (full dump):")
            print(_safe_dump(events[0]))
    except Exception as e:
        print(f"failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
