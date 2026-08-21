"""THE CELLAR — offline selftest.

No network, no credentials, no Supabase. Everything here runs on a bare
checkout, which is the point: it can be run on the house box before any
secret has been copied onto it, and in CI, and in a cloud sandbox.

Covers the parts where a bug is expensive:
  * the runner refuses configs that would double-fire engines
  * the lease FAILS CLOSED when the DB is unreachable
  * the journal actually survives a simulated crash
"""
from __future__ import annotations

import os
import tempfile

_PASS, _FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (_PASS if cond else _FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail and not cond else ''}")


# ---------------------------------------------------------------------------

class _FakeExec:
    def __init__(self, data): self._d = data
    def execute(self):
        class R: data = self._d
        return R()


class FakeSB:
    """Minimal Supabase stand-in: records rpc calls, returns scripted answers."""
    def __init__(self, answers=None, raise_on_rpc=False):
        self.answers = answers or {}
        self.raise_on_rpc = raise_on_rpc
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        if self.raise_on_rpc:
            raise RuntimeError("simulated network partition")
        return _FakeExec(self.answers.get(name, True))


def test_imports_without_creds() -> None:
    # The package must import on a box with no .env at all. If this breaks,
    # the selftest itself becomes impossible to run on a fresh machine.
    from cellar import config, lanes, lease, journal, runner  # noqa: F401
    check("package imports with no credentials", True)
    check("DRY_RUN defaults to True (fresh install is inert)", config.DRY_RUN is True,
          f"got {config.DRY_RUN}")
    check("no lanes enabled by default", config.LANES_ENABLED == [],
          f"got {config.LANES_ENABLED}")


def test_config_validation() -> None:
    from cellar.runner import Runner
    check("unknown lane is rejected",
          any("unknown lane" in p for p in Runner.validate(["nope"])))
    check("valid lane set is accepted", Runner.validate(["opener", "repeg"]) == [])
    # THE IMPORTANT ONE: the paperlog route internally runs the engine lanes,
    # so enabling both would fire every engine twice per tick.
    problems = Runner.validate(["paperlog", "opener"])
    check("paperlog+opener refused (would double-fire engines)",
          any("double-fire" in p for p in problems),
          f"got {problems}")


def test_lease_fails_closed() -> None:
    from cellar.lease import Lease
    # Unreachable DB must NOT be read as 'I own this'. Assuming ownership on
    # error is exactly how duplicate orders happen during a network blip.
    l = Lease(FakeSB(raise_on_rpc=True), "cellar")
    check("lease FAILS CLOSED when DB unreachable", l.claim("opener") is False)
    check("failed claim leaves nothing held", l.held == set())

    l2 = Lease(FakeSB({"cellar_claim": True}), "cellar")
    check("successful claim is tracked", l2.claim("opener") is True and l2.held == {"opener"})

    l3 = Lease(FakeSB({"cellar_claim": []}), "cellar")
    check("empty rpc result = not owned", l3.claim("opener") is False)

    sb = FakeSB({"cellar_claim": True, "cellar_release": True})
    l4 = Lease(sb, "cellar")
    l4.claim("repeg"); l4.release("repeg")
    check("release drops the lane", l4.held == set())
    check("release passes owner to the DB",
          any(c[0] == "cellar_release" and c[1]["p_owner"] == "cellar" for c in sb.calls))


