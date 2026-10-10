"""Probe: where + in what shape do the conferences publish football player
availability reports? (Oct 10 2026 — ESPN carries no college injury data, so
the college QB adjustment is blind.) Crawls each conference site one hop for
links mentioning availability/injury and prints each target's content type,
size and a text snippet. Read-only, runner-only (sandbox egress blocks these).
"""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin

import requests

H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                   "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"}
SEEDS = {
    "big12": ["https://big12sports.com/sports/2025/8/14/FBreporting.aspx",
              "https://big12sports.com/sports/2025/8/21/FBReportArchive.aspx"],
    "big10": ["https://bigten.org/news/2026/8/28/big-ten-announces-changes-to-football-availability-reporting-process.aspx",
              "https://bigten.org/sports/football"],
    "sec": ["https://www.secsports.com/news/2024/08/sec-to-provide-public-availability-reports-for-football-basketball-and-baseball",
            "https://www.secsports.com/availability-reports", "https://www.secsports.com/availability"],
}
HREF = re.compile(r'href="([^"]+)"', re.I)
KEY = re.compile(r"availab|injur", re.I)


def get(url):
    try:
        r = requests.get(url, headers=H, timeout=25, allow_redirects=True)
        return r
    except Exception as e:
        return e


def text_snip(html, n=1500):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t)
    i = max(0, t.lower().find("quarterback") - 200) if "quarterback" in t.lower() else 0
    return t[i:i + n]


def main():
    for conf, seeds in SEEDS.items():
        found = {}
        for s in seeds:
            r = get(s)
            if isinstance(r, Exception):
                print("AVAIL_SEED", json.dumps({"conf": conf, "url": s, "err": str(r)[:200]}))
                continue
            print("AVAIL_SEED", json.dumps({"conf": conf, "url": s, "final": r.url, "http": r.status_code,
                                            "ct": r.headers.get("content-type"), "len": len(r.text)}))
            if r.status_code != 200:
                continue
            print("AVAIL_SEEDSNIP", json.dumps({"conf": conf, "url": s, "snip": text_snip(r.text, 2500)}))
            for h in HREF.findall(r.text):
                if KEY.search(h):
                    found[urljoin(r.url, h)] = 1
            # also anchors whose TEXT mentions availability
            for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S | re.I):
                if KEY.search(re.sub("<[^>]+>", "", m.group(2))):
                    found[urljoin(r.url, m.group(1))] = 1
        links = list(found)[:12]
        print("AVAIL_LINKS", json.dumps({"conf": conf, "n": len(found), "links": links}))
        for u in links[:8]:
            r = get(u)
            if isinstance(r, Exception):
                continue
            ct = r.headers.get("content-type", "")
            sub = [urljoin(r.url, h) for h in HREF.findall(r.text) if KEY.search(h) or h.lower().endswith(".pdf")][:15] \
                if "html" in ct else []
            print("AVAIL_PAGE", json.dumps({"conf": conf, "url": u, "http": r.status_code, "ct": ct,
                                            "len": len(r.content), "sub": sub,
                                            "snip": text_snip(r.text) if "html" in ct else None, "pdf_head": (r.content[:8].decode("latin1") if "pdf" in ct else None)}))


if __name__ == "__main__":
    main()
