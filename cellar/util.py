"""THE CELLAR — small shared helpers."""
from __future__ import annotations

import logging
import time

log = logging.getLogger("cellar.util")

# Transport-level failures worth retrying. A long-lived process keeps HTTP
# keepalive connections open for hours, and Supabase (or anything between it
# and a house router) will drop an idle one without warning. The next request
# on that dead socket raises before it ever reaches the server.
#
# Vercel never saw this: its functions died after each request, so every call
# got a fresh connection. It shows up the moment the same process runs for
# hours, which is exactly what we just built.
_TRANSIENT = ("server disconnected", "connection reset", "connection aborted",
              "remote end closed", "broken pipe", "timed out", "timeout",
              "temporarily unavailable", "connection refused", "eof occurred")


def is_transient(exc: BaseException) -> bool:
    msg = f"{type(exc).__name__}: {exc}".lower()
    return any(t in msg for t in _TRANSIENT)


def retrying(fn, *, tries: int = 3, base_delay: float = 0.4, what: str = "call"):
    """Run `fn`, retrying transient transport errors with a short backoff.

    Re-raises the last exception when retries are exhausted, so CALLERS KEEP
    THEIR OWN FAILURE SEMANTICS. This must never turn a failed lease claim into
    a successful one -- the lease fails CLOSED on error, and that is the
    property that stops two machines placing the same order. This only stops a
    dropped socket from being mistaken for a real answer.
    """
    last = None
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:
            last = e
            if attempt == tries or not is_transient(e):
                raise
            delay = base_delay * (2 ** (attempt - 1))
            log.debug("%s: transient %s, retry %d/%d in %.1fs",
                      what, type(e).__name__, attempt, tries, delay)
            time.sleep(delay)
    raise last            # unreachable; keeps type checkers happy