def test_journal_survives_crash() -> None:
    from cellar.journal import Journal
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sub", "intents.sqlite3")
        j = Journal(path)

        iid = j.open("repeg", "mlb-nyy-bos-2026-08-16", price_c=42, order_id="abc")
        check("open intent is visible while in flight", len(j.open_intents()) == 1)
        j.close(iid, "done")
        check("closed intent disappears from the wound list", j.open_intents() == [])

        # Simulate dying between CANCEL and CREATE: open, then drop the handle
        # without closing, then reopen the DB as a fresh process would.
        j.open("repeg", "mlb-lad-sf-2026-08-16", stage="cancelled_awaiting_create")
        j.close_db()

        j2 = Journal(path)
        wounds = j2.open_intents()
        check("unfinished intent survives process death", len(wounds) == 1,
              f"got {wounds}")
        check("wound carries enough to reconcile",
              wounds and wounds[0]["key"] == "mlb-lad-sf-2026-08-16"
              and wounds[0]["payload"].get("stage") == "cancelled_awaiting_create")

        # Context manager must record an abort AND re-raise.
        raised = False
        try:
            with j2.intent("harvest", "slug-x"):
                raise ValueError("venue said no")
        except ValueError:
            raised = True
        check("intent() re-raises on failure", raised)
        # NOT `open_intents() == []` -- the mlb-lad-sf wound above is still
        # legitimately open (nothing reconciled it). Assert on THIS intent.
        check("aborted intent is closed, not left dangling",
              not any(w["kind"] == "harvest" for w in j2.open_intents()),
              f"still open: {j2.open_intents()}")
        check("the unreconciled wound is still open (not swallowed)",
              any(w["kind"] == "repeg" for w in j2.open_intents()))
        j2.close_db()


def test_lane_registry_matches_config() -> None:
    from cellar import config, lanes
    missing = [n for n in config.ALL_LANES if n not in lanes.REGISTRY]
    check("every configured lane has an implementation", not missing, f"missing {missing}")
    extra = [n for n in lanes.REGISTRY if n not in config.ALL_LANES]
    check("no orphan lane implementations", not extra, f"orphans {extra}")
    money = {n for n, l in config.ALL_LANES.items() if l.writes_money}
    check("money lanes are exactly opener/repeg/harvest",
          money == {"opener", "repeg", "harvest"}, f"got {sorted(money)}")
    bad = [n for n, l in config.ALL_LANES.items() if l.ttl_s <= l.every_s]
    check("every TTL exceeds its cadence", not bad, f"too tight: {bad}")


def test_batch_schedule() -> None:
    from datetime import datetime, timedelta
    from cellar.batch import AZ, JOBS, Job, due_at, is_due

    daily = Job("t", ["x"], hour=3, minute=30)
    now = datetime(2026, 8, 16, 5, 0, tzinfo=AZ)          # Sun 05:00 AZ

    check("daily: fire time is today when now is past it",
          due_at(daily, now) == datetime(2026, 8, 16, 3, 30, tzinfo=AZ))
    check("daily: fire time rolls back when now is before it",
          due_at(daily, now.replace(hour=2)) == datetime(2026, 8, 15, 3, 30, tzinfo=AZ))
    check("never run => due", is_due(daily, None, now))
    check("ran before today's fire => due",
          is_due(daily, datetime(2026, 8, 15, 3, 31, tzinfo=AZ), now))
    check("ran after today's fire => NOT due",
          not is_due(daily, datetime(2026, 8, 16, 3, 31, tzinfo=AZ), now))
    # The behavior a laptop needs and cron does not give you: a box asleep at
    # 03:30 must run the job when it wakes, not skip the day.
    check("CATCH-UP: box asleep for 3 days => due on wake",
          is_due(daily, now - timedelta(days=3), now))

    weekly = Job("w", ["x"], hour=4, weekday=0)            # Mondays 04:00
    wed = datetime(2026, 8, 19, 9, 0, tzinfo=AZ)           # Wed
    fire = due_at(weekly, wed)
    check("weekly: fires on the most recent Monday",
          fire.weekday() == 0 and fire <= wed and (wed - fire).days < 7,
          f"got {fire}")
    mon_early = datetime(2026, 8, 17, 2, 0, tzinfo=AZ)     # Mon, before 04:00
    fire2 = due_at(weekly, mon_early)
    # Mon 02:00, job fires Mondays 04:00 -> today's firing hasn't happened yet,
    # so the most recent one is LAST Monday. (Not `.days == 7`: the gap is
    # 6d22h, which floors to 6.)
    check("weekly: before the hour on the day => previous week",
          fire2 == datetime(2026, 8, 10, 4, 0, tzinfo=AZ), f"got {fire2}")

    names = [j.name for j in JOBS]
    check("batch job names are unique", len(names) == len(set(names)))
    check("no batch job schedules an impossible hour",
          all(0 <= j.hour <= 23 and 0 <= j.minute <= 59 for j in JOBS))


