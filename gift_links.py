"""Gift link reader — turn a product URL into {title, price, image, store}.

Used by the Gifts page so a kid can paste a link and the item fills itself in.
Stores publish this for their own search listings, so we read it in order:

  1. JSON-LD  (<script type="application/ld+json">, schema.org Product/Offer)
     — Walmart, Target, Best Buy, Lego, Nike, most Shopify stores
  2. Open Graph / product meta tags (og:title, og:image, product:price:amount)
  3. Microdata (itemprop="price")
  4. Store-specific fallbacks (Amazon's price block)

Anything we can't read comes back None and the person types it in. A
blocked store (Amazon often answers bots with a captcha page) returns
`blocked: True` so the page can say so instead of looking broken.

SSRF guard: only http(s), and the host must resolve to a public address —
this runs on our server, so a link must never reach a private network.
"""
import html as _html
import ipaddress
import json
import re
import socket
from urllib.parse import urlparse, urljoin

import requests

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_MAX_BYTES = 3_000_000
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)


def extract_url(text):
    """A pasted share often carries words around the link ("Check this out!
    https://a.co/d/xyz"). Pull the first URL out."""
    if not text:
        return None
    m = _URL_RE.search(text.strip())
    if not m:
        return None
    return m.group(0).rstrip(").,;!")


def _host_ok(host):
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def store_name(url):
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    host = re.sub(r"^(www|m|smile|shop)\.", "", host)
    known = {
        "amazon.com": "Amazon", "a.co": "Amazon", "amzn.to": "Amazon",
        "walmart.com": "Walmart", "target.com": "Target",
        "bestbuy.com": "Best Buy", "lego.com": "LEGO", "nike.com": "Nike",
        "etsy.com": "Etsy", "ebay.com": "eBay", "costco.com": "Costco",
        "kohls.com": "Kohl's", "macys.com": "Macy's", "apple.com": "Apple",
        "gamestop.com": "GameStop", "dickssportinggoods.com": "Dick's",
        "sephora.com": "Sephora", "ulta.com": "Ulta", "hm.com": "H&M",
        "oldnavy.com": "Old Navy", "gap.com": "Gap", "zappos.com": "Zappos",
        "barnesandnoble.com": "Barnes & Noble", "homedepot.com": "Home Depot",
        "lowes.com": "Lowe's", "academy.com": "Academy", "rei.com": "REI",
        "nintendo.com": "Nintendo", "playstation.com": "PlayStation",
        "adidas.com": "adidas", "lululemon.com": "lululemon",
        "americaneagle.com": "American Eagle", "ae.com": "American Eagle",
        "abercrombie.com": "Abercrombie", "hollisterco.com": "Hollister",
        "fivebelow.com": "Five Below", "michaels.com": "Michaels",
    }
    for k, v in known.items():
        if host == k or host.endswith("." + k):
            return v
    parts = host.split(".")
    return parts[-2].capitalize() if len(parts) >= 2 else host


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    s = str(v).replace(",", "").strip()
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        f = float(m.group(0))
    except ValueError:
        return None
    return f if 0 < f < 100000 else None


def _walk_ld(node, out):
    if isinstance(node, list):
        for n in node:
            _walk_ld(n, out)
        return
    if not isinstance(node, dict):
        return
    t = node.get("@type")
    types = t if isinstance(t, list) else [t]
    types = [str(x).lower() for x in types if x]
    if "product" in types or "productgroup" in types:
        out.append(node)
    for k in ("@graph", "mainEntity", "itemListElement", "hasVariant"):
        if k in node:
            _walk_ld(node[k], out)


