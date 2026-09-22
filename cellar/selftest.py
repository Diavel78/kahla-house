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
    check("money lanes are exactly opener/repeg/harvest/scalp/pair",
          money == {"opener", "repeg", "harvest", "scalp", "pair"},
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
        # PER-CONNECTION presence (Sep 6 2026): a row written by conn 1 is
        # vouched for by conn 1's liveness only
        _app.WS_QUOTES[slug] = (41.0, 43.0, old_ts)
        _app.WS_QUOTE_CONN[slug] = 1
        _app.WS_MKTS_CONN[1] = {"epoch": 3, "up": True, "rx": _t.monotonic()}
        _app.WS_QUOTE_EPOCH[slug] = 3
        check("conn-1 row + conn 1 live => fresh (conn 0 state irrelevant)",
              _app._ws_quote(slug) == (41.0, 43.0))
        _app.WS_MKTS_CONN[1]["up"] = False
        check("conn 1 down => its rows miss even with conn 0 up", _app._ws_quote(slug) is None)
    finally:
        _app.WS_QUOTES.pop(slug, None); _app.WS_QUOTE_EPOCH.pop(slug, None)
        _app.WS_QUOTE_CONN.pop(slug, None); _app.WS_MKTS_CONN.pop(1, None)
        _app.WS_MKTS_EPOCH, _app.WS_MKTS_UP, _app.WS_MKTS_LAST_RX = saved


def test_ws_mkts_request_budget() -> None:
    """Markets-feed request budget + PACKING (Sep 6 2026): ladders pool into
    cross-game packs (one request per PACK_SLUGS rungs), core keeps its
    reserve and evicts a pack to seat itself, a rejection un-covers and
    re-pends, and a full pack budget repacks from the live ladder set."""
    import json as _json
    import time as _t
    from cellar import wsfeed as W

    class FakeWS:
        def __init__(self): self.sent = []
        def send(self, raw): self.sent.append(_json.loads(raw))
    def fresh():
        mf = W.MarketsFeed.__new__(W.MarketsFeed)
        import threading as _th
        mf._lock = _th.Lock(); mf._groups = {}; mf._rid_slugs = {}
        mf._covered = set(); mf._ops = []; mf._expiry = {}; mf._gseq = 0
        mf._base_seen = set(); mf._dirty_add = None
        mf._ladders = {}; mf._pending = {}; mf._last_repack = 0.0; mf._pack_hold_until = 0.0
        mf._last_pack_build = 0.0; mf._pack_born = {}; mf._pack_audited = set()
        mf._miss_count = {}; mf._parked = {}; mf.audit_stats = {"packs": 0, "silent": 0, "missing": 0}
        mf.sb = None; mf.conn = 0; mf._core_rebuilt_at = 0.0; mf._last_hb = 0.0; mf.routed = 0
        return mf
    mf = fresh(); ws = FakeWS()
    for i in range(12):
        mf.add_group(f"lad{i}", {f"l{i}-{k}" for k in range(3)}, _t.time() + 3600 * (i + 1))
    mf._apply_ops(ws)
    check("12 ladders batch — nothing subscribed before the batch window",
          mf._pack_budget_used() == 0 and len(mf._pending) == 36)
    mf._pack_flush(ws, force=True)
    check("12 ladders (36 rungs) → ONE pack request", mf._pack_budget_used() == 1 and len(mf._covered) == 36,
          f"rids {mf._pack_budget_used()} covered {len(mf._covered)}")
    # a big board: 300 ladders × 10 rungs = 3,000 rungs
    for i in range(300):
        mf.add_group(f"big{i}", {f"b{i}-{k}" for k in range(10)}, _t.time() + 60 * (i + 1))
    mf._apply_ops(ws); mf._pack_flush(ws, force=True)
    budget = W.MKTS_MAX_RIDS - W.PACK_CORE_RESERVE
    check("packs stop at the pack budget (core reserve kept)", mf._pack_budget_used() == budget,
          f"got {mf._pack_budget_used()}")
    check("covered ≈ budget × PACK_SLUGS, the rest pending (REST prices them)",
          len(mf._covered) <= budget * W.PACK_SLUGS and len(mf._pending) > 0,
          f"covered {len(mf._covered)} pending {len(mf._pending)}")
    check("nearest-expiry rungs were packed first",
          all(s in mf._covered for s in {"b0-0", "b5-3"}) and "b299-0" not in mf._covered)
    mf.set_slugs({"core-1", "core-2"}, replace=True); mf._apply_ops(ws)
    check("core seats inside the reserve without evicting a pack",
          "core" in mf._groups and mf._pack_budget_used() == budget
          and sum(len(r) for r, _ in mf._groups.values()) <= W.MKTS_MAX_RIDS)
    core_rid = mf._groups["core"][0][0]
    mf._on_rejected(core_rid)
    check("a rejected core request un-covers its slugs", "core-1" not in mf._covered)
    check("and re-queues core", any(op[1] == "core" for op in mf._ops))
    mf._ops = []
    prid = next(r for g, (rs, _s) in mf._groups.items() if g.startswith("pack") for r in rs)
    pslugs = set(mf._rid_slugs[prid])
    mf._on_rejected(prid)
    check("a rejected pack un-covers and re-pends its rungs",
          not (pslugs & mf._covered) and pslugs <= set(mf._pending))
    # repack: budget full + rungs still wanting → tear packs down, re-queue live set
    mf2 = fresh(); ws2 = FakeWS()
    for i in range(300):
        mf2.add_group(f"big{i}", {f"b{i}-{k}" for k in range(10)}, _t.time() + 60 * (i + 1))
    mf2._apply_ops(ws2); mf2._pack_flush(ws2, force=True)
    n_before = len(ws2.sent)
    mf2._pack_flush(ws2, force=True)            # budget full, packs fresh → NO repack
    check("a full budget with fresh packs and nothing dead does NOT repack",
          mf2._pack_budget_used() == budget and len(ws2.sent) == n_before)
    mf2._base_seen |= set(mf2._covered)             # healthy packs: every baseline arrived
    later = _t.time() + 3 * 3600 + W.REPACK_MIN_S   # ~140 ladders kicked off → dead weight
    mf2._pack_flush(ws2, nowt=later, force=True)
    unsubs = [m for m in ws2.sent[n_before:] if "unsubscribe" in m]
    check("dead weight → repack unsubscribed every pack and holds 2s",
          len(unsubs) == budget and mf2._pack_budget_used() == 0 and mf2._pack_hold_until > later)
    mf2._pack_flush(ws2, nowt=later + 3.0, force=True)
    mf2._base_seen |= set(mf2._covered)
    check("after the hold the LIVE set re-packs (dead rungs gone)",
          mf2._pack_budget_used() >= 1 and "b0-0" not in mf2._covered and "b299-0" in mf2._covered,
          f"rids {mf2._pack_budget_used()} b0 {'b0-0' in mf2._covered} b299 {'b299-0' in mf2._covered}")
    # BASELINE AUDIT: the venue acks nothing; a rung with no baseline 30s
    # after its pack went out was never subscribed
    mf4 = fresh(); ws4 = FakeWS()
    for i in range(4):
        mf4.add_group(f"a{i}", {f"a{i}-{k}" for k in range(5)}, _t.time() + 3600)
    mf4._apply_ops(ws4); mf4._pack_flush(ws4, force=True)
    pk = next(g for g in mf4._groups if g.startswith("pack"))
    all_slugs = set(mf4._groups[pk][1])
    for s in all_slugs - {"a3-0", "a3-1"}:
        mf4._base_seen.add(s)                      # 18 baselines arrived, 2 never did
    mf4._pack_flush(ws4, nowt=_t.time() + W.BASELINE_WAIT_S + 1.0)
    check("rungs without a baseline are un-covered and re-queued",
          "a3-0" not in mf4._covered and "a3-0" in mf4._pending and "a0-0" in mf4._covered)
    check("audit stats count the miss", mf4.audit_stats == {"packs": 1, "silent": 0, "missing": 2}, f"got {mf4.audit_stats}")
    mf5 = fresh(); ws5 = FakeWS()
    mf5.add_group("z", {"z-1", "z-2", "z-3"}, _t.time() + 3600)
    mf5._apply_ops(ws5); mf5._pack_flush(ws5, force=True)
    n5 = len(ws5.sent)
    mf5._pack_flush(ws5, nowt=_t.time() + W.BASELINE_WAIT_S + 1.0)   # zero baselines → silent failure
    check("a pack with ZERO baselines is torn down and re-queued whole",
          mf5._pack_budget_used() == 0 and {"z-1", "z-2", "z-3"} <= set(mf5._pending)
          and any("unsubscribe" in m for m in ws5.sent[n5:]) and mf5.audit_stats["silent"] == 1)
    mf5._pack_flush(ws5, nowt=_t.time() + W.BASELINE_WAIT_S + 10.0, force=True)   # re-sent
    mf5._pack_flush(ws5, nowt=_t.time() + 2 * W.BASELINE_WAIT_S + 20.0)          # misses again → parked
    check("a rung that misses twice is PARKED (REST prices it) instead of churning",
          "z-1" in mf5._parked and "z-1" not in mf5._pending and mf5._pack_budget_used() == 0)
    mf5._pack_flush(ws5, nowt=_t.time() + 2 * W.BASELINE_WAIT_S + 20.0 + W.PARK_S + 1, force=True)
    check("after PARK_S it is tried again", "z-1" in mf5._covered)
    # core drift (unchanged semantics)
    mf3 = fresh(); ws3 = FakeWS()
    big = {f"core-{i}" for i in range(40)}
    mf3.set_slugs(big, replace=True); mf3._apply_ops(ws3)
    mf3.set_slugs(big - {"core-0"}, replace=True); mf3._apply_ops(ws3)
    check("one fallen-off core slug does NOT rebuild (cheap to keep)", "core" in mf3._groups)
    mf3.set_slugs({f"core-{i}" for i in range(5)}, replace=True); mf3._apply_ops(ws3)
    check("real core drift (>20 fallen off) triggers a one-request rebuild",
          "core" not in mf3._groups and any(op[1] == "core" for op in mf3._ops))


def test_gridiron_qb_adjust() -> None:
    """The QB adjustment (Sep 13 2026): _gridiron_proj adds each side's
    football_qb_adj points to its expected score, stamps the note every
    football bet carries, applies NOTHING when the rows are stale, and
    matches team names the way the ratings lookup does."""
    import app as _app
    snap = {"league_avg": 24.0, "params": {"hfa": 2.0},
            "ratings": {"Atlanta Falcons": {"off": 26.0, "def": 22.0},
                        "Tampa Bay Buccaneers": {"off": 24.0, "def": 24.0}}}
    _o_snap, _o_adj = _app._power_snapshot, _app._football_qb_adj
    _app._power_snapshot = lambda sb, sport: snap
    try:
        _app._football_qb_adj = lambda sb, sport: ({}, False)
        m0, t0, _ = _app._gridiron_proj(None, "NFL", "Tampa Bay Buccaneers @ Atlanta Falcons")
        adj = {"Atlanta Falcons": {"adj": -3.8, "starter": "Cooper Rush", "src": "depth_chart",
                                   "rated_on": "Kirk Cousins"},
               "Tampa Bay Buccaneers": {"adj": 1.0, "starter": "X", "src": "depth_chart",
                                        "rated_on": "X"}}
        _app._football_qb_adj = lambda sb, sport: (adj, False)
        m1, t1, _ = _app._gridiron_proj(None, "NFL", "Tampa Bay Buccaneers @ Atlanta Falcons")
        check("home QB dock + away QB bump move the margin by their sum",
              abs((m1 - m0) - (-3.8 - 1.0)) < 1e-9, f"{m0} → {m1}")
        check("the total moves by the net points", abs((t1 - t0) - (-2.8)) < 1e-9)
        note = _app._gridiron_qb_note("NFL", "Tampa Bay Buccaneers @ Atlanta Falcons")
        check("the bet stamp names the starter and who the rating was built on",
              bool(note) and note["home"] == -3.8 and note["home_qb"] == "Cooper Rush"
              and note["home_rated_on"] == "Kirk Cousins" and note["stale"] is False)
        _app._football_qb_adj = lambda sb, sport: ({}, True)
        m2, _, _ = _app._gridiron_proj(None, "NFL", "Tampa Bay Buccaneers @ Atlanta Falcons")
        note2 = _app._gridiron_qb_note("NFL", "Tampa Bay Buccaneers @ Atlanta Falcons")
        check("stale rows apply nothing and say so", m2 == m0 and note2 and note2["stale"] is True)
        check("substring team match (the ratings' own rule)",
              _app._fb_adj_for({"Atlanta Falcons": {"adj": 1}}, "Atlanta")["adj"] == 1
              and _app._fb_adj_for({"Atlanta Falcons": {"adj": 1}}, "Tampa") is None)
    finally:
        _app._power_snapshot, _app._football_qb_adj = _o_snap, _o_adj


def test_cfbd_consensus() -> None:
    """Rob, Sep 17 2026: the college pre-market line — SP+/FPI in points, Elo/25,
    one home edge; team match = longest CFBD school name prefixing ours."""
    import app as _app
    t = {"Notre Dame": 25.0, "Purdue": 3.0, "Miami": 20.0, "Miami (OH)": 4.0}
    check("prefix match picks the school", _app._cfbd_team(t, "Purdue Boilermakers") == 3.0)
    check("longest prefix wins: Miami (OH) RedHawks → Miami (OH)", _app._cfbd_team(t, "Miami (OH) RedHawks") == 4.0)
    check("Miami Hurricanes → Miami", _app._cfbd_team(t, "Miami Hurricanes") == 20.0)
    _app._CFBD_CACHE.update(at=_app._time.time(), rows={
        "sp": {"Notre Dame": 25.0, "Purdue": 3.0},
        "fpi": {"Notre Dame": 22.0, "Purdue": 4.0},
        "elo": {"Notre Dame": 2000.0, "Purdue": 1500.0},
        "srs": {"Notre Dame": 40.0, "Purdue": 1.0}})
    got = _app._cfbd_consensus(None, "Notre Dame Fighting Irish @ Purdue Boilermakers")
    # home = Purdue: sp 3-25+2.5=-19.5, fpi 4-22+2.5=-15.5, elo -500/25+2.5=-17.5 → mean -17.5; SRS excluded
    check("consensus home margin −17.5 (SRS held out)", got is not None and abs(got[0] + 17.5) < 0.01 and got[1]["n_src"] == 3, f"got {got}")
    _app._CFBD_CACHE.update(at=_app._time.time(), rows={"nfelo": {"Kansas City Chiefs": 6.0, "Miami Dolphins": -2.0}})
    got = _app._cfbd_consensus(None, "Kansas City Chiefs @ Miami Dolphins", "NFL")
    check("NFL prior: nfelo pts diff + 1.8 home edge → −6.2 (KC by 6.2)", got is not None and abs(got[0] + 6.2) < 0.01, f"got {got}")
    _app._CFBD_CACHE.update(at=0.0, rows=None)
    # moneyline → spread saturates: a 95% favorite is a guess, a 72% favorite is a line
    pm = {"spread_fit": {"sd": 16.0}}
    def _d(bid, ask):
        return {"odds": {"moneyline": {"polymarket": {"ladder": [{"side": "home", "synthetic": False, "quote": {"bid": bid, "ask": ask}}]}}}}
    check("ML→spread refuses a 95% favorite", _app._gridiron_ml_line(_d(0.95, 0.96), pm, "away", "home") is None)
    check("ML→spread prices a 72% favorite", _app._gridiron_ml_line(_d(0.71, 0.73), pm, "away", "home") is not None)


def test_gridiron_key_hook() -> None:
    """Rob, Sep 17 2026: the dog never sits at +K−0.5, the favorite never at
    −K−0.5, when a good-hook rung pays. Rung units = home line."""
    import app as _app
    bh = _app._gridiron_bad_hook
    check("home dog +20.5 is the bad hook", bh("spread", "home", 20.5) is True)
    check("home dog +21.5 is the good hook", bh("spread", "home", 21.5) is False)
    check("away dog +20.5 (home line −20.5) is the bad hook", bh("spread", "away", -20.5) is True)
    check("home favorite −3.5 is the bad hook", bh("spread", "home", -3.5) is True)
    check("home favorite −2.5 is the good hook", bh("spread", "home", -2.5) is False)
    check("away favorite −6.5 (home line +6.5) is the good hook", bh("spread", "away", 6.5) is False)
    check("+4.5 is nobody's key number", bh("spread", "home", 4.5) is False)
    check("totals are out of scope", bh("total", "over", 44.5) is False)


def test_gridiron_move_favorable() -> None:
    """Rob, Sep 15 2026: a rung jump may only BENEFIT the seat — favorite
    down, dog up, over down, under up. Rung units = home line / the total."""
    import app as _app
    f = _app._gridiron_move_favorable
    check("favorite (home −7.5) → −6.5 lays fewer: favorable", f("spread", "home", -7.5, -6.5))
    check("favorite (home −7.5) → −9.5 lays more: REFUSED", not f("spread", "home", -7.5, -9.5))
    check("dog (away +7.5 = home −7.5) → +9.5 (home −9.5): favorable", f("spread", "away", -7.5, -9.5))
    check("dog +24.5 pulled to +10.5 (the tail pull): REFUSED", not f("spread", "away", -24.5, -10.5))
    check("home dog +3.5 → +4.5 (home line larger): favorable", f("spread", "home", 3.5, 4.5))
    check("over 45.5 → 44.5: favorable; → 46.5 refused", f("total", "over", 45.5, 44.5) and not f("total", "over", 45.5, 46.5))
    check("under 45.5 → 46.5: favorable; → 44.5 refused", f("total", "under", 45.5, 46.5) and not f("total", "under", 45.5, 44.5))
    check("same rung is not a move", not f("spread", "home", -7.5, -7.5) and not f("total", "over", 45.5, 45.5))
    check("unknown rung never moves", not f("spread", "home", None, -6.5))


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
            {"side": "away", "quote": {"bid": 0.115, "ask": 0.12}}]}}}}
        rule = _app._gridiron_line_rule(None, {"id": "x"}, d, {"spread_fit": {"sd": 16.132}},
                                        "spread", -15.5, 15.5, [], None)
        check("no book line → venue ML (88% favorite) centers (~−19), model −15.5 inside ±7 stays",
              rule["center_src"] == "venue_ml" and -21.0 < rule["center"] < -17.0
              and rule["model_capped"] == -15.5, f"got {rule}")
        check("dog is value; a dog rung is legal only past the ML number",
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
    check("WKU 1.5/2.0 (98% favorite) → NO line: past the 90% saturation guard (Sep 17 2026)", ml2 is None, f"got {ml2}")
    d2b = {"odds": {"moneyline": {"polymarket": {"ladder": [{"side": "away", "quote": {"bid": 0.115, "ask": 0.12}}]}}}}
    ml2b = _app._gridiron_ml_line(d2b, {"spread_fit": {"sd": 16.132}}, "away", "home")
    check("an 88% favorite still converts (about −19)", ml2b is not None and -21.0 < ml2b < -17.0, f"got {ml2b}")
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


def test_snipe_target() -> None:
    """The sell sniper's rule off a socket frame (Sep 6 2026): touch − 1
    tick or cost, whichever higher, post-only above the bid; 'self' when
    the frame's touch is our own price; None when already there."""
    import app as _app
    snap = {"our_ask": 96.0, "floor_c": 58.5, "tick": 1.0, "synth": False, "qty": 4}
    check("competitor at 88 → 87", _app._snipe_target(snap, 83.0, 88.0) == 87.0)
    check("competitor at 60, bid 59 → 60 (join, never cross)", _app._snipe_target(snap, 59.0, 60.0) == 60.0)
    check("competitor under cost → cost", _app._snipe_target(snap, 40.0, 50.0) == 58.5 + 0.5 or _app._snipe_target(snap, 40.0, 50.0) == 59.0)
    check("no ask at all → cost", _app._snipe_target(snap, 40.0, None) in (58.5, 59.0))
    check("frame's touch is our own price → 'self'", _app._snipe_target(snap, 83.0, 96.0) == "self")
    check("already at target → None", _app._snipe_target({**snap, "our_ask": 87.0}, 83.0, 88.0) is None)
    # synthetic (short) side: YES frame bid 12 / ask 17 → our side bid 83 / ask 88
    ss = {"our_ask": 96.0, "floor_c": 58.5, "tick": 1.0, "synth": True, "qty": 4}
    check("synthetic side flips the frame: → 87", _app._snipe_target(ss, 12.0, 17.0) == 87.0)


def test_entry_sync_guard() -> None:
    """The Braves 74¢ lesson (Sep 7 2026): the venue's BLENDED position
    avg may never overwrite a machine pick's entry — a post-only bid fills
    at its own limit, so a drift past a few ticks is the blend, not a fill.
    Hand-placed picks (no order_id) keep the sync the feature was built for."""
    import app as _app
    mach = {"order_id": "X", "source": "autobet"}
    ok, why = _app._entry_sync_ok(153, 0.7396, mach)          # 39.5¢ pick, venue says 74¢
    check("machine pick: 39.5¢ → 74¢ REFUSED as venue_blend", (ok, why) == (False, "venue_blend"))
    ok, why = _app._entry_sync_ok(153, 0.392, mach)           # 39.5 → 39.2, a real fill
    check("machine pick: 39.5¢ → 39.2¢ is 'same' (under the 0.5¢ gate)", (ok, why) == (False, "same"))
    ok, why = _app._entry_sync_ok(153, 0.375, mach)           # 2¢ better fill
    check("machine pick: 2¢ drift syncs", (ok, why) == (True, "ok"))
    ok, why = _app._entry_sync_ok(-330, 0.541, mach)          # 76.7¢ poison → lot 54.1
    check("collapse-UP poison refused", (ok, why) == (False, "venue_blend"))
    ok, why = _app._entry_sync_ok(153, 0.7396, {"source": "manual"})
    check("hand-placed pick still syncs", (ok, why) == (True, "ok"))
    check("no current entry → sync", _app._entry_sync_ok(None, 0.5, mach) == (True, "no_current"))


def test_lot_ledger_floor() -> None:
    """Sep 7 2026: the sell floor is OUR trade walk when it covers the held
    lot — never the venue's blended avgPx (74¢ on a 39.2¢ Braves lot)."""
    import app as _app
    def tr(slug, qty, cost, sell=False):
        t = {"marketSlug": slug, "qty": qty, "cost": {"value": cost}}
        if sell:
            t["realizedPnl"] = {"value": 0}
        return {"payload": {"trade": t}}
    rows = [tr("s", 20, 7.84), tr("s", 6, 2.43, sell=True)]          # the Braves lot
    lots = _app._lot_walk(rows)
    check("walk: 14 held after a 6-lot sell", abs(lots["s"]["qty"] - 14) < 1e-9)
    check("walk: cost stays 39.2¢/share (sell removes at running avg)",
          abs(lots["s"]["cost"] / lots["s"]["qty"] - 0.392) < 1e-6)
    check("floor = lot cost when ledger covers the position",
          abs(_app._lot_cost_c(lots["s"], 13.98) - 39.2) < 0.01)
    check("ledger under-covers (venue holds 19.5, we saw 1) → None",
          _app._lot_cost_c({"qty": 1.0, "cost": 0.33}, 19.5) is None)
    check("no lot → None", _app._lot_cost_c(None, 20) is None)
    # fractional fills: integer qty "0", qtyDecimal "0.1000" (3,012 such rows Sep 2026)
    frac = [{"payload": {"trade": {"marketSlug": "f", "qty": "0", "qtyDecimal": "0.1000",
                                    "cost": {"value": "0.055"}}}}] * 195
    lf = _app._lot_walk(frac + [tr("f", 1, 0.55)])
    check("fractional fills count at their decimal size (19.5 + 1 = 20.5)",
          abs(lf["f"]["qty"] - 20.5) < 1e-6)
    check("fractional fills price correctly (55¢)",
          abs(lf["f"]["cost"] / lf["f"]["qty"] - 0.55) < 1e-6)
    # the real Braves history: a CLOSED 59→60¢ round trip BEFORE today's lot
    rows2 = [tr("s", 20, 11.84), tr("s", 20, 12.06, sell=True)] + rows
    lots2 = _app._lot_walk(rows2)
    check("a closed earlier round trip does not blend into the held lot (venue said 74¢)",
          abs(lots2["s"]["cost"] / lots2["s"]["qty"] - 0.392) < 1e-6)


def test_no_mangled_fresh_kwarg() -> None:
    """Sep 9 2026: a regex patch turned `_pmm_positions_raw(client, fresh=True)`
    into `(clientfresh=True)` at the OMS executor's verify step — every
    football create crashed AFTER the order was placed (ghost orders, no
    pick) and the OMS pass died on its first bet for two days (217 ticks).
    The venue-read helpers must never be called with a mangled kwarg."""
    import inspect, re
    import app as _app
    src = inspect.getsource(_app)
    check("no `clientfresh=` anywhere in app.py", "clientfresh" not in src)
    for fn in (_app._pmm_positions_raw, _app._pmm_open_orders_raw):
        params = inspect.signature(fn).parameters
        check(f"{fn.__name__} takes (client, fresh)", list(params)[:2] == ["client", "fresh"])


def test_snipe_target_at_cost() -> None:
    """Rob, Sep 9 2026: sell at cost or one tick higher, immediately — the
    ask is the FLOOR, not touch − 1; one tick over the bid when the bid is
    at or above cost. Never under cost."""
    import app as _app
    snap = {"our_ask": 96.0, "floor_c": 59.0, "tick": 1.0, "synth": False, "qty": 4, "at_cost": True}
    check("cost+1 when that leads the book: competitor at 88 → 60", _app._snipe_target(snap, 50.0, 88.0) == 60.0)
    check("competitor already at 60 → we would not be the touch → cost (59)", _app._snipe_target(snap, 50.0, 60.0) == 59.0)
    check("competitor under cost → cost, never under", _app._snipe_target(snap, 50.0, 55.0) == 59.0)
    check("bid 60 sits over cost → 61, never crossing", _app._snipe_target(snap, 60.0, 62.0) == 61.0)
    check("already at cost+1 with the book clear → None", _app._snipe_target({**snap, "our_ask": 60.0}, 50.0, 88.0) is None)
    check("helper: no competitor → cost+1", _app._cost_plus_target(59.0, None, 1.0) == 60.0)
    check("sniper never steps UP to cost+1 on its own (lap's call)", _app._snipe_target({**snap, "our_ask": 59.0}, 50.0, 88.0) is None)
    check("sniper still steps DOWN to cost", _app._snipe_target({**snap, "our_ask": 70.0}, 50.0, 60.0) == 59.0)
    check("sniper still follows a bid over cost UP", _app._snipe_target({**snap, "our_ask": 59.0}, 60.0, 62.0) == 61.0)
    check("flag off keeps touch − 1", _app._snipe_target({**snap, "at_cost": False}, 83.0, 88.0) == 87.0)


def test_gridiron_join_touch() -> None:
    """Rob, Sep 9 2026: football seats rest AT the touch, not a tick over
    it (147/157 leaders filled in a median 3.3h and then earned nothing).
    Virgin books keep touch + 1; MLB is untouched."""
    import app as _app
    check("NFL spread with a real bid → join", _app._gridiron_join_touch("asc-nfl-mia-lv-2026-09-13-pos-14pt5", 45.0))
    check("CFB total with a real bid → join", _app._gridiron_join_touch("tsc-cfb-usc-rutge-2026-09-19-total-53pt5", 52.0))
    check("penny stub → NOT join (virgin rule keeps touch+1)", not _app._gridiron_join_touch("asc-nfl-mia-lv-2026-09-13-pos-14pt5", 1.0))
    check("MLB moneyline → NOT join", not _app._gridiron_join_touch("aec-mlb-nym-mia-2026-09-10", 45.0))
    snap = {"our_bid": 46.0, "qty": 20, "synth": False, "cap_c": 60.0, "master_c": 65.0, "tick": 1.0, "join": True}
    check("buy sniper on football: competitor bid 44 → 44, not 45", _app._snipe_buy_target(snap, 44.0, 50.0) == 44.0)
    check("buy sniper without join: competitor bid 44 → 45", _app._snipe_buy_target({**snap, "join": False}, 44.0, 50.0) == 45.0)
    check("buy sniper model wall: competitor 44, wall 41 → 41", _app._snipe_buy_target({**snap, "join": False, "wall_c": 41.0}, 44.0, 50.0) == 41.0)
    check("buy sniper model wall: already at wall → no move", _app._snipe_buy_target({**snap, "join": False, "wall_c": 41.0, "our_bid": 41.0}, 44.0, 50.0) is None)


def test_seat_topup_plan() -> None:
    """Rob, Sep 9 2026: always 20 combined between held and open. A dust or
    partial seat re-bids the remainder at the lane's peg; football joins
    the touch, MLB leads by a tick; caps and the master rule hold."""
    import app as _app
    check("dust 0.3 of 20, football, bid 45 → 19 @ 45 (join)", _app._seat_topup_plan(20, 0.3, 45.0, 46.0, 1.0, True, 60.0, 13.0) == (19, 45.0))
    check("partial 12 of 20, MLB, bid 44/ask 46 → 8 @ 45 (lead)", _app._seat_topup_plan(20, 12.0, 44.0, 46.0, 1.0, False, 60.0, 13.0) == (8, 45.0))
    check("MLB one-tick book → join", _app._seat_topup_plan(20, 12.0, 44.0, 45.0, 1.0, False, 60.0, 13.0) == (8, 44.0))
    check("full seat → nothing", _app._seat_topup_plan(20, 19.6, 45.0, 46.0, 1.0, True, 60.0, 13.0)[0] is None)
    check("football penny stub → virgin, not ours", _app._seat_topup_plan(20, 0.3, 1.0, 51.0, 1.0, True, 60.0, 13.0) == (None, "virgin"))
    check("over the 60¢ cap → no bid", _app._seat_topup_plan(20, 0.3, 66.0, 67.0, 1.0, True, 60.0, 13.0) == (None, "cap"))
    check("master rule trims the size: 19 @ 60 = $11.40 ok; 20 @ 65 → 20 (13/0.65=20)", _app._seat_topup_plan(20, 0.0, 60.0, 61.0, 1.0, True, 60.0, 13.0) == (20, 60.0))
    check("master rule trims: 20 @ 60.0 on MLB → peg 61 > cap → cap", _app._seat_topup_plan(20, 0.0, 60.0, 62.0, 1.0, False, 60.0, 13.0) == (None, "cap"))


def test_vsin_dates_and_names() -> None:
    """Sep 10 2026: VSiN games carry their section date; the matcher
    requires it, strips poll ranks, and takes 'Miami FL Hurricanes' for
    'Miami Hurricanes' but never 'Miami (OH) RedHawks'."""
    import app as _app
    import handicapper_web as hw
    from datetime import date
    check("header parse", _app._vsin_group_header("MLB - Friday, Sep 11 Sep 11", date(2026, 9, 10)) == ("MLB", "2026-09-11"))
    check("header year rollover", _app._vsin_group_header("NFL - Sunday, Jan 3 Jan 3", date(2026, 12, 28)) == ("NFL", "2027-01-03"))
    check("non-header cell", _app._vsin_group_header("Tampa Bay Rays") == (None, None))
    check("rank + FL", hw._vsin_team_match("Miami Hurricanes", "Florida A&M Rattlers", "(7) Miami FL Hurricanes", "Florida A&M"))
    check("Miami OH is not Miami FL", not hw._vsin_team_match("Miami Hurricanes", "Florida A&M Rattlers", "Miami (OH) RedHawks", "Florida A&M"))
    evs = [{"away_team": "Colorado Rockies", "home_team": "New York Yankees", "date": "2026-09-10"},
           {"away_team": "Colorado Rockies", "home_team": "Detroit Tigers", "date": "2026-09-11"}]
    check("date gate: series game on the wrong day is not matched",
          hw._vsin_pick_event(evs, "Colorado Rockies", "New York Yankees", "2026-09-11") is None)
    check("date gate: same day matches",
          hw._vsin_pick_event(evs, "Colorado Rockies", "New York Yankees", "2026-09-10") is evs[0])
    check("no game date falls back to names", hw._vsin_pick_event(evs, "Colorado Rockies", "Detroit Tigers", None) is evs[1])
    m = hw._vsin_team_match
    check("ST = State", m("Oklahoma State Cowboys", "Oregon Ducks", "Oklahoma ST Cowboys", "(6) Oregon Ducks"))
    check("VSiN typo absorbed when the other side is exact", m("Iowa Hawkeyes", "Iowa State Cyclones", "(21) Iowa Hawkies", "Iowa ST Cyclones"))
    check("directions: E Tennessee ST", m("North Carolina Tar Heels", "East Tennessee State Buccaneers", "North Carolina Tar Heels", "E Tennessee ST"))
    check("directions: C Michigan", m("Central Michigan Chippewas", "Colgate Raiders", "C Michigan Chippewas", "Colgate"))
    check("aliases: UL Monroe / LA Monroe", m("UAB Blazers", "UL Monroe Warhawks", "UAB Blazers", "LA Monroe Warhawks"))
    check("aliases: UTSA / Texas-San Antonio", m("Texas State Bobcats", "UTSA Roadrunners", "Texas ST Bobcats", "Texas-San Antonio Roadrunners"))
    check("aliases: Wash Commanders", m("Philadelphia Eagles", "Washington Commanders", "Philadelphia Eagles", "Wash Commanders"))
    check("apostrophe: Hawai'i", m("Hawai'i Rainbow Warriors", "New Mexico State Aggies", "Hawaii Rainbow Warriors", "New Mexico ST Aggies"))
    check("weak side alone never matches", not m("Iowa Hawkeyes", "Kansas Jayhawks", "Iowa ST Cyclones", "(23) Missouri Tigers"))
    check("Miami OH still not Miami FL", not m("Miami Hurricanes", "Florida A&M Rattlers", "Miami (OH) RedHawks", "Florida A&M"))
    check("Middle Tenn ST", m("Marshall Thundering Herd", "Middle Tennessee Blue Raiders", "Marshall Thundering Herd", "Middle Tenn ST Blue Raiders"))
    check("FL Atlantic", m("Florida Atlantic Owls", "Navy Midshipmen", "FL Atlantic Owls", "Navy Midshipmen"))
    check("C Conn ST (school fallback)", m("Toledo Rockets", "Central Connecticut Blue Devils", "Toledo Rockets", "C Conn ST"))


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


def test_pair_plan() -> None:
    """THE MIDDLE PAIR (Rob, Sep 18 2026): combined bids ≤ cap, the empty
    leg chases up to cap − the held leg's cost, lone leg after a sale floors
    at cap − sold price, a ≤100 pair rides with no asks, both held at T−30
    cancel the asks, kickoff stops bids."""
    import app as _app
    def leg(**kw):
        d = {"h": 0.0, "cost": None, "sold": None, "bid": None, "ask": None,
             "tick": 0.5, "rent": True}
        d.update(kw)
        return d
    # both empty, touches 43.5 + 57.5 = 101 → both join
    p = _app._pair_plan({"a": leg(bid=43.5, ask=44.0), "b": leg(bid=57.5, ask=58.0)}, 15, 110, 2000)
    check("both empty under cap → join both touches",
          p["a"]["bid"] == (43.5, 15) and p["b"]["bid"] == (57.5, 15) and not p["a"]["ask"])
    # both empty, touches sum 114 > 110 → each gives up half the excess
    p = _app._pair_plan({"a": leg(bid=55.0, ask=56.0), "b": leg(bid=59.0, ask=60.0)}, 15, 110, 2000)
    check("both empty over cap → split the excess",
          p["a"]["bid"][0] + p["b"]["bid"][0] <= 110.0 and p["a"]["bid"][0] == 53.0)
    # a held at 44, b empty touching 70 → the 65c leg fence REFUSES b (Rob,
    # Sep 21 2026: a hedge we cannot buy at 65 is a hedge we skip). The held
    # leg still asks at cost, which is the flat exit the machine aims for.
    p = _app._pair_plan({"a": leg(h=15, cost=44.0, bid=43.0, ask=46.0),
                         "b": leg(bid=70.0, ask=71.0)}, 15, 110, 2000)
    check("one held, partner touch 70 → REFUSED by the 65c leg fence",
          p["b"]["bid"] is None and "leg_cap" in p["b"]["why"])
    # …and inside the fence the hedge still chases cap − cost
    p2 = _app._pair_plan({"a": leg(h=15, cost=44.0, bid=43.0, ask=46.0),
                          "b": leg(bid=60.0, ask=61.0)}, 15, 110, 2000)
    check("one held, partner touch 60 → hedge joins it", p2["b"]["bid"] == (60.0, 15))
    check("one held, never sold → ask at cost + tick when it leads", p["a"]["ask"] == (44.5, 15))
    # a sold at 60, b held at 57 → b floor = 110 − 60 = 50, sells at 50.5 if it leads
    p = _app._pair_plan({"a": leg(sold=60.0, bid=58.0, ask=61.0),
                         "b": leg(h=15, cost=57.0, bid=40.0, ask=55.0)}, 15, 110, 2000)
    check("lone leg after a sale → floor cap − sold (50 → 50.5)", p["b"]["ask"] == (50.5, 15))
    check("sold leg re-bids capped at cap − held cost (53)", p["a"]["bid"] == (53.0, 15))
    # both held at 44 + 55 = 99 → the lock rides, no asks
    p = _app._pair_plan({"a": leg(h=15, cost=44.0, bid=44.0, ask=45.0),
                         "b": leg(h=15, cost=55.0, bid=56.0, ask=57.0)}, 15, 110, 2000)
    check("both held ≤100 → no asks (lock rides)", p["a"]["ask"] is None and p["b"]["ask"] is None
          and "lock_rides" in p["a"]["why"])
    # both held at 102, 45 min out → asks at cost; 20 min out → none
    two = {"a": leg(h=15, cost=45.0, bid=44.0, ask=47.0), "b": leg(h=15, cost=57.0, bid=56.0, ask=59.0)}
    p = _app._pair_plan(two, 15, 110, 45)
    check("both held >100 before T−30 → asks at cost", p["a"]["ask"] == (45.5, 15) and p["b"]["ask"] == (57.5, 15))
    p = _app._pair_plan(two, 15, 110, 20)
    check("both held inside T−30 → no asks, hold for the middle",
          p["a"]["ask"] is None and "t30_middle" in p["b"]["why"])
    # An unreadable book leaves BOTH orders alone (Sep 19 2026: one failed read
    # cancelled a resting bid and re-placed it at the back of the queue).
    p = _app._pair_plan({"a": leg(h=15, cost=43.5, book_ok=False),
                         "b": leg(bid=57.5, ask=58.0)}, 15, 110, 2000)
    check("unreadable book → keep the orders, change nothing",
          p["a"]["bid"] == "keep" and p["a"]["ask"] == "keep"
          and "book_unreadable" in p["a"]["why"])
    check("the other leg is unaffected", p["b"]["bid"] == (57.5, 15))

    # T−30 with NOTHING held: both bids come down (Rob, Sep 20 2026). With one
    # leg held they stay up to kickoff — a fill then completes the pair.
    p = _app._pair_plan({"a": leg(bid=43.5, ask=44.0), "b": leg(bid=57.5, ask=58.0)}, 15, 110, 20)
    check("T-30, nothing held → both bids cancel",
          p["a"]["bid"] is None and p["b"]["bid"] is None
          and "t30_unpaired" in p["a"]["why"])
    p = _app._pair_plan({"a": leg(h=15, cost=43.5, bid=43.0, ask=45.0),
                         "b": leg(bid=57.5, ask=58.0)}, 15, 110, 20)
    check("T-30, one leg held → the other keeps bidding to complete the pair",
          p["b"]["bid"] == (57.5, 15))

    # kickoff: no bids, a lone leg keeps its ask
    p = _app._pair_plan({"a": leg(bid=43.0, ask=44.0), "b": leg(h=15, cost=57.0, bid=40.0, ask=55.0)}, 15, 110, -5)
    check("kickoff → no bids, lone ask stays live", p["a"]["bid"] is None and p["b"]["ask"] is not None)
    # a bid never crosses the ask
    p = _app._pair_plan({"a": leg(bid=44.0, ask=44.0), "b": leg(bid=50.0, ask=51.0)}, 15, 110, 2000)
    check("post-only: bid steps under a locked touch", p["a"]["bid"][0] < 44.0)
    # rule 4, first half: nothing held and ONE leg loses rent → the pair comes
    # down, not just that leg (renting was the whole reason to rest two orders)
    p = _app._pair_plan({"a": leg(bid=44.0, ask=45.0), "b": leg(bid=50.0, ask=51.0, rent=False)}, 15, 110, 2000)
    check("rent rule: nothing held + a leg loses rent → BOTH bids down",
          p["a"]["bid"] is None and p["b"]["bid"] is None
          and "rent_pulled" in p["b"]["why"])

    # RULE 4 (Rob, Sep 20 2026): with a leg HELD we keep working the pair even
    # if the rent is gone on either side — a half-built hedge is a naked bet.
    p = _app._pair_plan({"a": leg(h=15, cost=43.5, bid=43.0, ask=45.0, rent=False),
                         "b": leg(bid=57.5, ask=58.0, rent=False)}, 15, 110, 2000)
    check("rule 4: one leg held + no rent anywhere → still completing the pair",
          p["b"]["bid"] == (57.5, 15))
    check("rule 4: the held leg still lists its exit", p["a"]["ask"] is not None)


def test_pair_candidates() -> None:
    """The seeder's brain (Rob, Sep 19 2026): rank by EDGE (what the middle is
    worth minus what we pay), never by price; a tie-only window is not a
    middle; college keeps the big multiples of 7 the NFL would skip."""
    import app as _app

    def rungs(*rows):
        return [(side, line, bid, ask) for side, line, bid, ask in rows]

    # NFL: a middle on 3 (worth 17.2) vs one on 9 (worth 1.3) at the same price.
    # The first finder draft took the cheap one; the edge rule must not.
    r = rungs(("away", -2.5, 44.0, 45.0), ("home", 3.5, 55.0, 56.0),
              ("away", -8.5, 20.0, 21.0), ("home", 9.5, 79.0, 80.0))
    c = _app._pair_candidates(r, "spread", "NFL", 15)
    check("NFL: the middle on 3 ranks first", c and c[0]["hits"] == [3])
    check("NFL: the cheap middle on 9 is not seated at all (worth 1.3%)",
          not [x for x in c if x["hits"] == [9]])

    # The tie trap: away +0.5 with home +0.5 covers only a 0-point game.
    r2 = rungs(("away", 0.5, 44.0, 45.0), ("home", 0.5, 56.0, 57.0))
    check("a tie-only window is not a middle",
          _app._pair_candidates(r2, "spread", "NFL", 15) == [])

    # College: 21 is worth 3.5% and must qualify; NFL's table stops caring
    # about numbers that big, and its 6 is a steal college does not have.
    r3 = rungs(("away", -20.5, 45.0, 46.0), ("home", 21.5, 54.0, 55.0))
    check("college seats a middle on 21",
          [x for x in _app._pair_candidates(r3, "spread", "NCAAF", 15)
           if x["hits"] == [21]])
    check("NFL worth table rates 6 over 7 per cent paid",
          _app._PAIR_WORTH_SPREAD["NFL"][6] > _app._PAIR_WORTH_SPREAD["NCAAF"][6])

    # A pair priced over its cap never qualifies, however good the number.
    r4 = rungs(("away", -2.5, 62.0, 63.0), ("home", 3.5, 58.0, 59.0))
    check("over the cap = no seat",
          all(x["cost_c"] <= x["cap_c"] for x in _app._pair_candidates(r4, "spread", "NFL", 15)))

    # EARLY AND ALONE (Rob, Sep 20 2026): a wide, barely-quoted book whose
    # two legs sum UNDER 100 is a lock we want, not a stale quote to refuse —
    # and its nonsense mids must not set the cap (that would strand the second
    # leg with nowhere to chase).
    r6 = rungs(("away", -2.5, 41.0, 50.0), ("home", 3.5, 41.0, 52.0))
    c6 = _app._pair_candidates(r6, "spread", "NFL", 15)
    check("an early 82\u00a2 pair still qualifies", c6 and c6[0]["cost_c"] == 82.0)
    check("its cap comes from the middle's worth, not the broken mids",
          c6[0]["cap_c"] > 100.0)
    check("a pair under 100 has a NEGATIVE worst case (a lock)",
          c6[0]["worst_usd"] < 0)

    # Totals: flat worth, still a real middle.
    # mids must sum to at least 100 — a covering pair worth less than that is
    # a stale quote (the seeder's stale-quote guard), so the test data has to
    # be a coherent book: 48.5 + 52.5 = 101.
    r5 = rungs(("over", 44.5, 48.0, 49.0), ("under", 45.5, 52.0, 53.0))
    c5 = _app._pair_candidates(r5, "total", "NFL", 15)
    check("totals middle on 45 qualifies", c5 and c5[0]["hits"] == [45])


def test_pair_priority_gate() -> None:
    """PAIRS LOOK FIRST (Rob, Sep 20 2026): with machine_flags `pair_priority`
    on, the football executor defers a ladder ONLY until the pair machine has
    recorded a verdict on it — then bets whatever pairs refused."""
    import app as _app
    import inspect
    src = inspect.getsource(_app._gridiron_try_bet_impl)
    check("the executor waits for the pair machine's verdict, not forever",
          'pair_first_look' in src and '_pair_declined' in src)
    check("the OMS retries a deferred ladder in minutes",
          _app._OMS_RETRY_MIN.get("pair_first_look") == 10)
    seed = inspect.getsource(_app._pair_seed_tick)
    # 'owned' retired with the ladder rule — the slug-level pass is 'leg_taken'
    for why in ("no_middle", "leg_taken", "rent"):
        check(f"the seeder records its {why!r} pass", f'"{why}"' in seed)
    check("'owned' expires in minutes — ownership changes",
          _app._PAIR_DECLINE_TTL_S["owned"] <= 300
          and _app._PAIR_DECLINE_TTL_S["leg_taken"] <= 300)
    check("a market fact ('no middle', 'no rent') holds longer",
          _app._PAIR_DECLINE_TTL_S["no_middle"] >= 1800)
    check("an unreadable decline table lets the Ferrari keep betting",
          "return True" in inspect.getsource(_app._pair_declined))


def test_pair_owner_guard() -> None:
    """ONE OWNER PER MARKET (Sep 20 2026, the first armed seeder run): the spec
    always claimed the seeder skipped a ladder the Ferrari holds, and the code
    never checked. 5 of 6 pairs landed on slugs with existing picks/positions/
    orders. The engine now also refuses to manage a leg a pending pick owns."""
    import app as _app
    import inspect
    src = inspect.getsource(_app._pair_seed_tick)
    check("seeder builds a TAKEN set from picks, positions and orders",
          "taken_slugs" in src and "_pmm_positions_raw" in src
          and "_pmm_open_orders_raw" in src)
    check("the LADDER is shared — only the SLUG is exclusive (1,954 qualifying "
          "candidates were blocked by the ladder rule)",
          "taken_gm" not in src)
    grid = inspect.getsource(_app._gridiron_try_bet_impl)
    check("and the Ferrari refuses a rung a pair leg owns",
          "_pair_owned_slugs" in grid)
    check("seeder seats EARLY only — hours of lead, not minutes",
          _app._PAIR_MIN_LEAD_H >= 3 and "_PAIR_MIN_LEAD_H" in src)
    check("seeder prices from the quote table AND the tape (it was blind on the "
          "board the same logic found 11 pairs on)",
          "_pair_tape_quotes" in src)
    check("seeder fails CLOSED when the venue is unreadable",
          "venue_unreadable" in src)
    step = inspect.getsource(_app._pair_step)
    check("engine refuses a leg a pick owns", "_pair_foreign_slugs" in step)
    check("the wrong-side page is rate limited, not every 20s",
          "_PAIR_FREEZE_PING" in step)


def test_pair_read_budget() -> None:
    """THE RATE-LIMIT WALL (Sep 21 2026): per-pair venue reads were 1 order
    list + 2 book reads EACH tick — ~100 calls a minute at 11 pairs and ten
    times that at full throttle, which the venue answered with Cloudflare. The
    lane now takes ONE order read for every leg and pushes the legs onto the
    markets socket so their books come from the depth table."""
    import app as _app
    import inspect
    tick = inspect.getsource(_app._pair_tick)
    check("one order read for the whole lane", "all_slugs[:400]" in tick)
    check("legs go on the markets-socket watch list",
          "_WS_WATCHLIST_CB" in tick)
    check("an unreadable order list fails CLOSED",
          "orders_unreadable" in tick)
    step = inspect.getsource(_app._pair_step)
    check("the step filters the shared list instead of re-reading",
          "lane_orders" in step and 'o["slug"] in set(slugs)' in step)


def test_pair_venue_reads() -> None:
    """READ THE VENUE LESS, NOT HARDER (Rob, Sep 21 2026: "we are only going
    up in volume"). The pair lane's repeating reads: positions from the MIRROR
    (the private socket upserts it on every fill), leg books from the DEPTH
    table first, one order read for the lane, a 45s poll backstopped by socket
    wakes. Fresh reads stay where they decide money."""
    import app as _app
    import inspect
    from cellar import config, runner
    tick = inspect.getsource(_app._pair_tick)
    check("positions come from the mirror, not a fresh account read",
          "_pmm_positions_raw(client)" in tick)
    step = inspect.getsource(_app._pair_step)
    check("leg books try the depth table before REST",
          "_ws_depth(slug)" in step and "bk_rest" in step)
    check("the poll is a backstop (45s), not the mechanism",
          config.ALL_LANES["pair"].every_s >= 45)
    check("the socket can wake the pair lane",
          '"pair"' in inspect.getsource(runner.Runner._start_wsfeed)
          if hasattr(runner.Runner, "_start_wsfeed")
          else '"pair"' in open(runner.__file__).read())


def test_pair_completion_exempt() -> None:
    """At full throttle the book-wide dollar fence must not block the SECOND
    leg of a half-filled pair (Sep 21 2026) — that bid converts a naked bet
    into a hedge. A fresh pair still has to clear the fence."""
    import app as _app
    import inspect
    src = inspect.getsource(_app._pair_step)
    i = src.index('side == "bid" and have is None')
    window = src[i:i + 700]
    check("completion bids skip the exposure fence",
          'lg_in[x]["h"] >= 1.0' in window)
    check("fresh pairs still check it", "_book_exposure_usd" in window)


def test_pair_seed_throughput() -> None:
    """SEAT THE BOARD (Rob, Sep 21 2026). Three per pass was a first-night
    throttle; the limiter is capital and the venue's write rate. And a COLD
    mirror right after a restart is not a dead venue — one rate-limited read
    was failing the whole pass closed, costing a seeding window per restart."""
    import app as _app
    import inspect
    src = inspect.getsource(_app._pair_seed_tick)
    check("the per-pass count is a flag, not a hard 3",
          'pair_seed_max' in src)
    check("a cold mirror retries the venue before gating",
          "COLD MIRROR" in src and "fresh=True" in src)


def test_pair_leg_cap_in_the_engine() -> None:
    """THE ENGINE RE-PRICES, SO THE ENGINE NEEDS THE FENCE (Rob, Sep 21 2026:
    a torn-down Central Arkansas leg came back at 81.5¢ two minutes later).
    _pair_plan joins the touch every tick and was fenced only by the pair SUM,
    so with the partner at 31¢ a leg could walk to 89¢. The 65¢ leg cap lives
    in the planner as well as the seeder — and lifts once the other leg is
    HELD, where completing the hedge outranks balance."""
    import app as _app
    base = {"tick": 1.0, "rent": True, "ask": None, "cost": None, "sold": None}
    # nothing held, partner cheap: the leg must NOT chase past 65
    legs = {"away": dict(base, h=0.0, bid=81.5),
            "home": dict(base, h=0.0, bid=31.0)}
    plan = _app._pair_plan(legs, 15, 120.0, 4000.0)
    check("an 81.5c touch is REFUSED while nothing is held — at the touch or "
          "not there at all",
          plan["away"]["bid"] is None and "leg_cap" in plan["away"]["why"])
    check("the cheap partner still joins its own touch",
          isinstance(plan["home"]["bid"], tuple)
          and plan["home"]["bid"][0] == 31.0)
    # partner HELD at 40: the hedge may chase to cap - cost = 80
    legs2 = {"away": dict(base, h=0.0, bid=81.5),
             "home": dict(base, h=15.0, bid=None, cost=40.0)}
    plan2 = _app._pair_plan(legs2, 15, 120.0, 4000.0)
    px2 = plan2["away"]["bid"][0] if isinstance(plan2["away"]["bid"], tuple) else None
    check("the fence holds even with the other leg HELD — a hedge we cannot "
          "buy at 65 is a hedge we skip",
          plan2["away"]["bid"] is None and "leg_cap" in plan2["away"]["why"])


def test_pair_slugs_span_every_row_and_retired_leg() -> None:
    """THE AUTOLOG ADOPTED PAIR LEGS INTO THE FERRARI (Rob, Sep 21 2026: four
    orders on one CAR/CLE total ladder, and cancels that came straight back).
    `_pair_rows` filters enabled=True and `legs` holds only the CURRENT two
    slugs, so a re-pick or a teardown dropped the slug out of `_pair_slugs`;
    `_pmm_autolog` then saw an AUTOMATIC order with no pick and booked it as a
    gridiron pick, handing the rung to the chase, the buy sniper and the seat
    top-up while the pair re-seeded the game elsewhere.

    The exclusion set must span EVERY pair row (enabled or not) and every slug
    a pair has ever quoted."""
    import app as _app
    import inspect
    src = inspect.getsource(_app._pair_slugs)
    check("the exclusion set reads ALL pair rows, not just enabled ones",
          "_pair_all_rows" in src)
    check("…and includes slugs the pair has retired",
          "retired_slugs" in src)
    allsrc = inspect.getsource(_app._pair_all_rows)
    check("_pair_all_rows does NOT filter on enabled",
          '.eq("enabled"' not in allsrc)
    check("…and fails safe to the last good list, never empty",
          "_PAIR_ALL_CACHE" in allsrc and "except Exception" in allsrc)
    rr = inspect.getsource(_app._pair_rerung_both)
    check("a re-pick retires the slugs it walks away from",
          "retired_slugs" in rr and "retired[-40:]" in rr)
    # the set really is a union
    _app._PAIR_ALL_CACHE.update(at=_time.time() if False else 9e9, rows=[
        {"legs": [{"slug": "now-a"}, {"slug": "now-b"}],
         "retired_slugs": ["old-a", "old-b"]}])
    got = _app._pair_slugs(None)
    check("union of current legs and retired legs",
          got == {"now-a", "now-b", "old-a", "old-b"})
    _app._PAIR_ALL_CACHE.update(at=0.0, rows=[])


def test_pairs_own_football_spreads_and_totals() -> None:
    """A FOOTBALL SPREAD OR TOTAL IS HALF OF A MIDDLE OR IT IS NOT BET (Rob,
    Sep 21 2026: "as soon as the pairs turned on you should have obviously
    cancelled the ability for the Ferrari to bet spreads or over and unders.
    Because, duh. It can only be bet in a pair"). `pair_priority` is only a
    DEFERRAL — the Ferrari waits for the pair machine to decline a ladder and
    then takes it anyway. The rule is a wall, not a queue.

    Moneylines are deliberately untouched: no line means no middle, so the
    Ferrari stays the only engine that can bet one."""
    import app as _app
    import inspect
    src = inspect.getsource(_app._gridiron_try_bet_impl)
    check("the football spread/total executor refuses outright",
          'mt in ("spread", "total")' in src and '"pairs_own"' in src)
    check("…on a flag that can be flipped without a deploy",
          'machine_flag("pairs_own_football"' in src)
    check("…and it is the FIRST thing the body does, before any pricing",
          src.split('"""')[2].strip().startswith('if mt in ("spread", "total")'))
    ml = inspect.getsource(_app._gridiron_try_ml)
    check("moneylines are untouched — a moneyline cannot middle",
          "pairs_own_football" not in ml)


def test_pair_lot_cost_never_from_the_venue_blend() -> None:
    """THE VENUE'S avg_price IS A LIFETIME BLEND (Rob, Sep 22 2026 — pair 70).
    A re-pick wipes the leg's `bid_c`; the next tick sees the fill with no
    price of its own and used to take the venue's position average, which came
    back 1.0 on a 1.2-share dust fill — a 100¢ cost on a leg bought at 59.5.
    That capped the partner at `cap − 100` = 20¢ (37¢ under a 57¢ touch,
    earning nothing) AND blocked the held leg's sell, whose floor was 100."""
    import app as _app
    import inspect
    src = inspect.getsource(_app._pair_step)
    check("the lot ledger is consulted first", "_lot_ledger(sb)" in src)
    check("…then our own resting quote, never the venue average",
          'cur[k]["bid"]' in src and 'avg = (positions.get(slug)' not in src)
    check("a lot cost outside 0.5-99.5c is refused outright",
          "0.5 <= float(px) <= 99.5" in src)


def test_pair_reline() -> None:
    """A PAIR SEATED ON A BAD RUNG SITS AT ITS OWN TOUCH FOREVER (Rob, Sep 21
    2026). The off-touch rule only fires when the market walks away from a
    leg, so 59 pairs seated by the old worth table — ATL@GB away +4.5/home
    -3.5 against a Pinnacle -6, a window on 4 with the real number at 6 —
    would never have been re-picked. An unfilled football pair is re-judged
    against the executor's line rule on a slow clock instead."""
    import app as _app
    import inspect
    src = inspect.getsource(_app._pair_step)
    check("a freeze on a pick-owned slug is not a deadlock — re-pick off it",
          "unfroze" in src and "_foreign" in src)
    check("…but only with nothing held on either leg",
          "not any(abs(float((positions.get(sl)" in src)
    check("a decline does not leave an over-cap bid resting — tear it down",
          "TORN DOWN" in src and "_PAIR_MAX_LEG_C" in src)
    check("unfilled football pairs are re-judged against the line rule",
          "_PAIR_RELINE_TS" in src and "_pair_rerung_both" in src)
    check("only with NOTHING held (rule 5: both pending → re-rung is fine)",
          "not _h and mins > 0" in src)
    check("on a clock, not every lap — each look prices the game",
          _app._PAIR_RELINE_S >= 300.0)
    check("and only a few per lap — 59 due at once tripped the rate limiter",
          "_PAIR_RELINE_PER_TICK" in src and 1 <= _app._PAIR_RELINE_PER_TICK <= 8)
    rr = inspect.getsource(_app._pair_rerung_both)
    check("and the re-pick itself uses the Ferrari rule on football",
          "_pair_from_gridiron_rule" in rr)
    check("the re-pick routes around slugs a pick owns (else it freezes)",
          "_pair_foreign_slugs" in rr)
    check("it still cancels both old bids before swapping",
          "_pair_cancel" in rr and "never leave a stray seat out" in rr)


def test_ladder_window_total_sides() -> None:
    """TOTALS SHARE ONE NUMBER; SPREADS MIRROR IT (Sep 21 2026). _ladder_window
    negated the line of EVERY synthetic side to give both sides of a spread
    market one rung value. On a total, over 44.5 and under 44.5 are the same
    market, so negating put the under 89 points from the over and the ±12 trim
    deleted every under (or every over) from the ladder — the executor seated
    whichever side survived the election, and a total pair was impossible."""
    import app as _app
    lad = [{"side": "over", "line": 44.5, "slug": "t44", "synthetic": False,
            "quote": {"bid": 0.49, "ask": 0.52}},
           {"side": "under", "line": 44.5, "slug": "t44", "synthetic": True,
            "quote": {"bid": 0.48, "ask": 0.51}},
           {"side": "over", "line": 47.5, "slug": "t47", "synthetic": False,
            "quote": {"bid": 0.34, "ask": 0.37}},
           {"side": "under", "line": 47.5, "slug": "t47", "synthetic": True,
            "quote": {"bid": 0.63, "ask": 0.66}}]
    keep = _app._ladder_window(lad, "total")
    check("a total ladder keeps BOTH sides",
          {e["side"] for e in keep} == {"over", "under"} and len(keep) == 4)
    spr = [{"side": "away", "line": 6.5, "slug": "s6", "synthetic": False,
            "quote": {"bid": 0.49, "ask": 0.52}},
           {"side": "home", "line": -6.5, "slug": "s6", "synthetic": True,
            "quote": {"bid": 0.48, "ask": 0.51}},
           {"side": "away", "line": 40.5, "slug": "s40", "synthetic": False,
            "quote": {"bid": 0.02, "ask": 0.99}}]
    keep = _app._ladder_window(spr, "spread")
    check("a spread ladder still mirrors the synthetic side",
          {e["slug"] for e in keep} == {"s6"})


def test_team_totals_are_not_the_game_total() -> None:
    """Polymarket's 'football_team_points_full_game_total' (a TEAM's points,
    8.5-35.5) carries the same V2 bucket and the same question text as the
    game total. It sat in the game-total ladder, won the at-the-money
    election, and the window trim then deleted every real game total around
    the line — ATL@GB read 24.5-30.5 against a Pinnacle 44."""
    import pmm_markets as _pm
    check("team totals are blocked as a variant",
          any("team_points" == mk for mk in _pm._VARIANT_MARKERS))
    check("but MLB's 'baseball_team_full_game_total' still classifies",
          not any(mk in "baseball_team_full_game_total"
                  for mk in _pm._VARIANT_MARKERS))
    check("and so does the football game total",
          not any(mk in "football_game_full_game_total"
                  for mk in _pm._VARIANT_MARKERS))


def test_pair_window() -> None:
    """THE MIDDLE IS ARITHMETIC, AND THE SEEDER GOT IT BACKWARDS ONCE. Away
    +5.5 with home −4.5 pays both legs on exactly one number (GB by 5); read
    the window off the two away-lines instead and it reports a ten-point
    middle on a one-point pair."""
    import app as _app
    w, h = _app._pair_window("spread", 5.5, -4.5)
    check("away +5.5 / home -4.5 is a one-point middle on 5", w == 1.0 and h == [5])
    w, h = _app._pair_window("spread", 6.5, -3.5)
    check("away +6.5 / home -3.5 pays on 4,5,6", w == 3.0 and h == [4, 5, 6])
    w, h = _app._pair_window("spread", 4.5, -4.5)
    check("the mirror is a lock, not a middle (no hits)", w == 0.0 and not h)
    w, h = _app._pair_window("spread", 3.5, -4.5)
    check("below the mirror is a GAP and reads negative", w < 0)
    w, h = _app._pair_window("spread", 0.5, 0.5)
    check("the tie trap is not a middle (only 0 covered)", not h)
    w, h = _app._pair_window("total", 44.5, 47.5)
    check("over 44.5 / under 47.5 pays on 45,46,47",
          w == 3.0 and h == [45, 46, 47])
    w, h = _app._pair_window("total", 47.5, 44.5)
    check("under BELOW over on a total is a gap", w < 0)


def test_pair_uses_executor_rule() -> None:
    """FERRARI RULES, WITH A PAIR (Rob, Sep 21 2026: "Ferrari rules… with a
    pair… is the ENTIRE GOAL"; "bad rungs is the entire issue… why we can't
    find a middle"). Football pairs are built from `_gridiron_line_rule` and
    `_gridiron_seat_legal` — the same line and legality the executor has used
    since Sep 5 — so both legs sit on the real line instead of wherever a
    worth table pointed. Central Arkansas +17.5 on a game lined near −30 was
    the old path."""
    import app as _app
    import inspect
    src = inspect.getsource(_app._pair_from_gridiron_rule)
    check("the pair centers on the executor's line rule",
          "_gridiron_line_rule" in src)
    check("each leg must pass the executor's seat legality",
          "_gridiron_seat_legal" in src)
    check("each leg must pay rent and not be culled",
          "_rent_ok" in src and "_rent_dead" in src)
    check("legs peg the way the executor pegs (join the touch)",
          "_gridiron_join_touch" in src)
    check("the venue's NO side is a BUY_SHORT, per the ladder's own flag",
          'r.get("synthetic")' in src)
    # (the constant's NAME appears in the explaining comment — check the
    # actual price fence, which is the pair ceiling, not the single-seat cap)
    check("two fences: 65c a leg and 120c the pair (Rob, Sep 21)",
          "_PAIR_MAX_LEG_C" in src and _app._PAIR_MAX_LEG_C == 65.0
          and "peg <= _GRIDIRON_MAX_ENTRY_C" not in src)
    check("…and the pair cap is the loss budget",
          _app._pair_ceiling("NFL") == 120.0)
    check("seed NEAREST the line, not the widest window",
          "_off" in src and "NEAREST THE LINE" in src)
    check("…and nothing outranks it — no sub-100 seeding preference",
          'cand = (-_off, -cost)' in src)
    check("never both sides of ONE market (that is flat, not a pair)",
          'a["slug"] == b["slug"]' in src)
    seed = inspect.getsource(_app._pair_seed_tick)
    check("football routes through it; MLB keeps the standalone path",
          '("NFL", "NCAAF")' in seed)


def test_pair_dead_ladder() -> None:
    """NO RENT, NO SEAT (Rob, Sep 21 2026: "I want to collect rent and limit
    loss… not limit loss with no rent"). A one-sided ladder — every rung on one
    side bid a penny — can neither hold a touch worth earning nor offer a rung
    to re-rung to when the line moves. Central Arkansas @ FSU was seated on
    exactly that and ended as a naked leg with no buyable hedge."""
    import app as _app
    dead = [("away", 17.5, 1.0, 40.0), ("away", 13.5, 1.0, 38.0),
            ("away", 10.5, 1.0, 35.0), ("home", -17.5, 92.0, 99.0),
            ("home", -13.5, 94.0, 99.0), ("home", -10.5, 96.0, 99.0)]
    check("a one-sided ladder seats nothing",
          _app._pair_candidates(dead, "spread", "NCAAF", 15) == [])
    live = [("away", 4.5, 44.0, 45.0), ("away", 3.5, 41.0, 42.0),
            ("away", 2.5, 38.0, 39.0), ("home", -1.5, 58.0, 59.0),
            ("home", -2.5, 55.0, 56.0), ("home", -3.5, 52.0, 53.0)]
    check("a live two-sided ladder still seats",
          _app._pair_candidates(live, "spread", "NFL", 15))
    # and a WIDE early book is not a dead one — those are the seats we want
    early = [("away", -2.5, 41.0, 50.0), ("home", 3.5, 41.0, 52.0)]
    check("an early, barely-quoted ladder still seats",
          _app._pair_candidates(early, "spread", "NFL", 15))


def test_pair_off_touch_rule() -> None:
    """OFF THE TOUCH IS POINTLESS (Rob, Sep 21 2026: "ZERO point in buying on
    rungs you aren't at touch"). A bid under the touch earns no rent and will
    not fill, so it either moves to a window we can hold the touch on — for a
    half-filled pair walk the partner in, for a both-empty pair re-pick BOTH
    rungs — or it comes down. Pair 10 sat 6c under with a held leg 24c
    underwater and no reachable rung, and simply rested there."""
    import app as _app
    import inspect
    step = inspect.getsource(_app._pair_step)
    check("both-empty pairs re-pick their rungs", "_pair_rerung_both" in step)
    check("a bid with nowhere to go is cancelled",
          "off_touch_canceled" in step)
    check("but one tick under IS the touch — never cancelled for a tick",
          'lg_in[k].get("tick")' in step and "ONE TICK IS AT THE TOUCH" in step)
    both = inspect.getsource(_app._pair_rerung_both)
    check("the re-pick uses the seeder's own brain", "_pair_candidates" in both)
    check("and both old bids are cancelled before the swap",
          "_pair_cancel" in both and "never leave a stray seat out" in both)
    check("a re-picked pair still has to pay rent", "_rent_ok" in both)


def test_pair_mlb_totals() -> None:
    """MLB TOTALS PAIR (Rob, Sep 21 2026: "MLB totals can switch"). Over 8.5
    with Under 9.5 wins both on exactly 9 runs — 8.65% over 3,574 finals,
    three times what a football point is worth, and inside the 116 ceiling.
    The rent-list key map is football-only, so MLB gets its own board build."""
    import app as _app
    import inspect
    check("a 1-run MLB window is priced per number, not flat",
          _app._pair_worth("MLB", "total", [9]) > 8
          and _app._pair_worth("MLB", "total", [7]) > 11)
    check("a 2-run window adds both numbers",
          _app._pair_worth("MLB", "total", [8, 9]) > 16)
    check("MLB's floor ceiling survives the loss-budget rewrite",
          _app._PAIR_CEILING_FLOOR["MLB"] == 116.0
          and _app._pair_ceiling("MLB") >= 116.0)
    src = inspect.getsource(_app._pair_board_mlb)
    check("MLB slugs resolve by tricode + ET date", "America/New_York" in src)
    check("first-five and inning variants never qualify",
          "_PAIR_MLB_TOTAL_RE" in src)
    # a real MLB ladder: Over 8.5 at 52 / Under 9.5 at 54 = 106 for a window
    # worth 8.7 — a 2.7c edge. (At 108 the same window is a 0.7c edge and the
    # 1c floor refuses it, which is the floor doing its job.)
    rungs = [("over", 8.5, 52.0, 53.0), ("under", 9.5, 54.0, 55.0)]
    c = _app._pair_candidates(rungs, "total", "MLB", 15)
    check("an MLB total pair at 106 qualifies under the 116 ceiling",
          c and c[0]["hits"] == [9] and c[0]["cost_c"] == 106.0)
    check("the same window at 108 is refused on edge, not ceiling",
          not _app._pair_candidates(
              [("over", 8.5, 52.0, 53.0), ("under", 9.5, 56.0, 57.0)],
              "total", "MLB", 15))


def test_pair_rerung() -> None:
    """THE RE-RUNG LADDER (Rob, Sep 20 2026). Holding WAS −1.5 with SEA +4.5
    run away to 78: drop a rung at a time until we can sit AT the touch inside
    the cap, all the way to the mirror — "the goal is to get out, not have the
    bet". Never past the mirror: that is a gap where both legs lose."""
    import app as _app
    # away-side ladder as (side, line, bid, ask); we hold home −1.5 at 37
    rungs = [("away", 4.5, 78.0, 78.5), ("away", 3.5, 74.0, 74.5),
             ("away", 2.5, 70.0, 70.5), ("away", 1.5, 62.0, 62.5),
             ("away", 0.5, 55.0, 55.5)]
    opts = _app._pair_partner_options("home", -1.5, rungs, "spread", "NFL", 37.0)
    lines = [o["line"] for o in opts]
    # +4.5 at pair 115 is INSIDE the 120 loss budget now, and is the widest
    # window — so it is the pick, not a reject (Rob, Sep 21: raise the cap,
    # take the most expensive rung under it at the touch).
    check("the widest rung inside the loss budget is offered", 4.5 in lines)
    check("a rung we can reach at the touch is offered", lines)
    check("the mirror (+1.5, no middle, pure hedge) is legal", 1.5 in lines)
    check("past the mirror (+0.5 = both can lose) is never offered", 0.5 not in lines)
    best = opts[0]
    # WIDEST AFFORDABLE FIRST (Rob, Sep 21 2026): completion pays up to the
    # LOSS BUDGET, not the window's worth, so the mirror is reachable — but it
    # sorts last because it can never middle.
    check("the widest affordable window sorts ahead of the mirror",
          opts[0]["worth"] >= opts[-1]["worth"]
          and (opts[-1]["hits"] == [] or opts[0]["line"] >= opts[-1]["line"]))
    # on THIS book nothing but the mirror is reachable: +3.5 pairs at 111 and
    # +2.5 at 107, both past their own caps — so the ladder walks all the way
    # down, which is the rule ("the goal is to get out, not have the bet")
    check("it takes the WIDEST rung under the loss cap (+4.5, pair 115)",
          best["line"] == 4.5 and best["pair_c"] == 115.0)
    check("the mirror is last, not first", opts[-1]["line"] == 1.5)
    # tighten the budget and it must walk in
    # the budget is a machine_flag, so patch the LOOKUP — patching the module
    # constant alone leaves the live flag (20) in charge
    import app as _app2
    _old = _app2._machine_flag_val
    try:
        _app2._machine_flag_val = (
            lambda k, d=None: 8.0 if k == "pair_max_loss_c" else _old(k, d))
        tight = _app2._pair_partner_options("home", -1.5, rungs, "spread", "NFL", 37.0)
        check("a tighter loss budget walks the ladder in",
              tight and tight[0]["line"] <= 3.5)
    finally:
        _app2._machine_flag_val = _old
    check("and it sits at the touch inside its own cap",
          best["pair_c"] <= best["cap"] + 1e-9)
    # with the held leg cheaper, the wider window becomes affordable again and
    # the ladder prefers it — more middle for the same seat
    wide = _app._pair_partner_options("home", -1.5, rungs, "spread", "NFL", 30.0)
    check("a cheaper held leg buys back the bigger window", wide[0]["line"] >= 2.5)
    # mirror carries no middle and no worth
    mir = [o for o in opts if o["line"] == 1.5][0]
    check("the mirror is priced as a hedge, not a middle",
          mir["hits"] == [] and mir["worth"] == 0.0)


def main() -> int:
    print("THE CELLAR — offline selftest\n")
    for t in (test_imports_without_creds, test_config_validation,
              test_lease_fails_closed, test_journal_survives_crash,
              test_lane_registry_matches_config, test_batch_schedule,
              test_batch_commands_exist, test_batch_flags_are_real,
              test_batch_blocked_deps, test_owner_dependent_lanes,
              test_dry_run_blackout, test_overrun_detector,
              test_ws_quote_presence, test_ws_mkts_request_budget,
              test_gridiron_value_window, test_gridiron_move_favorable, test_gridiron_key_hook, test_cfbd_consensus, test_gridiron_qb_adjust,
              test_pin_line_center,
              test_gridiron_bounds, test_game_sport_key, test_snipe_target,
              test_entry_sync_guard, test_lot_ledger_floor, test_no_mangled_fresh_kwarg, test_snipe_target_at_cost, test_gridiron_join_touch, test_seat_topup_plan, test_vsin_dates_and_names,
              test_lane_covers_its_documented_engines, test_pair_plan, test_pair_candidates, test_pair_owner_guard, test_pair_priority_gate, test_pair_rerung, test_pair_mlb_totals, test_pair_off_touch_rule, test_pair_dead_ladder, test_pair_uses_executor_rule, test_pair_window, test_pair_reline, test_pair_lot_cost_never_from_the_venue_blend, test_pairs_own_football_spreads_and_totals, test_pair_slugs_span_every_row_and_retired_leg, test_pair_leg_cap_in_the_engine, test_ladder_window_total_sides, test_team_totals_are_not_the_game_total, test_pair_seed_throughput, test_pair_completion_exempt, test_pair_read_budget, test_pair_venue_reads,
              test_side_and_phase, test_ttls_agree_with_engines):
        t()
    print(f"\n  {len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        print("  FAILED: " + ", ".join(_FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