def test_batch_commands_exist() -> None:
    """Every job must point at a module that is actually on disk.

    A typo here would fail silently at 3am on a box nobody is watching, which
    is exactly the class of failure this migration is supposed to end.
    """
    import os
    from cellar.batch import JOBS, SCANNER_DIR

    missing = []
    for j in JOBS:
        for argv in (list(j.argv),) + tuple(list(t) for t in j.then):
            mod = argv[0]                       # e.g. scripts.ingest_nhl_shots
            path = os.path.join(SCANNER_DIR, *mod.split(".")) + ".py"
            if not os.path.exists(path):
                missing.append(mod)
    check("every batch command resolves to a real script",
          not missing, f"missing {missing}")


def test_batch_flags_are_real() -> None:
    """Every flag a job passes must exist in that script's argparse.

    Caught three real bugs the first time it ran: ufc_stats was being invoked
    with --delta (a flag it does not have, so argparse would have killed it),
    savant_xwoba was missing --platoon (so the platoon spine would silently
    stop updating), and the whole class was invisible because these jobs only
    run once a day or once a week, at 3am, on a box nobody watches.
    """
    import os
    import re
    from cellar.batch import JOBS, SCANNER_DIR

    problems = []
    for j in JOBS:
        for argv in (list(j.argv),) + tuple(list(t) for t in j.then):
            mod, args = argv[0], argv[1:]
            path = os.path.join(SCANNER_DIR, *mod.split(".")) + ".py"
            if not os.path.exists(path):
                problems.append(f"{mod}: script missing")
                continue
            src = open(path).read()
            declared = set(re.findall(r'add_argument\(\s*"(--[a-z0-9-]+)"', src))
            for a in args:
                if a.startswith("--") and a not in declared:
                    problems.append(f"{mod}: passes {a}, script does not declare it")
    check("every batch flag exists in its script's argparse",
          not problems, "; ".join(problems))


def test_batch_blocked_deps() -> None:
    """A job with an unmet dependency must report BLOCKED, not silently pass."""
    from cellar.batch import JOBS, _have, status

    needs = {j.name: j.needs for j in JOBS if j.needs}
    check("ufc_stats declares its playwright dependency",
          needs.get("ufc_stats") == "playwright", f"got {needs}")
    check("_have() detects a present module", _have("json") is True)
    check("_have() detects an absent module",
          _have("definitely_not_a_real_module_xyz") is False)


def test_owner_dependent_lanes() -> None:
    """The six engines that need the admin uid must be marked.

    Unmarked, they run and silently do nothing on a box without Firebase —
    242 healthy ticks with work=0 is what that looked like in production,
    and the dashboard read $0.00 the whole time.

    `opener` was the sixth, added at cutover time: _autobet_execute resolves
    _kalshi_owner_uid() and returns False on None, so an unmarked opener lane
    keeps persisting its shadow rows (work>0 — it reads ALIVE) while placing
    zero bets. With the lease enforced, Vercel has stood down. That is a
    total betting blackout every dashboard calls healthy.
    """
    from cellar import config
    need = {n for n, l in config.ALL_LANES.items() if l.needs_owner}
    check("owner-dependent lanes are exactly the six that need a uid",
          need == {"repeg", "harvest", "ledger", "kalshi_autolog", "alerts",
                   "opener"},
          f"got {sorted(need)}")
    # A money lane failing this way is the dangerous case: healthy-looking
    # while real orders go unmanaged (or never placed at all).
    money_needing = {n for n, l in config.ALL_LANES.items()
                     if l.needs_owner and l.writes_money}
    check("every money lane is owner-covered",
          money_needing == {"repeg", "harvest", "opener"},
          f"got {sorted(money_needing)}")


