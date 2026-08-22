"""FOOTBALL WEEKLY GAME SHEETS — renderer / publisher.

Reads the week's `football_sheets` rows (data_blob + the narrative the
generation session wrote into sheet_md / friday_md), renders one printable
HTML sheet-pack per league, prints it to PDF via the pre-installed Chromium
(Playwright), uploads the PDF to the public `football-sheets` Supabase
Storage bucket, stamps `football_sheet_weeks`, and (with --telegram) queues
the Filled-Bot digest ping with the download links.

Runs in the CCR generation session (docs/football-sheet-runbook.md). The
sandbox blocks ESPN but reaches Supabase REST + Storage fine; Chromium is
preinstalled at $PLAYWRIGHT_BROWSERS_PATH. Every section renders only what
the blob actually carries — a missing source prints "unavailable", never an
invented number.

Usage:
  python -m scripts.football_sheet_render --week-key 2026-08-24 --sport NCAAF \
      --out /tmp/sheets [--upload] [--telegram] [--mode monday|friday]
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.football_sheet_data import (  # noqa: E402
    _sb_base, sb_select, sb_upsert)

log = logging.getLogger("football_sheet_render")
AZ = ZoneInfo("America/Phoenix")

try:
    import markdown as _md_mod
except ImportError:                       # graceful: narrative shows as <pre>
    _md_mod = None


def _md(text: str | None) -> str:
    if not text:
        return ""
    if _md_mod:
        return _md_mod.markdown(text, extensions=["tables"])
    return f"<pre class='mdfallback'>{html.escape(text)}</pre>"


def _e(v) -> str:
    return html.escape(str(v)) if v is not None else ""


def _az(iso: str | None) -> str:
    if not iso:
        return "TBD"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(AZ).strftime("%a %b %-d · %-I:%M %p AZ")
    except ValueError:
        return _e(iso)


def _pct(p) -> str:
    try:
        return f"{float(p) * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"


def _fair(a) -> str:
    if a is None:
        return "—"
    return f"+{a}" if a > 0 else str(a)


_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font: 10.5pt/1.45 'Helvetica Neue', Arial, sans-serif; color: #16202c;
       -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.page { padding: 28px 34px; }
h1 { font-size: 21pt; letter-spacing: -.02em; }
h2 { font-size: 13pt; margin: 2px 0 2px; }
h3 { font-size: 9.5pt; text-transform: uppercase; letter-spacing: .08em;
     color: #5a6b7f; margin: 14px 0 5px; border-bottom: 1px solid #d8e0e8;
     padding-bottom: 3px; }
table { border-collapse: collapse; width: 100%; margin: 4px 0 8px; }
th, td { text-align: left; padding: 3px 8px; font-size: 9.5pt;
         border-bottom: 1px solid #e6ecf2; }
th { font-size: 8.5pt; text-transform: uppercase; letter-spacing: .06em;
     color: #5a6b7f; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.cover { padding: 60px 34px; }
.cover .sub { color: #5a6b7f; font-size: 11pt; margin: 6px 0 26px; }
.game { page-break-before: always; }
.gamehead { display: flex; justify-content: space-between; align-items: baseline;
            border-bottom: 3px solid #16202c; padding-bottom: 6px; }
.meta { color: #5a6b7f; font-size: 9.5pt; margin-top: 3px; }
.tag { display: inline-block; font-size: 7.5pt; font-weight: 700;
       text-transform: uppercase; letter-spacing: .06em; padding: 2px 7px;
       border-radius: 9px; margin-left: 6px; vertical-align: middle; }
.tag.deep { background: #103c2c; color: #7ce6b2; }
.tag.data { background: #e6ecf2; color: #5a6b7f; }
.tag.edge { background: #4a1420; color: #ff9db0; }
.ournum { background: #f2f6fa; border: 1px solid #d8e0e8; border-radius: 8px;
          padding: 10px 14px; margin: 10px 0; }
.ournum .big { font-size: 13pt; font-weight: 700; }
.two { display: flex; gap: 22px; }
.two > div { flex: 1; }
.narrative { margin-top: 10px; }
.narrative h1, .narrative h2 { font-size: 11.5pt; margin: 10px 0 4px; }
.narrative h3 { border: none; margin: 8px 0 2px; }
.narrative p, .narrative li { font-size: 10pt; margin: 4px 0; }
.narrative ul, .narrative ol { padding-left: 18px; }
.unavail { color: #98a6b6; font-style: italic; font-size: 9.5pt; }
.rlm { color: #b02040; font-weight: 700; }
.small { font-size: 8.5pt; color: #5a6b7f; }
.idx td { font-size: 9pt; padding: 2.5px 7px; }
.pos { color: #0c7a4d; font-weight: 700; } .neg { color: #b02040; font-weight: 700; }
.betline { margin: 5px 0; }
.verdict { display: inline-block; font-size: 7.5pt; font-weight: 800;
           letter-spacing: .07em; padding: 2px 7px; border-radius: 9px;
           vertical-align: 1px; margin-right: 4px; }
.vplay { background: #103c2c; color: #7ce6b2; }
.vlean { background: #4a3a10; color: #ffd27a; }
.vpass { background: #e6ecf2; color: #5a6b7f; }
pre.mdfallback { white-space: pre-wrap; font: inherit; }
.footer { margin-top: 14px; padding-top: 6px; border-top: 1px solid #d8e0e8;
          font-size: 8pt; color: #98a6b6; }
"""


