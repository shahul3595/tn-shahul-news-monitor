#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe.py -- step 1: can GitHub's servers do what the collector needs?

Read-only and self-contained. It stores nothing and needs no database or
keyword list. It answers four questions, in the order the collector meets them:

  1. FEEDS     Do Google News and YouTube answer GitHub's servers?
  2. DECODE    Does Google turn news.google.com links into real article links for them?
               (from home: 694 of 694 converted, 1.3-3.2 s each)
  3. ARTICLES  Do the news sites let GitHub's servers download the article?
               (from home: 93% usable)
  4. TELEGRAM  Only if the two Telegram secrets are set: can it post to your channel?

    python probe.py                  everything, about 5 minutes
    python probe.py --decode 10      fewer link conversions

The report is printed and written to probe_report.md. On GitHub it also
appears on the run's Summary page.
"""

import argparse
import html
import itertools
import json
import os
import re
import statistics
import sys
import time
import traceback
from collections import Counter
from pathlib import Path
from urllib.parse import quote, urlparse

import feedparser
import httpx
import trafilatura

HERE = Path(__file__).resolve().parent
REPORT = HERE / "probe_report.md"

# the same browser identity, timeouts and thresholds as collect.py
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HTTP_TIMEOUT = 25.0
THIN_TEXT_CHARS = 400
SOCIAL_HOSTS = ("facebook.com", "instagram.com", "twitter.com", "x.com",
                "threads.net", "linkedin.com", "whatsapp.com", "t.me")
VIDEO_HOSTS = ("youtube.com", "youtu.be", "dailymotion.com", "vimeo.com")
DEFAULT_BLOCKED = ("theprint.in", "fuelcarmagazine.com", "tamil.getlokalapp.com")

# what counts as "works" -- set against the numbers measured from home
FEEDS_OK = 0.90        # Google sometimes answers one poll with a 503; that is normal
DECODE_OK = 0.80
DECODE_BLOCKED = 0.40
DECODE_SLOW_S = 6.0
ARTICLES_OK = 0.70

TAMIL = re.compile(r"[஀-௿]")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def host_of(url):
    h = (urlparse(url or "").hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def host_in(host, suffixes):
    host = (host or "").lower().rstrip(".")
    return any(host == s or host.endswith("." + s) for s in suffixes)


def clip(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def md(s, n=70):
    """Safe inside a markdown table cell."""
    return clip(s, n).replace("|", "¦").replace("\n", " ")


def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "n/a"


def feed_url_for(s):
    """Verbatim logic from collect.py."""
    if s["kind"] == "google_news":
        q = quote(s["query"])
        if s.get("language") == "ta":
            return f"https://news.google.com/rss/search?q={q}&hl=ta&gl=IN&ceid=IN%3Ata"
        return f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN%3Aen"
    if s["kind"] == "youtube":
        cid = (s.get("channel_id") or "").strip()
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}" if cid else ""
    return s.get("url", "")


def load_sources():
    cfg = json.loads((HERE / "sources.json").read_text(encoding="utf-8"))
    blocked = tuple(h.strip().lower() for h in (cfg.get("blocked_hosts") or DEFAULT_BLOCKED) if h.strip())
    srcs = [s for s in cfg["sources"]
            if s.get("enabled", True) and s.get("kind") in ("google_news", "youtube") and feed_url_for(s)]
    return srcs, blocked


def say(s=""):
    try:
        print(s, flush=True)
    except UnicodeEncodeError:
        print(s.encode("ascii", "replace").decode(), flush=True)


# --------------------------------------------------------------------------
# 1. feeds
# --------------------------------------------------------------------------

def poll_feeds(client, srcs, pace):
    say(f"\n== 1. FEEDS: polling {len(srcs)} enabled sources, {pace}s apart ==")
    out = []
    for s in srcs:
        rec = {"id": s["source_id"], "kind": s["kind"], "status": None, "entries": 0,
               "secs": 0.0, "problem": None, "items": []}
        t0 = time.monotonic()
        try:
            r = client.get(feed_url_for(s))
            rec["status"] = r.status_code
            head = r.content[:3000].lower()
            if r.status_code != 200:
                rec["problem"] = f"HTTP {r.status_code}"
            elif b"<rss" not in head and b"<feed" not in head:
                rec["problem"] = "not a feed (a block or CAPTCHA page?)"
            else:
                f = feedparser.parse(r.content)
                rec["entries"] = len(f.entries)
                for e in f.entries:
                    src = e.get("source") or {}
                    rec["items"].append({
                        "source_id": s["source_id"],
                        "link": e.get("link", ""),
                        "title": e.get("title", ""),
                        "publisher_host": host_of(src.get("href", "")) if isinstance(src, dict) else "",
                    })
        except Exception as ex:
            rec["problem"] = f"{type(ex).__name__}: {clip(str(ex), 80)}"
        rec["secs"] = round(time.monotonic() - t0, 1)
        out.append(rec)
        say(f"  {rec['id']:<22} {str(rec['status'] or '-'):>4}  {rec['entries']:>3} entries  "
            f"{rec['secs']:>4}s  {rec['problem'] or ''}")
        time.sleep(pace)
    return out


# --------------------------------------------------------------------------
# 2. decode
# --------------------------------------------------------------------------

def pick_for_decode(feeds, n, blocked):
    """Round-robin across sources, so the sample is not 30 items from one query
    (the --limit lesson in HANDOFF section 10)."""
    per_source = []
    for f in feeds:
        if f["kind"] != "google_news":
            continue
        per_source.append([it for it in f["items"]
                           if "news.google.com" in it["link"]
                           and not host_in(it["publisher_host"], blocked)])
    out, seen = [], set()
    for row in itertools.zip_longest(*per_source):
        for it in row:
            if it and it["link"] not in seen:
                seen.add(it["link"])
                out.append(it)
                if len(out) >= n:
                    return out
    return out


def decode_links(items, pace):
    say(f"\n== 2. DECODE: converting {len(items)} Google News links, {pace}s apart ==")
    try:
        from googlenewsdecoder import gnewsdecoder
    except ImportError:
        say("  googlenewsdecoder is not installed")
        return [dict(it, url=None, secs=0.0, error="googlenewsdecoder not installed") for it in items]
    out = []
    for k, it in enumerate(items, 1):
        t0 = time.monotonic()
        url, err = None, None
        try:
            res = gnewsdecoder(it["link"], interval=None)
            if res.get("status"):
                url = res.get("decoded_url")
            else:
                err = clip(res.get("message", "decode failed"), 120)
        except Exception as ex:
            err = f"{type(ex).__name__}: {clip(str(ex), 100)}"
        rec = dict(it, url=url, secs=round(time.monotonic() - t0, 1), error=err)
        out.append(rec)
        say(f"  [{k:>2}/{len(items)}] {rec['secs']:>5.1f}s  "
            + (f"-> {clip(host_of(url), 40)}" if url else f"FAILED {err}"))
        time.sleep(pace)
    return out


# --------------------------------------------------------------------------
# 3. articles
# --------------------------------------------------------------------------

def pick_for_articles(decoded, n, blocked):
    usable = [d for d in decoded if d["url"]
              and not host_in(host_of(d["url"]), VIDEO_HOSTS + SOCIAL_HOSTS + blocked)]
    first, rest, hosts = [], [], set()
    for d in usable:
        h = host_of(d["url"])
        (rest if h in hosts else first).append(d)
        hosts.add(h)
    return (first + rest)[:n]


def fetch_articles(client, items, pace):
    say(f"\n== 3. ARTICLES: downloading {len(items)} articles, {pace}s apart ==")
    out = []
    for k, it in enumerate(items, 1):
        t0 = time.monotonic()
        code, chars, err = None, 0, None
        try:
            r = client.get(it["url"], headers={"User-Agent": UA})
            code = r.status_code
            text = trafilatura.extract(r.text, include_comments=False, include_tables=False) or ""
            chars = len(text)
        except Exception as ex:
            err = f"{type(ex).__name__}: {clip(str(ex), 80)}"
        if chars >= THIN_TEXT_CHARS:
            verdict = "OK"
        elif code and code >= 400:
            verdict = "REFUSED"                  # 403/451/503: the site turned GitHub away
        else:
            verdict = "THIN" if chars else "FAILED"
        rec = dict(it, http=code, chars=chars, verdict=verdict, error=err,
                   secs=round(time.monotonic() - t0, 1), host=host_of(it["url"]))
        out.append(rec)
        say(f"  [{k:>2}/{len(items)}] {verdict:<6} HTTP {str(code or '-'):>3}  {chars:>6} chars  "
            f"{clip(rec['host'], 32):<32} {err or ''}")
        time.sleep(pace)
    return out


# --------------------------------------------------------------------------
# 4. verdict and report
# --------------------------------------------------------------------------

def assess(feeds, decoded, articles):
    gn = [f for f in feeds if f["kind"] == "google_news"]
    yt = [f for f in feeds if f["kind"] == "youtube"]
    gn_ok = sum(1 for f in gn if not f["problem"])
    yt_ok = sum(1 for f in yt if not f["problem"])
    dec_ok = [d for d in decoded if d["url"]]
    dec_med = statistics.median(d["secs"] for d in dec_ok) if dec_ok else None
    dec_429 = sum(1 for d in decoded if d["error"] and ("429" in d["error"] or "too many" in d["error"].lower()))
    art_ok = sum(1 for a in articles if a["verdict"] == "OK")

    a = {
        "gn": (gn_ok, len(gn)), "yt": (yt_ok, len(yt)),
        "decode": (len(dec_ok), len(decoded)), "decode_median": dec_med, "decode_429": dec_429,
        "articles": (art_ok, len(articles)),
    }
    feeds_good = bool(gn) and gn_ok >= FEEDS_OK * len(gn)
    dec_rate = len(dec_ok) / len(decoded) if decoded else 0.0
    art_rate = art_ok / len(articles) if articles else 0.0

    notes = []
    if not gn:
        overall = "NO RESULT — no Google News sources are enabled in sources.json."
    elif not feeds_good:
        overall = ("NO — Google News is refusing GitHub's servers, so GitHub can't be the collector. "
                   "Paste this report back to Claude.")
    elif not decoded:
        overall = "NO RESULT — the feeds worked but returned no links to test."
    elif dec_rate < DECODE_BLOCKED:
        overall = ("NOT AS PLANNED — the feeds work, but Google won't convert links for GitHub. "
                   "The fallback is a digest built from headlines and Google News links.")
    elif dec_rate < DECODE_OK or (articles and art_rate < ARTICLES_OK):
        overall = "PARTLY — GitHub can run the collector, with the gaps listed below."
    else:
        overall = "YES — GitHub can run the collector."

    if feeds_good and decoded and DECODE_BLOCKED <= dec_rate < DECODE_OK:
        notes.append(f"Only {pct(len(dec_ok), len(decoded))} of links converted (home: 100%). "
                     "The collector would need slower pacing.")
    if dec_med is not None and dec_med > DECODE_SLOW_S:
        notes.append(f"Link conversion is slow: {dec_med:.1f}s each (home: 1.3–3.2s). "
                     "Each run gets through fewer items.")
    if dec_429:
        notes.append(f"Google answered {dec_429} conversion(s) with 'too many requests'.")
    if articles and art_rate < ARTICLES_OK:
        bad = Counter(x["host"] for x in articles if x["verdict"] != "OK")
        notes.append(f"Only {pct(art_ok, len(articles))} of articles were readable (home: 93%). "
                     f"Sites that failed: {', '.join(h for h, _ in bad.most_common(8))}.")
    if yt and yt_ok < len(yt):
        notes.append(f"{len(yt) - yt_ok} of {len(yt)} YouTube feeds failed.")
    if gn and feeds_good and gn_ok < len(gn):
        notes.append(f"{len(gn) - gn_ok} Google News feed(s) failed once — normal if it's only one or two.")
    return a, overall, notes


def build_report(feeds, decoded, articles, a, overall, notes, tg, elapsed):
    L = []
    p = L.append
    p("# GitHub test run — can GitHub's servers collect the news?\n")
    p(f"**Verdict: {overall}**\n")
    gn_ok, gn_n = a["gn"]
    yt_ok, yt_n = a["yt"]
    d_ok, d_n = a["decode"]
    ar_ok, ar_n = a["articles"]
    med = f"{a['decode_median']:.1f}s" if a["decode_median"] is not None else "—"
    p("| Check | From GitHub | From your PC (measured earlier) |")
    p("|---|---|---|")
    p(f"| Google News feeds answered | {gn_ok} of {gn_n} ({pct(gn_ok, gn_n)}) | all |")
    p(f"| Google links converted to real links | {d_ok} of {d_n} ({pct(d_ok, d_n)}), median {med} | 100%, 1.3–3.2s |")
    p(f"| Articles readable (400+ characters) | {ar_ok} of {ar_n} ({pct(ar_ok, ar_n)}) | 93% |")
    p(f"| YouTube channel feeds answered | {yt_ok} of {yt_n} | all |")
    p(f"| Telegram test message | {tg} | — |")
    p("")
    if notes:
        p("**Notes**\n")
        for n in notes:
            p(f"- {n}")
        p("")

    bad_feeds = [f for f in feeds if f["problem"]]
    if bad_feeds:
        p("## Feeds that failed\n")
        for f in bad_feeds:
            p(f"- `{f['id']}` — {md(f['problem'], 100)}")
        p("")

    p("## Link conversions\n")
    p("| # | Query | Seconds | Result |")
    p("|---|---|---|---|")
    for k, d in enumerate(decoded, 1):
        res = md(host_of(d["url"]), 40) if d["url"] else "FAILED: " + md(d["error"], 60)
        p(f"| {k} | {d['source_id']} | {d['secs']} | {res} |")
    p("")

    p("## Article downloads\n")
    p("| # | Site | HTTP | Characters | Result |")
    p("|---|---|---|---|---|")
    for k, x in enumerate(articles, 1):
        p(f"| {k} | {md(x['host'], 40)} | {x['http'] or '—'} | {x['chars']} | "
          f"{x['verdict']}{' — ' + md(x['error'], 50) if x['error'] else ''} |")
    p("")

    p("<details><summary>All feeds</summary>\n")
    p("| Source | HTTP | Entries | Seconds |")
    p("|---|---|---|---|")
    for f in feeds:
        p(f"| {f['id']} | {f['status'] or '—'} | {f['entries']} | {f['secs']} |")
    p("\n</details>\n")
    p(f"_Took {elapsed / 60:.1f} minutes. Nothing was stored; no database was touched._")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------
# optional: telegram
# --------------------------------------------------------------------------

def telegram_check(client, feeds, overall, a):
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        return "not tested (secrets not set yet)"
    sample = next((it["title"] for f in feeds for it in f["items"] if TAMIL.search(it["title"] or "")), "")
    d_ok, d_n = a["decode"]
    ar_ok, ar_n = a["articles"]
    esc = lambda s: html.escape(s or "", quote=False)            # noqa: E731
    text = ("🧪 <b>GitHub test run</b> — not a real alert\n"
            f"{esc(overall)}\n\n"
            f"Feeds: {a['gn'][0]}/{a['gn'][1]} · Links: {d_ok}/{d_n} · Articles: {ar_ok}/{ar_n} · "
            f"YouTube: {a['yt'][0]}/{a['yt'][1]}\n"
            + (f"\nTamil check: <b>{esc(clip(sample, 120))}</b>\n" if sample else "")
            + "Symbols check: &amp; &lt; &gt; \" ( ) . -")
    try:
        r = client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                              "link_preview_options": {"is_disabled": True}}, timeout=20)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return "sent — check your channel"
        desc = str(data.get("description") or r.text[:120]).replace(token, "<token>")
        hint = ""
        if r.status_code in (401, 404):
            hint = " (the bot token is wrong)"
        elif "chat not found" in desc.lower():
            hint = " (the chat id is wrong)"
        elif r.status_code == 403 or "rights" in desc.lower():
            hint = " (add the bot to the channel as an administrator)"
        return f"FAILED: HTTP {r.status_code} {clip(desc, 80)}{hint}"
    except Exception as ex:
        return f"FAILED: {type(ex).__name__}: {clip(str(ex).replace(token, '<token>'), 80)}"


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Can GitHub's servers collect the news?")
    ap.add_argument("--decode", type=int, default=30, help="Google links to convert (default 30)")
    ap.add_argument("--articles", type=int, default=15, help="articles to download (default 15)")
    ap.add_argument("--feed-pace", type=float, default=1.2)
    ap.add_argument("--decode-pace", type=float, default=2.5)
    ap.add_argument("--article-pace", type=float, default=2.0)
    a = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    t0 = time.monotonic()
    feeds, decoded, articles = [], [], []
    tg = "not tested"
    try:
        srcs, blocked = load_sources()
        with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True, headers={"User-Agent": UA}) as client:
            feeds = poll_feeds(client, srcs, a.feed_pace)
            decoded = decode_links(pick_for_decode(feeds, a.decode, blocked), a.decode_pace)
            articles = fetch_articles(client, pick_for_articles(decoded, a.articles, blocked), a.article_pace)
            assessment, overall, notes = assess(feeds, decoded, articles)
            tg = telegram_check(client, feeds, overall, assessment)
    except Exception:
        say("\nTHE PROBE ITSELF CRASHED -- paste everything below to Claude:\n")
        say(traceback.format_exc())
        assessment, overall, notes = assess(feeds, decoded, articles)
        overall = "PROBE ERROR — the test script crashed partway. Paste the log to Claude. " + overall
    report = build_report(feeds, decoded, articles, assessment, overall, notes, tg, time.monotonic() - t0)
    REPORT.write_text(report, encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report)
    say("\n" + "=" * 78)
    say(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
