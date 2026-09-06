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
    import inspect

    from cellar import lanes as lanes_mod
    from cellar.runner import Runner
    check("unknown lane is rejected",
          any("unknown lane" in p for p in Runner.validate(["nope"])))
    check("valid lane set is accepted", Runner.validate(["opener", "repeg"]) == [])
    # WAS "paperlog+opener must be refused" until Aug 20 2026. The route
    # runs the engines inline, so in one process both callers see their own
    # lease and fire twice. That is now solved at the source -- lane_paperlog
    # drives the route with engines=0 -- so the combination is ALLOWED and
    # the param is the thing under test.
    check("paperlog+opener no longer collides on the engines",
          not any("double-fire" in p
                  for p in Runner.validate(["paperlog", "opener"])))
    # Moving paperlog strands every engine that only ran inside its route.
    probs = Runner.validate(["paperlog", "opener"])
    check("paperlog without repeg/alerts/ledger is refused",
          any("run NOWHERE" in p for p in probs), f"got {probs}")
    check("paperlog with the full hot path is accepted",
          Runner.validate(["paperlog", "opener", "repeg", "alerts",
                           "ledger"]) == [])
    src = inspect.getsource(lanes_mod.lane_paperlog)
    check("lane_paperlog drives the route with engines=0 (LOAD-BEARING: "
          "without it, paperlog+opener double-fires every engine)",
          "engines=0" in src, "the param is gone from lane_paperlog")


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
    check("money lanes are exactly opener/repeg/harvest/scalp",
          money == {"opener", "repeg", "harvest", "scalp"},
          f"got {sorted(money)}")
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
    check("owner-dependent lanes are exactly the seven that need a uid",
          need == {"repeg", "harvest", "ledger", "kalshi_autolog", "alerts",
                   "opener", "scalp"},
          f"got {sorted(need)}")
    # A money lane failing this way is the dangerous case: healthy-looking
    # while real orders go unmanaged (or never placed at all).
    money_needing = {n for n, l in config.ALL_LANES.items()
                     if l.needs_owner and l.writes_money}
    check("every money lane is owner-covered",
          money_needing == {"repeg", "harvest", "opener", "scalp"},
          f"got {sorted(money_needing)}")


def test_lane_covers_its_documented_engines() -> None:
    """A lane must call EVERY engine app.py says it owns.

    `_gridiron_opener_pass` had one call site -- inside the paperlog route --
    while app.py's lease-gate table said the `opener` lane runs it. Harmless
    until the cellar took paperlog with engines=0, at which point the
    football pass stopped executing anywhere and four consecutive fixes to
    it could not possibly have produced a row.
    """
    import inspect
    import re

    from cellar import lanes as lanes_mod
    import os as _os
    _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    src = open(_os.path.join(_root, "app.py"), encoding="utf-8").read()
    # The table in app.py is the contract: `#   "lane" -> _engine_a + _engine_b`
    for m in re.finditer(r'^#\s+"(\w+)"\s*->\s*([^\n]+)$', src, re.M):
        lane, rhs = m.group(1), m.group(2)
        fn = lanes_mod.REGISTRY.get(lane)
        if fn is None:
            continue
        body = inspect.getsource(fn)
        for eng in re.findall(r'_[a-z_]+(?:_pass|_tick|_alerts|_flush)', rhs):
            check(f"lane {lane!r} calls {eng}", eng in body,
                  f"app.py says {lane} owns {eng}; {fn.__name__} never calls it")


