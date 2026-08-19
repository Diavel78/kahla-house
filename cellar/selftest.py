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
    """The five engines that need the admin uid must be marked.

    Unmarked, they run and silently do nothing on a box without Firebase —
    242 healthy ticks with work=0 is what that looked like in production,
    and the dashboard read $0.00 the whole time.
    """
    from cellar import config
    need = {n for n, l in config.ALL_LANES.items() if l.needs_owner}
    check("owner-dependent lanes are exactly the five that need a uid",
          need == {"repeg", "harvest", "ledger", "kalshi_autolog", "alerts"},
          f"got {sorted(need)}")
    # A money lane failing this way is the dangerous case: healthy-looking
    # while real orders go unmanaged.
    money_needing = {n for n, l in config.ALL_LANES.items()
                     if l.needs_owner and l.writes_money}
    check("repeg and harvest are covered (money lanes)",
          money_needing == {"repeg", "harvest"}, f"got {sorted(money_needing)}")


def main() -> int:
    print("THE CELLAR — offline selftest\n")
    for t in (test_imports_without_creds, test_config_validation,
              test_lease_fails_closed, test_journal_survives_crash,
              test_lane_registry_matches_config, test_batch_schedule,
              test_batch_commands_exist, test_batch_flags_are_real,
              test_batch_blocked_deps, test_owner_dependent_lanes):
        t()
    print(f"\n  {len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        print("  FAILED: " + ", ".join(_FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