# --------------------------------------------------------------- fragments
def _ml_str(node: dict | None) -> str:
    if not node:
        return "—"
    o, n = node.get("open_c"), node.get("now_c")
    if o is None and n is None:
        return "—"
    if o == n or o is None:
        return f"{n}¢"
    return f"{o}¢ → {n}¢"


def _line_str(node: dict | None) -> str:
    if not node or node.get("line") is None:
        return "—"
    s = f"{node['line']:+g}" if node.get("side") == "home" else f"{node['line']:g}"
    first = node.get("first_line")
    if first is not None and first != node["line"]:
        f0 = f"{first:+g}" if node.get("side") == "home" else f"{first:g}"
        s = f"{f0} → {s}"
    o, n = node.get("open_c"), node.get("now_c")
    if n is not None:
        s += f" ({o}¢→{n}¢)" if o is not None and o != n else f" ({n}¢)"
    return s


def _lines_table(blob: dict) -> str:
    lines, splits = blob.get("lines") or {}, blob.get("splits") or {}
    rows = []
    bo = blob.get("book_odds") or {}
    if bo:
        sp = bo.get("spread_home")
        tt = bo.get("total")
        rows.append(
            f"<tr><td>{_e(bo.get('provider') or 'Consensus')}</td><td>—</td>"
            f"<td>{f'{sp:+g}' if sp is not None else '—'}</td>"
            f"<td>{f'{tt:g}' if tt is not None else '—'}</td>"
            f"<td></td><td></td></tr>")
    for src, label in (("pmm", "Polymarket"), ("kalshi", "Kalshi")):
        s = lines.get(src) or {}
        if not s:
            continue
        ml = s.get("ml") or {}
        rows.append(
            f"<tr><td>{label}</td>"
            f"<td>{_ml_str(ml.get('away'))} / {_ml_str(ml.get('home'))}</td>"
            f"<td>{_line_str(s.get('spread'))}</td>"
            f"<td>{_line_str(s.get('total'))}</td><td></td><td></td></tr>")
    for book, label in (("draftkings", "DraftKings"), ("circa", "Circa")):
        b = splits.get(book) or {}
        if not b:
            continue

        def _side(mt, side):
            v = (b.get(mt) or {}).get(side) or {}
            ln = v.get("line")
            if ln is None:
                return "—"
            s = f"{ln:+g}" if mt == "spread" and side == "home" else f"{ln:g}"
            if v.get("open_line") is not None and v["open_line"] != ln:
                s = f"{v['open_line']:g} → {s}"
            return s

        def _sp(mt):
            a = (b.get(mt) or {}).get("away") or {}
            h = (b.get(mt) or {}).get("home") or {}
            if mt == "total":
                a = (b.get(mt) or {}).get("over") or {}
                h = (b.get(mt) or {}).get("under") or {}
            if a.get("handle") is None and h.get("handle") is None:
                return ""
            return (f"{a.get('handle', '—')}/{a.get('bets', '—')} · "
                    f"{h.get('handle', '—')}/{h.get('bets', '—')}")

        rows.append(
            f"<tr><td>{label}</td>"
            f"<td>{_side('ml', 'away')} / {_side('ml', 'home')}</td>"
            f"<td>{_side('spread', 'home')}</td><td>{_side('total', 'over')}</td>"
            f"<td>{_sp('ml')}</td><td>{_sp('spread')}</td></tr>")
    if not rows:
        return "<p class='unavail'>No posted lines captured yet (game not " \
               "listed on the exchanges / VSiN outside its window).</p>"
    rlm = ""
    for f in (splits.get("rlm") or []):
        rlm += (f"<p class='rlm'>⚠ RLM — {f['book']} {f['market']}: "
                f"{f['handle']}% of handle on {f['heavy_side']}, line moved "
                f"{f['open_line']:g} → {f['line']:g} against the money.</p>")
    return ("<table><tr><th>Source</th><th>ML (away/home)</th>"
            "<th>Spread (home)</th><th>Total</th>"
            "<th>ML handle%/bets%</th><th>SPR handle%/bets%</th></tr>"
            + "".join(rows) + "</table>" + rlm
            + "<p class='small'>Splits are away·home (over·under for totals),"
              " handle%/bets%. Arrows = open → now.</p>")