def test_ws_quote_presence() -> None:
    """Quote-table freshness (Sep 5 2026): a quiet row stays valid while the
    markets socket is up, in the epoch that wrote it, and recently heard;
    any of those failing falls back to the 90s age rule."""
    import time as _t
    import app as _app
    slug = "__selftest-slug__"
    saved = (_app.WS_MKTS_EPOCH, _app.WS_MKTS_UP, _app.WS_MKTS_LAST_RX)
    try:
        old_ts = _t.monotonic() - (_app.WS_QUOTE_FRESH_S + 30.0)
        _app.WS_QUOTES[slug] = (41.0, 43.0, old_ts)
        _app.WS_MKTS_EPOCH, _app.WS_MKTS_UP = 7, True
        _app.WS_MKTS_LAST_RX = _t.monotonic()
        _app.WS_QUOTE_EPOCH[slug] = 7
        check("old row + live socket + same epoch => fresh",
              _app._ws_quote(slug) == (41.0, 43.0))
        _app.WS_QUOTE_EPOCH[slug] = 6
        check("old row from a previous epoch => miss", _app._ws_quote(slug) is None)
        _app.WS_QUOTE_EPOCH[slug] = 7
        _app.WS_MKTS_UP = False
        check("socket down => miss", _app._ws_quote(slug) is None)
        _app.WS_MKTS_UP = True
        _app.WS_MKTS_LAST_RX = _t.monotonic() - (_app.WS_MKTS_ALIVE_S + 5.0)
        check("silent socket => miss", _app._ws_quote(slug) is None)
        _app.WS_MKTS_LAST_RX = _t.monotonic()
        _app.WS_QUOTE_EPOCH.pop(slug, None)          # unsubscribed (forgotten)
        check("forgotten slug => miss", _app._ws_quote(slug) is None)
        _app.WS_QUOTES[slug] = (41.0, 43.0, _t.monotonic())
        check("young row => fresh regardless", _app._ws_quote(slug) == (41.0, 43.0))
    finally:
        _app.WS_QUOTES.pop(slug, None); _app.WS_QUOTE_EPOCH.pop(slug, None)
        _app.WS_MKTS_EPOCH, _app.WS_MKTS_UP, _app.WS_MKTS_LAST_RX = saved


def test_ws_mkts_request_budget() -> None:
    """Markets-feed request budget (Sep 5 2026): core evicts a ladder to
    seat itself, a ladder past the budget is skipped, core rebuilds as one
    request, and a venue rejection un-covers the request's slugs."""
    import json as _json
    import time as _t
    from cellar import wsfeed as W

    class FakeWS:
        def __init__(self): self.sent = []
        def send(self, raw): self.sent.append(_json.loads(raw))
    mf = W.MarketsFeed.__new__(W.MarketsFeed)
    import threading as _th
    mf._lock = _th.Lock(); mf._groups = {}; mf._rid_slugs = {}
    mf._covered = set(); mf._ops = []; mf._expiry = {}; mf._gseq = 0
    mf._base_seen = set(); mf._dirty_add = None
    ws = FakeWS()
    for i in range(W.MKTS_MAX_RIDS):
        mf.add_group(f"lad{i}", {f"l{i}-a", f"l{i}-b"}, _t.time() + 3600 * (i + 1))
    mf._apply_ops(ws)
    check("ten ladders fill the request budget",
          sum(len(r) for r, _ in mf._groups.values()) == W.MKTS_MAX_RIDS)
    mf.add_group("lad_extra", {"x-a"}, _t.time() + 99)
    mf._apply_ops(ws)
    check("an 11th ladder is skipped, not subscribed", "lad_extra" not in mf._groups)
    mf.set_slugs({"core-1", "core-2"}, replace=True)
    mf._apply_ops(ws)
    check("core evicted a ladder and subscribed", "core" in mf._groups
          and sum(len(r) for r, _ in mf._groups.values()) <= W.MKTS_MAX_RIDS)
    farthest = f"lad{W.MKTS_MAX_RIDS - 1}"
    check("the evicted ladder was the farthest-expiring one", farthest not in mf._groups)
    core_rid = mf._groups["core"][0][0]
    mf._on_rejected(core_rid)
    check("a rejected core request un-covers its slugs", "core-1" not in mf._covered)
    check("and re-queues core", any(op[1] == "core" for op in mf._ops))
    mf._ops = []
    big = {f"core-{i}" for i in range(40)}
    mf.set_slugs(big, replace=True); mf._apply_ops(ws)
    mf.set_slugs(big - {"core-0"}, replace=True); mf._apply_ops(ws)   # one fell off
    check("one fallen-off core slug does NOT rebuild (cheap to keep)",
          "core" in mf._groups)
    mf.set_slugs({f"core-{i}" for i in range(5)}, replace=True); mf._apply_ops(ws)  # 35 fell off
    check("real core drift (>20 fallen off) triggers a one-request rebuild",
          "core" not in mf._groups and any(op[1] == "core" for op in mf._ops))
    unsubs = [m for m in ws.sent if "unsubscribe" in m]
    check("rebuild unsubscribed the old core request(s)", len(unsubs) >= 1)


