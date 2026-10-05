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
    check("money lanes are exactly opener/repeg/harvest/scalp/pair/pair_seed/handbets",
          money == {"opener", "repeg", "harvest", "scalp", "pair", "pair_seed", "handbets"},
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
    check("one held, partner touch 70 → taken; cap - cost (66) is the fence",
          p["b"]["bid"] == (66.0, 15))
    # …and inside the fence the hedge still chases cap − cost
    p2 = _app._pair_plan({"a": leg(h=15, cost=44.0, bid=43.0, ask=46.0),
                          "b": leg(bid=60.0, ask=61.0)}, 15, 110, 2000)
    check("one held, partner touch 60 → hedge joins it", p2["b"]["bid"] == (60.0, 15))
    check("one held, never sold → ask at cost + tick when it leads", p["a"]["ask"] == (44.5, 15))
    # a (cost 53) sold at 60, b held at 57 → FLAT floor = 57 + 53 − 60 = 50,
    # sells at 50.5 if it leads (Sep 25 2026: flat, not cap − sold — under a
    # 120 loss budget "cap − sold" had become a profit demand parked 5-11¢
    # over the touch on ten held legs)
    p = _app._pair_plan({"a": leg(sold=60.0, sold_cost=53.0, bid=58.0, ask=61.0),
                         "b": leg(h=15, cost=57.0, bid=40.0, ask=55.0)}, 15, 110, 2000)
    check("lone leg after a sale → FLAT floor (57 + 53 − 60 = 50 → 50.5)", p["b"]["ask"] == (50.5, 15))
    check("sold leg re-bids capped at cap − held cost (53)", p["a"]["bid"] == (53.0, 15))
    # the sold leg's cost unknown → this leg's own cost is the floor, never
    # cap − sold (120 − 60 = 60 would sit 3¢ over cost with no fill)
    p3 = _app._pair_plan({"a": leg(sold=60.0, bid=58.0, ask=61.0),
                          "b": leg(h=15, cost=57.0, bid=40.0, ask=55.0)}, 15, 120, 2000)
    check("sold cost unknown → floor at this leg's own cost (57), never cap − sold (60)",
          p3["b"]["ask"] == (57.0, 15))
    # UNREADABLE RENT IS NOT UNPAID (Sep 25 2026): rent=None with nothing
    # held keeps the bid exactly where it is, like an unreadable book
    p = _app._pair_plan({"a": leg(bid=44.0, ask=45.0, rent=None), "b": leg(bid=50.0, ask=51.0)}, 15, 110, 2000)
    check("rent unreadable + nothing held → keep both bids",
          p["a"]["bid"] == "keep" and p["b"]["bid"] == "keep"
          and "rent_unreadable" in p["a"]["why"])
    import inspect as _insp0
    # THE PROP SISTERS (Sep 27 2026): slug parse, player code, the widest-under-cap pick
    m = _app._PAIR_PROP_RE.match("astatc-nfl-bal-dal-2026-09-27-recyd-ceelam-gte80")
    check("prop slug parses (fam, code, rung)", m and m.group(5) == "recyd" and m.group(6) == "ceelam" and m.group(7) == "80")
    check("count-stat and period props never parse as a sister ladder",
          not _app._PAIR_PROP_RE.match("astatc-nfl-bal-dal-2026-09-27-rec-ceelam-gte5")
          and not _app._PAIR_PROP_RE.match("astatc-nfl-bal-dal-2026-09-27-td-ceelam-gte1"))
    check("player code: CeeDee Lamb → ceelam, Ja'Marr Chase → jamcha, Amon-Ra St. Brown → amobro, Kenneth Walker III → kenwal",
          _app._pair_player_code("CeeDee Lamb") == "ceelam" and _app._pair_player_code("Ja'Marr Chase") == "jamcha"
          and _app._pair_player_code("Amon-Ra St. Brown") == "amobro" and _app._pair_player_code("Kenneth Walker III") == "kenwal")
    check("player code alternates: D'Andre Swift offers dswi, J.K. Dobbins offers jdob, Colby Parkinson offers colbpar",
          "dswi" in _app._pair_player_codes("D'Andre Swift") and "jdob" in _app._pair_player_codes("J.K. Dobbins")
          and "colbpar" in _app._pair_player_codes("Colby Parkinson") and _app._pair_player_codes("Colby Parkinson")[0] == "colpar")
    lamb = {70: (60, 62), 80: (52, 53), 90: (28, 29), 100: (15, 16)}
    check("a fairly-quoted 10-yd band (80/90 = 52 + 71 = 123) does NOT fit under 110", _app._pair_prop_pick(lamb, 82.5, 110) is None)
    wide = {40: (52, 53), 50: (40, 42), 60: (30, 33), 30: (62, 64)}
    pk = _app._pair_prop_pick(wide, 42.5, 110)
    check("widest first: 30/60 (62+67=129) and 30/50 (62+58=120) fail, 40/60 (52+67=119) fails, 40/50 (52+58=110) seats",
          pk and (pk["lo"], pk["hi"], pk["cost_c"], pk["hits"]) == (40, 50, 110.0, list(range(40, 50))))
    check("no rung on one side of the line → no sister", _app._pair_prop_pick({40: (52, 53)}, 42.5, 110) is None)
    check("the props cap is 110 while spreads/totals stay 105",
          _app._PAIR_PROP_CAP_C == 110.0 and _app._pair_ceiling("NFL") == 105.0)
    check("the seeder calls the prop pass", "_pair_seed_props(sb, now, res, armed, taken_slugs, have, max_new)" in _insp0.getsource(_app._pair_seed_tick))
    # THE RE-RUNG CANCELS FROM A FRESH READ (Sep 27 2026, LAR@DEN 11:20:17)
    from types import SimpleNamespace as _NS
    _cxl = []
    _fake = _NS(orders=_NS(cancel=lambda oid, m: _cxl.append(oid), list=lambda *a: {"orders": []}))
    _oo = app_orders = [{"id": "o1", "slug": "s-old", "intent": "ORDER_INTENT_BUY_LONG", "state": "ORDER_STATE_NEW", "auto": True},
                        {"id": "o2", "slug": "s-old", "intent": "ORDER_INTENT_SELL_LONG", "state": "ORDER_STATE_NEW", "auto": True},
                        {"id": "o3", "slug": "s-keep", "intent": "ORDER_INTENT_BUY_LONG", "state": "ORDER_STATE_NEW", "auto": True}]
    _oraw = _app._pmm_open_orders_raw
    try:
        _app._pmm_open_orders_raw = lambda c, fresh=False: list(_oo)
        _r = _app._pair_cancel_rungs_fresh(_fake, ["s-old"])
        check("re-rung fresh cancel: takes down the AUTOMATIC BUY on the walked-off rung only", _r is True and _cxl == ["o1"])
        _app._pmm_open_orders_raw = lambda c, fresh=False: None
        check("re-rung fresh cancel: an unreadable venue refuses (caller must not rewrite legs)", _app._pair_cancel_rungs_fresh(_fake, ["s-old"]) is False)
    finally:
        _app._pmm_open_orders_raw = _oraw
    _src_tick = _insp0.getsource(_app._pair_tick)
    check("the lap hands each row the mirror as it is NOW, not the lap-start copy",
          "_o_now = _pmm_open_orders_raw(client, fresh=False)" in _src_tick and "_pair_step(sb, client, row, _p_now" in _src_tick)
    _src_rr = _insp0.getsource(_app._pair_rerung) + _insp0.getsource(_app._pair_rerung_both) + _insp0.getsource(_app._pair_rerung_prop)
    check("all three re-rungs cancel from a fresh read, none from the lap snapshot",
          _src_rr.count("_pair_cancel_rungs_fresh(") == 4 and "_pair_cancel(client, cur[" not in _src_rr)
    check("no re-rung within a minute of a create on the row", "_recent_create" in _insp0.getsource(_app._pair_step) and _app._PAIR_RERUNG_GRACE_S == 60.0)
    # THE ORPHAN SWEEP (Sep 27 2026, Bateman): a bid on a walked-off rung no leg claims is cancelled
    _ob = _app._pair_orphan_bids(
        [{"slug": "s-old", "intent": "ORDER_INTENT_BUY_SHORT", "state": "ORDER_STATE_NEW", "auto": True},
         {"slug": "s-old", "intent": "ORDER_INTENT_SELL_SHORT", "state": "ORDER_STATE_NEW", "auto": True},
         {"slug": "s-old", "intent": "ORDER_INTENT_BUY_LONG", "state": "ORDER_STATE_NEW", "auto": False},
         {"slug": "s-cur", "intent": "ORDER_INTENT_BUY_LONG", "state": "ORDER_STATE_NEW", "auto": True},
         {"slug": "s-other", "intent": "ORDER_INTENT_BUY_LONG", "state": "ORDER_STATE_NEW", "auto": True}],
        retired={"s-old"}, want={"s-cur"})
    check("orphan sweep: only the AUTOMATIC BUY on the retired rung (not the sell, not MANUAL, not a live leg, not a stranger)",
          [o["slug"] + "/" + o["intent"][-9:] for o in _ob] == ["s-old/BUY_SHORT"])
    check("the pair lap runs the orphan sweep", "_pair_orphan_bids(_all, _retired, _want)" in _insp0.getsource(_app._pair_tick))
    # THE PROP RE-RUNG (Sep 27 2026, Rob: "figure out the re-rung on these props")
    check("prop leg label → rung: 'over 14.5' = YES 15+, 'under 19.5' = NO 20+",
          _app._pair_prop_rung({"label": "over 14.5"}) == ("over", 15) and _app._pair_prop_rung({"label": "under 19.5"}) == ("under", 20))
    pr = {15: (54, 55), 20: (43, 44), 25: (30, 31)}
    check("held YES 15 @54, cap 110: widest partner under budget 56 is NO 20 (peg 56), not NO 25 (69)",
          _app._pair_prop_partner(pr, "over", 15, 54.0, 110) == (20, 56.0, list(range(15, 20))))
    check("held YES 15 @60, cap 110: budget 50 → NO 20 (56) fails and there is NO mirror on a prop (same market) → None",
          _app._pair_prop_partner(pr, "over", 15, 60.0, 110) is None)
    check("held NO 20: the mirror YES 20 on the same market is never offered",
          _app._pair_prop_partner({20: (43, 44)}, "under", 20, 30.0, 110) is None)
    check("held NO 20 @56: partner is YES on a rung ≤ 20, widest first → YES 15 @54 (budget 54)",
          _app._pair_prop_partner(pr, "under", 20, 56.0, 110) == (15, 54.0, list(range(15, 20))))
    check("nothing under budget → None", _app._pair_prop_partner(pr, "over", 15, 70.0, 110) is None)
    check("prop prefix parses fam + code", _app._pair_prop_parts("astatc-nfl-ari-sf-2026-09-27-ryd-bropur") == ("ryd", "bropur"))
    _src_step = _insp0.getsource(_app._pair_step)
    check("the step dispatches astatc- rows to the prop re-rung and keeps the football reline off them",
          "_pair_rerung_prop(sb, client, row, lg_in, st, cur, now, res)" in _src_step
          and 'not str(row.get("game_prefix") or "").startswith("astatc-")' in _src_step)
    check("props have their own 1h lead floor, not the 3h game-line floor",
          'pair_props_min_lead_h", 1.0' in _insp0.getsource(_app._pair_seed_props))
    # HOCKEY JOINS THE BOARD (Sep 27 2026): the venue's NHL dialect parses,
    # the map covers all 32 teams, the worth tables keep the cap at 105
    m = _app._PAIR_NHL_TOTAL_RE.match("tsc-nhl-pit-phi-2026-09-30-6pt5")
    check("NHL total slug parses (MLB-style tail)", m and m.group(1) == "pit" and m.group(4) == "6")
    m2 = _app._PAIR_NHL_SPREAD_RE.match("asc-nhl-pit-phi-2026-09-30-neg-1pt5")
    check("NHL puck-line slug parses", m2 and m2.group(4) == "neg" and m2.group(5) == "1")
    check("NHL period/team-total variants never parse",
          not _app._PAIR_NHL_TOTAL_RE.match("tsc-nhl-pit-phi-2026-09-30-1p-2pt5")
          and not _app._PAIR_NHL_TOTAL_RE.match("tsc-nhl-pit-phi-2026-09-30-tt-pit-2pt5"))
    check("NHL tricode map covers 32 teams", len(_app._NHL_CODE_TEAM) == 32 and len(set(_app._NHL_CODE_TEAM.values())) == 32)
    check("accent/punctuation folding: Montréal + St. Louis resolve",
          _app._pair_team_key("Montréal Canadiens") == "montreal canadiens"
          and _app._pair_team_key("St. Louis Blues") == "st louis blues")
    check("NHL total worth: 5.5/6.5 middles on 6 at 11.0, not the 2.0 default",
          abs(_app._pair_worth("NHL", "total", [6]) - 11.0) < 1e-9)
    check("NHL puck-line worth: home −1.5 + away +2.5 middles on exactly 2 (9.7, one direction)",
          _app._pair_window("spread", 2.5, -1.5) == (1.0, [2])
          and abs(_app._pair_worth("NHL", "spread", [2]) - 9.7) < 1e-9)
    check("away +1.5 with home −2.5 is a GAP, never a pair", _app._pair_window("spread", 1.5, -2.5)[0] < 0)
    check("the board builder is wired beside MLB", "_pair_board_nhl(sb, board, prefixes)" in _insp0.getsource(_app._pair_board))
    # PAIR OR NOTHING (Rob, Sep 27 2026): the single-leg creators are gated
    import inspect as _insp0
    check("pair or nothing: the autobet executor, the NRFI create and the top-up all check the flag",
          "if _pair_or_nothing():" in _insp0.getsource(_app._autobet_execute)
          and "if _pair_or_nothing():" in _insp0.getsource(_app._opener_pass)
          and '{"gate": "pair_or_nothing"}' in _insp0.getsource(_app._seat_topup_tick))
    check("pair or nothing: the PAIR tick does not check it (pairs are the only buyer)",
          "_pair_or_nothing" not in _insp0.getsource(_app._pair_tick)
          and "_pair_or_nothing" not in _insp0.getsource(_app._pair_step))
    check("pair or nothing defaults ON in code", "_machine_flag(\"pair_or_nothing\", True)" in _insp0.getsource(_app._pair_or_nothing))
    # THE FERRARI IS PARKED (Sep 27 2026): the top-up must skip single-leg
    # football/prop/UFC seats while the flag holds; MLB seats are untouched
    check("ferrari pick: gridiron / fbprop / ghost-adopt seats are Ferrari, MLB autobet is not",
          _app._is_ferrari_pick({"gridiron_autobet": True}) and _app._is_ferrari_pick({"fbprop_autobet": True})
          and _app._is_ferrari_pick({"ghost_adopt": True}) and not _app._is_ferrari_pick({"autobet": True, "source": "autobet"}))
    import inspect as _insp
    _src = _insp.getsource(_app._seat_topup_tick)
    check("seat top-up honours the parked flag before planning a bid",
          "_ferrari_parked() and _is_ferrari_pick(b)" in _src)
    # DUST IS NOT A SEAT (Sep 27 2026): a pick-owned rung holding <5 with no
    # resting bid does not fence the pair off; ≥5, or a resting bid, does
    ff = _app._pair_foreign_filter_dust(
        {"s-dust", "s-seat", "s-bid", "s-none"},
        positions={"s-dust": {"net": -2.4}, "s-seat": {"net": 15.0}, "s-bid": {"net": 0.3}},
        orders=[{"slug": "s-bid", "intent": "ORDER_INTENT_BUY_LONG"},
                {"slug": "s-dust", "intent": "ORDER_INTENT_SELL_SHORT"}])
    check("foreign filter: dust lot with only a sell resting is released; a seat and a bid stay",
          ff == {"s-seat", "s-bid"})
    check("foreign filter: unreadable mirror keeps everything foreign",
          _app._pair_foreign_filter_dust({"x", "y"}, positions=None, orders=[]) == {"x", "y"})
    # PAIR LEGS SURVIVE A REPLACE PUSH (Sep 27 2026): the repeg lap's open-orders push
    # must not evict held pair legs from the depth/core watch list
    try:
        from cellar import wsfeed as _wsf
        _old_pls = getattr(_app, "PAIR_LIVE_SLUGS", set())
        _app.PAIR_LIVE_SLUGS = {"astatc-nfl-x-y-2026-09-28-ryd-abc-gte20", "astatc-nfl-x-y-2026-09-28-ryd-abc-gte30"}
        class _FakeMk:
            def __init__(self): self.got = None
            def set_slugs(self, s, replace=True): self.got = set(s)
        _wf = _wsf.WsFeed.__new__(_wsf.WsFeed)
        _wf._watch_full = set(); _wf.mkts = _FakeMk(); _wf.depth_feed = _FakeMk()
        _wf.push_watch({"asc-nfl-order-1"}, replace=True)
        check("a replace push keeps every pair leg on the watch list (depth + core)",
              _wf.mkts.got == {"asc-nfl-order-1"} | _app.PAIR_LIVE_SLUGS
              and _wf.depth_feed.got == _wf.mkts.got)
        _wf.push_watch({"asc-nfl-order-2"}, replace=False)
        check("a merge push adds without dropping the pair legs or the prior list",
              _wf.mkts.got == {"asc-nfl-order-1", "asc-nfl-order-2"} | _app.PAIR_LIVE_SLUGS)
        _app.PAIR_LIVE_SLUGS = _old_pls
    except Exception as _e:
        check(f"push_watch pair-survival test ran ({_e})", False)
    # FOOTBALL SPREAD WORTH IS LINE-CONDITIONAL, FAVORITE-SIGNED (Oct 2 2026, measured)
    check("NFL fav by exactly 3 at a 2.5 line is the measured 8.25",
          abs(_app._pair_worth("NFL", "spread", [3], line=-2.5) - 8.25) < 1e-6)
    check("NCAAF -6.5/+8.5 at a 7.5 line is worth 7+8 = 9.67",
          abs(_app._pair_worth("NCAAF", "spread", [7, 8], line=7.5) - 9.67) < 1e-6)
    check("a whole-number line reads the half above it",
          _app._pair_spread_cond_row("NFL", 7) is _app._pair_spread_cond_row("NFL", 7.5))
    check("no line falls back to the unconditional |margin| table, unhalved",
          abs(_app._pair_worth("NFL", "spread", [3]) - 17.2) < 1e-6)
    check("prop sisters seat 10 a leg (Rob, Oct 3 2026)", _app._pair_prop_qty() == 10 or _app._machine_flag_val("pair_props_qty") is not None)
    # THE NEBRASKA BET (Oct 3 2026): a position built by a MANUAL buy is never adopted
    class _NebSB:
        def __init__(self, moi): self.moi = moi
        def table(self, *_a): return self
        def select(self, *_a): return self
        def eq(self, *_a): return self
        def limit(self, *_a): return self
        def execute(self):
            class _R: pass
            r = _R(); r.data = [{"payload": {"trade": {"isAggressor": False, "passive": {
                "intent": "ORDER_INTENT_BUY_SHORT", "manualOrderIndicator": self.moi}}}}] if self.moi else []
            return r
    check("a hand-placed fill reads as the user's (True)",
          _app._hand_fill_verdict(_NebSB("MANUAL_ORDER_INDICATOR_MANUAL"), "aec-cfb-mary-nebr-2026-10-03", True) is True)
    check("a machine fill reads as ours (False)",
          _app._hand_fill_verdict(_NebSB("MANUAL_ORDER_INDICATOR_AUTOMATIC"), "aec-cfb-mary-nebr-2026-10-03", True) is False)
    check("no buy trade visible yet is unknown (None) — adoption waits",
          _app._hand_fill_verdict(_NebSB(None), "aec-cfb-mary-nebr-2026-10-03", True) is None)
    check("the ghost loop refuses a position on True or None (source shows both gates)",
          "manual_fill" in _insp0.getsource(_app._pmm_autolog) and "fill_owner_unknown" in _insp0.getsource(_app._pmm_autolog))
    # WIND-DOWN (Rob, Oct 3 2026): flat rows are cancelled + retired, held rows keep running
    _src_pt = _insp0.getsource(_app._pair_tick)
    check("pair_wind_down retires only rows holding nothing (both legs flat)",
          "pair_wind_down" in _src_pt and "wind_down_retired" in _src_pt
          and "not any(_held(lg.get(\"slug\")) for lg in legs_)" in _src_pt)
    check("a line far from any measured row returns no conditional row",
          _app._pair_spread_cond_row("NFL", 30.5) is None)
    check("NHL stays as measured (already one-direction)", abs(_app._pair_worth("NHL", "spread", [2]) - 9.7) < 1e-6)
    # ONE RENT QUESTION IN THE PAIR ENGINE (Oct 2 2026): no pair function calls _rent_ok directly
    import re as _re_pr
    for _fn in ("_pair_from_gridiron_rule", "_pair_rerung", "_pair_rerung_both", "_pair_rerung_prop"):
        _f = getattr(_app, _fn, None)
        if _f is not None:
            check(f"{_fn} asks rent through _pair_rent_ok, never _rent_ok directly",
                  not _re_pr.search(r"(?<!_pair)_rent_ok\(", _insp0.getsource(_f)))
    _g0 = _app._pair_rent_gated
    try:
        _app._pair_rent_gated = lambda sp: str(sp).upper() != "NFL"
        check("an ungated sport's leg passes the pair rent question without asking the venue",
              _app._pair_rent_ok("asc-nfl-x-y-2026-10-04-neg-2pt5", None, None, None) is True
              and _app._pair_rent("asc-nfl-x-y-2026-10-04-neg-2pt5", None, None, None) is True)
    finally:
        _app._pair_rent_gated = _g0
    # EARLY NOT EARLY-EARLY + HOLD BOTH (Rob, Sep 30 2026)
    check("rent gate is ON by default in code for every sport (the DB flag opens it)", _app._PAIR_RENT_GATE_OFF == set())
    check("seat ceiling: NFL 72h, NHL 24h, props 48h", _app._pair_max_lead_h("NFL") == 72.0 and _app._pair_max_lead_h("NHL") == 24.0 and _app._pair_props_max_lead_h() == 48.0)
    p = _app._pair_plan({"a": leg(h=15, cost=55.7, bid=50.0, ask=51.0), "b": leg(h=15, cost=54.7, bid=48.0, ask=49.0)}, 15, 111, 300, hold_both=True)
    check("both held over 100 → HOLD: no asks on either leg", p["a"]["ask"] is None and p["b"]["ask"] is None and "hold_both" in p["a"]["why"])
    p2 = _app._pair_plan({"a": leg(h=15, cost=55.7, bid=50.0, ask=51.0), "b": leg(h=15, cost=54.7, bid=48.0, ask=49.0)}, 15, 111, 300, hold_both=False)
    check("…and with hold_both off the old cost asks come back", p2["a"]["ask"] is not None)
    p3 = _app._pair_plan({"a": leg(h=15, cost=45.0, bid=50.0, ask=51.0), "b": leg(h=15, cost=50.0, bid=48.0, ask=49.0)}, 15, 111, 300, hold_both=True)
    check("both held UNDER 100 is still the lock, no asks", p3["a"]["ask"] is None and "lock_rides" in p3["a"]["why"])
    check("the plan call site passes the hold-both flag", "hold_both=_pair_hold_both()" in _insp0.getsource(_app._pair_step))
    # CAPS BY VALUE, EVERY SPORT + PROPS (Rob, Sep 29 2026)
    check("NFL and MLB are cap-by-worth too", _app._pair_cap_by_worth("NFL") and _app._pair_cap_by_worth("MLB"))
    check("a 10-yd receiving band is worth 13.0 → cap 113.0", _app._pair_prop_cap("recyd", 20, 30) == 113.0)
    check("a 25-yd passing band is worth 12.1 → cap 112.1", _app._pair_prop_cap("pyd", 200, 225) == 112.1)
    _pk = _app._pair_prop_pick({20: (55.0, 56.0), 30: (43.0, 44.0)}, 24.5, 110.0, fam="recyd")
    check("rec 20/30 at 55 + 56 = 111 seats under its 113 worth cap (was refused at 110)",
          _pk is not None and _pk["cost_c"] == 111.0 and _pk["cap_c"] == 113.0)
    _pk2 = _app._pair_prop_pick({20: (57.0, 58.0), 30: (43.0, 44.0)}, 24.5, 110.0, fam="recyd")
    check("rec 20/30 at 57 + 56 = 113 is refused — at its worth, no edge", _pk2 is None)
    _pt = _app._pair_prop_partner({20: (55.0, 56.0), 30: (40.0, 43.0), 40: (18.0, 20.0)}, "over", 20, 55.0, 110.0, fam="recyd")
    check("partner budget follows the band's worth: the 80¢ NO on 40 is over the leg fence, 30 at 57¢ fits 113 − 1 − 55",
          _pt is not None and _pt[0] == 30)
    _pt2 = _app._pair_prop_partner({20: (55.0, 56.0), 30: (40.0, 42.0)}, "over", 20, 55.0, 110.0, fam="recyd")
    check("…and 30 at 58¢ does not (57 is the budget)", _pt2 is None)
    # CAP BY WORTH (Sep 29 2026): hockey's ceiling is 100 + P(middle), not the flat 105
    check("NHL is cap-by-worth by default (and a made-up sport is not)", _app._pair_cap_by_worth("NHL") and not _app._pair_cap_by_worth("CURLING"))
    check("NHL puck-line worth is one-direction (fav by exactly 2 ≈ 9.7, not 18.4)",
          abs(_app._pair_worth("NHL", "spread", [2]) - 9.7) < 1e-6)
    _c = _app._pair_candidates([("over", 5.5, 55.0, 56.0), ("under", 6.5, 54.0, 55.0),
                                ("over", 6.5, 43.0, 44.0), ("under", 7.5, 60.0, 61.0)],
                               "total", "NHL", 15)
    _by = {(c["a_line"], c["b_line"]): c for c in _c}
    check("5.5/6.5 at 109 seats under a worth cap of 111 (was refused at 105)", (5.5, 6.5) in _by and _by[(5.5, 6.5)]["cap_c"] == 111.0)
    check("6.5/7.5 at 103 seats under its 121.1 worth cap", (6.5, 7.5) in _by)
    _c2 = _app._pair_candidates([("over", 5.5, 57.0, 58.0), ("under", 6.5, 56.0, 57.0)], "total", "NHL", 15)
    check("5.5/6.5 at 113 (worth 111) is refused — the book must price the middle under its worth", not _c2)
    # THE SEEDER CANNOT ADMIT A LEG THE PLAN REFUSES (Sep 29 2026, MTL@TOR 7.5/8.5)
    check("seeder leg band upper bound == the plan's 65¢ fresh-leg fence",
          abs(_app._PAIR_LEG_BAND[1] - _app._PAIR_MAX_LEG_C) < 1e-9)
    _c = _app._pair_candidates([("over", 7.5, 27.0, 28.0), ("under", 8.5, 78.0, 81.0),
                                ("over", 6.5, 45.0, 46.0), ("under", 7.5, 72.0, 73.0)],
                               "total", "NHL", 15)
    check("a 27 + 78 hockey tail pair is NOT a candidate (78 > the leg fence)",
          not any(c["b_c"] > _app._PAIR_MAX_LEG_C for c in _c))
    # SETTLING (Sep 27 2026, Nix): a leg the venue resolved means the pair places NOTHING
    p = _app._pair_plan({"a": leg(h=15, cost=53.0, bid=98.0, ask=99.0),
                         "b": {**leg(bid=1.0, ask=2.0), "resolved": True}}, 15, 110, -190)
    check("partner resolved → the held leg gets no ask (not even at 99) and no bid",
          p["a"]["ask"] is None and p["a"]["bid"] is None and "settling" in p["a"]["why"] and p["b"]["bid"] is None)
    check("the step stamps a venue settlement, not a sale, when a both-held leg goes to zero in-play with no ask",
          's["resolved"] = now.isoformat()' in _insp0.getsource(_app._pair_step))
    # NO NAKED PAIRS (Sep 26 2026): one held, partner empty, inside T-10 →
    # the held leg asks one tick above the BID whatever its cost, and the
    # partner's completion bid comes off with it
    p = _app._pair_plan({"a": leg(h=15, cost=58.0, bid=52.0, ask=54.0),
                         "b": leg(bid=40.0, ask=41.0)}, 15, 105, 8)
    check("unpaired held leg inside T-10 joins the ask touch (54), not cost (58), post-only",
          p["a"]["ask"] == (54.0, 15) and "t30_exit" in p["a"]["why"])
    p = _app._pair_plan({"a": leg(h=15, cost=58.0, bid=52.0, ask=None),
                         "b": leg(bid=40.0, ask=41.0)}, 15, 105, 8)
    check("…no ask on the book → one tick above the bid (52.5)", p["a"]["ask"] == (52.5, 15))
    check("…and the partner's completion bid is pulled",
          p["b"]["bid"] is None and "t30_unpaired" in p["b"]["why"])
    p = _app._pair_plan({"a": leg(h=15, cost=58.0, bid=52.0, ask=54.0),
                         "b": leg(bid=40.0, ask=41.0)}, 15, 105, -10)
    check("…and in-play too", p["a"]["ask"] == (54.0, 15) and "t30_exit" in p["a"]["why"])
    p = _app._pair_plan({"a": leg(h=15, cost=58.0, bid=52.0, ask=54.0),
                         "b": leg(bid=40.0, ask=41.0, sold=61.5, sold_cost=60.0)}, 15, 105, 8)
    check("…and when the partner SOLD earlier (a naked leg is a naked leg): exit, not the flat floor",
          p["a"]["ask"] == (54.0, 15) and "t30_exit" in p["a"]["why"])
    p = _app._pair_plan({"a": leg(h=15, cost=58.0, bid=52.0, ask=54.0),
                         "b": leg(bid=40.0, ask=41.0, sold=61.5, sold_cost=60.0)}, 15, 105, 200)
    check("…outside T-10 the sold-partner leg keeps its flat floor (58+60−61.5=56.5)",
          p["a"]["ask"] == (56.5, 15) and "pair_floor" in p["a"]["why"])
    p = _app._pair_plan({"a": leg(h=15, cost=58.0, bid=52.0, ask=54.0),
                         "b": leg(bid=40.0, ask=41.0)}, 15, 105, 8, t30_exit=False)
    check("kill switch restores the cost floor + the completion bid",
          p["a"]["ask"] == (58.0, 15) and p["b"]["bid"] is not None)
    p = _app._pair_plan({"a": leg(h=15, cost=58.0, bid=52.0, ask=54.0),
                         "b": leg(bid=40.0, ask=41.0)}, 15, 105, 25)
    check("at T-25 (outside T-10) the held leg still floors at cost and the partner bids",
          p["a"]["ask"] == (58.0, 15) and p["b"]["bid"] is not None)
    # both held at 44 + 55 = 99 → the lock rides, no asks
    p = _app._pair_plan({"a": leg(h=15, cost=44.0, bid=44.0, ask=45.0),
                         "b": leg(h=15, cost=55.0, bid=56.0, ask=57.0)}, 15, 110, 2000)
    check("both held ≤100 → no asks (lock rides)", p["a"]["ask"] is None and p["b"]["ask"] is None
          and "lock_rides" in p["a"]["why"])
    # both held at 102, 45 min out → asks at cost; 20 min out → none
    two = {"a": leg(h=15, cost=45.0, bid=44.0, ask=47.0), "b": leg(h=15, cost=57.0, bid=56.0, ask=59.0)}
    p = _app._pair_plan(two, 15, 110, 45, hold_both=False)
    check("both held >100 before T−30 → asks at cost", p["a"]["ask"] == (45.5, 15) and p["b"]["ask"] == (57.5, 15))
    p = _app._pair_plan(two, 15, 110, 8, hold_both=False)
    check("both held inside T−10 → no asks, hold for the middle",
          p["a"]["ask"] is None and "t30_middle" in p["b"]["why"])
    # An unreadable book leaves BOTH orders alone (Sep 19 2026: one failed read
    # cancelled a resting bid and re-placed it at the back of the queue).
    p = _app._pair_plan({"a": leg(h=15, cost=43.5, book_ok=False),
                         "b": leg(bid=57.5, ask=58.0)}, 15, 110, 2000)
    check("unreadable book → keep the orders, change nothing",
          p["a"]["bid"] == "keep" and p["a"]["ask"] == "keep"
          and "book_unreadable" in p["a"]["why"])
    check("the other leg is unaffected", p["b"]["bid"] == (57.5, 15))

    # T−10 with NOTHING held: both bids come down (Rob, Sep 20 2026). With one
    # leg held the partner keeps bidding until T-10 (Sep 26: the cut moved
    # from 30 to 10 — "it's really that last 30 seconds when anything moves").
    p = _app._pair_plan({"a": leg(bid=43.5, ask=44.0), "b": leg(bid=57.5, ask=58.0)}, 15, 110, 8)
    check("T-10, nothing held → both bids cancel",
          p["a"]["bid"] is None and p["b"]["bid"] is None
          and "t30_unpaired" in p["a"]["why"])
    p = _app._pair_plan({"a": leg(bid=43.5, ask=44.0), "b": leg(bid=57.5, ask=58.0)}, 15, 110, 20)
    check("T-20, nothing held → the bids still work (the cut is T-10 now)",
          p["a"]["bid"] is not None and p["b"]["bid"] is not None)
    p = _app._pair_plan({"a": leg(h=15, cost=43.5, bid=43.0, ask=45.0),
                         "b": leg(bid=57.5, ask=58.0)}, 15, 110, 20)
    check("T-20, one leg held → the other keeps bidding to complete the pair",
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

    # College: at a 21.5 line the favorite wins by EXACTLY 21 six per cent of
    # the time (CFBD closing lines 2017→) — a real middle. The Oct 2 halving
    # priced it at 1.75 and refused it; the measured row is the judge now.
    check("college middle on 21 at a 21.5 line is worth the measured 6.0",
          abs(_app._pair_worth("NCAAF", "spread", [21], line=-21.5) - 6.0) < 1e-6)
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
    check("seeder seats EARLY only — hours of lead, not minutes (6h while the "
          "family pays early, 3h when it pays day-of only — Sep 25 2026)",
          _app._PAIR_MIN_LEAD_H >= 3 and _app._PAIR_MIN_LEAD_DAYOF_H >= 2
          and "_pair_min_lead_h(" in src)
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
    check("one order read for the whole lane", "_pmm_open_orders_raw(client, fresh=(_oage > 120.0))" in tick)
    check("legs go on the markets-socket watch list (MERGED, never a replace)",
          "_WS_WATCHLIST_MERGE_CB" in tick)
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
    i = src.index('if (side == "bid" and have is None')
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
    # …and the cheap partner comes DOWN with it (Sep 29 2026, MTL@TOR over
    # 7.5 resting alone): half a pair with nothing held is a naked bet.
    check("the cheap partner does NOT rest alone — pair or nothing",
          plan["home"]["bid"] is None
          and "partner_refused" in plan["home"]["why"])
    check("hockey rows re-rung as hockey, never as NFL",
          _app._pair_sport_of("tsc-nhl-mon-tor-2026-09-29") == "NHL"
          and _app._pair_sport_of("asc-cfb-x-y-2026-10-03") == "NCAAF")
    # partner HELD at 40: the hedge may chase to cap - cost = 80
    legs2 = {"away": dict(base, h=0.0, bid=81.5),
             "home": dict(base, h=15.0, bid=None, cost=40.0)}
    plan2 = _app._pair_plan(legs2, 15, 120.0, 4000.0)
    px2 = plan2["away"]["bid"][0] if isinstance(plan2["away"]["bid"], tuple) else None
    check("with the other leg HELD the hedge completes — 120 pair cap is the "
          "fence, not 65 (Rob, Sep 22: at a 120 cap there IS a legal pair)",
          plan2["away"]["bid"] is not None and plan2["away"]["bid"][0] == 80.0)


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
    for fn in (_app._pair_rerung_both, _app._pair_rerung):
        rr = inspect.getsource(fn)
        check(f"{fn.__name__} retires the slug it walks away from",
              "retired_slugs" in rr and "retired[-40:]" in rr)
        check(f"{fn.__name__} never fails silently",
              "_pair_rr_why" in rr)
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


def test_pair_price_refusal_triggers_a_rerung() -> None:
    """NO ORDERS, NO RENT (Rob, Sep 22 2026). When the touch drifts past the
    65¢ leg cap the planner returns bid=None, the step cancels the resting
    order, and the leg used to drop out of the re-rung candidate set entirely
    — it only held legs we WOULD bid but below the touch. So the expensive leg
    went silent instead of walking toward the line where it gets cheaper.
    Overnight: ~15 legs an hour cancelled, exactly ONE re-rung, 19 pairs
    quoting neither side."""
    import app as _app
    import inspect
    src = inspect.getsource(_app._pair_step)
    check("a price-refused leg joins the re-rung candidates",
          'plan[k]["bid"] is None' in src and '"leg_cap" in (plan[k].get("why")' in src)
    check("…only when that leg is not already held",
          "_under += [k for k in _e" in src)
    rr = inspect.getsource(_app._pair_rerung_both)
    check("a missing market row says so instead of 'no legal pair'",
          "no market row" in rr)


def test_pair_sign_rule_does_not_freeze_the_whole_pair() -> None:
    """THE SIGN RULE FROZE 26 PAIRS SILENTLY (Rob, Sep 22 2026: "no orders, no
    god damn rent"). The venue's net is per MARKET and a rung's two sides share
    one slug, so when anything else on the account holds the other side the leg
    reads an opposite-signed net. The old rule returned immediately, logging
    nothing but a Telegram ping that is dead on the box — the pair quoted
    neither leg and never said why.

    That position is not ours, so the leg holds ZERO: no ask (we cannot sell
    what we do not have), but the bid still goes out."""
    import app as _app
    import inspect
    src = inspect.getsource(_app._pair_step)
    i = src.index("THE SIGN RULE")
    seg = src[i:i + 1600]
    check("it no longer returns out of the whole pair",
          "net = 0.0" in seg and "res[\"errors\"] += 1\n            return" not in seg)
    check("…and it LOGS, instead of only pinging a dead Telegram",
          "app.logger.warning" in seg)


def test_pair_recovers_a_missing_lot_cost() -> None:
    """A HELD LEG WITH NO COST IS DEAD BOTH WAYS (Rob, Sep 22 2026). The sell
    floor IS the cost and the partner's ceiling is `cap − that cost`, so a
    leg holding a position with cost=None quotes nothing at all. Cost is
    stamped once when the fill is first seen, and a re-rung resets leg state
    and wipes it — four pairs sat holding and silent. Recover it from OUR lot
    ledger, never the venue's blended average."""
    import app as _app
    import inspect
    src = inspect.getsource(_app._pair_step)
    check("a held leg with no cost is repaired from the lot ledger",
          'h >= 1.0 and s.get("cost") is None' in src and "_lot_ledger(sb)" in src)
    check("…only when the ledger covers what we hold",
          "_lq >= h - 0.01" in src)
    check("…and the recovered cost is range-checked",
          "0.5 <= _c <= 99.5" in src)
    rr = inspect.getsource(_app._pair_rerung_both)
    check("'already on the rule's rungs' is split from a real refusal",
          "already on the rule's rungs" in rr)


def test_pair_keep_still_records_the_price() -> None:
    """'keep' MEANS THE ORDER IS ALREADY THERE (Sep 22 2026). The write path
    stamped bid_c/ask_c only on created/amended, so a leg resting at exactly
    the planned price recorded nothing and read as unquoted — pair 81 had live
    bids on both legs and the row showed one. Not cosmetic: bid_c is what the
    lot-cost fallback reads on a fill, and ask_c is recorded as the sell price,
    so a blank one loses the number after the trade. A failed write now says
    so too, instead of leaving an empty reason."""
    import app as _app
    import inspect
    src = inspect.getsource(_app._pair_step)
    check("a kept order still records its price",
          'elif verdict == "keep"' in src and 's[side + "_c"] = want[0]' in src)
    check("a failed write leaves a reason, not a blank",
          '"write_failed"' in src)


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
    check("…and when no re-seat exists and nothing is held, it tears down",
          "a pick owns a leg and" in src and "TORN DOWN" in src)
    check("the freeze warning is rate-limited, not once per tick",
          '_PAIR_FREEZE_PING.get("pick:" + _fs' in src)
    check("a freeze on a pick-owned slug is not a deadlock — re-pick off it",
          "unfroze" in src and "_foreign" in src)
    check("…only with nothing held on OUR legs — a pick's position is not ours",
          "for sl in slugs if sl not in _foreign" in src)
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
    check("…and the pair cap is the loss budget (105 since Sep 26)",
          _app._pair_ceiling("NFL") == 105.0)
    check("WIDEST window under the cap, centre as the tie-break",
          "cand = (len(hits), -_off, -cost)" in src)
    check("…and the 65c leg cap is what keeps widest from buying a -441 side",
          "_PAIR_MAX_LEG_C" in src)
    check("…and nothing outranks it — no sub-100 seeding preference",
          "cost < 100.0" not in src)
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
    check("the per-sport floors are gone — the loss budget IS the ceiling (Sep 26)",
          all(v == 100.0 for v in _app._PAIR_CEILING_FLOOR.values())
          and _app._PAIR_MAX_LOSS_C == 5.0)
    src = inspect.getsource(_app._pair_board_mlb)
    check("MLB slugs resolve by tricode + ET date", "America/New_York" in src)
    check("first-five and inning variants never qualify",
          "_PAIR_MLB_TOTAL_RE" in src)
    # a real MLB ladder: Over 8.5 at 50 / Under 9.5 at 54 = 104 for a window
    # worth 8.7 — a 4.7c edge, under the 105 cap. (At 108 the same window is
    # over the cap AND a 0.7c edge — refused either way.)
    rungs = [("over", 8.5, 50.0, 51.0), ("under", 9.5, 54.0, 55.0)]
    c = _app._pair_candidates(rungs, "total", "MLB", 15)
    check("an MLB total pair at 104 qualifies (worth 8.7 → cap 108.7)",
          c and c[0]["hits"] == [9] and c[0]["cost_c"] == 104.0)
    check("the same window at 106 still seats under its 108.7 worth cap (the flat 105 no longer binds)",
          bool(_app._pair_candidates([("over", 8.5, 52.0, 53.0), ("under", 9.5, 54.0, 55.0)], "total", "MLB", 15)))
    check("the same window at 108 is refused on edge (worth 8.7, 8 paid — under the 1¢ floor)",
          not _app._pair_candidates(
              [("over", 8.5, 53.0, 54.0), ("under", 9.5, 55.0, 56.0)],
              "total", "MLB", 15))
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
    # the LIVE budget is 5 (cap 105, Sep 26 2026): on this book only the
    # mirror (+1.5 at 62 → 99) is reachable — the widest rung under the cap
    live = _app._pair_partner_options("home", -1.5, rungs, "spread", "NFL", 37.0)
    check("at cap 105 only the mirror is affordable on this book",
          live and live[0]["line"] == 1.5 and all(o["line"] == 1.5 for o in live))
    # the ladder-walk MECHANICS below are exercised at a 20 budget (cap 120)
    import app as _app0
    _old0 = _app0._machine_flag_val
    _app0._machine_flag_val = (
        lambda k, d=None: 20.0 if k == "pair_max_loss_c" else _old0(k, d))
    opts = _app._pair_partner_options("home", -1.5, rungs, "spread", "NFL", 37.0)
    lines = [o["line"] for o in opts]
    # +4.5 at pair 115 is INSIDE a 120 loss budget, and is the widest
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
        _app2._machine_flag_val = (
            lambda k, d=None: 20.0 if k == "pair_max_loss_c" else _old0(k, d))
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
    _app0._machine_flag_val = _old0


def test_pair_leg_side() -> None:
    """A LEG'S SIDE COMES FROM ITS LABEL (Sep 25 2026): rows 86/93/97 were
    hand-written with leg `a` on the HOME/UNDER side, and every path that
    read a=away/over would have walked the partner onto the held leg's own
    side of the line."""
    import app as _app
    check("home label on key a reads home",
          _app._pair_leg_side({"key": "a", "label": "home +0.5"}, "spread") == "home")
    check("under label on key a reads under",
          _app._pair_leg_side({"key": "a", "label": "under +47.5"}, "total") == "under")
    check("no label → the key's convention (a = away)",
          _app._pair_leg_side({"key": "a"}, "spread") == "away")
    check("no label → the key's convention (b = under)",
          _app._pair_leg_side({"key": "b"}, "total") == "under")
    import inspect
    src = inspect.getsource(_app._pair_rerung)
    check("the re-rung walks every option, not just the first",
          "for o in opts:" in src and "why_last" in src)
    check("…and reads both sides off the labels",
          "_pair_leg_side(legs[hk], mt)" in src and "_pair_leg_side(legs[ek], mt)" in src)
    st = inspect.getsource(_app.api_pair_status)
    check("/api/pair/status reads sides the same way", "_pair_leg_side(legs[hk], mt)" in st)


def test_buy_amend_sends_the_total() -> None:
    """MODIFY'S `quantity` IS THE ORDER TOTAL, FILLS CARRIED (the sell arm's
    Sep 9 measurement). Sending LEAVES shrank every partially filled bid by
    its fill on each amend (Sep 25 2026): pair legs, the repeg chase and
    the buy sniper all did it."""
    import app as _app
    import inspect
    # Sep 26 2026: the Sep 25 strings truncated decimals (int(19.7)+0.3 = 19);
    # every site now goes through _amend_total (behavioral test in
    # test_review_sep26_sizing_and_state). These check the wiring only.
    pw = inspect.getsource(_app._pair_order_write)
    check("pair amend adds the filled part", "_amend_total(n, _cum)" in pw)
    rp = inspect.getsource(_app._repeg_tick)
    check("repeg chase amends with cum + leaves",
          "_amend_total(leaves, f.get(\"order_cum\"))" in rp
          and "_repeg_amend(client, oid, slug, canon, qty_amend, _gtt_a)" in rp)
    sb = inspect.getsource(_app._snipe_buy_one)
    check("buy sniper amends with cum + leaves", '_amend_total(snap.get("leaves_f"' in sb)
    bp = inspect.getsource(_app._buy_snap_publish)
    check("the snapshot carries cum", '"cum": float(f.get("order_cum") or 0.0)' in bp)
    check("the pop guard is real, not `pass`",
          'SCALP_POPPED.get(slug, 0.0) > float(fs.get("read_mono") or t_read)' in bp
          and "            pass" not in bp.split("read_mono")[0][-400:])


def test_football_wall_is_checked_before_the_price() -> None:
    """pairs_own_football (Sep 21 2026) walled the executor; the callers kept
    paying for pricing (OMS every 30 min, the sweep's dossier builds) and the
    recenter kept CANCELLING seats the executor could no longer re-seat."""
    import app as _app
    import inspect
    oms = inspect.getsource(_app._oms_pass)
    check("OMS refuses walled rows before pricing",
          '_walled = (mtype in ("spread", "total")' in oms
          and '"pairs_own", _OMS_RETRY_MIN["pairs_own"]' in oms)
    check("pairs_own retries in hours", _app._OMS_RETRY_MIN.get("pairs_own", 0) >= 120)
    rc = inspect.getsource(_app._gridiron_recenter_tick)
    check("recenter stands down behind the wall", '{"gate": "pairs_own"}' in rc)
    sw = inspect.getsource(_app._gridiron_bet_sweep)
    check("the bet sweep skips its builds behind the wall", 'stats["sweep_gate"] = "pairs_own"' in sw)


def test_review_sep26_sizing_and_state() -> None:
    """Behavioral checks for the Sep 26 independent review: fractional-fill
    amend totals (5), the pair's gone-is-not-a-create (2), malformed venue
    envelopes (7), and the sniper read stamp (6)."""
    import app as _app, inspect, time as _t
    from types import SimpleNamespace as NS
    at = _app._amend_total
    check("amend total: 19.7 leaves + 0.3 filled = 20 (not 19)", at(19.7, 0.3) == 20)
    check("amend total: 14.7 + 0.3 = 15", at(14.7, 0.3) == 15)
    check("amend total: whole numbers pass through", at(15, 0) == 15 and at(19.7, 0) == 20)
    check("amend total: dust is 0, never a 1-lot", at(0.3, 0) == 0 and at(0.6, 0) == 1)
    # pair write: an order that vanished during the amend is handed back, not replaced
    created = []
    client = NS(orders=NS(create=lambda p: (created.append(p) or {"id": "second"}),
                          modify=lambda *a: None, list=lambda *a: {"orders": []}))
    _sleep = _app._time.sleep
    try:
        _app._time.sleep = lambda *a: None
        v = _app._pair_order_write(client, "test", "ORDER_INTENT_BUY_LONG", 51, 15,
                                   "2099-01-01T00:00:00Z",
                                   {"id": "first", "price_yes": .50, "leaves": 15, "cum": 0})
    finally:
        _app._time.sleep = _sleep
    check("pair write: 'gone' returns gone and creates NOTHING", v[0] == "gone" and not created)
    # pair write: a partially filled bid is amended to the rounded total
    mods = []
    client2 = NS(orders=NS(create=lambda p: created.append(p),
                           modify=lambda oid, m: mods.append(m),
                           list=lambda *a: {"orders": [{"id": "first", "state": "ORDER_STATE_REPLACED",
                                                         "price": {"value": "0.510"}, "quantity": 15}]}))
    try:
        _app._time.sleep = lambda *a: None
        v2 = _app._pair_order_write(client2, "test", "ORDER_INTENT_BUY_LONG", 51, 14.7,
                                    "2099-01-01T00:00:00Z",
                                    {"id": "first", "price_yes": .50, "leaves": 14.7, "cum": 0.3})
    finally:
        _app._time.sleep = _sleep
    check("pair write: 14.7 wanted + 0.3 filled amends to quantity 15",
          bool(mods) and mods[-1].get("quantity") == 15 and v2[0] == "amended")
    # malformed positions envelope keeps the mirror
    m = _app._VENUE_MIRROR
    _keep = dict(m["positions"]); _rc = _app._pmm_read_client
    try:
        m["positions"] = {"t": {"net": 20.0, "qty": 20.0, "avg_price": 0.5}}
        _app._pmm_read_client = lambda c: c
        out = _app._pmm_positions_raw(NS(portfolio=NS(positions=lambda: {"unexpected": 1})), fresh=True)
        check("positions: a response without 'positions' is unreadable (None), mirror kept",
              out is None and "t" in m["positions"])
        out2 = _app._pmm_open_orders_raw(NS(orders=NS(list=lambda: {"unexpected": 1})), fresh=True)
        check("orders: a response without 'orders' is unreadable (None)", out2 is None)
    finally:
        m["positions"] = _keep; _app._pmm_read_client = _rc
    # REST install keeps a socket fill that landed during the fetch (finding 3)
    _keep = dict(m["positions"]); _kat = m.get("positions_at"); _rc = _app._pmm_read_client
    try:
        m["positions"] = {}; m["positions_at"] = 0.0; m.setdefault("pos_ts", {}).clear()
        _app._pmm_read_client = lambda c: c
        def _positions_with_fill_midflight():
            _app._mirror_position_event({"marketSlug": "t", "afterPosition": {"netPositionDecimal": "20", "cost": {"value": "10"}}})
            return {"positions": {}}        # snapshot taken BEFORE the fill
        out3 = _app._pmm_positions_raw(NS(portfolio=NS(positions=_positions_with_fill_midflight)), fresh=True)
        check("positions: a socket fill during the REST fetch survives the install",
              out3 is not None and "t" in out3 and abs(float(out3["t"]["net"]) - 20) < 0.01
              and "t" in m["positions"])
    finally:
        m["positions"] = _keep; m["positions_at"] = _kat or 0.0; _app._pmm_read_client = _rc
    src = inspect.getsource(_app._repeg_tick)
    check("sniper read stamp is taken when the lap snapshot is read, not later",
          src.index("_fs_read = _time.monotonic()") < src.index("lap_orders = _pmm_open_orders_raw(lap_client)"))
    rp = inspect.getsource(_app._repeg_tick)
    check("repeg chase sizes the amend from decimal leaves", "_amend_total(leaves, f.get(\"order_cum\"))" in rp)
    sn = inspect.getsource(_app._snipe_buy_one)
    check("buy sniper sizes the amend from decimal leaves", "_amend_total(snap.get(\"leaves_f\"" in sn)


def test_executor_one_order_per_slug() -> None:
    """Review Sep 26, findings 1 and 4 — behavioral, fake venue + fake DB:
    a second call on a slug with our bid resting is refused; a create whose
    response was lost is booked from the venue's list, not repeated; an
    unreadable list fails closed; a duplicate-key insert on someone else's
    order id cancels our twin."""
    import app as _app
    from types import SimpleNamespace as NS
    class DB:
        def __init__(self): self.rows = []; self.pending = None
        def table(self, *a): self.pending = None; return self
        def select(self, *a): return self
        def filter(self, *a): return self
        def eq(self, *a): return self
        def gte(self, *a): return self
        def limit(self, *a): return self
        def contains(self, *a): return self
        def insert(self, row): self.pending = row; return self
        def execute(self):
            if self.pending is not None:
                if self.rows: raise RuntimeError("23505 duplicate key")
                self.rows.append(self.pending)
            return NS(data=[dict(r) for r in self.rows])
    class Venue:
        def __init__(self, fail_create=False, list_raises=False):
            self.created = []; self.cancelled = []; self.fail_create = fail_create; self.list_raises = list_raises
            self.orders = NS(create=self._create, list=self._list, cancel=lambda oid, p: self.cancelled.append(oid))
        def _create(self, p):
            self.created.append(dict(p, id=f"o{len(self.created)+1}"))
            if self.fail_create: raise TimeoutError("response lost")
            return {"id": self.created[-1]["id"]}
        def _list(self, p):
            if self.list_raises: raise RuntimeError("429")
            return {"orders": [{"id": c["id"], "intent": c["intent"], "state": "ORDER_STATE_NEW",
                                "manualOrderIndicator": c["manualOrderIndicator"]} for c in self.created]}
    saved = {k: getattr(_app, k) for k in ("get_client", "_pmm_positions_raw", "_rent_ok", "_kalshi_owner_uid",
             "_book_exposure_usd", "_machine_flag", "_machine_flag_val", "_repeg_verify_or_recreate",
             "_send_fill_telegram")}
    _sleep = _app._time.sleep
    args = ({"id": "game", "event_name": "Test game"}, "2099-01-01T00:00:00Z", "spread", "away", "Away",
            "test-slug", False, 50, .55, 5, 5, 49, 51)
    def run(v, db, **kw):
        _app.get_client = lambda: v
        return _app._autobet_execute(db, *args, skip_game_dedup=True, **kw)
    try:
        _app._time.sleep = lambda *a: None
        _app._pmm_positions_raw = lambda *a, **k: {}
        _app._rent_ok = lambda *a, **k: (True, "day_of")
        _app._kalshi_owner_uid = lambda: "owner"
        _app._book_exposure_usd = lambda: 0.0
        _app._machine_flag = lambda *a, **k: False
        _app._machine_flag_val = lambda n, d=None: d
        _app._repeg_verify_or_recreate = lambda *a, **k: "ok"
        _app._send_fill_telegram = lambda *a, **k: None
        v = Venue(); db = DB()
        a = run(v, db); tags = []; b = run(v, db, fail_tag=tags)
        check("executor: first call places, second call is refused (bid already resting)",
              a == "placed" and b is False and "resting_order_exists" in tags and len(v.created) == 1)
        v2 = Venue(fail_create=True); db2 = DB()
        a2 = run(v2, db2)
        check("executor: a create whose response was lost is booked from the venue, not repeated",
              a2 == "placed" and len(v2.created) == 1 and len(db2.rows) == 1
              and db2.rows[0]["signal_blob"]["order_id"] == "o1")
        v3 = Venue(list_raises=True); db3 = DB(); tags3 = []
        a3 = run(v3, db3, fail_tag=tags3)
        check("executor: an unreadable order list fails CLOSED (no create)",
              a3 is False and "orders_unreadable" in tags3 and not v3.created)
        v4 = Venue(); db4 = DB()
        db4.rows.append({"id": 1, "signal_blob": {"pmm_slug": "test-slug", "order_id": "someone-else"}})
        tags4 = []
        a4 = run(v4, db4, fail_tag=tags4)
        check("executor: duplicate-key on another order id cancels OUR twin",
              a4 is False and "twin_cancelled" in tags4 and v4.cancelled == ["o1"])
    finally:
        for k, f in saved.items(): setattr(_app, k, f)
        _app._time.sleep = _sleep


def test_neutral_and_rejections_sep26() -> None:
    """A NEUTRAL (void) resolution is a refund, never a loss; a venue REJECTED
    create is remembered and not retried every lap (Sep 26 2026)."""
    import app as _app, inspect
    dm = inspect.getsource(_app._venue_day_map)
    check("day map: NEUTRAL resolution scores 0", 'neutral = side.endswith("_NEUTRAL")' in dm and "0.0 if neutral" in dm)
    pa = inspect.getsource(_app.parse_activities)
    check("parse_activities: NEUTRAL → pnl 0", 'if side == "NEUTRAL":\n                    pnl = 0.0' in pa)
    import pathlib
    sql = pathlib.Path(__file__).resolve().parents[1].joinpath("kahla-scanner/supabase/poly_gameday_pnl.sql").read_text()
    check("RPC: NEUTRAL → 0", "POSITION_RESOLUTION_SIDE_NEUTRAL' then 0" in sql)
    sc = inspect.getsource(_app._scalp_create)
    check("scalp create: a REJECTED frame after our create backs the slug off",
          "ORDER_REJECTED.get((slug, sell_intent), 0.0) >= _t_create - 1.0" in sc and "_SCALP_REJECT_UNTIL[slug]" in sc)
    st = inspect.getsource(_app._scalp_tick)
    check("scalp tick: slugs under rejection backoff are skipped and counted", '"skip_rejected"' in st)
    ch = inspect.getsource(_app._cellar_health)
    check("tripwire names a rejection storm", "SCALP REJECTED" in ch)
    ps = inspect.getsource(_app._pair_step)
    check("pair: a bid the venue just rejected is not re-created for 30 min", '"rejected_recently"' in ps)
    ws = pathlib.Path(__file__).resolve().parent.joinpath("wsfeed.py").read_text()
    check("wsfeed stamps ORDER_REJECTED and dumps the first raw frame", "ORDER_REJECTED[(" in ws and "ws priv ORDER REJECTED" in ws)
    sc2 = inspect.getsource(_app._scalp_create)
    check("scalp ask expiry is floored in the future (stale event_start can't expire it)", "_flo = now + timedelta(hours=8)" in sc2)
    rc = inspect.getsource(_app._reconcile_tick)
    check("reconcile re-syncs a pick's event_start from its market", "re-synced event_start" in rc)


def test_pair_tick_guards() -> None:
    """One row per slug, a slug cap that names what it cannot serve, a lap
    budget with rotation, a merge (not replace) watch push, park-after-cancel
    and the unseen-fill guard (Sep 25 2026)."""
    import app as _app
    import inspect
    tk = inspect.getsource(_app._pair_tick)
    check("younger twin rows are skipped and named", '"dup_rows"' in tk and "_owner" in tk)
    check("a shared slug is owned by the row holding MORE legs, not the older one",
          "sum(1 for lg in (r.get(\"legs\") or []) if _held(lg.get(\"slug\")))" in tk)
    check("a losing twin whose own leg is held keeps running (its ask stays managed)",
          '"dup_rows_kept"' in tk)
    check("a losing twin's third-leg bid is cancelled", '"dup_bids_cancelled"' in tk)
    check("two legs on one side is not a pair: skipped, bids cancelled",
          '"not_pair_rows"' in tk and '"not_pair_bids_cancelled"' in tk)
    st2 = inspect.getsource(_app._pair_step)
    check("a bid never makes a second leg on a side we already hold on the game",
          "_pair_side_held(positions, row.get(\"game_prefix\"), mt," in st2 and "side_held" in st2)
    sd2 = inspect.getsource(_app._pair_seed_tick)
    check("the seeder declines a game where a seat is already held (adopt or decline, never stack)",
          '"held_seat"' in sd2)
    al = inspect.getsource(_app._pmm_autolog)
    check("ghost adoption treats only a MANUAL ask as the user's takeover (an AUTOMATIC ask is ours)",
          "if not o.get(\"auto\"):\n                _sell_slugs.add(_sl)" in al)
    ps = inspect.getsource(_app._pair_slugs)
    check("a retired-but-held leg is released to the autolog", "net" in ps and "retired_slugs" in ps)
    # the side helper reads the venue's sign convention
    P = {"asc-x-a-b-2026-01-01-pos-3pt5": {"net": 15}, "asc-x-a-b-2026-01-01-neg-2pt5": {"net": -9},
         "tsc-x-a-b-2026-01-01-total-41pt5": {"net": -19}, "asc-x-a-b-2026-01-01-pos-1pt5": {"net": 0.3}}
    check("side helper: away +3.5 x15 reads as AWAY held",
          _app._pair_side_held(P, "asc-x-a-b-2026-01-01", "spread", "away") == 15)
    check("side helper: short on away -2.5 reads as HOME held",
          _app._pair_side_held(P, "asc-x-a-b-2026-01-01", "spread", "home") == 9)
    check("side helper: dust is ignored and the excluded slug is skipped",
          _app._pair_side_held(P, "asc-x-a-b-2026-01-01", "spread", "away",
                               exclude_slug="asc-x-a-b-2026-01-01-pos-3pt5") == 0)
    check("side helper: short total reads as UNDER, spreads don't leak into totals",
          _app._pair_side_held(P, "tsc-x-a-b-2026-01-01", "total", "under") == 19
          and _app._pair_side_held(P, "tsc-x-a-b-2026-01-01", "total", "over") == 0)
    check("the lane reads the WHOLE account's orders (mirror first, REST only when >120s stale; no slugs in the URL — 414 at ~200)",
          "_pmm_open_orders_raw(client, fresh=(_oage > 120.0))" in tk and 'orders.list({"slugs": all_slugs' not in tk)
    check("the lap has a budget and rotates", "_PAIR_TICK_BUDGET_S" in tk and "_PAIR_ROT" in tk)
    check("the watch push merges", "_WS_WATCHLIST_MERGE_CB(set(all_slugs))" in tk
          and "_WS_WATCHLIST_CB(set(all_slugs))" not in tk)
    st = inspect.getsource(_app._pair_step)
    check("an off-touch cancel parks the leg", 'st[k]["parked_at"] = now.isoformat()' in st
          and '"parked"' in st)
    check("a vanished bid re-reads the venue before a new lot",
          '"fill_unseen"' in st and "_pmm_positions_raw(client, fresh=True)" in st)
    check("the $13 rule governs bids only",
          'if side == "bid" and want[1] * want[0] / 100.0 > _REPEG_MAX_COST_USD' in st)
    check("rent is read tri-state", "_pair_rent(slug, ko, now, sb)" in st)
    sd = inspect.getsource(_app._pair_seed_tick)
    check("the seeder's lead floor follows the program", "_pair_min_lead_h(r.get(\"sport\"), sb)" in sd)
    st3 = inspect.getsource(_app._pair_step)
    check("a frozen pair is decided before the leg walk and counted as frozen, not error",
          '_frozen = [lg["slug"] for lg in legs_def if lg.get("slug") in _foreign]' in st3
          and 'res["frozen"] = res.get("frozen", 0) + 1' in st3)
    gr = inspect.getsource(_app._pair_from_gridiron_rule)
    check("football seeder ranks WIDEST window first (Rob, Sep 26: widest to start, rerung tighter as needed)",
          "cand = (len(hits), -_off, -cost)" in gr and "STAY ON TOUCH" in gr)
    po = inspect.getsource(_app._pair_partner_options)
    check("the re-rung walks widest → tighter", 'out.sort(key=lambda o: (-o["worth"], o["pair_c"]))' in po)
    pr = inspect.getsource(_app._pin_daily_refresh)
    check("Pinnacle is pulled twice a day: 02:30 (before the first day-of window) and 06:00",
          '_slot = "0230"' in pr and '_slot = "0600"' in pr and _app._PIN_REFRESH_EARLY_AZ_MIN == 150)


def test_bet_sheet_rung() -> None:
    """Bet Sheets (Oct 3 2026): exact rung first, else the nearest MORE
    FAVORABLE rung (Rob: "auto pick the more favorable side"), never a worse
    one; ML is side-only."""
    import app as _app
    sp = [{"side": "home", "line": -3.5, "slug": "h35"}, {"side": "home", "line": -3.0, "slug": "h3"},
          {"side": "home", "line": -4.5, "slug": "h45"}, {"side": "away", "line": 3.5, "slug": "a35"},
          {"side": "away", "line": 4.5, "slug": "a45", "synthetic": True}, {"side": "away", "line": 2.5, "slug": "a25"}]
    e, why = _app._bet_sheet_rung(sp, "spread", "home", -3.5)
    check("exact home rung", e["slug"] == "h35" and why == "exact")
    e, why = _app._bet_sheet_rung(sp, "spread", "home", -4.0)
    check("favorite missing −4 → −3.5 (fewer points), not −4.5", e["slug"] == "h35" and why == "favorable")
    e, why = _app._bet_sheet_rung(sp, "spread", "away", 4.0)
    check("dog missing +4 → +4.5 (more points), not +3.5", e["slug"] == "a45" and why == "favorable")
    e, why = _app._bet_sheet_rung(sp, "spread", "home", -2.0)
    check("favorite wants −2, venue only worse → refused with the ladder", e is None and "venue has" in why)
    tt = [{"side": "over", "line": 6.5, "slug": "o65"}, {"side": "over", "line": 5.5, "slug": "o55"},
          {"side": "under", "line": 6.5, "slug": "u65"}, {"side": "under", "line": 7.5, "slug": "u75"}]
    e, why = _app._bet_sheet_rung(tt, "total", "over", 6.0)
    check("over 6 missing → over 5.5 (lower)", e["slug"] == "o55" and why == "favorable")
    e, why = _app._bet_sheet_rung(tt, "total", "under", 7.0)
    check("under 7 missing → under 7.5 (higher)", e["slug"] == "u75" and why == "favorable")
    e, why = _app._bet_sheet_rung(tt, "total", "over", 7.0)
    check("over 7: 6.5 and 5.5 both better → nearest (6.5)", e["slug"] == "o65")
    e, why = _app._bet_sheet_rung([{"side": "home", "slug": "mh"}, {"side": "away", "slug": "ma"}], "ml", "away", None)
    check("ML is side-only", e["slug"] == "ma")
    e, why = _app._bet_sheet_rung([], "spread", "home", -3.5)
    check("empty ladder → reason, no crash", e is None and why)


def test_hand_move_plan() -> None:
    """Line-movement cancel (Oct 4 2026): our-side touch size 70% off its
    60-90s high AND re-posted a rung or two lower (depth below the old touch
    grew by ≥70% of the high) → cancel. A pure vanish (makers leaving) stays.
    The ask is recorded, not required; `need_ask=True` restores it."""
    import app as _app
    f, calm, orient = _app._hand_move_plan, _app._hand_calm, _app._hand_side_rows
    N = 10000.0
    def row(ts, bc, bq, ac, aq, d3=None):
        return (ts, bc, bq, ac, aq, d3 if d3 is not None else bq)
    # 150k at 55, 20k at 54.5, 10k at 54 behind it
    base = [row(N - 80, 55.0, 150000, 55.5, 120000, 180000), row(N - 50, 55.0, 148000, 55.5, 121000, 178000),
            row(N - 20, 55.0, 151000, 55.5, 119000, 181000)]
    # THE MOVE: the 55 level empties and the size re-posts at 54 (touch moves) → cancel
    v, d = f(base + [row(N - 2, 54.0, 140000, 54.5, 90000, 170000)], 55.0, 1.0, N, 70)
    assert v == "cancel" and d["why"] == "move" and d["relocated_pct"] > 70, (v, d)
    # the same move with a straggler (us) still holding the touch at 55 → cancel
    v, d = f(base + [row(N - 2, 55.0, 190, 55.5, 119000, 155190)], 55.0, 1.0, N, 70)
    assert v == "cancel" and d["why"] == "move" and d["ask_down"] is False, (v, d)
    # THE VANISH (Rob: "if they just went away because institutional money
    # left the game, that's not really a book movement") → stay
    v, d = f(base + [row(N - 2, 55.0, 190, 55.5, 119000, 30190)], 55.0, 1.0, N, 70)
    assert v == "stay" and d["why"] == "vanish", (v, d)
    # relocated but the ask hasn't moved, with the ask confirmation on → stay
    v, d = f(base + [row(N - 2, 55.0, 190, 55.5, 119000, 155190)], 55.0, 1.0, N, 70, need_ask=True)
    assert v == "stay" and d["why"] == "pull", (v, d)
    # ask down but size intact → stay (not a collapse)
    v, d = f(base + [row(N - 2, 55.0, 140000, 55.0, 90000, 170000)], 55.0, 1.0, N, 70)
    assert v == "stay" and d["why"] == "quiet", (v, d)
    # 60% off at the touch is not 70% off, however much sits below
    v, d = f(base + [row(N - 2, 55.0, 60000, 55.5, 90000, 200000)], 55.0, 1.0, N, 70)
    assert v == "stay" and d["why"] == "quiet", (v, d)
    # a smaller relocation (half the size came back lower) is not a move at the default 60…
    v, d = f(base + [row(N - 2, 54.5, 75000, 55.0, 90000, 95000)], 55.0, 1.0, N, 70)
    assert v == "stay" and d["why"] == "vanish", (v, d)
    # …but 65% of it coming back lower IS (Rob: "I'm the 10% that gets fucked" — 60, not 90)
    v, d = f(base + [row(N - 2, 54.5, 100000, 55.0, 90000, 130000)], 55.0, 1.0, N, 70)
    assert v == "cancel" and d["why"] == "move", (v, d)
    assert f(base + [row(N - 2, 54.5, 75000, 55.0, 90000, 95000)], 55.0, 1.0, N, 70, relocate_pct=40)[0] == "cancel"
    # the touch IMPROVED (bids above us) → nothing collapsed at P
    v, d = f(base + [row(N - 2, 55.5, 90000, 56.0, 90000, 200000)], 55.0, 1.0, N, 70)
    assert v == "stay" and d["why"] == "quiet", (v, d)
    # WE SIT BELOW THE DEPARTURE RUNG (Rob, Oct 4 2026: "51.5 went from 1800
    # to 200… who cares… we were at 51"): the exact move that cancels a bid
    # at 55 is nobody's business when our bid rests at 54.5
    v, d = f(base + [row(N - 2, 54.0, 140000, 54.5, 90000, 170000)], 54.5, 1.0, N, 70)
    assert v == "stay" and d["why"] == "below_rung", (v, d)
    # our own contracts never count toward the baseline or the collapse
    lone = [row(N - 80, 55.0, 5.0, 55.5, 120000), row(N - 50, 55.0, 5.0, 55.5, 120000)]
    v, d = f(lone + [row(N - 2, 55.0, 5.0, 54.5, 90000)], 55.0, 5.0, N, 70)
    assert v == "stay" and d["why"] == "quiet", (v, d)
    # stale newest frame / no baseline → stay
    assert f(base + [row(N - 2, 54.0, 140000, 54.5, 90000, 170000)], 55.0, 1.0, N + 400, 70)[0] == "stay"
    assert f([row(N - 2, 54.0, 140000, 54.5, 90000, 170000)], 55.0, 1.0, N, 70)[0] == "stay"
    # THE VANISH DROP (Rob, Oct 4 2026: "I probably don't want to be out
    # there alone… instead of cancel we drop a rung to the new fat rung"):
    # 70% off at 55 with NOTHING re-posted below → amend down to the first
    # rung beneath us that has company. Needs the ladder (element 6).
    def lrow(ts, bc, bq, ac, aq, ladder):
        return (ts, bc, bq, ac, aq, sum(q for _, q in ladder[:3]), ladder)
    lbase = [lrow(N - 80, 55.0, 150000, 55.5, 120000, [(55.0, 150000), (54.5, 20000), (54.0, 10000)]),
             lrow(N - 50, 55.0, 148000, 55.5, 121000, [(55.0, 148000), (54.5, 20000), (54.0, 10000)]),
             lrow(N - 20, 55.0, 151000, 55.5, 119000, [(55.0, 151000), (54.5, 20000), (54.0, 10000)])]
    v, d = f(lbase + [lrow(N - 2, 55.0, 190, 55.5, 119000, [(55.0, 190), (54.5, 20000), (54.0, 10000)])], 55.0, 5.0, N, 70)
    check("vanish with a fat rung below → drop to 54.5", v == "drop" and d["why"] == "vanish_drop" and d["drop_c"] == 54.5)
    # the first rung below is itself a sliver (300 over 10000) → skip it, drop to 54
    v, d = f(lbase + [lrow(N - 2, 55.0, 190, 55.5, 119000, [(55.0, 190), (54.5, 300), (54.0, 10000)])], 55.0, 5.0, N, 70)
    check("vanish: sliver rung skipped → drop to 54", v == "drop" and d["drop_c"] == 54.0)
    # nothing below at all → stay (nowhere to go)
    v, d = f(lbase + [lrow(N - 2, 55.0, 190, 55.5, 119000, [(55.0, 190)])], 55.0, 5.0, N, 70)
    check("vanish with nothing below → stay", v == "stay" and d["why"] == "vanish")
    # drop switched off → the old vanish verdict
    v, d = f(lbase + [lrow(N - 2, 55.0, 190, 55.5, 119000, [(55.0, 190), (54.5, 20000)])], 55.0, 5.0, N, 70, drop=False)
    check("drop off → stay/vanish", v == "stay" and d["why"] == "vanish")
    # a RELOCATION still cancels (the move rule is untouched by the drop)
    v, d = f(lbase + [lrow(N - 2, 55.0, 190, 55.5, 119000, [(55.0, 190), (54.5, 140000), (54.0, 10000)])], 55.0, 5.0, N, 70)
    check("relocation still cancels", v == "cancel" and d["why"] == "move")
    # legacy rows (no ladder) keep the old vanish → stay
    v, d = f(base + [row(N - 2, 55.0, 190, 55.5, 119000, 30190)], 55.0, 1.0, N, 70)
    check("no ladder → stay/vanish (nowhere known to drop)", v == "stay" and d["why"] == "vanish")
    # synthetic NO orientation: YES ask is our bid, YES bid is our ask, YES ask depth is our bid depth
    yes = [(N - 50, 44.5, 120000, 45.0, 150000, 130000, 180000), (N - 2, 45.5, 90000, 46.0, 30000, 95000, 40000)]
    ours = orient(yes, True)
    assert abs(ours[0][1] - 55.0) < 1e-9 and ours[0][2] == 150000 and abs(ours[0][3] - 55.5) < 1e-9, ours[0]
    assert ours[0][5] == 180000 and ours[1][5] == 40000, ours
    assert abs(ours[1][1] - 54.0) < 1e-9 and abs(ours[1][3] - 54.5) < 1e-9, ours[1]
    assert orient(yes, False)[0][5] == 130000
    # PER-RUNG (Rob: "watch four rungs below the departure rung… which rung
    # has the biggest increase, that's our rejoin spot"): rows carry the
    # our-side ladder as element 6; relocation is measured over P-0.5..P-2.0.
    def lrow(ts, lad, ac=55.5, aq=100000):
        return (ts, lad[0][0], lad[0][1], ac, aq, sum(q for _, q in lad[:3]), lad)
    L0 = [(55.0, 150000), (54.5, 20000), (54.0, 10000), (53.5, 5000), (53.0, 40000)]
    lbase = [lrow(N - 80, L0), lrow(N - 50, L0), lrow(N - 20, L0)]
    # 10% to 54.5, 70% to 54, 10% to 53.5 → move, rejoin at 54
    L1 = [(54.5, 35000), (54.0, 115000), (53.5, 20000), (53.0, 40000)]
    v, d = f(lbase + [lrow(N - 2, L1)], 55.0, 1.0, N, 70)
    assert v == "cancel" and d["why"] == "move" and d["rejoin_c"] == 54.0, (v, d)
    assert d["below_before_q"] == 75000 and d["below_now_q"] == 210000, d
    # the size went FOUR rungs down (53.0 = 2¢ under 55) — still inside the window
    L2 = [(54.5, 20000), (54.0, 10000), (53.5, 5000), (53.0, 140000)]
    v, d = f(lbase + [lrow(N - 2, L2)], 55.0, 1.0, N, 70)
    assert v == "cancel" and d["rejoin_c"] == 53.0, (v, d)
    # the size reappeared 3¢ down (52.0) — outside the four-rung window → vanish
    L3 = [(54.5, 20000), (54.0, 10000), (53.5, 5000), (53.0, 40000), (52.0, 140000)]
    v, d = f(lbase + [lrow(N - 2, L3)], 55.0, 1.0, N, 70)
    assert v == "stay" and d["why"] == "vanish" and d["rejoin_c"] is None, (v, d)
    # stragglers (us) still at 55, size relocated → move, rejoin at the gaining rung not the touch
    L4 = [(55.0, 10), (54.5, 25000), (54.0, 120000), (53.5, 5000), (53.0, 40000)]
    v, d = f(lbase + [lrow(N - 2, L4)], 55.0, 10.0, N, 70)
    assert v == "cancel" and d["rejoin_c"] == 54.0, (v, d)
    # synthetic NO ladder: YES asks become our bids, inverted
    yes_l = (N - 2, 44.0, 1000, 45.0, 90000, 1000, 95000, [(44.0, 1000)], [(45.0, 90000), (45.5, 20000)])
    o = orient([yes_l], True)[0]
    assert abs(o[1] - 55.0) < 1e-9 and o[6] == [(55.0, 90000), (54.5, 20000)], o
    # calm: mid held a tick for the window; moving mid is not calm; empty window is not calm
    assert calm([row(N - 50, 55.0, 1, 55.5, 1), row(N - 20, 55.0, 1, 55.5, 1), row(N - 2, 55.0, 1, 55.5, 1)], N)
    assert not calm([row(N - 50, 55.0, 1, 55.5, 1), row(N - 2, 54.0, 1, 54.5, 1)], N)
    assert not calm([row(N - 500, 55.0, 1, 55.5, 1)], N)
    print("  PASS  hand line-move planner (collapse + 4-rung relocation → cancel at the gaining rung; vanish stays) + calm")


def test_price_grid() -> None:
    """Quarter-cent ticks (Oct 4 2026, NE@BUF ML bid 64.00 / ask 64.25): the
    hand rails check the MARKET's grid, never a hard-coded half cent."""
    import app as _app
    g = _app._on_grid
    assert g(67.75, 0.25) and g(67.5, 0.25) and g(67.0, 0.25) and not g(67.8, 0.25)
    assert g(67.5, 0.5) and not g(67.75, 0.5)
    assert g(67.0, 1.0) and not g(67.5, 1.0)
    assert not g(None, 0.5)
    assert f"{0.6425:.4f}" == "0.6425" and f"{0.6425:.3f}" != "0.6425"
    print("  PASS  price grid honours the market tick (0.25 / 0.5 / 1.0)")


def test_slug_side_line() -> None:
    """App adoption (Oct 4 2026): side/line off the venue slug convention
    when the market question can't be read."""
    import app as _app
    f = _app._slug_side_line
    assert f("asc-cfb-neb-ind-2026-10-04-neg-9pt5", False) == ("spread", "away", -9.5)
    assert f("asc-cfb-neb-ind-2026-10-04-neg-9pt5", True) == ("spread", "home", 9.5)
    assert f("asc-nfl-dal-hou-2026-10-04-pos-2pt5", False) == ("spread", "away", 2.5)
    assert f("tsc-nfl-ne-buf-2026-10-04-total-49pt5", False) == ("total", "over", 49.5)
    assert f("tsc-nhl-pit-phi-2026-09-30-6pt5", True) is None or f("tsc-nhl-pit-phi-2026-09-30-6pt5", True)[1] == "under"
    assert f("aec-nfl-tb-sea-2026-10-05", False) == ("moneyline", "away", None)
    assert f("aec-nfl-tb-sea-2026-10-05", True) == ("moneyline", "home", None)
    assert f("astatc-nfl-foo-2026-10-05-pyd-abc-gte250", False) is None
    assert _app._intent_short("ORDER_INTENT_BUY_SHORT") == "BUY_SHORT" and _app._intent_short("BUY_LONG") == "BUY_LONG"
    assert _app._intent_short("ORDER_INTENT_SELL_LONG").startswith("SELL") and _app._intent_short(None) == ""
    e = _app._event_slug_from_market
    assert e("aec-cfb-nevada-ndkst-2026-10-17") == "cfb-nevada-ndkst-2026-10-17"
    assert e("asc-cfb-neb-ind-2026-10-04-neg-9pt5") == "cfb-neb-ind-2026-10-04"
    assert e("tsc-nfl-ne-buf-2026-10-04-total-49pt5") == "nfl-ne-buf-2026-10-04"
    assert e("tsc-nhl-pit-phi-2026-09-30-6pt5") == "nhl-pit-phi-2026-09-30"
    assert e("astatc-nfl-den-sf-2026-10-04-recyd-chrmcc-gte30") is None
    assert _app._slug_sport("asc-cfb-neb-ind-2026-10-04-neg-9pt5") == "NCAAF"
    assert _app._slug_sport("aec-nfl-tb-sea-2026-10-05") == "NFL"
    print("  PASS  app-adoption slug side/line")


def test_hand_start_plan() -> None:
    """The 'game started' rule (Oct 4 2026): both-side touch size 70% off
    its pre-start high, at or after the listed start, two frames, cancel."""
    import app as _app
    f = _app._hand_start_plan
    S = 1000.0
    def row(ts, bq, aq):
        return (ts, 47.0, bq, 47.5, aq, bq, aq)
    pre = [row(S - 60, 150000, 120000), row(S - 30, 140000, 125000), row(S - 5, 60000, 30000)]
    # pre-start: never fires whatever the sizes
    assert f(pre + [row(S - 2, 1000, 1000)], S, S - 1, 70)[0] == "pre"
    # no tape before the start → no baseline
    assert f([row(S + 1, 100, 100), row(S + 2, 100, 100)], S, S + 3, 70)[0] == "no_baseline"
    # one post-start frame is not enough
    assert f(pre + [row(S + 2, 5000, 4000)], S, S + 3, 70)[0] == "wait"
    # two frames under 30% of the 270k high → started (baseline is the HIGH, not the last pre-start size)
    v = f(pre + [row(S + 2, 5000, 4000), row(S + 3, 6000, 3000)], S, S + 4, 70)
    assert v[0] == "started" and v[1] == 270000 and v[2] == 9000 and v[3] > 96, v
    # a bounce above the line on the newest frame → wait
    assert f(pre + [row(S + 2, 5000, 4000), row(S + 3, 90000, 60000)], S, S + 4, 70)[0] == "wait"
    # 60% off is not 70% off
    assert f(pre + [row(S + 2, 60000, 50000), row(S + 3, 60000, 50000)], S, S + 4, 70)[0] == "wait"
    # stale newest frame → wait (the socket may be dead; the GTD backstop owns it)
    assert f(pre + [row(S + 2, 5000, 4000), row(S + 3, 6000, 3000)], S, S + 400, 70)[0] == "wait"
    # pre-pull up to 30s early still measures against the real high (DAL total read 29% at T-26s)
    early = [row(S - 80, 100000, 128000), row(S - 26, 50000, 15000)]
    v = f(early + [row(S + 2, 2600, 14700), row(S + 9, 2900, 12700)], S, S + 10, 70)
    assert v[0] == "started" and v[1] == 228000, v
    # GTD slack is a positive offset from the listed start
    from datetime import datetime, timezone
    g = _app._hand_gtt(datetime(2026, 10, 4, 17, 0, tzinfo=timezone.utc))
    assert g > "2026-10-04T17:00:00Z" and g.endswith("Z"), g
    print("  PASS  hand start-cancel planner + GTD slack")


def test_hand_chase_plan() -> None:
    """The hand-bet chase (Oct 4 2026): JOIN the touch, only UP, and a
    touch more than the leash past Rob's anchor is a HOLD, not a move."""
    import app as _app
    plan = _app._hand_chase_plan
    check("at touch → stay", plan(47.0, 47.0, 47.0, 2.0)[0] == "stay")
    check("we ARE the best bid → stay", plan(47.5, 47.0, 47.0, 2.0)[0] == "stay")
    v, tgt, note = plan(51.0, 52.0, 51.0, 2.0)
    check("one tick behind, inside the leash → move to the touch", v == "move" and tgt == 52.0)
    v, tgt, note = plan(49.0, 49.5, 49.0, 2.0)
    check("half a tick behind → move", v == "move" and tgt == 49.5)
    v, tgt, note = plan(47.0, 49.0, 47.0, 2.0)
    check("exactly the leash (2¢) still moves", v == "move" and tgt == 49.0)
    v, tgt, note = plan(47.0, 49.5, 47.0, 2.0)
    check("past the leash → hold with the touch recorded", v == "hold" and tgt == 49.5 and note == "leash")
    v, _t, _n = plan(49.0, 49.5, 47.0, 2.0)
    check("leash is measured from the ANCHOR, not the current price", v == "hold")
    check("no anchor → the current price is the anchor", plan(47.0, 48.0, None, 2.0)[0] == "move")
    check("no quote → stay", plan(47.0, None, 47.0, 2.0)[0] == "stay")
    check("touch at 99 → stay", plan(47.0, 99.0, 47.0, 60.0)[0] == "stay")
    # THE REJOIN HOLD (Oct 4 2026): sat back down at 51.0 after a move, the
    # straggler rung at 51.5 is inside the leash but above the cap → stay.
    v, _t, note = plan(51.0, 51.5, 51.0, 2.0, cap_c=51.0)
    check("rejoin hold: touch above the cap → stay", v == "stay" and note == "rejoin_hold")
    check("rejoin hold: touch AT the cap → at_touch", plan(51.0, 51.0, 51.0, 2.0, cap_c=51.0)[0] == "stay")
    check("rejoin hold: below the cap still moves", plan(50.0, 50.5, 50.0, 2.0, cap_c=51.0)[0] == "move")
    check("no cap → the old rule", plan(51.0, 51.5, 51.0, 2.0)[0] == "move")


def test_hand_thin_touch() -> None:
    """THE THIN-TOUCH GUARD (Oct 4 2026): don't join a touch rung that is a
    sliver of the rung under it — that rung is the straggler's."""
    import app as _app
    f = _app._hand_thin_touch
    thin, d = f([(52.0, 150.0), (51.5, 1800.0), (51.0, 900.0)], 51.0, 5, 20.0)
    check("150 over 1800 (8%) → thin", thin and d["why"] == "thin" and d["touch_c"] == 52.0)
    thin, d = f([(52.0, 600.0), (51.5, 1800.0)], 51.0, 5, 20.0)
    check("600 over 1800 (33%) → ok", not thin and d["why"] == "ok")
    thin, d = f([(52.0, 365.0), (51.5, 1800.0)], 51.5, 5, 20.0)
    check("our 5 at the rung below are not its company (365 vs 1795 = 20.3%) → ok", not thin)
    thin, d = f([(52.0, 358.0), (51.5, 1800.0)], 51.5, 5, 20.0)
    check("a hair under 20% (358 vs 1795) → thin", thin)
    thin, d = f([(52.0, 5.0), (51.5, 1800.0)], 52.0, 5, 20.0)
    check("we ARE the whole touch rung → 0 over 1800 → thin", thin and d["touch_q"] == 0.0)
    check("one rung → nothing to compare → ok", f([(52.0, 10.0)], 51.0, 5, 20.0)[0] is False)
    check("pct 0 → off", f([(52.0, 1.0), (51.5, 1800.0)], 51.0, 5, 0)[0] is False)
    check("unsorted ladder is sorted best-first", f([(51.5, 1800.0), (52.0, 10.0)], 51.0, 5, 20.0)[0] is True)
    check("empty → ok", f(None, 51.0, 5, 20.0)[0] is False)
    # the chase plan itself is unchanged: the guard sits in the tick between 'move' and the amend
    check("plan still says move", _app._hand_chase_plan(51.0, 52.0, 51.0, 2.0)[0] == "move")
    # the boot fallback: a REST book → our-side ladder (Oct 4 2026, the 28s-after-boot join)
    g = _app._hand_ladder_from_book
    bk = {"bids": [(46.0, 3409.0), (45.5, 784.0)], "asks": [(46.5, 57.0), (47.0, 450.0)],
          "best_bid": 46.0, "best_ask": 46.5}
    lad = g(bk, True)
    check("synthetic NO ladder = inverted asks, best-first", lad == [(53.5, 57.0), (53.0, 450.0)])
    check("…and that touch reads thin", f(lad, 51.5, 10, 20.0)[0] is True)
    check("YES ladder = the bids", g(bk, False) == [(46.0, 3409.0), (45.5, 784.0)])
    check("no book → None (the tick holds)", g(None, True) is None and g({"asks": []}, True) is None)
    # ONE SEAT RULE for quote / place / re-peg / chase (Rob, Oct 4 2026: "the
    # original bet's the problem on half of these… We're on the thin rung.
    # It's fat under me. Should fucking work")
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    sp = _app._hand_seat_price
    now = _dt(2026, 10, 4, 21, 0, tzinfo=_tz.utc)
    far = (now + _td(days=5)).isoformat()
    near = (now + _td(hours=3)).isoformat()
    pc, d = sp(bk, True, far, now, 20.0, 6.0)
    check("far out, thin touch → the fat rung under it (53.0)", pc == 53.0 and d["thin"] and d["why"] == "thin_touch")
    pc, d = sp(bk, True, near, now, 20.0, 6.0)
    check("inside T-6h → the touch (53.5), guard off", pc == 53.5 and not d["thin"])
    fat = {"asks": [(46.5, 400.0), (47.0, 450.0)], "bids": [], "best_bid": None, "best_ask": 46.5}
    pc, d = sp(fat, True, far, now, 20.0, 6.0)
    check("fat touch → the touch", pc == 53.5 and not d["thin"])
    pc, d = sp(bk, True, far, now, 20.0, 6.0, our_price_c=53.5, our_qty=10)
    check("our own 10 on the touch are not its company → still the rung under", pc == 53.0 and d["thin"])
    check("pct 0 → the touch", sp(bk, True, far, now, 0, 6.0)[0] == 53.5)
    check("no bids → None", sp({"asks": [], "bids": []}, True, far, now, 20.0, 6.0)[0] is None)


def test_hand_orders_queue() -> None:
    """THE HAND-ORDER QUEUE (Oct 3 2026): Vercel enqueues, the box claims
    atomically; with no box, Vercel claims the row itself (claim-first, so
    a row never runs twice) and the executor runs exactly once."""
    import app as _app

    class _Q:
        def __init__(self, db, table): self.db, self.t, self.f, self.op, self.payload = db, table, [], None, None
        def select(self, *a): self.op = "select"; return self
        def insert(self, row): self.op, self.payload = "insert", row; return self
        def update(self, row): self.op, self.payload = "update", row; return self
        def eq(self, k, v): self.f.append((k, v)); return self
        def in_(self, k, vs): self.f.append((k, set(vs))); return self
        def order(self, *a, **k): return self
        def limit(self, n): return self
        def _match(self, r):
            return all((r.get(k) in v) if isinstance(v, set) else (r.get(k) == v) for k, v in self.f)
        def execute(self):
            rows = self.db.setdefault(self.t, [])
            class R: pass
            out = R()
            if self.op == "insert":
                row = dict(self.payload); row["id"] = len(rows) + 1; rows.append(row); out.data = [row]
            elif self.op == "update":
                hit = [r for r in rows if self._match(r)]
                for r in hit: r.update(self.payload)
                out.data = [dict(r) for r in hit]
            else:
                out.data = [dict(r) for r in rows if self._match(r)]
            return out

    class _SB:
        def __init__(self): self.db = {}
        def table(self, t): return _Q(self.db, t)

    sb = _SB()
    sb.db["hand_orders"] = []
    saved = (_app._HAND_ENSURED["ok"], _app._HAND_CLAIM_WAIT_S, _app.get_client, _app._hand_order_execute)
    ran = []
    try:
        _app._HAND_ENSURED["ok"] = True
        _app._HAND_CLAIM_WAIT_S = 0.0
        _app.get_client = lambda: object()
        _app._hand_order_execute = lambda sb_, c, row, worker: (ran.append((row.get("id"), worker)) or {"ok": True, "order_id": "X1"})
        rid = _app._hand_order_enqueue(sb, {"op": "create", "slug": "s", "synthetic": False,
                                            "contracts": 1, "payload": {"asked_by": "u"}})
        check("enqueue returns an id", rid == 1)
        check("row starts pending", sb.db["hand_orders"][0]["state"] == "pending")
        import time as _t
        done = _app._hand_orders_wait(sb, [rid], _t.monotonic() + 5.0)
        check("no box → Vercel claims and runs it", done.get(rid, {}).get("ok") is True and ran == [(1, "vercel")])
        check("row is done with the result", sb.db["hand_orders"][0]["state"] == "done"
              and (sb.db["hand_orders"][0]["result"] or {}).get("order_id") == "X1")
        # a row the box already claimed: Vercel must NOT run it
        rid2 = _app._hand_order_enqueue(sb, {"op": "create", "slug": "s2", "contracts": 1})
        check("box claim wins the atomic update", _app._hand_order_claim(sb, rid2, "box") is True)
        check("second claim loses", _app._hand_order_claim(sb, rid2, "vercel") is False)
        done2 = _app._hand_orders_wait(sb, [rid2], _t.monotonic() + 1.5)
        check("Vercel waits on a box-claimed row and never runs it", rid2 not in done2 and ran == [(1, "vercel")])
        # the box lane: claims pending rows and runs them
        rid3 = _app._hand_order_enqueue(sb, {"op": "create", "slug": "s3", "contracts": 1, "created_at": "2099-01-01T00:00:00+00:00"})
        from datetime import datetime as _dt, timezone as _tz
        st = _app._hand_orders_tick(sb, _dt.now(_tz.utc), worker="box")
        check("box tick claims + runs the pending row", st.get("claimed") == 1 and st.get("done") == 1 and ran[-1] == (3, "box"))
    finally:
        _app._HAND_ENSURED["ok"], _app._HAND_CLAIM_WAIT_S, _app.get_client, _app._hand_order_execute = saved
    # THE END STATE (Rob, Oct 3 2026: the box "is wrapping down everything it
    # does… betting nothing new"): the hand executor must boot ALONE, with no
    # machine lane beside it and no owner uid.
    from cellar import config as _cfg
    from cellar.runner import Runner as _Runner
    check("the daemon boots with CELLAR_LANES=handbets alone", _Runner.validate(["handbets"]) == [])
    check("handbets needs no owner uid", not _cfg.ALL_LANES["handbets"].needs_owner)
    check("handbets is quiet (no idle tick flood)", _cfg.ALL_LANES["handbets"].quiet)


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
              test_lane_covers_its_documented_engines, test_pair_plan, test_pair_candidates, test_pair_owner_guard, test_pair_priority_gate, test_pair_rerung, test_pair_mlb_totals, test_pair_off_touch_rule, test_pair_dead_ladder, test_pair_uses_executor_rule, test_pair_window, test_pair_reline, test_pair_keep_still_records_the_price, test_pair_recovers_a_missing_lot_cost, test_pair_sign_rule_does_not_freeze_the_whole_pair, test_pair_price_refusal_triggers_a_rerung, test_pair_lot_cost_never_from_the_venue_blend, test_pairs_own_football_spreads_and_totals, test_pair_slugs_span_every_row_and_retired_leg, test_pair_leg_cap_in_the_engine, test_ladder_window_total_sides, test_team_totals_are_not_the_game_total, test_pair_seed_throughput, test_pair_completion_exempt, test_pair_read_budget, test_pair_venue_reads,
              test_pair_leg_side, test_buy_amend_sends_the_total, test_review_sep26_sizing_and_state, test_executor_one_order_per_slug, test_neutral_and_rejections_sep26,
              test_football_wall_is_checked_before_the_price, test_pair_tick_guards,
              test_side_and_phase, test_ttls_agree_with_engines, test_bet_sheet_rung, test_hand_chase_plan, test_hand_thin_touch, test_hand_start_plan, test_hand_move_plan, test_slug_side_line, test_price_grid, test_hand_orders_queue):
        t()
    print(f"\n  {len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        print("  FAILED: " + ", ".join(_FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