def _offer_price(offers):
    if offers is None:
        return None
    if isinstance(offers, list):
        prices = [p for p in (_offer_price(o) for o in offers) if p]
        return min(prices) if prices else None
    if not isinstance(offers, dict):
        return None
    for k in ("price", "lowPrice", "highPrice"):
        p = _num(offers.get(k))
        if p:
            return p
    spec = offers.get("priceSpecification")
    if spec:
        if isinstance(spec, list):
            spec = spec[0] if spec else {}
        if isinstance(spec, dict):
            p = _num(spec.get("price"))
            if p:
                return p
    if "offers" in offers:
        return _offer_price(offers.get("offers"))
    return None


def _ld_image(img):
    if isinstance(img, list):
        img = img[0] if img else None
    if isinstance(img, dict):
        img = img.get("url") or img.get("contentUrl")
    return img if isinstance(img, str) else None


def _meta(page, *names):
    for n in names:
        for pat in (
            r'<meta[^>]+(?:property|name|itemprop)\s*=\s*["\']%s["\'][^>]*content\s*=\s*["\']([^"\']*)["\']' % re.escape(n),
            r'<meta[^>]+content\s*=\s*["\']([^"\']*)["\'][^>]*(?:property|name|itemprop)\s*=\s*["\']%s["\']' % re.escape(n),
        ):
            m = re.search(pat, page, re.I)
            if m and m.group(1).strip():
                return _html.unescape(m.group(1).strip())
    return None


