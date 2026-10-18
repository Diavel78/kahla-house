#!/usr/bin/env python3
"""THE KALSHI PROOF, RUN ON THE BOX BY HAND (Oct 18 2026).

    python3 kahla-scanner/scripts/kalshi_probe.py            # reads only: creds, balance, orders, a market
    python3 kahla-scanner/scripts/kalshi_probe.py --place    # + ONE post-only 1-lot 10¢ under the bid,
                                                             #   verify resting, cancel, verify gone
    python3 kahla-scanner/scripts/kalshi_probe.py --place --ticker KXNHLGAME-26OCT18EDMUTA-UTA --off 15

Prints the verdict and the full record as JSON; also stamps exec_probe_runs
kind=kalshi_probe when the box DB is reachable. Verdicts: read_only_ok ·
write_ok · write_scope_ok_no_funds (the key CAN trade, the account is
empty) · write_denied (the key cannot trade) · create_rejected (read
`create.data` — the venue's own words). The same code runs from the
handbets tick when `machine_flags kalshi_probe` is set (app._kalshi_probe_tick).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
for _p in (ROOT,):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--place", action="store_true", help="place ONE post-only order and cancel it")
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--contracts", type=int, default=1)
    ap.add_argument("--off", type=int, default=10, help="cents under the bid")
    ap.add_argument("--no-cancel", action="store_true", help="leave the order resting (default cancels)")
    ap.add_argument("--no-stamp", action="store_true")
    a = ap.parse_args()
    import kalshi_venue as kv
    out = kv.probe(place=a.place, ticker=a.ticker, contracts=a.contracts, off_c=a.off,
                   cancel=not a.no_cancel, log_fn=lambda m: print(m, file=sys.stderr))
    out["kind"] = "kalshi_probe"
    out["cfg"] = {"cli": True, "place": a.place, "ticker": a.ticker}
    if not a.no_stamp:
        try:
            import app
            app._probe_log(out)
        except Exception as e:  # noqa: BLE001
            out["stamp_err"] = f"{type(e).__name__}: {e}"[:160]
    print(json.dumps(out, indent=1, default=str))
    print(f"\nVERDICT: {out.get('verdict')}", file=sys.stderr)
    return 0 if out.get("verdict") in ("read_only_ok", "write_ok", "write_scope_ok_no_funds") else 1


if __name__ == "__main__":
    sys.exit(main())
