"""Fight IQ — the UFC model engine (Phase 2, July 2026).

Method-weighted fighter Elo + adjustment layers + a fitted Elo->prob
scale, mirroring the _lib/power_ratings.py architecture (core rating
solved from results; per-bout layers; calibration fit from history).
Pure Python, no ML stack — every constant inspectable.

Two outputs per bout:
  • WINNER: P(A beats B). A/B are NEUTRALLY ordered (min fighter id
    first) because ufc_fights lists the WINNER as fighter1 — predicting
    on table order would leak the label.
  • DURATION: P(finish) + the finish-TIMING curve (share of finishes
    inside 2:30/7:30/12:30 — walk-forward counters), which together
    price the whole derivative family: the ROUNDS ladder (over 0.5 /
    1.5 / 2.5 — books post whichever is nearest 50/50 per fight) AND
    the DISTANCE prop (P(distance) = 1 − P(finish); ~50.9% base rate,
    the market's natural coin flip; Kalshi series KXUFCDISTANCE).

LEAKAGE NOTES (deliberate, documented):
  • Elo, age, layoff, chin, finish-rates: as-of-date from the fights
    table — leakage-free.
  • Reach / stance / TD career rates: CURRENT career values from the
    fighter page (UFCStats has no historical snapshots) — mild lookahead
    for backtests, same caveat as the NRFI backtest. The backtest
    reports the clean core and the full stack separately.
"""
from __future__ import annotations

import math
from datetime import date, datetime

# ── Elo core ──────────────────────────────────────────────────────────
ELO_START   = 1500.0
K_BASE      = 32.0
K_FINISH    = 1.25     # KO/SUB moves ratings more than a decision
K_TITLE     = 1.20

# ── Layers (Elo-point adjustments) ────────────────────────────────────
AGE_KNEE        = 32.0    # decline starts here...
AGE_PTS_PER_YR  = 12.0    # ...at this many Elo points per year (train-fit 2015-21)
CHIN_PTS        = 25.0    # per KO/TKO loss in the fighter's last 2 bouts
LAYOFF_1_PTS    = 30.0    # >= 12 months out (train-fit)
LAYOFF_2_PTS    = 60.0    # >= 24 months out (replaces, not additive; train-fit)
REACH_PTS_PER_IN = 1.5
REACH_CAP       = 9.0
STANCE_PTS      = 10.0    # southpaw vs orthodox
STYLE_PTS       = 12.0    # per net expected takedown (TD_avg x opp 1-TD_def)
STYLE_CAP       = 36.0

WARMUP_FIGHTS   = 3       # both fighters need >= this many prior UFC bouts

# ── Duration models (rounds ladder + distance prop) ──────────────────
# A rounds line of X.5 settles at the midpoint of round X+1, so the
# elapsed-minute cuts are 2:30 / 7:30 / 12:30. Distance (3-rounders) =
# the full 15:00. Graded on non-title bouts; ~5% of those are 5-round
# main events (visible when they end R4/R5) — small, documented noise
# until a bout_order/scheduled_rounds column exists.
ROUND_CUTS   = {"0.5": 2.5, "1.5": 7.5, "2.5": 12.5}
DISTANCE_MIN = 14.99
FINISH_CLAMP = (0.05, 0.95)

# Fitted by fit_scale()/fit_rounds() on the training window; these are
# just fallbacks when no fit has run.
DEFAULT_SCALE   = 300.0
DEFAULT_BETA    = 0.5


def _pdate(s):
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _age_on(dob, when):
    if not dob or not when:
        return None
    return (when - dob).days / 365.25


def _is_finish(method: str) -> bool:
    m = (method or "").upper()
    return m.startswith("KO") or m.startswith("SUB")


def _is_ko(method: str) -> bool:
    return (method or "").upper().startswith("KO")


def _is_title(weight_class: str) -> bool:
    return "title" in (weight_class or "").lower()


