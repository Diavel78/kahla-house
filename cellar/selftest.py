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


def main() -> int:
    print("THE CELLAR — offline selftest\n")
    for t in (test_imports_without_creds, test_config_validation,
              test_lease_fails_closed, test_journal_survives_crash,
              test_lane_registry_matches_config):
        t()
    print(f"\n  {len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        print("  FAILED: " + ", ".join(_FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
