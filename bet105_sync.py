"""BET105 ACCOUNT LINK (Nov 16 2026, Rob: "I think we need to build it onto the box. JuiceReel
links into Bet105 with ease"). Bet105 publishes no API and its Cloudflare front refuses our cloud
servers, so the box — at Rob's house, home internet, a real Chromium — logs in AS HIM and READS
his bets. Never places, cancels or changes anything (the box is a dashboard).

Phase 1 is this PROBE: log in, open whatever pages look like open bets / bet history, and record
what the browser saw — page text, the form fields, the links, and every JSON response the site's
own front end fetched — so the reader is built against the real pages, not a guess. The password
never leaves this function: it is typed into the login form and is never logged or returned.
"""
from __future__ import annotations

import json
import re
import time

START_URLS = ("https://www.bet105.ag/", "https://bet105.ag/")
LOGIN_URL = "https://app.bet105.ag/login"
ACCOUNT_PATHS = ("/my-bets", "/bets", "/account", "/account/bets", "/history", "/wagers", "/open-bets")
PAGE_WORDS = re.compile(r"open\s*bets|pending|my\s*bets|wagers?|bet\s*history|history|account|statement|figures",
                        re.I)
LOGIN_WORDS = re.compile(r"log\s*in|sign\s*in|login|enter", re.I)
MAX_JSON = 80
MAX_BODY = 6000
MAX_TEXT = 6000


def _type_into(page, loc, text: str) -> None:
    """Click the box and type like a person — fill() refuses a field still animating in."""
    try:
        loc.click(timeout=10000, force=True)
    except Exception:
        pass
    try:
        loc.fill("", timeout=3000)
    except Exception:
        pass
    page.keyboard.type(text, delay=60)


def _snap(page, label: str) -> dict:
    """What a page looks like: url, title, visible text, form fields, buttons, links."""
    out = {"label": label, "url": page.url, "at": time.strftime("%H:%M:%S")}
    try:
        out["title"] = page.title()
    except Exception:
        pass
    try:
        out["text"] = (page.inner_text("body") or "")[:MAX_TEXT]
    except Exception as e:
        out["text_err"] = str(e)[:120]
    try:
        out["inputs"] = page.eval_on_selector_all(
            "input,select,textarea",
            "els => els.slice(0,40).map(e => ({tag:e.tagName, type:e.type, name:e.name, id:e.id,"
            " ph:e.placeholder, vis: !!(e.offsetWidth||e.offsetHeight)}))")
    except Exception:
        pass
    try:
        out["buttons"] = page.eval_on_selector_all(
            "button,input[type=submit],a.btn,[role=button]",
            "els => els.slice(0,40).map(e => (e.innerText||e.value||'').trim().slice(0,40)).filter(Boolean)")
    except Exception:
        pass
    try:
        out["links"] = page.eval_on_selector_all(
            "a[href]", "els => els.slice(0,120).map(e => [(e.innerText||'').trim().slice(0,40), e.getAttribute('href')])")
    except Exception:
        pass
    try:
        out["frames"] = [f.url for f in page.frames][:10]
    except Exception:
        pass
    return out