def _short(name: str, blob: dict | None = None) -> str:
    """Short handle for a team. Prefer ESPN's `location` from the blob
    ('TCU', 'North Carolina') — mascots are multi-word too often for a
    drop-the-last-word heuristic ('TCU Horned'). Fallback: drop one word."""
    g = (blob or {}).get("game") or {}
    locs = g.get("locs") or {}
    for k in ("home", "away"):
        if g.get(k) == name and locs.get(k):
            return locs[k]
    parts = (name or "").split()
    return " ".join(parts[:-1]) if len(parts) > 1 else (name or "")


_VERDICT = {"play": ("PLAY", "vplay"), "lean": ("LEAN", "vlean"),
            "pass": ("PASS", "vpass")}


def _ladder_txt(bet: dict, label_fn) -> str:
    """'−6.5 needs −152 · −8.5 needs −118' — the shopping rungs after the
    market number."""
    alts = bet.get("ladder", [])[1:]
    if not alts:
        return ""
    return " · alt: " + " / ".join(
        f"{label_fn(a['line'])} to {_fair(a['fair'])}" for a in alts)


def _ournum(blob: dict) -> str:
    """THE BET card. A handicapper's read: the side, the line, and the
    price it's good to — model machinery stays in the engine room."""
    m = blob.get("model")
    if not m:
        return ("<div class='ournum'><span class='unavail'>No number on "
                "this one (unrated opponent — usually FCS). Market only."
                "</span></div>")
    away, home = blob["game"]["away"], blob["game"]["home"]
    margin = m.get("margin_cal", m.get("margin_raw"))
    total = m.get("total_cal", m.get("total_raw"))
    fav = home if margin >= 0 else away
    rows = [f"<div class='big'>Our number: {_e(_short(fav, blob))} "
            f"−{abs(margin):.1f} · total {total:.1f}"
            + (" · <b>neutral site</b>" if m.get("neutral_site") else "")
            + "</div>"]

    bs, bt = m.get("bet_spread"), m.get("bet_total")
    if bs:
        vtxt, vcls = _VERDICT[bs["verdict"]]
        team = _short(bs["team"], blob)
        rows.append(
            f"<div class='betline'><span class='verdict {vcls}'>{vtxt}</span> "
            f"<b>Spread</b> — market {_e(team)} {bs['line']:+g} "
            f"({_e(bs['src'])}): "
            + (f"<b>{_e(team)} {bs['line']:+g} at {_fair(bs['fair'])} or "
               f"better</b>{_ladder_txt(bs, lambda l: f'{l:+g}')}"
               if bs["verdict"] != "pass" else
               f"our number sits {abs(bs['edge_pts']):.1f} pt off the market "
               f"— no edge, pass")
            + "</div>")
    if bt:
        vtxt, vcls = _VERDICT[bt["verdict"]]
        side = "Over" if bt["side"] == "over" else "Under"
        rows.append(
            f"<div class='betline'><span class='verdict {vcls}'>{vtxt}</span> "
            f"<b>Total</b> — market {bt['line']:g} ({_e(bt['src'])}): "
            + (f"<b>{side} {bt['line']:g} at {_fair(bt['fair'])} or better"
               f"</b>{_ladder_txt(bt, lambda l: f'{l:g}')}"
               if bt["verdict"] != "pass" else
               f"our number {abs(bt['edge_pts']):.1f} off the market — pass")
            + "</div>")
    if not bs and not bt:
        rows.append("<div class='unavail'>No posted line yet — the plays "
                    "price in once a number is up (Friday update).</div>")
    win = m.get("win_prob_home")
    rows.append(f"<div class='small'>ML reference: {_e(_short(home, blob))} "
                f"{_pct(win)} / {_e(_short(away, blob))} "
                f"{_pct(1 - win) if win is not None else '—'} · "
                f"Gridiron IQ, {m.get('n_games')} games rated</div>")
    return "<div class='ournum'>" + "".join(rows) + "</div>"