def _duration_min(rnd, fight_time) -> float | None:
    """Fight length in minutes from (round, mm:ss)."""
    if not rnd:
        return None
    try:
        mm, ss = str(fight_time or "5:00").split(":")
        return (int(rnd) - 1) * 5.0 + int(mm) + int(ss) / 60.0
    except (ValueError, TypeError):
        return None


def _wc_key(weight_class: str) -> str:
    wc = (weight_class or "").lower()
    for k in ("strawweight", "flyweight", "bantamweight", "featherweight",
              "lightweight", "welterweight", "middleweight",
              "light heavyweight", "heavyweight"):
        if k in wc:
            # check LHW before HW would fail with this order — handle:
            if k == "heavyweight" and "light heavyweight" in wc:
                return "light heavyweight"
            return k
    return "other"


class _F:
    """Per-fighter walk-forward state."""
    __slots__ = ("elo", "n", "last_date", "recent_ko_losses", "bouts",
                 "finished_bouts")

    def __init__(self):
        self.elo = ELO_START
        self.n = 0
        self.last_date = None
        self.recent_ko_losses = []   # bools, most recent last, len<=2
        self.bouts = 0               # bouts with known duration
        self.finished_bouts = 0      # of those, ended inside the distance


def _adj(f: _F, dob, when):
    """Leakage-free per-fighter layer sum (Elo points)."""
    pts = 0.0
    a = _age_on(dob, when)
    if a is not None and a > AGE_KNEE:
        pts -= AGE_PTS_PER_YR * (a - AGE_KNEE)
    pts -= CHIN_PTS * sum(1 for k in f.recent_ko_losses if k)
    if f.last_date is not None and when is not None:
        gap = (when - f.last_date).days
        if gap >= 730:
            pts -= LAYOFF_2_PTS
        elif gap >= 365:
            pts -= LAYOFF_1_PTS
    return pts


def _static_pair_pts(fa: dict, fb: dict) -> float:
    """Static-stat layers for A vs B (positive favors A). CURRENT career
    values — the documented mild-lookahead set."""
    pts = 0.0
    ra, rb = fa.get("reach_in"), fb.get("reach_in")
    if ra is not None and rb is not None:
        d = max(-REACH_CAP, min(REACH_CAP,
                                (float(ra) - float(rb)) * REACH_PTS_PER_IN))
        pts += d
    sa = (fa.get("stance") or "").lower()
    sb = (fb.get("stance") or "").lower()
    if sa.startswith("south") and sb.startswith("orth"):
        pts += STANCE_PTS
    elif sb.startswith("south") and sa.startswith("orth"):
        pts -= STANCE_PTS

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    ta, da = _num(fa.get("td_avg")), _num(fa.get("td_def"))
    tb, db = _num(fb.get("td_avg")), _num(fb.get("td_def"))
    if None not in (ta, da, tb, db):
        edge = ta * (1.0 - db) - tb * (1.0 - da)
        pts += max(-STYLE_CAP, min(STYLE_CAP, STYLE_PTS * edge))
    return pts


def prob_from_diff(d: float, scale: float) -> float:
    return 1.0 / (1.0 + 10.0 ** (-d / scale))


