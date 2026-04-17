"""Introspect the PolymarketUS SDK so we can find a real discovery path.

Run from kahla-scanner/ with the venv activated:

    source venv/bin/activate
    python scripts/probe_sdk.py

Prints every attribute on the client's top-level namespaces, tries a handful of
likely discovery method names defensively, and confirms positions() creds.
No writes, no side effects.
"""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

# Make the scanner package importable when the script is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import config  # noqa: E402


def _listing(obj) -> list[str]:
    return sorted(a for a in dir(obj) if not a.startswith("_"))


def _signature(fn) -> str:
    try:
        return str(inspect.signature(fn))
    except (TypeError, ValueError):
        return "(?)"


def main() -> int:
    if not config.poly_api_key_id or not config.poly_api_secret:
        print("no Poly creds in env — can't init client", file=sys.stderr)
        return 1

    from polymarket_us import PolymarketUS
    import polymarket_us

    print(f"polymarket_us version: {getattr(polymarket_us, '__version__', '?')}")
    print(f"polymarket_us file:    {polymarket_us.__file__}")

    c = PolymarketUS(key_id=config.poly_api_key_id, secret_key=config.poly_api_secret)

    print("\n== client namespaces ==")
    for ns_name in _listing(c):
        ns = getattr(c, ns_name, None)
        if callable(ns) or ns is None:
            continue
        print(f"\nclient.{ns_name}:")
        for attr in _listing(ns):
            target = getattr(ns, attr, None)
            if callable(target):
                print(f"  .{attr}{_signature(target)}")
            else:
                print(f"  .{attr}  (attr)")

    print("\n== positions() sanity check ==")
    try:
        resp = c.portfolio.positions()
        positions = (resp or {}).get("positions", {}) if isinstance(resp, dict) else resp
        count = len(positions) if hasattr(positions, "__len__") else "?"
        print(f"positions count: {count}")
        if isinstance(positions, dict) and positions:
            k, v = next(iter(positions.items()))
            print(f"first position key: {k}")
            print(f"first position keys: {sorted((v or {}).keys())[:20]}")
        elif isinstance(positions, list) and positions:
            print(f"first position keys: {sorted((positions[0] or {}).keys())[:20]}")
        else:
            print("(no positions returned — could be empty portfolio or wrong account)")
    except Exception as e:
        print(f"positions() failed: {e}")

    print("\n== discovery probes ==")
    probes = [
        ("markets.list", ()),
        ("markets.list", ({"sport": "mlb"},)),
        ("markets.search", ("mlb",)),
        ("markets.active", ()),
        ("markets.upcoming", ()),
        ("events.list", ()),
        ("events.upcoming", ()),
        ("sports.list", ()),
        ("sports.markets", ("mlb",)),
        ("markets.by_sport", ("mlb",)),
        ("catalog.markets", ()),
        ("catalog.list", ()),
    ]
    for path, args in probes:
        ns_name, fn_name = path.split(".", 1)
        ns = getattr(c, ns_name, None)
        fn = getattr(ns, fn_name, None) if ns else None
        if not callable(fn):
            print(f"  {path}{args}: (no attribute)")
            continue
        try:
            r = fn(*args)
            # Summarize the shape without flooding output.
            if isinstance(r, dict):
                keys = list(r.keys())[:8]
                print(f"  {path}{args}: dict keys={keys}")
            elif isinstance(r, list):
                print(f"  {path}{args}: list len={len(r)}")
                if r:
                    first = r[0]
                    if isinstance(first, dict):
                        print(f"      first keys: {sorted(first.keys())[:12]}")
            else:
                s = json.dumps(r, default=str)[:200]
                print(f"  {path}{args}: {type(r).__name__} {s}")
        except TypeError as e:
            print(f"  {path}{args}: TypeError {e}")
        except Exception as e:
            print(f"  {path}{args}: {type(e).__name__} {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