def _injuries(blob: dict) -> str:
    inj = blob.get("injuries") or {}
    if "injuries" in (blob.get("unavailable") or []):
        return "<p class='unavail'>Injury feed unavailable at build time.</p>"
    if not inj:
        return "<p class='unavail'>No injuries reported for either team.</p>"
    out = "<div class='two'>"
    for k in ("away", "home"):
        rows = inj.get(k) or []
        team = blob["game"][k]
        out += f"<div><b>{_e(team)}</b>"
        if not rows:
            out += "<p class='unavail'>none reported</p></div>"
            continue
        out += "<table><tr><th>Player</th><th>Pos</th><th>Status</th></tr>"
        for r in rows[:12]:
            out += (f"<tr><td>{_e(r.get('name'))}</td>"
                    f"<td>{_e(r.get('position'))}</td>"
                    f"<td>{_e(r.get('status'))}</td></tr>")
        out += "</table></div>"
    return out + "</div>"


def _history(blob: dict) -> str:
    h = blob.get("history") or {}
    if not any(h.get(k) for k in ("last5_away", "last5_home", "h2h",
                                  "common_opponents")):
        return "<p class='unavail'>No prior results on file.</p>"

    def _fmt5(games):
        return " · ".join(
            f"{'W' if g['won'] else 'L'} {g['us']}-{g['them']} "
            f"{'v' if g['home'] else '@'} {g['opp'].split()[-1]}"
            for g in reversed(games)) or "—"

    out = (f"<p><b>{_e(blob['game']['away'])}</b> last 5: "
           f"{_e(_fmt5(h.get('last5_away') or []))}<br>"
           f"<b>{_e(blob['game']['home'])}</b> last 5: "
           f"{_e(_fmt5(h.get('last5_home') or []))}</p>")
    if h.get("h2h"):
        out += "<p><b>Head-to-head:</b> " + " · ".join(
            f"{g['date'][:4]}: {'W' if g['won'] else 'L'} {g['us']}-{g['them']}"
            f" ({_e(blob['game']['home'].split()[-1])} persp.)"
            for g in h["h2h"]) + "</p>"
    if h.get("common_opponents"):
        out += ("<table><tr><th>Common opp (2025)</th>"
                f"<th>{_e(blob['game']['home'].split()[-1])}</th>"
                f"<th>{_e(blob['game']['away'].split()[-1])}</th></tr>")
        for c in h["common_opponents"]:
            out += (f"<tr><td>{_e(c['opp'])}</td><td>{_e(c['home_res'])}</td>"
                    f"<td>{_e(c['away_res'])}</td></tr>")
        out += "</table>"
    out += f"<p class='small'>{_e(h.get('note') or '')}</p>"
    return out