def parse(page, url):
    """Pure: product facts from a page's HTML. Separate from the fetch so it
    can be tested on saved pages."""
    title = price = image = None
    for m in re.finditer(
            r'<script[^>]+type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            page, re.I | re.S):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except Exception:
            try:
                data = json.loads(re.sub(r"[\x00-\x1f]", " ", raw))
            except Exception:
                continue
        prods = []
        _walk_ld(data, prods)
        for p in prods:
            title = title or (p.get("name") if isinstance(p.get("name"), str) else None)
            image = image or _ld_image(p.get("image"))
            price = price or _offer_price(p.get("offers"))
            if not price and p.get("hasVariant"):
                vs = p.get("hasVariant")
                vs = vs if isinstance(vs, list) else [vs]
                vp = [x for x in (_offer_price((v or {}).get("offers")) for v in vs
                                  if isinstance(v, dict)) if x]
                price = min(vp) if vp else None
        if title and price and image:
            break

    title = title or _meta(page, "og:title", "twitter:title")
    image = image or _meta(page, "og:image", "og:image:secure_url", "twitter:image")
    price = price or _num(_meta(page, "product:price:amount", "og:price:amount",
                                "price", "twitter:data1"))
    if not price:
        m = re.search(r'itemprop\s*=\s*["\']price["\'][^>]*content\s*=\s*["\']([\d.,]+)', page, re.I)
        if m:
            price = _num(m.group(1))

    host = (urlparse(url).hostname or "").lower()
    if "amazon." in host:
        if not title:
            m = re.search(r'id\s*=\s*["\']productTitle["\'][^>]*>\s*([^<]+)', page, re.I)
            if m:
                title = m.group(1).strip()
        if not price:
            for pat in (
                r'class="a-price[^"]*"[^>]*>\s*<span class="a-offscreen">\s*\$([\d,]+\.\d{2})',
                r'"priceAmount"\s*:\s*([\d.]+)',
                r'id="priceblock_(?:our|deal)price"[^>]*>\s*\$([\d,]+\.\d{2})',
                r'<span class="a-offscreen">\s*\$([\d,]+\.\d{2})',
            ):
                m = re.search(pat, page)
                if m:
                    price = _num(m.group(1))
                    if price:
                        break
        if not image:
            m = re.search(r'"hiRes"\s*:\s*"(https://[^"]+)"', page) or \
                re.search(r'id="landingImage"[^>]+src="(https://[^"]+)"', page)
            if m:
                image = m.group(1)

    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
        if m:
            title = re.sub(r"\s+", " ", _html.unescape(m.group(1))).strip()
    if title:
        title = _html.unescape(title).strip()
        title = re.sub(r"^Amazon\.com\s*:\s*", "", title)
        title = re.sub(r"\s*[:|\-–]\s*(Amazon\.com|Walmart\.com|Target|Best Buy)\b.*$", "", title, flags=re.I)
        title = title[:200]
    if image and not image.startswith("http"):
        image = urljoin(url, image)
    return {"title": title or None, "price": round(price, 2) if price else None,
            "image": image or None}


_JUNK_TITLES = ("just a moment", "robot or human", "access denied", "attention required",
                "are you a human", "page not found", "404", "pardon our interruption",
                "something went wrong", "security check")


def _target_price(url, timeout):
    """Target renders its price in the browser; the page's own data feed has
    it. The key is the public one target.com's pages send."""
    m = re.search(r"/A-(\d+)", url)
    if not m:
        return None
    try:
        r = requests.get(
            "https://redsky.target.com/redsky_aggregations/v1/web/pdp_client_v1",
            params={"key": "9f36aeafbe60771e321a7cc95a78140772ab3e96",
                    "tcin": m.group(1), "pricing_store_id": "3991",
                    "channel": "WEB", "page": "/p/A-" + m.group(1)},
            headers=_HEADERS, timeout=timeout)
        price = ((r.json().get("data") or {}).get("product") or {}).get("price") or {}
        return _num(price.get("current_retail") or price.get("current_retail_min")
                    or price.get("reg_retail") or price.get("formatted_current_price"))
    except Exception:
        return None


def _blocked(page, status):
    if status in (403, 429, 503):
        return True
    low = page[:20000].lower()
    return any(s in low for s in (
        "captcha", "robot check", "are you a human", "access denied",
        "px-captcha", "verify you are human", "request unsuccessful"))


def fetch(raw, timeout=6):
    """URL (or a pasted share) → {ok, url, store, title, price, image, blocked, error}."""
    url = extract_url(raw)
    if not url:
        return {"ok": False, "error": "No link found"}
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not _host_ok(p.hostname):
        return {"ok": False, "error": "That link can't be opened"}
    out = {"ok": True, "url": url, "store": store_name(url),
           "title": None, "price": None, "image": None, "blocked": False}
    try:
        # Follow redirects by hand so EVERY hop is checked against the
        # private-network guard (share links like a.co bounce 2-3 times).
        final, r = url, None
        for _ in range(7):
            fp = urlparse(final)
            if fp.scheme not in ("http", "https") or not _host_ok(fp.hostname):
                return {"ok": False, "error": "That link can't be opened"}
            r = requests.get(final, headers=_HEADERS, timeout=timeout, stream=True,
                             allow_redirects=False)
            if r.is_redirect and r.headers.get("location"):
                final = urljoin(final, r.headers["location"])
                r.close()
                continue
            break
        else:
            return {"ok": False, "error": "Too many redirects"}
        buf = b""
        for chunk in r.iter_content(65536):
            buf += chunk
            if len(buf) > _MAX_BYTES:
                break
        page = buf.decode(r.encoding or "utf-8", errors="replace")
        out["url"] = final if len(final) < 2000 and "/blocked" not in final else url
        out["store"] = store_name(final)
        facts = parse(page, final)
        if r.status_code >= 400 or (facts["title"] and facts["title"].lower().startswith(_JUNK_TITLES)):
            facts["title"] = None if not facts["price"] else facts["title"]
        if not facts["price"] and "target.com" in (urlparse(final).hostname or ""):
            facts["price"] = _target_price(final, timeout)
        out.update(facts)
        if not (facts["price"] or facts["title"]) and _blocked(page, r.status_code):
            out["blocked"] = True
        elif not facts["price"] and _blocked(page, r.status_code):
            out["blocked"] = True
        out["status"] = r.status_code
        if facts["price"] and facts["title"]:
            out["blocked"] = False
    except requests.RequestException as e:
        out["error"] = type(e).__name__
    return out
