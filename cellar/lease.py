"""THE CELLAR — the single-writer lease.

Spec: docs/cellar-migration-spec.md §5.

This is the safety-critical file. Everything else in the package is
convenience; this is what stops the house box and Vercel from both placing
the same order.

Correctness rests on TWO properties, both enforced in Postgres, not here:

  * ATOMICITY -- `cellar_claim` is a single UPDATE. Row locking serializes
    concurrent callers and the loser re-evaluates its WHERE after the winner
    commits, so exactly one owner can hold a lane. Do not reimplement this as
    a read-then-write in Python; that race is precisely the bug this file
    exists to prevent.

  * SERVER CLOCK -- staleness is `now() - heartbeat_at` evaluated in Postgres.
    Never pass a client timestamp. A laptop with a skewed clock must not be
    able to declare the other side dead.

Proven against the live DB before any Python was written against it:
cellar preempts vercel / vercel blocked while cellar is alive / renew /
clean release / and failover at 200s past a 180s TTL.
"""
from __future__ import annotations

import logging
import threading

from .util import retrying

log = logging.getLogger("cellar.lease")


class LeaseError(RuntimeError):
    pass


class Lease:
    """Per-process lease client. Thread-safe.

    Usage is deliberately blunt::

        if not lease.claim("opener", ttl_s=180):
            return          # someone else owns it. Not an error. Do nothing.
    """

    def __init__(self, sb, owner: str):
        self._sb = sb
        self._owner = owner
        self._held: set[str] = set()
        self._lock = threading.Lock()

    # -- core ---------------------------------------------------------------

    def claim(self, lane: str, ttl_s: int | None = None) -> bool:
        """Claim or renew `lane`. Returns True iff we now own it.

        A False return is a NORMAL outcome, not a failure -- it means the
        other side holds the lane and this caller must no-op.
        """
        try:
            res = retrying(
                lambda: self._sb.rpc(
                    "cellar_claim",
                    {"p_lane": lane, "p_owner": self._owner, "p_ttl": ttl_s},
                ).execute(),
                what=f"claim {lane}")
        except Exception as e:
            # FAIL CLOSED. An unreachable lease table means we cannot prove we
            # are the only writer, so we must behave as though we are not.
            # The alternative -- assuming ownership on error -- is exactly how
            # you get duplicate orders during a network blip.
            log.warning("lease claim %s failed, treating as NOT owned: %s", lane, e)
            with self._lock:
                self._held.discard(lane)
            return False

        ok = bool(res.data)
        with self._lock:
            if ok:
                self._held.add(lane)
            else:
                self._held.discard(lane)
        return ok

    def release(self, lane: str) -> bool:
        """Voluntarily hand a lane back so the standby can take it at once
        rather than waiting out the TTL. Called on clean shutdown."""
        try:
            res = retrying(
                lambda: self._sb.rpc(
                    "cellar_release", {"p_lane": lane, "p_owner": self._owner}
                ).execute(),
                what=f"release {lane}")
        except Exception as e:
            log.warning("lease release %s failed: %s", lane, e)
            return False
        with self._lock:
            self._held.discard(lane)
        return bool(res.data)

    def release_all(self) -> None:
        for lane in list(self.held):
            self.release(lane)

    @property
    def held(self) -> set[str]:
        with self._lock:
            return set(self._held)

    # -- introspection ------------------------------------------------------

    def status(self) -> list[dict]:
        """Current ownership of every lane. Read-only; for the health surface
        and for a human asking 'who is running what right now'."""
        try:
            return (
                self._sb.table("cellar_lease")
                .select("lane,owner,heartbeat_at,ttl_seconds,note")
                .order("lane")
                .execute()
                .data
            ) or []
        except Exception as e:
            log.warning("lease status failed: %s", e)
            return []
