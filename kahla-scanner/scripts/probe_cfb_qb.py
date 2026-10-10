"""Probe: where does ESPN say a COLLEGE quarterback is out? (Oct 10 2026)

compute_football_qb's college path reads the roster page's status/injury
flags, but nobody ever checked that ESPN fills them for college. This prints,
for each team id given: the roster QBs with status + injuries, the core-API
team injuries, the league injuries feed, the core depth chart, and the next
game's summary `injuries` block. Read-only; the sandbox can't reach ESPN.

  python -m scripts.probe_cfb_qb --teams 66
"""
from __future__ import annotations

import argparse
import json

import requests

S = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
C = "https://sports.core.api.espn.com/v2/sports/football/leagues/college-football"
H = {"User-Agent": "Mozilla/5.0"}


def get(url, params=None):
    try:
        r = requests.get(url, params=params, headers=H, timeout=25)
        return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else None)
    except Exception as e:
        return f"ERR {e}", None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teams", default="66")
    ap.add_argument("--season", default="2026")
    a = ap.parse_args()
    for tid in [t.strip() for t in a.teams.split(",") if t.strip()]:
        out = {"team": tid}
        st, d = get(f"{S}/teams/{tid}/roster", {"limit": 200})
        qbs, inj_any, n = [], 0, 0
        for g in (d or {}).get("athletes") or []:
            for x in g.get("items") or []:
                n += 1
                if x.get("injuries"):
                    inj_any += 1
                if ((x.get("position") or {}).get("abbreviation")) == "QB":
                    qbs.append({"id": x.get("id"), "name": x.get("displayName"),
                                "status": x.get("status"), "injuries": x.get("injuries")})
        out["roster"] = {"http": st, "n": n, "with_injuries": inj_any, "qbs": qbs}
        st, d = get(f"{C}/teams/{tid}/injuries", {"limit": 100})
        items = (d or {}).get("items") or []
        det = []
        for it in items[:15]:
            s2, d2 = get(it.get("$ref", ""))
            det.append({k: (d2 or {}).get(k) for k in ("status", "type", "shortComment", "longComment", "date")}
                       | {"athlete": ((d2 or {}).get("athlete") or {}).get("$ref")})
        out["core_injuries"] = {"http": st, "count": (d or {}).get("count"), "items": det}
        st, d = get(f"{C}/seasons/{a.season}/teams/{tid}/depthcharts")
        out["core_depth"] = {"http": st, "count": (d or {}).get("count"),
                             "positions": [list(((i.get("positions") or {}).keys()))[:30]
                                           for i in ((d or {}).get("items") or [])][:3]}
        st, d = get(f"{S}/teams/{tid}/schedule")
        nxt = None
        for ev in (d or {}).get("events") or []:
            stt = (((ev.get("competitions") or [{}])[0].get("status") or {}).get("type") or {}).get("state")
            if stt == "pre":
                nxt = ev.get("id")
                out["next_game"] = ev.get("name")
                break
        if nxt:
            st, d = get(f"{S}/summary", {"event": nxt})
            out["summary_injuries"] = {"http": st, "keys": list((d or {}).keys()),
                                       "injuries": (d or {}).get("injuries")}
        print("CFB_QB_PROBE", json.dumps(out, default=str)[:20000])
    st, d = get(f"{S}/injuries")
    out = {"http": st, "teams": len((d or {}).get("injuries") or [])}
    print("CFB_LEAGUE_INJURIES", json.dumps(out))


if __name__ == "__main__":
    main()