def test_gridiron_value_window() -> None:
    """Rob's rule (Sep 5 2026): Pinnacle is the line; bet toward the model,
    away from Pinnacle; favorable rungs only. Rung units = home line."""
    import app as _app
    # Pinnacle USC -51, model USC -45 → model says USC covers LESS → dog (away) is value,
    # favorable rungs give the dog MORE points = home line more negative.
    side, d = _app._gridiron_value_side("spread", -45.0, -51.0, "away", "home")
    check("model -45 vs Pinnacle -51 → the DOG (away) is value", side == "away" and d == -1)
    # BOOK-LINE MODE through the shared rule (Rob, Sep 6 2026: "move to the
    # line, and 1 favorable rung" — the line is the bound for BOTH sides)
    _orig = _app._book_line_center
    _app._book_line_center = lambda sb, mid, mt, now: ((-51.0, "pinnacle") if mt == "spread" else (51.0, "pinnacle"))
    try:
        rule = _app._gridiron_line_rule(None, {"id": "x"}, {"odds": {}}, {"spread_fit": {"sd": 16.1}},
                                        "spread", -45.0, 45.0, [], None)
        check("book line centers and bounds both sides at −51",
              rule["center"] == -51.0 and rule["bounds"] == {"away": -51.0, "home": -51.0}
              and rule["center_src"] == "pinnacle", f"got {rule}")
        check("model −45 vs Pinnacle −51 → dog is value side", rule["value_side"] == "away")
        leg = _app._gridiron_seat_legal
        check("dog +51.5 (one rung past) is legal", leg(rule, "spread", "away", -51.5))
        check("dog +50.5 (inside the line) is REFUSED", not leg(rule, "spread", "away", -50.5))
        check("dog +51 exactly AT the line is REFUSED (one rung minimum)", not leg(rule, "spread", "away", -51.0))
        check("favorite −50.5 (one rung past) is legal", leg(rule, "spread", "home", -50.5))
        check("favorite −51.5 (more points laid) is REFUSED", not leg(rule, "spread", "home", -51.5))
        check("dog +61.5 is outside the 10-pt tail → REFUSED", not leg(rule, "spread", "away", -61.5))
        rt = _app._gridiron_line_rule(None, {"id": "x"}, {"odds": {}}, {"total_fit": {"sd": 12}},
                                      "total", 56.0, 56.0, [], None)
        check("total: Pinnacle 51, model 56 → OVER is value", rt["value_side"] == "over" and rt["center"] == 51.0)
        check("over 50.5 legal, over 51.5 refused", leg(rt, "total", "over", 50.5) and not leg(rt, "total", "over", 51.5))
        check("under 51.5 legal, under 50.5 refused", leg(rt, "total", "under", 51.5) and not leg(rt, "total", "under", 50.5))
    finally:
        _app._book_line_center = _orig
    # NO BOOK LINE through the same rule: ML-implied number + capped model
    _app._book_line_center = lambda sb, mid, mt, now: (None, None)
    try:
        d = {"odds": {"moneyline": {"polymarket": {"ladder": [
            {"side": "away", "quote": {"bid": 0.015, "ask": 0.02}}]}}}}
        rule = _app._gridiron_line_rule(None, {"id": "x"}, d, {"spread_fit": {"sd": 16.132}},
                                        "spread", -20.5, 20.5, [], None)
        check("no book line → venue ML centers (~−34), model capped to −27",
              rule["center_src"] == "venue_ml" and -35.5 < rule["center"] < -32.5
              and rule["model_capped"] == round(rule["center"] + 7.0, 1), f"got {rule}")
        check("dog is value; +34.5 legal only if past the ML number",
              rule["value_side"] == "away" and leg(rule, "spread", "away", round(rule["center"] - 0.5, 1))
              and not leg(rule, "spread", "away", round(rule["center"] + 0.5, 1)))
        rule2 = _app._gridiron_line_rule(None, {"id": "x"}, {"odds": {}}, {"spread_fit": {"sd": 16.132}},
                                         "spread", -20.5, 20.5, [], None)
        check("nothing but the model → model bounds both sides, still bet",
              rule2["center_src"] == "model" and rule2["bounds"] == {"away": -20.5, "home": -20.5})
    finally:
        _app._book_line_center = _orig
    # model agrees with the line → symmetric ±1, side by edge
    side, d = _app._gridiron_value_side("spread", -20.5, -20.5, "away", "home")
    check("model at the line → no forced side", side is None and d == 0)
    check("1/51 stub is a placeholder", _app._gridiron_is_placeholder(0.01, 0.51))
    check("43/44 real book is not", not _app._gridiron_is_placeholder(0.43, 0.44))
    usc = [("away", -34.5, 0.01, 0.51), ("home", 0.5, 0.49, 0.99), ("away", 14.5, 0.11, 0.30),
           ("away", 16.5, 0.34, 0.35), ("away", 20.5, 0.43, 0.44), ("away", 24.5, 0.33, 0.57),
           ("home", -20.5, 0.56, 0.57), ("home", -16.5, 0.65, 0.66)]
    c, src = _app._gridiron_ladder_center(usc, "spread", 18.34)
    check("venue line on the USC ladder = -20.5 (tight 43/44), not the stubs, not zero",
          c == -20.5 and src == "venue", f"got {c} {src}")
    c2, s2 = _app._gridiron_ladder_center([("over", 35.5, 0.45, 0.49)], "total", 55.1)
    check("a lone stray quote 20 pts off the model is not a venue line", c2 is None)
    c3, s3 = _app._gridiron_ladder_center([("over", 59.5, 0.25, 0.29)], "total", 51.1)
    check("a 25/29 book is not a 50/50 mark", c3 is None)


