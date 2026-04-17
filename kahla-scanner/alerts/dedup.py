"""Alert dedup — DB-backed via alerts_log unique (signal_id, subscriber_id).

The in-memory layer here guards against retries within one scheduler tick.
Supabase's unique index is the durable source of truth across process restarts.
"""
from __future__ import annotations

from collections import OrderedDict
from threading import Lock

_MAX_ENTRIES = 10_000


class _RecentSet:
    def __init__(self, maxlen: int = _MAX_ENTRIES) -> None:
        self._d: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._maxlen = maxlen
        self._lock = Lock()

    def seen(self, signal_id: str, subscriber_id: str) -> bool:
        key = (signal_id, subscriber_id)
        with self._lock:
            if key in self._d:
                return True
            self._d[key] = None
            if len(self._d) > self._maxlen:
                self._d.popitem(last=False)
            return False


_recent = _RecentSet()


def mark_and_check(signal_id: str, subscriber_id: str) -> bool:
    """Returns True if this pair is new (should send). False if already seen."""
    return not _recent.seen(signal_id, subscriber_id)
