#!/usr/bin/env python3
# <xbar.title>Kahla House Pick Bot</xbar.title>
# <xbar.desc>Live projected units + today's settled units from the Pick Bot,
# in the macOS menu bar. SwiftBar and xbar compatible.</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>
#
# Menu bar:  ⚡+2.3u ✓−1.1u   (live projected book | today's settled book)
# Dropdown:  each live bet with score + win% + its projected units, today's
#            record, and an "Open Pick Bot" link.
#
# Config lives in ~/.config/kahla/widget.env (NOT in this file, so the repo
# never carries the secret):
#   KAHLA_TICKER_KEY=<WIDGET_SECRET or FILLS_CRON_SECRET from Vercel>
#   KAHLA_BASE_URL=https://thekahlahouse.com        (optional override)
# See widget/README.md for one-time install steps.

import json
import os
import urllib.parse
import urllib.request

CONFIG_PATH = os.path.expanduser("~/.config/kahla/widget.env")


def _load_config():
    cfg = {}
    try:
        with open(CONFIG_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    for k in ("KAHLA_TICKER_KEY", "KAHLA_BASE_URL"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


def _fmt_u(v):
    return f"{'+' if v >= 0 else '−'}{abs(v):.2f}u"


def _amer(p):
    if p is None:
        return "?"
    p = int(p)
    return f"+{p}" if p > 0 else str(p)


def main():
    cfg = _load_config()
    key = cfg.get("KAHLA_TICKER_KEY")
    base = (cfg.get("KAHLA_BASE_URL") or "https://thekahlahouse.com").rstrip("/")
    if not key:
        print("PB ⚙︎ setup")
        print("---")
        print(f"Missing {CONFIG_PATH} | color=red")
        print("Add: KAHLA_TICKER_KEY=<your secret>")
        return

    url = f"{base}/api/handicapper/ticker?key={urllib.parse.quote(key)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print("PB ⚠︎")
        print("---")
        print(f"Fetch failed: {e} | color=red")
        print(f"Refresh now | refresh=true")
        return
    if not data.get("ok"):
        print("PB ⚠︎")
        print("---")
        print(f"API error: {data.get('error')} | color=red")
        return

    live = data.get("live") or {}
    today = data.get("today") or {}
    proj = float(live.get("proj_units") or 0)
    n_live = int(live.get("n") or 0)
    settled = float(today.get("pnl_units") or 0)
    graded = (today.get("won") or 0) + (today.get("lost") or 0) + (today.get("push") or 0)

    # ── Menu bar line ────────────────────────────────────────────────
    parts = []
    if n_live:
        parts.append(f"⚡{_fmt_u(proj)}")
    if graded:
        parts.append(f"✓{_fmt_u(settled)}")
    if not parts:
        parts.append(f"PB {today.get('pending') or 0}◌")   # nothing live/graded yet
    print(" ".join(parts))

    # ── Dropdown ─────────────────────────────────────────────────────
    print("---")
    if n_live:
        priced = live.get("priced") or 0
        note = "" if priced == n_live else f" ({n_live - priced} unpriced)"
        print(f"Live — {n_live} bet{'s' if n_live != 1 else ''}, "
              f"projected {_fmt_u(proj)}{note} | "
              f"color={'green' if proj >= 0 else 'red'}")
        for r in (live.get("rows") or []):
            sc = r.get("score") or {}
            score = ""
            if sc.get("away_score") is not None:
                score = f"  {sc['away_score']}-{sc['home_score']}"
                if sc.get("display_status"):
                    score += f" {sc['display_status']}"
            wp = r.get("win_prob")
            wp_s = f" · {round(wp * 100)}%" if wp is not None else ""
            mt = {"moneyline": "ML", "spread": "SPR", "total": "TOT",
                  "nrfi": "1ST"}.get(r.get("market_type"), r.get("market_type"))
            side = (r.get("side") or "").upper()
            if r.get("market_type") == "nrfi":
                side = "NRFI" if side == "NO" else "YRFI"
            elif side in ("HOME", "AWAY") and " @ " in (r.get("event") or ""):
                a, h = r["event"].split(" @ ", 1)
                side = (h if side == "HOME" else a).split()[-1]
            print(f"{r.get('event')} — {mt} {side} {_amer(r.get('entry_price'))} "
                  f"· {r.get('units'):g}u{score}{wp_s} | size=12")
    else:
        print("No live bets right now")
    print("---")
    rec = f"{today.get('won', 0)}-{today.get('lost', 0)}"
    if today.get("push"):
        rec += f"-{today['push']}"
    print(f"Today settled: {rec} · {_fmt_u(settled)} | "
          f"color={'green' if settled >= 0 else 'red'}")
    print(f"Pending today: {today.get('pending') or 0}")
    print("---")
    print(f"Open Pick Bot | href={base}/handicapper")
    print("Refresh | refresh=true")


if __name__ == "__main__":
    main()