def _fpi(blob: dict) -> str:
    f = blob.get("fpi") or {}
    if not f:
        return ""
    rows = "".join(
        f"<tr><td>{_e(blob['game'][k])}</td>"
        f"<td class='num'>{_e((f.get(k) or {}).get('fpi'))}</td>"
        f"<td class='num'>{_e((f.get(k) or {}).get('rank'))}</td></tr>"
        for k in ("away", "home") if f.get(k))
    if not rows:
        return ""
    return ("<h3>ESPN FPI</h3><table><tr><th>Team</th><th class='num'>FPI"
            "</th><th class='num'>Rank</th></tr>" + rows + "</table>")


def _game_header(row: dict, blob: dict) -> str:
    g = blob["game"]
    ranks = blob.get("ap_ranks") or {}

    def _nm(k):
        r = ranks.get(k)
        return (f"#{r} " if r else "") + g[k]

    tier = row.get("tier") or "data"
    edge_tag = ""
    bs = (blob.get("model") or {}).get("bet_spread") or {}
    if bs.get("verdict") == "play":
        edge_tag = "<span class='tag edge'>EDGE</span>"
    recs = g.get("records") or {}
    rec_txt = ""
    if (recs.get("away") or recs.get("home")) and \
            {recs.get("away"), recs.get("home")} != {"0-0"}:
        rec_txt = f" · {recs.get('away', '')} / {recs.get('home', '')}"
    return (f"<div class='gamehead'><h2>{_e(_nm('away'))} @ {_e(_nm('home'))}"
            f"<span class='tag {tier}'>{'DEEP DIVE' if tier == 'deep' else 'DATA SHEET'}</span>"
            f"{edge_tag}</h2></div>"
            f"<div class='meta'>{_az(g.get('event_start'))}"
            f"{' · ' + _e(g['venue']) if g.get('venue') else ''}"
            f"{' · NEUTRAL SITE' if g.get('neutral_site') else ''}"
            f"{' · ' + _e(g['broadcast']) if g.get('broadcast') else ''}"
            f"{_e(rec_txt)}</div>")


