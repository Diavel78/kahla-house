"""PICK SHEETS, BUILT ON THE BOX WITH THE BOX'S MODEL.

Rob, Oct 3 2026: "The sheets need to run the current damn model… UP TO DATE.
The website posts from the box now… Can't you run the model on the box???"

Until today the football sheets were assembled on GitHub Actions against the
ORIGINAL cloud project: their ratings solved from a game_results table nothing
had written since Sep 13, and with none of the layers the betting lanes price
off (QB adjustment, CFBD/nfelo consensus blend) because those live in app.py
on the box. This script runs the same sheet builder
(scripts/football_sheet_data.py) as a batch-lane job on the house box, where:

  * SUPABASE_URL is the box's own Postgres — the one the site reads — so the
    rows land where /pick-sheets looks, with no sync step to forget;
  * game_results / power_ratings / cfbd_ratings / football_qb_adj are the
    live, daily-refreshed tables;
  * the model number is app._gridiron_proj — THE projection every seat,
    recenter and re-peg prices off — through football_sheet_data's
    MODEL_HOOK / PRICE_HOOK. One model, two consumers (machine + humans).

Modes mirror the old workflow: `monday` = full build of the week (refused
on any other weekday unless --force, so a catch-up firing after a daemon
restart cannot rebuild a half-played week and sweep its finished games);
`friday` = re-price every existing row of the week into data_blob.friday,
which the site prefers. Each run stamps exec_probe_runs kind=box_sheets.

Runs as `python -m scripts.box_sheets …` from kahla-scanner (the batch
lane's cwd); imports app.py from the repo root, exactly as `python -m cellar`
does on the same box.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
SCANNER = os.path.dirname(HERE)
ROOT = os.path.dirname(SCANNER)
for _p in (SCANNER, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

log = logging.getLogger("box_sheets")
AZ = ZoneInfo("America/Phoenix")


def _app():
    import app  # the site module; heavy, imported once per run
    return app


def model_hook(sport: str) -> dict | None:
    """The live power_ratings snapshot in the builder's model shape. The
    cover/total math (cover_at/over_at) keys off params.spread_fit /
    total_fit, which the box computes with the same gridiron_spread.fit."""
    from scripts.football_sheet_data import sb_select
    rows = sb_select("power_ratings", {
        "select": "computed_at,league_avg,n_games,ratings,params",
        "sport": f"eq.{sport}", "order": "computed_at.desc", "limit": "1"})
    if not rows:
        log.error("%s: no power_ratings snapshot on the box", sport)
        return None
    row = rows[0]
    return {"sport": sport,
            "R": {"teams": row.get("ratings") or {}, "league_avg": row.get("league_avg")},
            "params": row.get("params") or {},
            "computed_at": row.get("computed_at"), "n_games": row.get("n_games"),
            "src": "box:power_ratings"}


_PIN_SLATES: dict = {}          # sport -> (events, age_min) — one cache read per run


def pin_pull(sports, spend: bool) -> dict:
    """Refresh the Pinnacle slate for each sport through app._pin_slate
    (3 credits a sport, 90-min cache, 900/1,000 monthly hard stop — a
    budget-exhausted pull serves the stale cache) and load it for the
    line hook. spend=False loads the cache only."""
    app = _app()
    sb = app.get_supabase()
    now = datetime.now(ZoneInfo("UTC"))
    out: dict = {}
    for sport in sports:
        try:
            if spend:
                ev = app._pin_slate(sb, sport, now)
            events, age = app._pin_slate_cached(sb, sport, now)
            _PIN_SLATES[sport] = (events, age)
            out[sport] = {"events": len(events or []),
                          "age_min": round(age, 1) if age is not None else None}
        except Exception as e:
            log.warning("%s: pinnacle slate failed: %s", sport, e)
            out[sport] = {"error": str(e)[:120]}
    try:
        month_k = "usage:" + now.strftime("%Y-%m")
        out["credits_used"] = (((app._parlay_state_get(sb, month_k) or {}).get("v") or {})
                               .get("credits") or 0)
    except Exception:
        pass
    return out


def line_hook(sport: str, away: str, home: str) -> dict | None:
    """Pinnacle's spread (home-oriented) + total for one game off the
    cached slate; None when the slate is missing, older than the app's
    _SHEET_PIN_FRESH_S (Oct 6 2026: one 07:00 pull a day, the later
    refreshes price off the book line), or does not list the game — the
    builder then falls back to DK / ESPN consensus exactly as before."""
    app = _app()
    events, age = _PIN_SLATES.get(sport, (None, None))
    fresh_s = getattr(app, "_SHEET_PIN_FRESH_S", app._PIN_CENTER_MAX_AGE_S)
    if not events or age is None or age * 60.0 > fresh_s:
        return None
    sp = app._pin_line_from_events(events, away, home, "spread", book="pinnacle")
    tt = app._pin_line_from_events(events, away, home, "total", book="pinnacle")
    if sp is None and tt is None:
        return None
    return {"spread_home": sp, "total": tt, "src": "pinnacle",
            "age_min": round(age, 1)}


def price_hook(model: dict, home: str, away: str, neutral: bool) -> dict | None:
    """One matchup through app._gridiron_proj: results solve + fitted HFA +
    QB adjustment + (NCAAF/NFL) CFBD/nfelo consensus blend, in RAW margin
    space, so the sheet's own shrinkage fit applies exactly as it does for
    the machine's seats. Returns the `priced` dict build_game_blob expects."""
    app = _app()
    from _lib import power_ratings as pr
    sb = app.get_supabase()
    sport = model["sport"]
    event = f"{away} @ {home}"
    got = app._gridiron_proj(sb, sport, event)
    if not got:
        return None          # unrated / FCS — same as the builder's gp guard
    margin, total, params = got
    params = params or model.get("params") or {}
    hfa = float(params.get("hfa") or 0.0)
    if neutral:
        margin -= hfa        # _gridiron_proj always books the home edge
    sf, tf = params.get("spread_fit"), params.get("total_fit")
    teams = model["R"].get("teams") or {}

    def _net(nm):
        t = teams.get(nm)
        if not t:
            nl = nm.lower()
            for k, v in teams.items():
                if nl in k.lower() or k.lower() in nl:
                    t = v
                    break
        return round(float((t or {}).get("net") or 0.0), 2)

    key = f"{sport}|{event}"
    out = {
        "margin_raw": round(margin, 2), "total_raw": round(total, 2),
        "exp_home": round((total + margin) / 2.0, 2),
        "exp_away": round((total - margin) / 2.0, 2),
        "home_net": _net(home), "away_net": _net(away),
        "neutral_site": neutral,
        "win_prob_home": round(pr.margin_to_prob(
            margin, float(params.get("scale") or 7.0)), 4),
        "n_games": model.get("n_games"), "computed_at": model.get("computed_at"),
        "box_model": {
            "src": "app._gridiron_proj",
            "qb": app._GRIDIRON_QB_NOTE.get(key),
            "cfbd": app._GRIDIRON_CFBD_NOTE.get(key),
        },
    }
    if sf:
        out["margin_cal"] = round(sf["alpha"] + sf["beta"] * margin, 1)
        out["spread_fit_n"] = sf.get("n")
    if tf:
        out["total_cal"] = round(tf["alpha"] + tf["beta"] * total, 1)
        out["total_fit_n"] = tf.get("n")
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["monday", "friday"], default="friday")
    ap.add_argument("--sport", action="append", choices=["NFL", "NCAAF"])
    ap.add_argument("--days", type=int, default=8)
    ap.add_argument("--week-key", default=None)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="allow a monday (full) build on a non-Monday")
    ap.add_argument("--pin", action="store_true",
                    help="pull Pinnacle's slate (parlay-api, ~3 credits a sport, "
                         "90-min cache) and price the sheet against it")
    args = ap.parse_args()

    now_az = datetime.now(AZ)
    if args.mode == "monday" and now_az.weekday() != 0 and not args.force:
        print(json.dumps({"skipped": "monday build only runs on Monday "
                          "(catch-up guard); use --mode friday or --force"}))
        return 0

    from scripts import football_sheet_data as fsd
    fsd.MODEL_HOOK = model_hook
    fsd.PRICE_HOOK = price_hook
    fsd.LINE_HOOK = line_hook
    sports = args.sport or ["NFL", "NCAAF"]
    pin = pin_pull(sports, spend=args.pin)
    wk = args.week_key or fsd.week_key_default()
    summary = fsd.run(args.mode, sports, args.days, wk, args.commit)
    summary["model"] = "box:_gridiron_proj"
    summary["pinnacle"] = pin
    print(json.dumps(summary, indent=2, default=str))
    try:
        _app()._probe_log({"kind": "box_sheets", "mode": args.mode,
                           "week_key": wk, "summary": summary})
    except Exception as e:  # never fail the build over the stamp
        log.warning("probe stamp failed: %s", e)
    total = sum((summary.get(s) or {}).get("games", 0) for s in sports)
    return 1 if (args.mode == "monday" and total == 0) else 0


if __name__ == "__main__":
    sys.exit(main())
