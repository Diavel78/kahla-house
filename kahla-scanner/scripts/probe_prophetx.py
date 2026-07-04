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


def docs_pages(client: httpx.Client) -> None:
    """No sitemap — mine the docs ROOT for page hrefs, fetch those, grep
    for base URLs / endpoint paths / auth hints (ReadMe SSRs page bodies)."""
    try:
        root = client.get("https://docs.prophetx.co", timeout=15).text
    except Exception as e:
        print(f"docs root failed: {e}")
        return
    slugs = sorted(set(re.findall(r'href="(/(?:docs|reference|page)/[a-z0-9-]+)"', root)))
    print(f"\nDOCS page hrefs ({len(slugs)}):")
    for sl in slugs[:50]:
        print(f"  {sl}")
    for sl in slugs[:12]:
        try:
            r = client.get("https://docs.prophetx.co" + sl, timeout=15)
            hosts = sorted(set(re.findall(r"https://[a-z0-9.-]+\.(?:prophetx\.co|betprophet\.co)[a-z0-9./_{}-]*", r.text)))[:12]
            paths = sorted(set(re.findall(r'["`](/(?:partner|v[0-9]|mm|api)[a-zA-Z0-9./_{}-]{3,70})["`]', r.text)))[:25]
            auth = sorted(set(re.findall(r"(?i)(access[ _-]?key|secret[ _-]?key|x-api-key|bearer|hmac|signature)", r.text)))[:8]
            if hosts or paths or auth:
                print(f"\n--- {sl}\n  hosts: {hosts}\n  paths: {paths}\n  auth: {auth}")
        except Exception as e:
            print(f"  {sl} failed: {e}")


def gateway_probe(client: httpx.Client) -> None:
    """401/403 on a path = path EXISTS and wants auth; 404 = wrong path;
    405 on GET = exists, POST-only. Maps the PARTICIPANT (regular-account
    Trading API) surface alongside partner/affiliate, on both hosts."""
    hosts = ["https://cash.api.prophetx.co",
             "https://api-ss-sandbox.prophetexchange.com"]
    paths = [
        "/participant/auth/login",
        "/participant/mm/get_tournaments",
        "/participant/mm/get_sport_events",
        "/participant/mm/get_markets",
        "/participant/get_tournaments",
        "/partner/auth/login",
        "/partner/mm/get_tournaments",
        "/partner/affiliate/get_tournaments",
    ]
    for host in hosts:
        print(f"\nGATEWAY probe ({host}) — 401/403=exists, 405=POST-only:")
        for pth in paths:
            try:
                r = client.get(host + pth, timeout=12)
                print(f"  GET  {pth} → {r.status_code} · {r.text[:110]!r}")
            except Exception as e:
                print(f"  GET  {pth} → ERROR {str(e)[:80]}")
        # POST the login with empty body — the error shape reveals the
        # expected fields (access_key/secret vs username/password).
        for pth in ("/participant/auth/login", "/partner/auth/login"):
            try:
                r = client.post(host + pth, json={}, timeout=12)
                print(f"  POST {pth} → {r.status_code} · {r.text[:160]!r}")
            except Exception as e:
                print(f"  POST {pth} → ERROR {str(e)[:80]}")


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=None,
                    help="fetch ONE url and print it whole (.md pages)")
    args = ap.parse_args()
    client = httpx.Client(headers=UA)
    if args.url:
        r = client.get(args.url, timeout=20, follow_redirects=True)
        print(f"##### {args.url} → {r.status_code} ({len(r.text)} bytes)")
        print(r.text[:60000])
        return 0
    pages: dict[str, str] = {}
    for url in CANDIDATES:
        body = peek(client, url)
        if body:
            pages[url] = body
    # v5: pull the key onboarding pages as raw markdown (ReadMe serves
    # every doc page at <slug>.md) and print them whole — the goal is the
    # exact access-request form URL / contact the user should use.
    for u, cap in (("https://docs.prophetx.co/docs/requesting-api-access.md", 6000),
                   ("https://docs.prophetx.co/docs/getting-started.md", 5000),
                   ("https://docs.prophetx.co/docs/market-data-integration.md", 5000)):
        try:
            r = client.get(u, timeout=15)
            print(f"\n##### {u} → {r.status_code} ({len(r.text)} bytes)")
            print(r.text[:cap])
        except Exception as e:
            print(f"  {u} failed: {e}")
    gateway_probe(client)

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