def _index_table(rows: list[dict]) -> str:
    body = ""
    for r in rows:
        blob = r.get("data_blob") or {}
        m = blob.get("model") or {}
        margin = m.get("margin_cal", m.get("margin_raw"))
        g = blob.get("game") or {}
        model_txt = "—"
        if margin is not None:
            fav = (g.get("home") if margin >= 0 else g.get("away")) or ""
            model_txt = f"{_short(fav, blob)} −{abs(margin):.1f}"
        bs, bt = m.get("bet_spread") or {}, m.get("bet_total") or {}
        mkt = (f"{_short(bs.get('team', ''), blob)} {bs['line']:+g}"
               if bs else "—")
        sp_play = "—"
        sp_cls = ""
        if bs:
            if bs["verdict"] == "pass":
                sp_play = "pass"
            else:
                sp_play = (f"{_short(bs['team'], blob)} {bs['line']:+g} "
                           f"@ {_fair(bs['fair'])}")
                sp_cls = "pos" if bs["verdict"] == "play" else ""
        ou_play = "—"
        ou_cls = ""
        if bt:
            if bt["verdict"] == "pass":
                ou_play = f"{bt['line']:g} · pass"
            else:
                ou_play = (f"{'O' if bt['side'] == 'over' else 'U'} "
                           f"{bt['line']:g} @ {_fair(bt['fair'])}")
                ou_cls = "pos" if bt["verdict"] == "play" else ""
        body += (f"<tr><td>{_az(r.get('event_start'))}</td>"
                 f"<td>{_e(r.get('event_name'))}</td>"
                 f"<td class='num'>{_e(model_txt)}</td>"
                 f"<td class='num'>{_e(mkt)}</td>"
                 f"<td class='num {sp_cls}'>{_e(sp_play)}</td>"
                 f"<td class='num {ou_cls}'>{_e(ou_play)}</td>"
                 f"<td>{'●' if r.get('tier') == 'deep' else ''}</td></tr>")
    return ("<table class='idx'><tr><th>Kickoff (AZ)</th><th>Game</th>"
            "<th class='num'>Our line</th><th class='num'>Market</th>"
            "<th class='num'>Spread play (or better)</th>"
            "<th class='num'>Total play</th>"
            "<th>Deep</th></tr>" + body + "</table>")


def render_week_html(week_key: str, sport: str, rows: list[dict],
                     mode: str) -> str:
    rows = sorted(rows, key=lambda r: (r.get("event_start") or "",
                                       r.get("event_name") or ""))
    built = datetime.now(AZ).strftime("%b %-d, %Y %-I:%M %p AZ")
    title = f"{sport} Week Sheets — week of {week_key}"
    if mode == "friday":
        title = f"{sport} Friday Update — week of {week_key}"
    head = (f"<div class='page cover'><h1>🏈 {title}</h1>"
            f"<div class='sub'>The Kahla House · Gridiron IQ model + market "
            f"tape · generated {built}. Analysis only — bet at your own "
            f"judgment.</div>")
    if mode == "friday":
        body = head
        any_changes = False
        for r in rows:
            blob = r.get("data_blob") or {}
            fri = blob.get("friday") or {}
            changes = fri.get("changes") or []
            note = r.get("friday_md")
            if not changes and not note:
                continue
            any_changes = True
            body += (f"<h3>{_e(r['event_name'])} · "
                     f"{_az(r.get('event_start'))}</h3>")
            if changes:
                body += "<ul>" + "".join(f"<li>{_e(c)}</li>" for c in changes) \
                        + "</ul>"
            if note:
                body += f"<div class='narrative'>{_md(note)}</div>"
        if not any_changes:
            body += "<p>No material changes since Monday.</p>"
        return f"<style>{_CSS}</style><title>{_e(title)}</title>" + body + \
               "</div>"

    out = head + "<h3>The board</h3>" + _index_table(rows) + "</div>"
    for r in rows:
        blob = r.get("data_blob") or {}
        if not blob:
            continue
        out += "<div class='page game'>" + _game_header(r, blob)
        out += _ournum(blob)
        out += "<h3>Lines & splits</h3>" + _lines_table(blob)
        if r.get("tier") == "deep":
            out += "<h3>Personnel</h3>" + _injuries(blob)
            out += _fpi(blob)
            out += "<h3>Recent form & common opponents</h3>" + _history(blob)
        if r.get("sheet_md"):
            out += "<h3>Analysis</h3><div class='narrative'>" \
                   + _md(r["sheet_md"]) + "</div>"
        else:
            out += "<p class='unavail'>Narrative pending.</p>"
        out += ("<div class='footer'>The Kahla House · sheet data built "
                f"{_e((r.get('data_built_at') or '')[:16])}Z · sources: "
                "Gridiron IQ (opponent-adjusted ratings), Polymarket/Kalshi "
                "tape, VSiN (DK+Circa), ESPN</div></div>")
    return f"<style>{_CSS}</style><title>{_e(title)}</title>" + out