def replay(fights: list[dict], fighters: dict[str, dict],
           collect_since: str | None = "2015-01-01") -> tuple[dict, list[dict]]:
    """Chronological walk-forward over the fights. Returns
    (final_state, records). Each record (for graded bouts on/after
    collect_since with both fighters warmed up) carries everything a
    calibration fit or backtest needs — crucially D values, NOT
    probabilities, so scale fitting is free afterwards.

    Neutral orientation: A = lexicographically smaller fighter id.
    """
    state: dict[str, _F] = {}
    wc_fin = {}                    # wc -> [finished, total] running counters
    fin_cut = {k: 0 for k in ROUND_CUTS}   # finishes inside each cut (non-title)
    fin_tot = 0                            # all non-title finishes
    recs: list[dict] = []
    since = _pdate(collect_since) if collect_since else None

    for row in fights:
        when = _pdate(row.get("event_date"))
        f1, f2 = row.get("fighter1_id"), row.get("fighter2_id")
        if not f1 or not f2 or when is None:
            continue
        s1 = state.setdefault(f1, _F())
        s2 = state.setdefault(f2, _F())
        result = row.get("result") or "win"
        method = row.get("method") or ""
        title = _is_title(row.get("weight_class"))
        dur = _duration_min(row.get("round"), row.get("fight_time"))

        # ── record a prediction BEFORE updating state ──
        if ((since is None or when >= since)
                and s1.n >= WARMUP_FIGHTS and s2.n >= WARMUP_FIGHTS
                and result == "win"):
            a_id, b_id = (f1, f2) if f1 < f2 else (f2, f1)
            sa, sb = state[a_id], state[b_id]
            fa = fighters.get(a_id) or {}
            fb = fighters.get(b_id) or {}
            d_core = ((sa.elo + _adj(sa, _pdate(fa.get("dob")), when))
                      - (sb.elo + _adj(sb, _pdate(fb.get("dob")), when)))
            d_full = d_core + _static_pair_pts(fa, fb)
            wc = _wc_key(row.get("weight_class"))
            wf = wc_fin.get(wc) or [0, 0]
            wc_rate = (wf[0] / wf[1]) if wf[1] >= 30 else None
            overall = None
            tot_f = sum(v[0] for v in wc_fin.values())
            tot_n = sum(v[1] for v in wc_fin.values())
            if tot_n >= 200:
                overall = tot_f / tot_n
            pf_a = (sa.finished_bouts / sa.bouts) if sa.bouts >= 3 else None
            pf_b = (sb.finished_bouts / sb.bouts) if sb.bouts >= 3 else None
            conds = {k: (fin_cut[k] / fin_tot if fin_tot >= 100 else None)
                     for k in ROUND_CUTS}
            gradable = dur is not None and not title
            recs.append({
                "date": when.isoformat(),
                "d_core": d_core, "d_full": d_full,
                "y": 1 if row.get("winner_id") == a_id else 0,
                "title": title,
                "wc_rate": wc_rate, "overall": overall,
                "pf_a": pf_a, "pf_b": pf_b, "conds": conds,
                "over_y": {k: (1 if dur > cut else 0)
                           for k, cut in ROUND_CUTS.items()} if gradable else None,
                # distance graded only when the bout provably ran on a
                # 3-round clock or ended early (R4/R5 bouts are 5-rounders
                # with a different distance definition)
                "dist_y": ((1 if dur >= DISTANCE_MIN else 0)
                           if (gradable and (row.get("round") or 0) <= 3)
                           else None),
            })

        # ── update state ──
        fin = _is_finish(method)
        if result == "win" and row.get("winner_id") in (f1, f2):
            w, l = ((s1, s2) if row["winner_id"] == f1 else (s2, s1))
            ew = 1.0 / (1.0 + 10.0 ** (-(w.elo - l.elo) / 400.0))
            k = K_BASE * (K_FINISH if fin else 1.0) * (K_TITLE if title else 1.0)
            w.elo += k * (1.0 - ew)
            l.elo -= k * (1.0 - ew)
            l.recent_ko_losses = (l.recent_ko_losses + [_is_ko(method)])[-2:]
            w.recent_ko_losses = (w.recent_ko_losses + [False])[-2:]
        elif result == "draw":
            e1 = 1.0 / (1.0 + 10.0 ** (-(s1.elo - s2.elo) / 400.0))
            s1.elo += K_BASE * (0.5 - e1)
            s2.elo -= K_BASE * (0.5 - e1)
        # NC: no rating change, but activity still counts below.

        for s in (s1, s2):
            s.n += 1
            s.last_date = when
            if dur is not None and result == "win":
                s.bouts += 1
                if fin:
                    s.finished_bouts += 1
        if dur is not None and result == "win":
            wf = wc_fin.setdefault(_wc_key(row.get("weight_class")), [0, 0])
            wf[1] += 1
            if fin:
                wf[0] += 1
                if not title:
                    fin_tot += 1
                    for k, cut in ROUND_CUTS.items():
                        if dur <= cut:
                            fin_cut[k] += 1
    aggs = {
        "wc_rates": {k: round(v[0] / v[1], 4) for k, v in wc_fin.items()
                     if v[1] >= 30},
        "overall_finish": (round(sum(v[0] for v in wc_fin.values())
                                 / max(1, sum(v[1] for v in wc_fin.values())), 4)),
        "finish_conds": {k: (round(fin_cut[k] / fin_tot, 4) if fin_tot else None)
                         for k in ROUND_CUTS},
    }
    return state, recs, aggs


