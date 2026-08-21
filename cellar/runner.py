"""THE CELLAR — the scheduler.

Spec: docs/cellar-migration-spec.md §3, §5.

A single long-lived process. For each enabled lane: wait until due, claim the
lease, run it, record a heartbeat. That is the whole design.

Three invariants it enforces, in order of how badly they hurt when broken:

1. NEVER RUN A LANE WE DO NOT OWN. Claim first, always. A lost claim is a
   silent no-op, recorded with claimed=false so the log still shows the tick.

2. VENUE WRITES ARE SERIAL. One process-wide lock around every money lane.
   This is the "RUN WRITE ENDPOINTS SERIALLY" rule from CLAUDE.md, finally
   enforced by a mutex instead of by remembering. Overlapping write batches
   are what produced 9 duplicate orders across 8 markets in two minutes.

3. ONE SLOW LANE MUST NOT STARVE THE REST. Lanes run on their own threads,
   with the write-lock providing the serialization that correctness needs and
   nothing more.
"""
from __future__ import annotations

import logging
import signal
import threading
import time

from . import config, lanes as lanes_mod
from .journal import Journal
from .lease import Lease
from .util import retrying

log = logging.getLogger("cellar.runner")


class Runner:
    def __init__(self, sb, lease: Lease, journal: Journal | None = None):
        self.sb = sb
        self.lease = lease
        self.journal = journal
        self.stop = threading.Event()
        # Invariant 2. Money lanes hold this for the whole of their run.
        self.write_lock = threading.Lock()
        self._next_due: dict[str, float] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._started: dict[str, float] = {}     # lane -> t0 of the live run
        self._stuck: set[str] = set()            # lanes already reported stuck
        self._renewer: threading.Thread | None = None

    # -- validation ---------------------------------------------------------

    @staticmethod
    def validate(enabled: list[str]) -> list[str]:
        """Return a list of fatal config problems. Empty list = safe to start.

        Refusing to boot on a bad config is deliberate: the alternative is a
        daemon that looks healthy while running the wrong set of engines.
        """
        problems = []
        for n in enabled:
            if n not in config.ALL_LANES:
                problems.append(f"unknown lane {n!r} (known: {', '.join(sorted(config.ALL_LANES))})")
        for lane, conflicts in lanes_mod.CONFLICTS.items():
            if lane in enabled:
                clash = sorted(conflicts & set(enabled))
                if clash:
                    problems.append(
                        f"lane {lane!r} internally runs {', '.join(clash)} — "
                        f"enabling both double-fires every engine. Pick one."
                    )
        return problems

    # -- lease renewal ------------------------------------------------------

    def _renew_loop(self) -> None:
        """Keep held leases fresh INDEPENDENTLY of lane execution.

        Without this, a lane only renews when it runs -- so a long job lets its
        own lease go stale while it works. Caught live: ufc_stats ran 5m12s
        through a browser and the batch heartbeat aged past 3 minutes with the
        daemon perfectly healthy, which reads externally as "the cellar died."

        Worse than cosmetic: if a job outran its TTL, the standby could reclaim
        the lane MID-RUN and start doing the same work. batch survives only
        because its TTL is 3600s and its longest timeout is also 3600s -- a
        coin-flip margin, which is not a margin.

        Renews every 30s: comfortably inside the tightest TTL (180s) and one
        cheap RPC per held lane.
        """
        while not self.stop.is_set():
            for lane in list(self.lease.held):
                spec = config.ALL_LANES.get(lane)
                if spec is not None:
                    self.lease.claim(lane, ttl_s=spec.ttl_s)
            self.stop.wait(30.0)

    # -- heartbeat ----------------------------------------------------------

    def record(self, lane: str, *, claimed: bool, ok: bool,
               work: int, ms: int, detail: dict | None = None,
               error: str | None = None) -> None:
        try:
            retrying(lambda: self.sb.table("cellar_ticks").insert({
                "lane": lane,
                "owner": config.OWNER,
                "duration_ms": ms,
                "ok": ok,
                "claimed": claimed,
                "work": work,
                "detail": detail,
                "error": (error or None) and str(error)[:2000],
            }).execute(), what=f"tick {lane}")
        except Exception as e:
            # Never let observability failure take down the machine.
            log.warning("heartbeat write failed for %s: %s", lane, e)

    # -- execution ----------------------------------------------------------

    def run_lane(self, name: str) -> None:
        spec = config.ALL_LANES[name]
        fn = lanes_mod.REGISTRY.get(name)
        if fn is None:
            log.error("lane %s has no implementation", name)
            return

        t0 = time.time()
        self._started[name] = t0

        # Invariant 1: claim BEFORE any work.
        if not self.lease.claim(name, ttl_s=spec.ttl_s):
            log.debug("lane %s: not ours this tick", name)
            self.record(name, claimed=False, ok=True, work=0,
                        ms=int((time.time() - t0) * 1000))
            return

        ctx = lanes_mod.Ctx(sb=self.sb, now=lanes_mod.utcnow(),
                            dry_run=config.DRY_RUN, journal=self.journal)

        # Invariant 2: serialize venue writes.
        lock = self.write_lock if spec.writes_money else None
        if lock:
            lock.acquire()
        try:
            work = int(fn(ctx) or 0)
            ms = int((time.time() - t0) * 1000)
            log.info("lane %s ok work=%d %dms", name, work, ms)
            self.record(name, claimed=True, ok=True, work=work, ms=ms)
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            log.exception("lane %s FAILED after %dms", name, ms)
            self.record(name, claimed=True, ok=False, work=0, ms=ms, error=repr(e))
        finally:
            if lock:
                lock.release()
            self._started.pop(name, None)
            self._stuck.discard(name)      # it finished — re-arm the alarm

    def _overrun_check(self, name: str, spec, now: float) -> None:
        """A lane running longer than its OWN lease TTL is stuck. Say so.

        Why the TTL is the right line: the TTL is exactly the window in which
        the standby side treats the holder as dead. `_renew_loop` refreshes
        the heartbeat independently of lane execution, so a hung lane keeps
        its lease green forever -- the alarm the renewal loop suppresses is
        the one we need back. Past its own TTL, a lane is by its own
        definition too slow to be trusted.

        WE DO NOT RELEASE THE LEASE. A hung thread cannot be killed in Python
        and may be sitting inside a venue write; dropping the lease would let
        the standby start the same work alongside it, which is the Aug 16
        duplicate-order incident with extra steps. Holding a lease and doing
        nothing is bad; two sides writing at once is worse. So convert the
        SILENT stall into a LOUD one -- a failed tick row (so the dashboard's
        lane health stops reading green) and one urgent ping -- and let a
        human decide. Once per episode, re-armed when the lane completes.
        """
        t0 = self._started.get(name)
        if t0 is None or name in self._stuck:
            return
        held = now - t0
        if held < spec.ttl_s:
            return
        self._stuck.add(name)
        msg = (f"lane {name} has been running {int(held)}s, past its own "
               f"{spec.ttl_s}s lease TTL — the lease is HELD and the standby "
               f"is stood down, so this lane is doing nothing and looks "
               f"healthy. Check the daemon.")
        log.error("STUCK LANE: %s", msg)
        self.record(name, claimed=True, ok=False, work=0,
                    ms=int(held * 1000), error=f"overrun: {int(held)}s")
        try:
            import app as _app
            _app._send_fill_telegram(f"🚨 STUCK LANE: {msg}", urgent=True)
        except Exception as e:
            log.warning("stuck-lane ping failed: %s", e)

    def _tick(self, now: float, enabled: list[str]) -> None:
        for name in enabled:
            spec = config.ALL_LANES[name]
            if now < self._next_due.get(name, 0.0):
                continue
            prev = self._threads.get(name)
            if prev is not None and prev.is_alive():
                # Still running from last time. Do NOT stack a second copy --
                # that is the overlapping-batch bug in miniature.
                log.warning("lane %s still running, skipping this tick", name)
                self._overrun_check(name, spec, now)
                self._next_due[name] = now + spec.every_s
                continue
            self._next_due[name] = now + spec.every_s
            t = threading.Thread(target=self.run_lane, args=(name,),
                                 name=f"cellar-{name}", daemon=True)
            self._threads[name] = t
            t.start()

    # -- lifecycle ----------------------------------------------------------

    def serve(self, enabled: list[str]) -> int:
        problems = self.validate(enabled)
        if problems:
            for p in problems:
                log.error("CONFIG: %s", p)
            return 2

        if not enabled:
            log.warning("no lanes enabled (CELLAR_LANES is empty) — idling. "
                        "This is the correct posture for a fresh install.")

        # DRY-RUN PRE-FLIGHT. A money lane claims its lease BEFORE it looks
        # at dry_run (Invariant 1), so an enabled-but-inert money lane holds
        # the lease and — with CELLAR_LEASE_ENFORCED on — makes Vercel stand
        # down, then does nothing itself. That is a total betting blackout
        # that reads healthy everywhere: the lane ticks, the heartbeat is
        # fresh, the dashboard is green. There is no rehearsal value worth
        # that; rehearse with the read-only lanes instead.
        inert = config.dry_run_blackout(enabled)
        if inert:
            log.error("CONFIG: money lane(s) %s enabled while "
                      "CELLAR_DRY_RUN is on.", ", ".join(sorted(inert)))
            log.error("  They would hold the lease, stand Vercel down, and "
                      "place nothing. Set CELLAR_DRY_RUN=0 in .env, or drop "
                      "them from CELLAR_LANES.")
            log.error("  Refusing to start rather than run a blackout that "
                      "looks healthy.")
            return 2

        # OWNER PRE-FLIGHT. Six engines need the admin's uid to know whose
        # book they are acting on. Without it they return at their second line
        # with a zero — no exception, no error, just nothing done. That is not
        # hypothetical: `ledger` ran 242 consecutive healthy ticks on this box
        # doing exactly that, and the dashboard's day card read $0.00 while
        # looking fine. A money lane failing this way (harvest, repeg) would
        # report healthy while leaving real orders unmanaged.
        #
        # So refuse to start. A daemon that runs and does nothing is strictly
        # worse than one that will not start and says why.
        need = [n for n in enabled if config.ALL_LANES[n].needs_owner]
        if need:
            try:
                import app as _app
                owner = _app._kalshi_owner_uid()
            except Exception as e:
                owner, _ = None, log.error("owner pre-flight failed: %s", e)
            if not owner:
                log.error("CONFIG: lane(s) %s need the admin uid and none "
                          "resolves on this box.", ", ".join(sorted(need)))
                log.error("  Set KALSHI_OWNER_UID=<uid> in .env (one line, no "
                          "Firebase needed), or provide FIREBASE_SERVICE_ACCOUNT "
                          "so the sole-admin lookup works.")
                log.error("  Refusing to start rather than tick healthily "
                          "while doing nothing.")
                return 2
            log.info("owner pre-flight ok (%s lane(s), uid ...%s)",
                     len(need), str(owner)[-6:])

        # Boot reconciliation: anything left open is a write we died inside of.
        if self.journal is not None:
            for wound in self.journal.open_intents():
                log.error("UNFINISHED INTENT from a previous life: %s key=%s "
                          "age=%ss payload=%s — reconcile against the venue "
                          "before trusting this lane",
                          wound["kind"], wound["key"], wound["age_s"], wound["payload"])

        for s in (signal.SIGINT, signal.SIGTERM):
            signal.signal(s, lambda *_: self.stop.set())

        self._renewer = threading.Thread(target=self._renew_loop,
                                         name="cellar-renew", daemon=True)
        self._renewer.start()

        log.info("%s", config.summary())
        while not self.stop.is_set():
            self._tick(time.time(), enabled)
            self.stop.wait(1.0)

        log.info("shutting down — releasing lanes so the standby can resume")
        for t in self._threads.values():
            if t.is_alive():
                t.join(timeout=15)
        self.lease.release_all()
        return 0
