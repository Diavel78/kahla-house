#!/usr/bin/env python3
"""THE WEEKLY ZONE REVIEW (Nov 15 2026, Rob: "Every Sunday at 11 pm, you will analyze the
last week, add it to the database for all sports, and make any zone changes needed").

Input: JSON files holding saved zone checkpoints (/api/zone-data dumps: {"rows": [...]},
each row {sport, ev, mt, start, final{home,away}, slope, cp{mark: {pin, poly, kalshi, pl, ph, pa}}}).
Output: a markdown report + the proposed zone list (the machine_flags `zones` value).

THE RULE (deterministic — the weekly session runs it, reads it, and applies it):
  * every candidate window × sport × market: Pinnacle's move inside it (cents, one scale),
    the correlation r of that move with the result at Pinnacle's line at the window's end
  * kind = chase when r > 0, fade when r < 0
  * at each threshold (1, 1.5, 2, 3¢): the record of betting the zone's side at Pinnacle's
    price at the window's end (vig in), ROI, and z = (wins − losses) / sqrt(bets)
  * a zone QUALIFIES when |r| ≥ 0.15 over ≥ 60 games (a zone ALREADY LIVE — same sport, market,
    window and kind — needs only |r| ≥ 0.10: Rob, Nov 16 2026, after the review dropped the NHL total
    morning zone at 19-10 +27% for r 0.14: "Why are we dropping a profitable zone????"), and at its threshold ≥ 20 bets,
    ROI ≥ +10% and z ≥ 1.5, and the next threshold down points the same way (ROI > 0)
  * min_c = the qualifying threshold with the best z; play_c = the next threshold up when its
    ROI is higher on ≥ 10 bets, else min_c + 1; strength = ROI at min_c
  * at most 2 zones per sport/market, never two overlapping windows (best z first)
Usage: zone_review.py OUT_PREFIX [--live zones_live.json] dump1.json [dump2.json ...]
"""
import json
import math
import sys

WINDOWS = [("o", 0, "o", 1), ("o", 0, "o", 3), ("o", 0, "o", 6), ("o", 1, "o", 6), ("o", 3, "o", 12),
           ("o", 6, "o", 24), ("s", -48, "s", -24), ("s", -24, "s", -12), ("s", -12, "s", -6),
           ("s", -6, "s", -3), ("s", -3, "s", -1), ("s", -1, "s", -0.25), ("s", -0.25, "s", 0),
           ("o", 0, "s", -6), ("o", 0, "s", -1), ("o", 0, "s", -0.25)]
THS = (1.0, 1.5, 2.0, 3.0)
LABEL = {("o", 0, "o", 1): "first hour after Pinnacle opens", ("o", 0, "o", 3): "first 3h after Pinnacle opens",
         ("o", 0, "o", 6): "first 6h after Pinnacle opens", ("o", 1, "o", 6): "1–6h after open",
         ("o", 3, "o", 12): "daytime move (3–12h after open)", ("o", 6, "o", 24): "6–24h after open",
         ("s", -48, "s", -24): "2 days out", ("s", -24, "s", -12): "the day before",
         ("s", -12, "s", -6): "game-day morning move", ("s", -6, "s", -3): "6–3h before the start",
         ("s", -3, "s", -1): "3–1h before the start", ("s", -1, "s", -0.25): "last hour before the start",
         ("s", -0.25, "s", 0): "last 15 minutes", ("o", 0, "s", -6): "open to 6h before",
         ("o", 0, "s", -1): "open to 1h before", ("o", 0, "s", -0.25): "the move since Pinnacle opened"}


def mark(a, h):
    return f"{a}{h:g}"


def grade(mt, d, line, fin):
    h, a = float(fin["home"]), float(fin["away"])
    x = (h + a) - line if mt == "total" else (h - a) if mt == "ml" else (h - a) + line
    x *= d
    return 0 if abs(x) < 1e-9 else (1 if x > 0 else -1)


def pay(px):
    px = float(px)
    return 5.0 * (px / 100.0 if px > 0 else 100.0 / -px)


def window_stats(rows, w):
    a, b = mark(w[0], w[1]), mark(w[2], w[3])
    sam = []
    for r in rows:
        A, B = (r.get("cp") or {}).get(a), (r.get("cp") or {}).get(b)
        fin = r.get("final") or {}
        if not A or not B or "pin" not in A or "pin" not in B or B.get("ph") is None or fin.get("home") is None:
            continue
        if r["mt"] != "ml" and B.get("pl") is None:
            continue
        mv = (B["pin"] - A["pin"]) * float(r.get("slope") or 1.0)
        g = grade(r["mt"], 1, B.get("pl"), fin)
        sam.append((mv, g, B))
    xs = [s[0] for s in sam if s[1] != 0 and abs(s[0]) > 0.01]
    ys = [s[1] for s in sam if s[1] != 0 and abs(s[0]) > 0.01]
    n = len(xs)
    r_ = None
    if n >= 10:
        mx, my = sum(xs) / n, sum(ys) / n
        vx, vy = sum((x - mx) ** 2 for x in xs), sum((y - my) ** 2 for y in ys)
        r_ = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(vx * vy) if vx and vy else 0.0
    return sam, r_, n