def test_dry_run_blackout() -> None:
    """A money lane enabled under DRY_RUN must refuse the boot.

    It claims its lease before it checks dry_run, so with the lease enforced
    it stands Vercel down and then places nothing — the blackout that reads
    healthy. Read-only lanes under dry-run are fine (that is rehearsal).
    """
    from cellar import config
    real = config.DRY_RUN
    try:
        config.DRY_RUN = True
        check("dry-run + money lane => blackout flagged",
              config.dry_run_blackout(["opener", "pm_snapshot"]) == ["opener"],
              f"got {config.dry_run_blackout(['opener', 'pm_snapshot'])}")
        check("dry-run + read-only lanes only => fine",
              config.dry_run_blackout(["pm_snapshot", "vsin"]) == [])
        config.DRY_RUN = False
        check("live + money lane => fine",
              config.dry_run_blackout(["opener", "repeg"]) == [])
    finally:
        config.DRY_RUN = real


def test_overrun_detector() -> None:
    """A lane past its own TTL must go LOUD, once, and keep its lease.

    This test exists because the thing it replaces -- config.LANE_TIMEOUT_S --
    sat in the file for weeks naming a ceiling that nothing enforced. A guard
    with no test is the same fiction with more steps.
    """
    import sys
    import types
    from cellar import config
    from cellar.runner import Runner

    pings, rows = [], []

    class _Exec:
        def __init__(self, payload): self.payload = payload
        def execute(self): rows.append(self.payload); return self
    class _Tbl:
        def insert(self, payload): return _Exec(payload)
    class _SB:
        def table(self, _n): return _Tbl()

    fake_app = types.ModuleType("app")
    fake_app._send_fill_telegram = lambda text, urgent=False: pings.append(
        (text, urgent))
    real_app = sys.modules.get("app")
    sys.modules["app"] = fake_app
    try:
        r = Runner(_SB(), lease=None)
        spec = config.ALL_LANES["opener"]
        now = 1_000_000.0

        # Running, but inside its TTL — silence.
        r._started["opener"] = now - (spec.ttl_s - 5)
        r._overrun_check("opener", spec, now)
        check("inside its TTL => no alarm", not rows and not pings,
              f"rows={len(rows)} pings={len(pings)}")

        # Past the TTL — one failed tick row and one URGENT ping.
        r._started["opener"] = now - (spec.ttl_s + 30)
        r._overrun_check("opener", spec, now)
        check("past its TTL => failed tick recorded",
              len(rows) == 1 and rows[0]["ok"] is False
              and str(rows[0]["error"]).startswith("overrun:"),
              f"got {rows}")
        check("past its TTL => one urgent ping",
              len(pings) == 1 and pings[0][1] is True, f"got {pings}")

        # Still stuck next tick — must NOT re-ping every minute.
        r._overrun_check("opener", spec, now + 60)
        check("stuck lane pings once per episode",
              len(pings) == 1 and len(rows) == 1,
              f"rows={len(rows)} pings={len(pings)}")

        # Completing re-arms the alarm for the next episode.
        r._started.pop("opener", None)
        r._stuck.discard("opener")
        r._started["opener"] = now - (spec.ttl_s + 30)
        r._overrun_check("opener", spec, now + 120)
        check("a completed run re-arms the alarm", len(pings) == 2,
              f"got {len(pings)}")
    finally:
        if real_app is not None:
            sys.modules["app"] = real_app
        else:
            sys.modules.pop("app", None)


