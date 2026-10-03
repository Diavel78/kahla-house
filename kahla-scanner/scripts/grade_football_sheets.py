"""FOOTBALL SHEETS — grade the sheet's own picks against ESPN finals.

"How did the sheet do" for a slate of dates. Reads football_sheets for the
requested (week_key, sport) rows whose event_start falls on one of the
given AZ-anchored dates, pulls the model's bet_spread/bet_total picks
(Friday-overlay aware, same preference order the renderer uses), fetches
ESPN finals for those same dates via espn_id (exact join, no fuzzy team
matching needed — football_sheets.espn_id is stamped at assembly time),
and grades each pick against the actual final score.

This is REFERENCE ONLY — the sheet is a human-read handicapping product,
not a bot_picks/venue-money lane, so there is no resolver for it. This
script is the resolver, run on demand.

Runs on GitHub Actions (reaches both ESPN and Supabase; the CCR sandbox is
ESPN-blocked, same reason football_sheet_data.py runs there).

Usage:
  python -m scripts.grade_football_sheets --sport NCAAF --date 2026-09-19
  python -m scripts.grade_football_sheets --sport NCAAF --sport NFL --date 2026-09-19 --date 2026-09-20
  python -m scripts.grade_football_sheets --sport NCAAF --week-key 2026-09-14 --date 2026-09-19
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, __import__("os").path.dirname(
    __import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from scripts.football_sheet_data import sb_select, sb_patch, _espn_get, _LEAGUES  # noqa: E402

log = logging.getLogger("grade_football_sheets")
# Hockey sheets (sport='NHL', same table — Sep 30 2026) stamp ESPN hockey
# event ids, so they grade off the same scoreboard shape; football_sheet_data
# only knows the two football leagues.
_GRADE_LEAGUES = dict(_LEAGUES, NHL=("hockey", "nhl"))
AZ = ZoneInfo("America/Phoenix")


def _az_date(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.astimezone(AZ).date().isoformat()


def espn_finals(sport: str, date: str) -> dict[str, dict]:
    """espn_id -> {home_score, away_score, state} for one AZ-anchored date.

    ESPN's `dates=` param is a plain YYYYMMDD (no timezone attached to the
    query itself — it buckets by the game's own local/UTC date on their
    side), so a date that starts an evening slate late AZ-time can spill
    into ESPN's next UTC day. Fetch the requested date AND the next one,
    keep whichever event actually lands on our AZ date.
    """
    grp, lg = _GRADE_LEAGUES[sport]
    url = f"https://site.api.espn.com/apis/site/v2/sports/{grp}/{lg}/scoreboard"
    y, m, d = (int(x) for x in date.split("-"))
    from datetime import date as _date, timedelta
    base = _date(y, m, d)
    out: dict[str, dict] = {}
    for dd in (base, base + timedelta(days=1)):
        params = {"dates": f"{dd:%Y%m%d}"}
        if sport == "NCAAF":
            params.update({"groups": "80", "limit": "400"})
        doc = _espn_get(url, params)
        if not doc:
            continue
        for ev in doc.get("events") or []:
            comp = (ev.get("competitions") or [{}])[0]
            try:
                start_iso = ev.get("date") or ""
                if _az_date(start_iso) != date:
                    continue
            except Exception:
                continue
            state = ((ev.get("status") or {}).get("type") or {}).get("state")
            scores = {}
            for c in comp.get("competitors") or []:
                ha = c.get("homeAway")
                try:
                    scores[ha] = float(c.get("score"))
                except (TypeError, ValueError):
                    scores[ha] = None
            out[str(ev.get("id"))] = {
                "state": state,
                "home_score": scores.get("home"),
                "away_score": scores.get("away"),
                "name": ev.get("shortName"),
            }
    return out


def _model_block(blob: dict) -> dict | None:
    friday = (blob or {}).get("friday") or {}
    return friday.get("model") or (blob or {}).get("model")


def grade_spread(bs: dict, home_score: float, away_score: float) -> str | None:
    line = bs.get("market_home_line")
    if line is None:
        return None
    margin = (home_score - away_score) + line   # home-cover margin
    if margin == 0:
        return "push"
    home_covered = margin > 0
    side = bs.get("side")
    if side == "home":
        return "win" if home_covered else "loss"
    if side == "away":
        return "win" if not home_covered else "loss"
    return None


def grade_ml(pk: dict, home_score: float, away_score: float) -> str | None:
    if home_score == away_score:
        return "push"
    home_won = home_score > away_score
    side = pk.get("side")
    if side == "home":
        return "win" if home_won else "loss"
    if side == "away":
        return "win" if not home_won else "loss"
    return None


def grade_total(bt: dict, home_score: float, away_score: float) -> str | None:
    line = bt.get("line")
    if line is None:
        return None
    total = home_score + away_score
    if total == line:
        return "push"
    over_hit = total > line
    side = bt.get("side")
    if side == "over":
        return "win" if over_hit else "loss"
    if side == "under":
        return "win" if not over_hit else "loss"
    return None


def grade_model(sport: str, data_blob: dict, hs: float, aws: float) -> dict:
    """The MODEL's side on every market it priced, pass or not (Rob, Oct 3
    2026: "grade the model, not the picks"). Football: the calibrated margin
    vs the market home line, the calibrated total vs the market total.
    Hockey: win_home vs 50% on the ML, exp_total vs the market total.
    A model number that lands exactly on the line has no side → no key."""
    out: dict = {}
    db = data_blob or {}
    if sport == "NHL":
        m = db.get("model") or {}
        ln = db.get("lines") or {}
        wh = m.get("win_home")
        if wh is not None and wh != 0.5:
            res = grade_ml({"side": "home" if wh > 0.5 else "away"}, hs, aws)
            if res:
                out["ml"] = res
        et, lt = m.get("exp_total"), ln.get("total")
        if et is not None and lt is not None and et != lt:
            res = grade_total({"line": lt, "side": "over" if et > lt else "under"}, hs, aws)
            if res:
                out["total"] = res
        return out
    blob = _model_block(db) or {}
    if not blob:
        return out
    mm = blob.get("margin_cal", blob.get("margin_raw"))
    L = (blob.get("bet_spread") or {}).get("market_home_line")
    if mm is not None and L is not None and (mm + L) != 0:
        # home covers when (home margin + home line) > 0 → the model's side
        res = grade_spread({"market_home_line": L,
                            "side": "home" if (mm + L) > 0 else "away"}, hs, aws)
        if res:
            out["spread"] = res
    mt = blob.get("total_cal", blob.get("total_raw"))
    lt = (blob.get("bet_total") or {}).get("line")
    if mt is not None and lt is not None and mt != lt:
        res = grade_total({"line": lt, "side": "over" if mt > lt else "under"}, hs, aws)
        if res:
            out["total"] = res
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", action="append", choices=["NFL", "NCAAF", "NHL"], required=True)
    ap.add_argument("--date", action="append", default=[],
                     help="AZ-anchored date YYYY-MM-DD; repeatable")
    ap.add_argument("--days-back", type=int, default=0,
                     help="also grade the N AZ days before today (box batch job)")
    ap.add_argument("--today", action="store_true",
                     help="also grade today's AZ date — intraday passes; a game ESPN "
                          "has not marked final is skipped, so this is safe to run "
                          "while games are in progress")
    ap.add_argument("--stamp", action="store_true",
                     help="write the tally to exec_probe_runs (kind=sheet_grade)")
    ap.add_argument("--write", action="store_true",
                     help="stamp each graded game's result into football_sheets."
                          "data_blob.grade (what /pick-sheets renders as ✅/❌)")
    ap.add_argument("--week-key", default=None,
                     help="omit to check the latest football_sheet_weeks for each sport")
    args = ap.parse_args()

    dates = set(args.date)
    today_az = datetime.now(AZ).date()
    if args.today:
        dates.add(today_az.isoformat())
    if args.days_back:
        from datetime import timedelta
        for n in range(1, args.days_back + 1):
            dates.add((today_az - timedelta(days=n)).isoformat())
    if not dates:
        ap.error("give --date and/or --days-back")
    detail = []
    writes: list[tuple[dict, dict]] = []   # (row, grade) — stamped after the walk
    # tally[sport][market][verdict_tier] = {win, loss, push}
    tally: dict[str, dict[str, dict[str, dict[str, int]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(int))))

    for sport in args.sport:
        wk = args.week_key
        if not wk:
            wk_rows = sb_select("football_sheet_weeks", {
                "select": "week_key", "sport": f"eq.{sport}",
                "order": "week_key.desc", "limit": "1"})
            if not wk_rows:
                log.warning("%s: no football_sheet_weeks row", sport)
                continue
            wk = wk_rows[0]["week_key"]

        rows = sb_select("football_sheets", {
            "select": "id,espn_id,event_name,event_start,tier,data_blob",
            "week_key": f"eq.{wk}", "sport": f"eq.{sport}"})
        rows = [r for r in rows if r.get("espn_id")
                and _az_date(r["event_start"]) in dates]
        if not rows:
            log.warning("%s week %s: no games on %s", sport, wk, sorted(dates))
            continue

        finals: dict[str, dict] = {}
        for d in dates:
            finals.update(espn_finals(sport, d))

        for r in rows:
            eid = r["espn_id"]
            fin = finals.get(eid)
            blob = _model_block(r.get("data_blob"))
            hockey = (r.get("data_blob") or {}).get("picks") if sport == "NHL" else None
            row_detail = {
                "sport": sport, "event_name": r["event_name"],
                "espn_id": eid, "espn_state": (fin or {}).get("state"),
            }
            if not fin or fin.get("state") != "post" \
                    or fin.get("home_score") is None or fin.get("away_score") is None:
                row_detail["note"] = "no final score yet"
                detail.append(row_detail)
                continue
            hs, aws = fin["home_score"], fin["away_score"]
            row_detail["final"] = f"{r['event_name']} ({aws:g}-{hs:g})"
            # The per-row stamp the sheet renders (Rob, Oct 3 2026: "add a
            # grader to the picksheet… so it shows ✅ or ❌"). One key per
            # graded market, win/loss/push; PASS verdicts get no key.
            grade: dict = {"final": {"home": hs, "away": aws}}

            if hockey:
                ml = hockey.get("ml") or {}
                if ml.get("verdict") in ("play", "lean"):
                    res = grade_ml(ml, hs, aws)
                    if res:
                        grade["ml"] = res
                        tier = ml["verdict"]
                        tally[sport]["ml"][tier][res] += 1
                        row_detail["ml"] = (
                            f"{ml.get('team')} {ml.get('price'):+g} "
                            f"[{tier}] -> {res}")
                bt = hockey.get("total") or {}
                if bt.get("verdict") in ("play", "lean"):
                    res = grade_total(bt, hs, aws)
                    if res:
                        grade["total"] = res
                        tier = bt["verdict"]
                        tally[sport]["total"][tier][res] += 1
                        row_detail["total"] = (
                            f"{bt.get('side')} {bt.get('line'):g} "
                            f"[{tier}] -> {res}")
            elif blob:
                bs = blob.get("bet_spread") or {}
                if bs.get("verdict") in ("play", "lean"):
                    res = grade_spread(bs, hs, aws)
                    if res:
                        grade["spread"] = res
                        tier = bs["verdict"]
                        tally[sport]["spread"][tier][res] += 1
                        row_detail["spread"] = (
                            f"{bs.get('team')} {bs.get('line'):+g} "
                            f"[{tier}] -> {res}")
                bt = blob.get("bet_total") or {}
                if bt.get("verdict") in ("play", "lean"):
                    res = grade_total(bt, hs, aws)
                    if res:
                        grade["total"] = res
                        tier = bt["verdict"]
                        tally[sport]["total"][tier][res] += 1
                        row_detail["total"] = (
                            f"{bt.get('side')} {bt.get('line'):g} "
                            f"[{tier}] -> {res}")
            mg = grade_model(sport, r.get("data_blob"), hs, aws)
            if mg:
                grade["model"] = mg
            writes.append((r, grade))
            detail.append(row_detail)

    # Stamp data_blob.grade. Idempotent: an unchanged grade is not rewritten,
    # so the daily 2-day re-grade is free. Merges into the existing blob —
    # the builders' refresh path (data_blob.friday) merges the same way, and
    # the NHL builder only upserts the coming slate, so a stamped row keeps
    # its grade until the game falls out of the week.
    n_stamped = 0
    if args.write:
        now_iso = datetime.now(timezone.utc).isoformat()
        for r, grade in writes:
            blob = dict(r.get("data_blob") or {})
            prev = dict(blob.get("grade") or {})
            prev.pop("graded_at", None)
            if prev == grade:
                continue
            blob["grade"] = dict(grade, graded_at=now_iso)
            try:
                sb_patch("football_sheets", {"id": f"eq.{r['id']}"},
                         {"data_blob": blob})
                n_stamped += 1
            except Exception as e:  # noqa: BLE001 — one bad row never stops the tally
                log.warning("grade stamp failed for %s: %s", r.get("event_name"), e)
        log.info("stamped %d/%d graded rows", n_stamped, len(writes))

    def _fmt(counts: dict) -> str:
        w, losses, p = counts.get("win", 0), counts.get("loss", 0), counts.get("push", 0)
        n = w + losses
        pct = f"{100*w/n:.1f}%" if n else "n/a"
        return f"{w}-{losses}" + (f"-{p}" if p else "") + f"  ({pct})"

    print("\n===== FOOTBALL SHEETS — GRADED RECORD =====")
    print(f"dates: {sorted(dates)}\n")
    grand = defaultdict(lambda: defaultdict(int))
    for sport, markets in tally.items():
        print(f"--- {sport} ---")
        for market in ("ml", "spread", "total"):
            tiers = markets.get(market, {})
            if not tiers:
                continue
            for tier in ("play", "lean"):
                if tier not in tiers:
                    continue
                print(f"  {market:7s} {tier:5s}  {_fmt(tiers[tier])}")
                for k, v in tiers[tier].items():
                    grand[f"{market}:{tier}"][k] += v
            combined = defaultdict(int)
            for tier_counts in tiers.values():
                for k, v in tier_counts.items():
                    combined[k] += v
            print(f"  {market:7s} ALL    {_fmt(combined)}")
        print()

    print("--- COMBINED (all sports, all markets) ---")
    play_only = defaultdict(int)
    play_and_lean = defaultdict(int)
    for key, counts in grand.items():
        market, tier = key.split(":")
        for k, v in counts.items():
            play_and_lean[k] += v
            if tier == "play":
                play_only[k] += v
    print(f"  PLAY only:      {_fmt(play_only)}")
    print(f"  PLAY + LEAN:    {_fmt(play_and_lean)}")

    unresolved = [d for d in detail if "note" in d]
    if unresolved:
        print(f"\n{len(unresolved)} game(s) with no final score yet:")
        for d in unresolved:
            print(f"  - {d['event_name']} (espn_state={d.get('espn_state')})")

    print("\n--- per-game detail (only games with a graded pick) ---")
    for d in detail:
        if "ml" in d or "spread" in d or "total" in d:
            line = f"  {d['final']}"
            if "ml" in d:
                line += f"  | ML {d['ml']}"
            if "spread" in d:
                line += f"  | SPR {d['spread']}"
            if "total" in d:
                line += f"  | TOT {d['total']}"
            print(line)

    out = {"tally": {s: {mk: {t: dict(c) for t, c in tiers.items()}
                         for mk, tiers in m.items()} for s, m in tally.items()},
           "dates": sorted(dates), "play_only": dict(play_only),
           "play_and_lean": dict(play_and_lean),
           "stamped": n_stamped, "graded_rows": len(writes)}
    print("\n" + json.dumps(out, default=dict))
    if args.stamp:
        # exec_probe_runs(at default now(), params jsonb, result jsonb) — the
        # box's probe ledger, readable from a sandbox through the site.
        from scripts.football_sheet_data import _sb_base
        import httpx
        base, headers = _sb_base()
        h = dict(headers, **{"Content-Type": "application/json",
                             "Prefer": "return=minimal"})
        r = httpx.post(f"{base}/rest/v1/exec_probe_runs", headers=h, timeout=30,
                       json=[{"params": {"kind": "sheet_grade", "ctx": "cellar"},
                              "result": {"kind": "sheet_grade", **out}}])
        r.raise_for_status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