def bets(sam, th, kind):
    w = l = p = 0
    usd = 0.0
    for mv, g, B in sam:
        if abs(mv) < th:
            continue
        d = (1 if mv > 0 else -1) * (1 if kind == "chase" else -1)
        res = g * d
        px = B["ph"] if d > 0 else B["pa"]
        if res == 1:
            w += 1
            usd += pay(px)
        elif res == -1:
            l += 1
            usd -= 5.0
        else:
            p += 1
    n = w + l + p
    return {"w": w, "l": l, "p": p, "n": n, "usd": round(usd, 2),
            "roi": round(100 * usd / (5 * n), 1) if n else 0.0,
            "z": round((w - l) / math.sqrt(w + l), 2) if w + l else 0.0}


def overlap(w1, w2, rows):
    # windows on different clocks (open vs start) are compared on a typical game: open 24h out
    def span(w):
        f = lambda a, h: (h if a == "o" else 24 + h)
        return f(w[0], w[1]), f(w[2], w[3])
    a1, b1 = span(w1)
    a2, b2 = span(w2)
    return a1 < b2 and a2 < b1


def main():
    out = sys.argv[1]
    files = sys.argv[2:]
    live = set()
    if files[:1] == ["--live"]:
        for zz in json.load(open(files[1])) or []:
            live.add((zz[0], zz[1], zz[2][0], float(zz[2][1]), zz[3][0], float(zz[3][1]), zz[4]))
        files = files[2:]
    rows = []
    seen = set()
    for f in files:
        for r in (json.load(open(f)).get("rows") or []):
            k = (r.get("sport"), r.get("ev"), r.get("start"), r.get("mt"))
            if k in seen or not r.get("cp"):
                continue
            seen.add(k)
            rows.append(r)
    rep = [f"# Zone review — {len(rows)} game-markets\n"]
    zones = []
    for sp in sorted({r["sport"] for r in rows}):
        for mt in ("ml", "spread", "total"):
            sub = [r for r in rows if r["sport"] == sp and r["mt"] == mt]
            if not sub:
                continue
            rep.append(f"\n## {sp} {mt} — {len(sub)} games\n\n| window | r | n | kind | " +
                       " | ".join(f"{t:g}¢" for t in THS) + " |\n|---|---|---|---|" + "---|" * len(THS))
            cands = []
            for w in WINDOWS:
                sam, r_, n = window_stats(sub, w)
                if r_ is None:
                    continue
                kind = "chase" if r_ > 0 else "fade"
                bs = {t: bets(sam, t, kind) for t in THS}
                rep.append(f"| {LABEL[w]} | {r_:+.2f} | {n} | {kind} | " +
                           " | ".join(f"{bs[t]['w']}-{bs[t]['l']} {bs[t]['roi']:+.0f}%" for t in THS) + " |")
                is_live = (sp, mt, w[0], float(w[1]), w[2], float(w[3]), kind) in live
                if abs(r_) < (0.10 if is_live else 0.15) or n < 60:
                    continue
                best = None
                for i, t in enumerate(THS):
                    b = bs[t]
                    lower_ok = i == 0 or bs[THS[i - 1]]["roi"] > 0
                    if b["n"] >= 20 and b["roi"] >= 10 and b["z"] >= 1.5 and lower_ok:
                        if best is None or b["z"] > bs[best]["z"]:
                            best = t
                if best is None:
                    continue
                i = THS.index(best)
                play = best + 1.0
                if i + 1 < len(THS) and bs[THS[i + 1]]["n"] >= 10 and bs[THS[i + 1]]["roi"] > bs[best]["roi"]:
                    play = THS[i + 1]
                cands.append((bs[best]["z"], w, kind, best, play, bs[best]["roi"], r_, bs[best]))
            cands.sort(key=lambda c: -c[0])
            chosen = []
            for c in cands:
                if len(chosen) >= 2 or any(overlap(c[1], d[1], sub) for d in chosen):
                    continue
                chosen.append(c)
            for z, w, kind, mc, pc, roi, r_, b in chosen:
                zones.append([sp, mt, [w[0], w[1]], [w[2], w[3]], kind, mc, pc, roi, LABEL[w]])
                rep.append(f"\n**ZONE: {LABEL[w]} — {kind}**, trigger {mc:g}¢ (PLAY {pc:g}¢): "
                           f"{b['w']}-{b['l']}-{b['p']}, {b['roi']:+.1f}% ROI, z {z:+.2f}, r {r_:+.2f}")
            if not chosen:
                rep.append("\n_no zone qualifies — no 🔥 on this market_")
    rep.append("\n## Proposed zones\n\n```json\n" + json.dumps(zones, indent=1) + "\n```\n")
    open(out + ".md", "w").write("\n".join(rep))
    json.dump(zones, open(out + ".zones.json", "w"))
    print("\n".join(rep))


if __name__ == "__main__":
    main()