def test_gridiron_bounds() -> None:
    """Rob's no-book-line rule (Sep 6 2026): venue ML → spread + the model
    (capped ±7), every seat at least one rung past the MORE favorable of
    the two for its side."""
    import app as _app
    pm = {"spread_fit": {"sd": 13.115}}
    # Chicago @ Carolina: YES=away at 59.5/60 → home line ≈ +3.3 (Pinnacle +3)
    d = {"odds": {"moneyline": {"polymarket": {"ladder": [
        {"side": "away", "quote": {"bid": 0.595, "ask": 0.60}},
        {"side": "home", "synthetic": True, "quote": {"bid": 0.40, "ask": 0.405}}]}}}}
    ml = _app._gridiron_ml_line(d, pm, "away", "home")
    check("ML 59.5/60 on the away side → home line +3.3", ml is not None and abs(ml - 3.3) < 0.15, f"got {ml}")
    d2 = {"odds": {"moneyline": {"polymarket": {"ladder": [
        {"side": "away", "quote": {"bid": 0.015, "ask": 0.02}}]}}}}
    ml2 = _app._gridiron_ml_line(d2, {"spread_fit": {"sd": 16.132}}, "away", "home")
    check("WKU 1.5/2.0 → Georgia about −34", ml2 is not None and -35.5 < ml2 < -32.5, f"got {ml2}")
    d3 = {"odds": {"moneyline": {"polymarket": {"ladder": [
        {"side": "away", "quote": {"bid": 0.03, "ask": 0.415}}]}}}}
    check("a 3/41.5 ML book is not a line", _app._gridiron_ml_line(d3, pm, "away", "home") is None)
    check("no ML ladder → None", _app._gridiron_ml_line({"odds": {}}, pm, "away", "home") is None)
    # Rob's literal example: model +20, ML +25 → WKU +26 only, Georgia −19 only
    b, mc, c = _app._gridiron_bounds("spread", -25.0, -20.0, "away", "home")
    check("bounds: home −20 / away −25, center = the market", b == {"home": -20.0, "away": -25.0} and c == -25.0, f"got {b} {c}")
    pb = _app._gridiron_past_bound
    check("WKU +25.5 is allowed", pb("spread", "away", -25.5, b["away"], "away", "home"))
    check("WKU +24.5 is REFUSED (inside the bound)", not pb("spread", "away", -24.5, b["away"], "away", "home"))
    check("Georgia −19.5 is allowed", pb("spread", "home", -19.5, b["home"], "away", "home"))
    check("Georgia −20.5 is REFUSED", not pb("spread", "home", -20.5, b["home"], "away", "home"))
    # the cap: ML −34, model −20.5 → model capped to −27; dog is value
    b, mc, c = _app._gridiron_bounds("spread", -34.0, -20.5, "away", "home")
    check("model capped to −27 (7 past the market)", mc == -27.0 and b == {"home": -27.0, "away": -34.0}, f"got {mc} {b}")
    side, _d = _app._gridiron_value_side("spread", mc, -34.0, "away", "home")
    check("capped model −27 vs market −34 → the DOG is value", side == "away")
    check("WKU +34.5 allowed, +33.5 refused",
          pb("spread", "away", -34.5, b["away"], "away", "home") and not pb("spread", "away", -33.5, b["away"], "away", "home"))
    # no market number → the model bounds both sides
    b, mc, c = _app._gridiron_bounds("spread", None, -20.5, "away", "home")
    check("model-only: both bounds = model, center = model", b == {"home": -20.5, "away": -20.5} and c == -20.5)
    check("model-only: dog +21.5 ok, +20.5 (AT the model) refused",
          pb("spread", "away", -21.5, -20.5, "away", "home") and not pb("spread", "away", -20.5, -20.5, "away", "home"))
    # totals: venue mark 51, model 56 → over past 51 (lower), under past 56 (higher)
    b, mc, c = _app._gridiron_bounds("total", 51.0, 56.0, "over", "under")
    check("total bounds over 51 / under 56", b == {"over": 51.0, "under": 56.0}, f"got {b}")
    check("over 50.5 ok, over 51.5 refused",
          pb("total", "over", 50.5, 51.0, "over", "under") and not pb("total", "over", 51.5, 51.0, "over", "under"))
    check("under 56.5 ok, under 55.5 refused",
          pb("total", "under", 56.5, 56.0, "over", "under") and not pb("total", "under", 55.5, 56.0, "over", "under"))