def test_side_and_phase() -> None:
    """Two wiring invariants that only bite in production.

    1. THIS PROCESS MUST CLAIM AS 'cellar'. The engines it drives share
       app._cellar_owns with Vercel, which claims under whatever side it
       is told it is. Left at the default, the cellar would claim as
       'vercel' -- and once enforcement is on, fail its own claim (its
       real lease is still fresh) and stop running the lane we moved
       here, healthily.

    2. NO LANE MAY INHERIT VERCEL'S MINUTE-MODULO. Several engines gate
       on `now.minute % N` because on Vercel they ride a 1-minute tick.
       A cellar lane has its own cadence, and if that cadence is a
       multiple of N the modulo is CONSTANT for the life of the process:
       always true or always false, decided by the minute the daemon
       booted on. `ledger` ran for 22 hours that way -- claimed, ran,
       returned zero, renewed -- with the dashboard reading $0.00.

       So: for every engine a lane calls directly, if that engine's body
       contains a minute-modulo, the lane must pass force=True. Derived
       from the source of both files rather than a hand-kept list, so a
       new lane or a newly-gated engine is covered without anyone
       remembering to update this test.
    """
    import os as _os, re as _re
    here = _os.path.dirname(_os.path.abspath(__file__))
    root = _os.path.dirname(here)
    main_src = open(_os.path.join(here, "__main__.py"), encoding="utf-8").read()
    lanes_src = open(_os.path.join(here, "lanes.py"), encoding="utf-8").read()
    app_src = open(_os.path.join(root, "app.py"), encoding="utf-8").read()

    check("the cellar declares its lease side as itself",
          'os.environ["CELLAR_SIDE"] = "cellar"' in main_src)
    check("side is set, not setdefault (no .env may claim we are vercel)",
          'setdefault("CELLAR_SIDE"' not in main_src)

    # Which app.py engines gate on a minute-modulo?
    gated = set()
    for m in _re.finditer(r"^def (_\w+)\(", app_src, _re.M):
        name = m.group(1)
        body = app_src[m.end():]
        nxt = _re.search(r"^def ", body, _re.M)
        if "now.minute %" in (body[:nxt.start()] if nxt else body):
            gated.add(name)
    check("found the modulo-gated engines in app.py", len(gated) >= 3,
          f"found {sorted(gated)}")

    # For each lane, every _app.<engine>( it calls directly.
    missing, checked = [], 0
    for m in _re.finditer(r"^def (lane_\w+)\(", lanes_src, _re.M):
        body = lanes_src[m.end():]
        nxt = _re.search(r"^def ", body, _re.M)
        body = body[:nxt.start()] if nxt else body
        for call in _re.finditer(r"_app\.(_\w+)\(([^)]*)\)", body):
            if call.group(1) in gated:
                checked += 1
                if "force=True" not in call.group(2):
                    missing.append(f"{m.group(1)} -> {call.group(1)}")
    check("every modulo-gated engine a lane drives is called with force=True",
          not missing, f"missing force=True: {missing}")
    check("the force check actually inspected some calls", checked >= 3,
          f"only inspected {checked}")

    # The telegram flush is the one engine with no modulo but a shared
    # queue: two drainers split or duplicate a digest.
    body = app_src[app_src.index("def _tg_flush("):]
    body = body[:body.index("\ndef ", 1)]
    check("_tg_flush is under the alerts lease (one drainer only)",
          '_cellar_owns(sb, "alerts"' in body)


def test_ttls_agree_with_engines() -> None:
    """A lane's TTL must be the SAME NUMBER on both sides of the lease.

    Both the cellar (via Lease) and the shared engine (via
    app._cellar_owns) pass a TTL on every claim, and `cellar_claim`
    overwrites the stored value with whatever it is handed. If the two
    disagree, the failover deadline silently becomes whichever side
    claimed most recently — so how long a dead cellar goes unnoticed
    depends on a race. Caught alerts at 180 vs 300.
    """
    import os as _os, re as _re
    from cellar import config
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    src = open(_os.path.join(root, "app.py"), encoding="utf-8").read()
    found = _re.findall(r'_cellar_owns\(\s*sb\s*,\s*"([a-z_]+)"\s*,\s*(\d+)\s*\)', src)
    check("every engine's lease gate names a known lane",
          all(n in config.ALL_LANES for n, _ in found),
          f"unknown: {[n for n, _ in found if n not in config.ALL_LANES]}")
    bad = [(n, t, config.ALL_LANES[n].ttl_s) for n, t in found
           if n in config.ALL_LANES and int(t) != config.ALL_LANES[n].ttl_s]
    check("lane TTLs agree between config and the engines", not bad,
          f"mismatched (lane, app.py, config): {bad}")


def main() -> int:
    print("THE CELLAR — offline selftest\n")
    for t in (test_imports_without_creds, test_config_validation,
              test_lease_fails_closed, test_journal_survives_crash,
              test_lane_registry_matches_config, test_batch_schedule,
              test_batch_commands_exist, test_batch_flags_are_real,
              test_batch_blocked_deps, test_owner_dependent_lanes,
              test_dry_run_blackout, test_overrun_detector,
              test_side_and_phase, test_ttls_agree_with_engines):
        t()
    print(f"\n  {len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        print("  FAILED: " + ", ".join(_FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