def _launch(pw, headless: bool, res: dict):
    """A browser the box actually has. New Playwright builds want a separate 'headless shell'
    download for headless=True (Nov 16 2026: not installed on the box — the first probe died
    there), so try, in order: the full Playwright Chromium in new-headless mode, the Mac's own
    Google Chrome, any Chromium already in the Playwright cache, then the plain default."""
    import glob
    import os
    tries = []
    for kw in ({"channel": "chromium"}, {"channel": "chrome"}):
        try:
            b = pw.chromium.launch(headless=headless, **kw)
            res["browser"] = kw
            return b
        except Exception as e:
            tries.append(f"{kw}: {str(e).splitlines()[0][:120]}")
    pats = [os.path.expanduser("~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium"),
            os.path.expanduser("~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"),
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    for pat in pats:
        for exe in sorted(glob.glob(pat), reverse=True):
            try:
                b = pw.chromium.launch(headless=headless, executable_path=exe,
                                       args=["--headless=new"] if headless else [])
                res["browser"] = {"exe": exe}
                return b
            except Exception as e:
                tries.append(f"{exe}: {str(e).splitlines()[0][:120]}")
    res["launch_tries"] = tries
    return pw.chromium.launch(headless=headless)


def probe(user: str, password: str, headless: bool = True, timeout_s: float = 150.0) -> dict:
    """Log in and record the account pages. Returns a dict safe to store (no password)."""
    t0 = time.time()
    res: dict = {"ok": False, "steps": [], "json": [], "headless": headless}
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        res["error"] = f"playwright missing: {e}"[:200]
        return res
    pw = sync_playwright().start()
    browser = None
    try:
        browser = _launch(pw, headless, res)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 900}, locale="en-US", timezone_id="America/Phoenix")
        page = ctx.new_page()
        page.set_default_timeout(20000)

        def _on_resp(r):
            if len(res["json"]) >= MAX_JSON:
                return
            try:
                ct = (r.headers or {}).get("content-type", "")
                u = r.url
                if "app.bet105.ag" not in u:
                    return
                if "json" not in ct and not re.search(r"/api/|__bff|wager|bet", u, re.I):
                    return
                if re.search(r"\.(png|jpe?g|gif|svg|woff2?|css|js)(\?|$)", u, re.I):
                    return
                body = ""
                try:
                    body = r.text()[:MAX_BODY]
                except Exception:
                    pass
                if password and password in body:
                    body = body.replace(password, "***")
                res["json"].append({"url": u[:300], "status": r.status, "ct": ct[:60],
                                    "method": r.request.method, "body": body})
            except Exception:
                pass
        page.on("response", _on_resp)

        # THE LOGIN PAGE DIRECTLY (Nov 16 2026, first real run: the landing page's Log In opened a
        # React modal on app.bet105.ag whose fields were still animating in, and fill() timed out;
        # the form also carries a Cloudflare Turnstile check that must pass before submit)
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
        except Exception as e:
            res["steps"].append({"label": "goto_fail", "url": LOGIN_URL, "err": str(e)[:160]})
        page.wait_for_timeout(6000)
        res["steps"].append(_snap(page, "login_page"))
        try:
            ub = page.get_by_placeholder(re.compile("username", re.I)).first
            ub.wait_for(state="visible", timeout=20000)
        except Exception:
            ub = None
        if ub is None:
            res["error"] = "no username field found"
            return res
        pb = page.locator("input[type=password]").first
        _type_into(page, ub, user)
        _type_into(page, pb, password)
        # the Turnstile token: a real Chrome usually earns it on its own within seconds
        tok = ""
        for _ in range(30):
            try:
                tok = page.eval_on_selector("input[name=cf-turnstile-response]", "e => e.value") or ""
            except Exception:
                tok = ""
            if tok:
                break
            page.wait_for_timeout(1000)
        res["turnstile"] = bool(tok)
        submitted = False
        try:
            btns = page.locator("button", has_text=re.compile(r"^\s*log\s*in\s*$", re.I))
            n = btns.count()
            if n:
                btns.nth(n - 1).click(timeout=8000)          # the form's own button sits last
                submitted = True
        except Exception as e:
            res["submit_err"] = str(e)[:160]
        if not submitted:
            try:
                pb.press("Enter")
            except Exception:
                pass
        page.wait_for_timeout(10000)
        res["steps"].append(_snap(page, "after_login"))
        txt = (res["steps"][-1].get("text") or "").lower()
        res["logged_in"] = (page.locator("input[type=password]").count() == 0
                            or "logout" in txt or "log out" in txt or "balance" in txt)

        # the account pages by URL first (guesses are cheap; a 404 page is still a record)
        for path in ACCOUNT_PATHS:
            if time.time() - t0 > timeout_s:
                break
            try:
                page.goto("https://app.bet105.ag" + path, wait_until="domcontentloaded")
                page.wait_for_timeout(5000)
                res["steps"].append(_snap(page, f"url:{path}"))
            except Exception as e:
                res["steps"].append({"label": f"url_fail:{path}", "err": str(e)[:160]})

        # every link / button that reads like the bets pages, once each
        seen = set()
        for _ in range(6):
            if time.time() - t0 > timeout_s:
                break
            target = None
            for el in page.query_selector_all("a,button,[role=tab],[role=button],li"):
                try:
                    lab = (el.inner_text() or "").strip()[:40]
                except Exception:
                    continue
                if lab and PAGE_WORDS.search(lab) and lab.lower() not in seen and len(lab) < 40:
                    target = (el, lab)
                    break
            if not target:
                break
            el, lab = target
            seen.add(lab.lower())
            try:
                el.click()
                page.wait_for_timeout(5000)
                res["steps"].append(_snap(page, f"click:{lab}"))
            except Exception as e:
                res["steps"].append({"label": f"click_fail:{lab}", "err": str(e)[:160]})
        res["ok"] = True
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"[:300]
    finally:
        res["ms"] = int((time.time() - t0) * 1000)
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
    # the password never goes into the record
    s = json.dumps(res)
    if password and password in s:
        res = json.loads(s.replace(password, "***"))
    return res