def test_game_sport_key() -> None:
    """The pm-snapshot tick stamps `_sport`; markets rows say `sport`. The
    NFL props pass read only `sport` and was blind to football for 15
    days (Sep 6 2026)."""
    import app as _app
    check("tick dict (_sport) reads as NFL", _app._game_sport({"id": "x", "_sport": "NFL"}) == "NFL")
    check("markets row (sport) reads as NFL", _app._game_sport({"id": "x", "sport": "NFL"}) == "NFL")
    check("no key → None, not a crash", _app._game_sport({"id": "x"}) is None)


def test_pin_line_center() -> None:
    """Pinnacle's line in rung units from the cached slate shape (the real
    Northern Arizona @ Arizona event, Sep 5 2026)."""
    import app as _app
    ev = [{"away_team": "Northern Arizona", "home_team": "Arizona",
           "bookmakers": [{"key": "pinnacle", "markets": [
               {"key": "totals", "outcomes": [{"name": "Over", "point": 59.0}, {"name": "Under", "point": 59.0}]},
               {"key": "spreads", "outcomes": [{"name": "Arizona", "point": -33.0}, {"name": "Northern Arizona", "point": 33.0}]}]}]}]
    check("spread center = Pinnacle's HOME line (-33)",
          _app._pin_line_from_events(ev, "Northern Arizona Lumberjacks", "Arizona Wildcats", "spread") == -33.0)
    check("total center = the Over point (59)",
          _app._pin_line_from_events(ev, "Northern Arizona Lumberjacks", "Arizona Wildcats", "total") == 59.0)
    check("unknown game → None", _app._pin_line_from_events(ev, "Texas Longhorns", "Ohio State Buckeyes", "spread") is None)


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
        # The stuck line, NOT the ttl: they diverged when the opener's
        # designed workload (two passes, ~180-200s healthy) outgrew its
        # 180s lease TTL — the renewer keeps the lease alive regardless.
        line = spec.stuck_s or spec.ttl_s
        now = 1_000_000.0

        # Running, but inside its stuck line — silence. A healthy full-slate
        # opener tick (~182s, past the old ttl-based line) must be silent.
        r._started["opener"] = now - max(line - 5, spec.ttl_s + 5)
        r._overrun_check("opener", spec, now)
        check("inside its stuck line => no alarm", not rows and not pings,
              f"rows={len(rows)} pings={len(pings)}")

        # Past the stuck line — one failed tick row and one URGENT ping.
        r._started["opener"] = now - (line + 30)
        r._overrun_check("opener", spec, now)
        check("past its stuck line => failed tick recorded",
              len(rows) == 1 and rows[0]["ok"] is False
              and str(rows[0]["error"]).startswith("overrun:"),
              f"got {rows}")
        check("past its stuck line => one urgent ping",
              len(pings) == 1 and pings[0][1] is True, f"got {pings}")

        # Still stuck next tick — must NOT re-ping every minute.
        r._overrun_check("opener", spec, now + 60)
        check("stuck lane pings once per episode",
              len(pings) == 1 and len(rows) == 1,
              f"rows={len(rows)} pings={len(pings)}")

        # Completing re-arms the alarm for the next episode.
        r._started.pop("opener", None)
        r._stuck.discard("opener")
        r._started["opener"] = now - (line + 30)
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
              test_ws_quote_presence, test_ws_mkts_request_budget,
              test_gridiron_value_window, test_pin_line_center,
              test_gridiron_bounds, test_game_sport_key,
              test_lane_covers_its_documented_engines,
              test_side_and_phase, test_ttls_agree_with_engines):
        t()
    print(f"\n  {len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        print("  FAILED: " + ", ".join(_FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
