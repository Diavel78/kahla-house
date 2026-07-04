"""ProphetX reachability + API-shape recon (read-only, no auth, no writes).

ProphetX (prophetx.co) is a peer-to-peer US sports exchange (launched in
AZ, 2026). Question under test: can we read its odds programmatically —
via a documented public/partner API, or via the JSON endpoints the web
app itself calls (the Action Network pattern)?

Run from GitHub Actions (sandbox egress is proxy-blocked):
  python -m scripts.probe_prophetx
Prints status / content-type / body head per candidate URL, then greps
the homepage + JS bundles for API hostnames and endpoint paths.
"""
from __future__ import annotations

import re
import sys

import httpx

UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/126.0.0.0 Safari/537.36")}

CANDIDATES = [
    "https://prophetx.co",
    "https://www.prophetx.co",
    "https://docs.prophetx.co",
    "https://api.prophetx.co",
    "https://api.prophetx.co/partner/mm/get_tournaments",
    "https://api-ss.betprophet.co",
    "https://cash.api.prophetx.co",
]


def peek(client: httpx.Client, url: str) -> str | None:
    try:
        r = client.get(url, timeout=15, follow_redirects=True)
        ct = r.headers.get("content-type", "?")
        body = r.text[:400].replace("\n", " ")
        print(f"\n=== {url}\n  {r.status_code} · {ct}\n  {body}")
        return r.text if r.status_code == 200 else None
    except Exception as e:
        print(f"\n=== {url}\n  ERROR {type(e).__name__}: {str(e)[:200]}")
        return None


def docs_mine(client: httpx.Client) -> None:
    """docs.prophetx.co is a ReadMe portal — mine it for base URLs, auth,
    and the market/odds endpoint list (sitemap → reference pages)."""
    try:
        sm = client.get("https://docs.prophetx.co/sitemap.xml", timeout=15)
        urls = re.findall(r"<loc>([^<]+)</loc>", sm.text) if sm.status_code == 200 else []
    except Exception as e:
        print(f"sitemap failed: {e}")
        urls = []
    print(f"\nDOCS sitemap: {len(urls)} pages")
    for u in urls[:60]:
        print(f"  {u}")
    # Pull a few likely pages and grep for base URLs + auth hints.
    keys = [u for u in urls if re.search(r"auth|getting|start|odds|market|event|tournament|websocket|feed", u, re.I)][:8]
    for u in keys or urls[:5]:
        try:
            r = client.get(u, timeout=15)
            hosts = sorted(set(re.findall(r"https://[a-z0-9.-]+\.(?:prophetx\.co|betprophet\.co)[a-z0-9./_-]*", r.text)))[:15]
            auth = sorted(set(re.findall(r"(?i)(access[ _-]?key|secret[ _-]?key|api[ _-]?key|bearer|token|x-api-key)", r.text)))[:8]
            print(f"\n--- {u}\n  hosts: {hosts}\n  auth-ish: {auth}")
        except Exception as e:
            print(f"  {u} failed: {e}")


def main() -> int:
    client = httpx.Client(headers=UA)
    pages: dict[str, str] = {}
    for url in CANDIDATES:
        body = peek(client, url)
        if body:
            pages[url] = body
    docs_mine(client)

    # Mine the homepage (and its JS bundles) for API hosts + endpoint paths.
    home = pages.get("https://prophetx.co") or pages.get("https://www.prophetx.co")
    if home:
        hosts = sorted(set(re.findall(r"https://[a-z0-9.-]*(?:api|ss|cash|ws)[a-z0-9.-]*\.[a-z]{2,6}", home)))
        print(f"\nAPI-ish hosts in homepage HTML: {hosts}")
        js = re.findall(r'src="(https?://[^"]+\.js[^"]*)"', home) + \
             re.findall(r'src="(/[^"]+\.js[^"]*)"', home)
        print(f"JS bundles found: {len(js)}")
        base = "https://prophetx.co"
        for j in js[:6]:
            jurl = j if j.startswith("http") else base + j
            try:
                r = client.get(jurl, timeout=20)
                if r.status_code != 200:
                    continue
                hosts = sorted(set(re.findall(
                    r"https://[a-z0-9.-]+\.(?:prophetx\.co|betprophet\.co)[a-z0-9./_-]*",
                    r.text)))[:30]
                paths = sorted(set(re.findall(
                    r'"(/(?:partner|api|v[0-9]|mm|sports?|events?|markets?|odds)[a-z0-9./_-]{3,60})"',
                    r.text)))[:40]
                if hosts or paths:
                    print(f"\n--- {jurl[:100]}\n  hosts: {hosts}\n  paths: {paths}")
            except Exception as e:
                print(f"  js fetch failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