# ── calibration + evaluation ─────────────────────────────────────────

def fit_scale(recs: list[dict], key: str = "d_full",
              grid=(250, 300, 350, 400, 450, 500, 600)) -> float:
    """Scale minimizing Brier on the given records."""
    best, best_b = DEFAULT_SCALE, 1e9
    for s in grid:
        b = sum((prob_from_diff(r[key], s) - r["y"]) ** 2 for r in recs) / len(recs)
        if b < best_b:
            best, best_b = s, b
    return float(best)


def p_finish(r: dict, beta: float) -> float | None:
    """P(fight ends inside the distance) from walk-forward priors."""
    base = r["wc_rate"] if r["wc_rate"] is not None else r["overall"]
    if base is None or r["overall"] is None:
        return None
    pf = [x for x in (r["pf_a"], r["pf_b"]) if x is not None]
    adj = (sum(pf) / len(pf) - r["overall"]) if pf else 0.0
    p = base + beta * adj
    return max(FINISH_CLAMP[0], min(FINISH_CLAMP[1], p))


def p_distance(r: dict, beta: float) -> float | None:
    """P(the bout goes the distance) = 1 − P(finish). The purest
    expression of the duration model (no timing term)."""
    pf = p_finish(r, beta)
    return None if pf is None else 1.0 - pf


def p_over(r: dict, beta: float, line: str = "2.5") -> float | None:
    """P(over `line` rounds): 1 − P(finish) x P(finish inside the cut)."""
    pf = p_finish(r, beta)
    cond = (r.get("conds") or {}).get(line)
    if pf is None or cond is None:
        return None
    return 1.0 - pf * cond


def fit_beta(recs: list[dict], grid=(0.0, 0.25, 0.5, 0.75, 1.0)) -> float:
    """Beta fit on the DISTANCE prop — the duration model's purest,
    largest-sample expression (every rounds line shares this beta)."""
    rr = [r for r in recs if r.get("dist_y") is not None]
    best, best_b = DEFAULT_BETA, 1e9
    for beta in grid:
        pts = [(p_distance(r, beta), r["dist_y"]) for r in rr]
        pts = [(p, y) for p, y in pts if p is not None]
        if not pts:
            continue
        b = sum((p - y) ** 2 for p, y in pts) / len(pts)
        if b < best_b:
            best, best_b = beta, b
    return float(best)