# ------------------------------------------------------------------ pdf/io
def html_to_pdf(html_path: str, pdf_path: str) -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception:
            browser = pw.chromium.launch(
                executable_path="/opt/pw-browsers/chromium")
        page = browser.new_page()
        page.goto(f"file://{os.path.abspath(html_path)}")
        page.pdf(path=pdf_path, format="Letter",
                 margin={"top": "0.35in", "bottom": "0.35in",
                         "left": "0.3in", "right": "0.3in"},
                 print_background=True)
        browser.close()


def upload_pdf(local_path: str, storage_path: str) -> str:
    import httpx
    base, headers = _sb_base()
    h = dict(headers)
    h["Content-Type"] = "application/pdf"
    h["x-upsert"] = "true"
    with open(local_path, "rb") as f:
        r = httpx.post(f"{base}/storage/v1/object/football-sheets/"
                       f"{storage_path}", headers=h, content=f.read(),
                       timeout=120)
    r.raise_for_status()
    return f"{base}/storage/v1/object/public/football-sheets/{storage_path}"


def queue_telegram(text: str) -> None:
    sb_upsert("telegram_queue",
              [{"text": text, "created_at":
                datetime.now(timezone.utc).isoformat()}],
              on_conflict="id")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--week-key", required=True)
    ap.add_argument("--sport", choices=["NFL", "NCAAF"], required=True)
    ap.add_argument("--mode", choices=["monday", "friday"], default="monday")
    ap.add_argument("--out", default="/tmp/football-sheets")
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--telegram", action="store_true")
    args = ap.parse_args()

    rows = sb_select("football_sheets", {
        "select": ("id,week_key,sport,event_name,event_start,tier,"
                   "data_blob,sheet_md,friday_md,data_built_at"),
        "week_key": f"eq.{args.week_key}", "sport": f"eq.{args.sport}",
        "order": "event_start.asc"})
    if not rows:
        log.error("no football_sheets rows for %s %s", args.week_key,
                  args.sport)
        return 1
    os.makedirs(args.out, exist_ok=True)
    tag = "friday-update" if args.mode == "friday" else "sheets"
    stem = f"{args.week_key}-{args.sport.lower()}-{tag}"
    html_path = os.path.join(args.out, stem + ".html")
    pdf_path = os.path.join(args.out, stem + ".pdf")
    with open(html_path, "w") as f:
        f.write(render_week_html(args.week_key, args.sport, rows, args.mode))
    html_to_pdf(html_path, pdf_path)
    size_kb = os.path.getsize(pdf_path) // 1024
    log.info("rendered %s (%d rows, %d KB)", pdf_path, len(rows), size_kb)

    result = {"pdf": pdf_path, "rows": len(rows), "kb": size_kb}
    if args.upload:
        url = upload_pdf(pdf_path, f"{args.week_key}/{stem}.pdf")
        now_iso = datetime.now(timezone.utc).isoformat()
        patch = ({"friday_pdf_path": f"{args.week_key}/{stem}.pdf",
                  "friday_published_at": now_iso} if args.mode == "friday"
                 else {"pdf_path": f"{args.week_key}/{stem}.pdf",
                       "published_at": now_iso})
        sb_upsert("football_sheet_weeks",
                  [{"week_key": args.week_key, "sport": args.sport, **patch}],
                  "week_key,sport")
        result["url"] = url
        if args.telegram:
            deep = sum(1 for r in rows if r.get("tier") == "deep")
            label = ("Friday update" if args.mode == "friday"
                     else "weekly sheets")
            queue_telegram(f"🏈 {args.sport} {label} — week of "
                           f"{args.week_key}: {len(rows)} games"
                           + (f" ({deep} deep dives)" if args.mode == "monday"
                              else "")
                           + f"\n{url}")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
