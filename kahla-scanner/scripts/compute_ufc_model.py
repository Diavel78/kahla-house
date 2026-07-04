"""Compute + persist the Fight IQ model snapshot (weekly).

Full chronological replay over ufc_fights, probability scale + rounds
beta fitted on all graded records since 2015, then one row into
`ufc_model` carrying the params + every fighter's current rating/state.
Flask (Phase 3) reads the latest row — it never runs the replay itself,
same split as power_ratings.

  python -m scripts.compute_ufc_model            # dry-run (prints summary)
  python -m scripts.compute_ufc_model --commit
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from storage import supabase_client as db
from _lib import ufc_model as M

log = logging.getLogger(__name__)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    sb = db.client()
    fights, fighters = M.load_from_db(sb)
    state, recs, aggs = M.replay(fights, fighters, collect_since="2015-01-01")
    if len(recs) < 500:
        log.error("only %d graded records — refusing to fit on that", len(recs))
        return 2
    scale = M.fit_scale(recs, key="d_full")
    beta = M.fit_beta(recs)
    snap = M.snapshot(state, fighters, scale, beta, aggs=aggs)
    n_active = sum(1 for r in snap["ratings"].values()
                   if (r.get("last") or "") >= "2024-01-01")
    log.info("snapshot: %d rated fighters (%d active since 2024) · "
             "scale=%s beta=%s · fit on %d records",
             len(snap["ratings"]), n_active, scale, beta, len(recs))
    top = sorted(((r["elo"], r.get("name") or fid)
                  for fid, r in snap["ratings"].items()
                  if (r.get("last") or "") >= "2025-07-01"),
                 reverse=True)[:5]
    for elo, name in top:
        log.info("  top active: %-28s %.0f", name, elo)
    if not args.commit:
        log.info("dry-run — nothing written")
        return 0
    sb.table("ufc_model").insert({
        "params": {"scale": scale, "beta": beta, "aggs": aggs,
                   "n_fit_records": len(recs), "n_fights": len(fights)},
        "ratings": snap["ratings"],
    }).execute()
    log.info("ufc_model snapshot written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