def evaluate(recs: list[dict], scale: float, beta: float,
             key: str = "d_full") -> dict:
    """Winner accuracy/Brier/calibration + rounds Brier vs base rate."""
    out: dict = {"n": len(recs)}
    if recs:
        preds = [(prob_from_diff(r[key], scale), r["y"]) for r in recs]
        out["winner_acc"] = sum(1 for p, y in preds
                                if (p >= 0.5) == (y == 1)) / len(preds)
        out["winner_brier"] = sum((p - y) ** 2 for p, y in preds) / len(preds)
        buckets = {}
        for p, y in preds:
            fav = p if p >= 0.5 else 1 - p
            won = y if p >= 0.5 else 1 - y
            k = ("50-55" if fav < 0.55 else "55-60" if fav < 0.60
                 else "60-70" if fav < 0.70 else "70+")
            b = buckets.setdefault(k, [0, 0])
            b[0] += won
            b[1] += 1
        out["calibration"] = {k: {"win": round(v[0] / v[1], 3), "n": v[1]}
                              for k, v in sorted(buckets.items())}
    # Distance prop — the headline duration market.
    dd = [(p_distance(r, beta), p_distance(r, 0.0), r["dist_y"])
          for r in recs if r.get("dist_y") is not None]
    dd = [(p, p0, y) for p, p0, y in dd if p is not None and p0 is not None]
    if dd:
        out["dist_n"] = len(dd)
        out["dist_rate"] = sum(y for _, _, y in dd) / len(dd)
        out["dist_brier"] = sum((p - y) ** 2 for p, _, y in dd) / len(dd)
        out["dist_brier_base"] = sum((p0 - y) ** 2 for _, p0, y in dd) / len(dd)
    # Rounds ladder — every line, each vs its beta=0 base-rate baseline.
    out["rounds"] = {}
    for line in ROUND_CUTS:
        pts = [(p_over(r, beta, line), p_over(r, 0.0, line),
                (r["over_y"] or {}).get(line))
               for r in recs if r.get("over_y") is not None]
        pts = [(p, p0, y) for p, p0, y in pts
               if p is not None and p0 is not None and y is not None]
        if not pts:
            continue
        out["rounds"][line] = {
            "n": len(pts),
            "over_rate": round(sum(y for _, _, y in pts) / len(pts), 3),
            "brier": round(sum((p - y) ** 2 for p, _, y in pts) / len(pts), 4),
            "brier_base": round(sum((p0 - y) ** 2 for _, p0, y in pts)
                                / len(pts), 4),
        }
    return out


def snapshot(state: dict, fighters: dict[str, dict], scale: float,
             beta: float, aggs: dict | None = None,
             min_fights: int = 1) -> dict:
    """Serializable model snapshot for the ufc_model table. `aggs`
    (wc finish rates + finish-timing conds + overall rate from replay)
    ships in the snapshot so Phase 3 can price the rounds ladder and
    the distance prop live without re-running the replay."""
    ratings = {}
    for fid, f in state.items():
        if f.n < min_fights:
            continue
        ratings[fid] = {
            "elo": round(f.elo, 1), "n": f.n,
            "last": f.last_date.isoformat() if f.last_date else None,
            "ko2": sum(1 for k in f.recent_ko_losses if k),
            "fin_rate": (round(f.finished_bouts / f.bouts, 3)
                         if f.bouts >= 3 else None),
            "name": (fighters.get(fid) or {}).get("name"),
        }
    return {"scale": scale, "beta": beta, "aggs": aggs or {},
            "ratings": ratings}


def load_from_db(sb, since: str = "2005-01-01"):
    """Paginated load of (fights, fighters) — Supabase caps un-limited
    selects at 1,000 rows (the Phase-1 landmine), so page explicitly."""
    fights, page = [], 0
    while True:
        rows = (sb.table("ufc_fights")
                .select("id,event_date,fighter1_id,fighter2_id,winner_id,"
                        "result,weight_class,method,round,fight_time")
                .gte("event_date", since)
                .order("event_date").order("id")
                .range(page * 1000, page * 1000 + 999).execute().data) or []
        fights.extend(rows)
        if len(rows) < 1000:
            break
        page += 1
    fighters, page = {}, 0
    while True:
        rows = (sb.table("ufc_fighters")
                .select("id,name,dob,reach_in,stance,td_avg,td_def")
                .not_.is_("detail_scraped_at", "null")
                .range(page * 1000, page * 1000 + 999).execute().data) or []
        for r in rows:
            fighters[r["id"]] = r
        if len(rows) < 1000:
            break
        page += 1
    return fights, fighters
