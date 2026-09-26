#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trace.py -- watch the pipeline happen, one stage at a time.

READ-ONLY. Never opens corpus.db, never writes anything, never sends anything.
Drop it in the same folder as collect.py / rules.py and run it.

    python trace.py                            # default: Velachery, 2 days
    python trace.py --query "வேளச்சேரி" --lang ta --window 2d
    python trace.py --query "திருவள்ளூர்" --lang ta --window 1d --resolve 5
    python trace.py --query "தவெக" --lang ta --window 6h --resolve 0

What you will see, in order:

    STAGE 1  the URL that is actually requested
    STAGE 2  the raw RSS -- every headline, publisher and date, before any filtering
    STAGE 3  scoring on TITLE ONLY -- what a pre-resolve filter would keep or drop
    STAGE 4  token decoding -- Google's link turned into the publisher's real link
    STAGE 5  extraction -- the full article body
    STAGE 6  re-scoring WITH the body -- how much the body changed the verdict
    STAGE 7  dedup -- every pair compared, and what would have merged

Flags:
    --query    the search terms (default: Velachery)
    --lang     en | ta            (default: en)
    --window   Google News when: value, e.g. 6h, 1d, 2d, 7d   (default: 2d)
    --resolve  how many tokens to decode (default 3; 0 to stop after stage 3)
    --pace     seconds between decode calls (default 5.0, same as the collector)
    --full     print whole article bodies instead of the first 300 chars
"""

import argparse
import html as _html
import sys
import time
from urllib.parse import quote

try:
    import feedparser
    import httpx
    import trafilatura
except ImportError as ex:
    sys.exit(f"missing dependency: {ex}\nrun:  pip install -r requirements.txt")

try:
    import rules
except ImportError:
    sys.exit("run this from the same folder as rules.py")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

W = 78


def rule(title=""):
    print()
    print("=" * W)
    if title:
        print("  " + title)
        print("=" * W)


def sub(title):
    print()
    print("-" * W)
    print("  " + title)
    print("-" * W)


def clip(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def band_mark(band):
    return {"AUTO_KEEP": "KEEP ", "AI": "ASK  ", "KEYWORD_KEEP": "KEEP ",
            "DROP": "drop "}.get(band, f"{band:<5}")


# ---------------------------------------------------------------- stage 1
def build_url(query, lang, window):
    q = quote(f"{query} when:{window}") if window else quote(query)
    if lang == "ta":
        return f"https://news.google.com/rss/search?q={q}&hl=ta&gl=IN&ceid=IN%3Ata"
    return f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN%3Aen"


# ---------------------------------------------------------------- stage 2
def fetch_feed(url):
    t0 = time.time()
    with httpx.Client(timeout=30, follow_redirects=True,
                      headers={"User-Agent": UA}) as c:
        r = c.get(url)
    ms = int((time.time() - t0) * 1000)
    print(f"  HTTP {r.status_code}   {len(r.content):,} bytes   {ms} ms")
    if r.status_code != 200:
        sys.exit(f"  Google returned {r.status_code}. 503 is transient -- retry.")
    return feedparser.parse(r.content)


def entry_publisher(e):
    src = getattr(e, "source", None)
    if src is not None:
        t = getattr(src, "title", None)
        if t:
            return t
    return (getattr(e, "author", "") or "").strip() or "?"


def entry_desc(e):
    raw = getattr(e, "summary", "") or ""
    return " ".join(_html.unescape(rules.strip_html(raw)).split())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="Velachery")
    ap.add_argument("--lang", default="en", choices=["en", "ta"])
    ap.add_argument("--window", default="2d")
    ap.add_argument("--resolve", type=int, default=3)
    ap.add_argument("--pace", type=float, default=5.0)
    ap.add_argument("--full", action="store_true")
    a = ap.parse_args()

    kw = rules.keywords_from_file()

    rule("STAGE 1 -- THE REQUEST")
    print(f"  query   : {a.query}")
    print(f"  language: {a.lang}        window: when:{a.window}")
    url = build_url(a.query, a.lang, a.window)
    print(f"\n  {url}")
    print(f"\n  vocabulary: {len(kw.terms)} active terms, {len(kw.excludes)} excludes"
          f"  (from {kw.origin}, hash {kw.hash})")
    print("  NOTE: these terms are NOT sent to Google. The query above is the only")
    print("        thing that decides what arrives. Keywords filter it afterwards.")

    rule("STAGE 2 -- WHAT THE RSS FEED RETURNED (no filtering yet)")
    feed = fetch_feed(url)
    entries = list(feed.entries)
    print(f"  {len(entries)} entries"
          + ("   <-- AT GOOGLE'S 100-ITEM CAP: more exists that you did not get"
             if len(entries) >= 100 else ""))
    if not entries:
        sys.exit("\n  Nothing returned. Try a wider window, or check the query spelling.")

    items = []
    for i, e in enumerate(entries, 1):
        items.append(dict(
            i=i,
            title=rules.clean_title(getattr(e, "title", ""), entry_publisher(e)),
            publisher=entry_publisher(e),
            published=getattr(e, "published", "") or "?",
            link=getattr(e, "link", ""),
            desc=entry_desc(e),
        ))

    print(f"\n  {'#':>3}  {'published':<17} {'publisher':<22} headline")
    for it in items:
        print(f"  {it['i']:>3}  {clip(it['published'], 17):<17} "
              f"{clip(it['publisher'], 22):<22} {clip(it['title'], 60)}")

    sub("what one RSS entry actually contains (item 1, in full)")
    one = items[0]
    print(f"  title      : {one['title']}")
    print(f"  publisher  : {one['publisher']}")
    print(f"  published  : {one['published']}")
    print(f"  description: {clip(one['desc'], 300) or '(empty)'}")
    print(f"  link       : {clip(one['link'], 100)}")
    if "news.google.com" in one["link"]:
        print("               ^ a Google TOKEN. Useless until decoded -- stage 4.")

    rule("STAGE 3 -- SCORING ON TITLE ONLY (before any decode is spent)")
    print("  This is the filter that does NOT exist in the collector today.")
    print("  Every item below is currently decoded and downloaded regardless.\n")
    print(f"  {'#':>3}  {'band':<6}{'score':>5}  {'urgent':<7} terms matched")
    counts = {}
    for it in items:
        sc = rules.score(kw, it["title"], it["desc"], "", has_ai=False)
        ur = rules.urgent(kw, it["title"], it["desc"], "")
        it["title_score"] = sc
        it["title_urgent"] = ur
        counts[sc["band"]] = counts.get(sc["band"], 0) + 1
        print(f"  {it['i']:>3}  {band_mark(sc['band'])}{sc['score']:>5}  "
              f"{('URGENT' if ur else ''):<7} "
              f"{clip(', '.join(sc['terms']) or '-', 44)}")
        if sc.get("veto"):
            print(f"       {'':<6}{'':>5}  VETO: {sc['veto']}")

    keep = sum(v for k, v in counts.items() if k != "DROP")
    print(f"\n  bands: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    print(f"  a title pre-filter would resolve {keep} of {len(items)} "
          f"({keep * a.pace / 60:.1f} min instead of {len(items) * a.pace / 60:.1f} min)")

    if a.resolve <= 0:
        rule("STOPPED (--resolve 0). No tokens decoded, nothing downloaded.")
        return

    rule(f"STAGE 4 -- DECODING {a.resolve} TOKENS INTO REAL LINKS")
    print(f"  pacing {a.pace}s between calls, same as the collector.")
    print("  Google throttles by LATENCY, not 429s -- watch the seconds column.\n")
    try:
        from googlenewsdecoder import gnewsdecoder
    except ImportError:
        sys.exit("  googlenewsdecoder not installed -- pip install -r requirements.txt")

    picked = [it for it in items if it["title_score"]["band"] != "DROP"][:a.resolve]
    if not picked:
        picked = items[:a.resolve]
        print("  (nothing scored above DROP; tracing the first few anyway)\n")

    for it in picked:
        if "news.google.com" not in it["link"]:
            it["resolved"] = it["link"]
            print(f"  [{it['i']:>3}] no decode needed (already a real link)")
            continue
        t0 = time.time()
        try:
            res = gnewsdecoder(it["link"], interval=None)
            it["resolved"] = res.get("decoded_url") if res.get("status") else None
            err = None if it["resolved"] else res.get("message", "unknown")
        except Exception as ex:
            it["resolved"], err = None, f"{type(ex).__name__}: {ex}"
        secs = time.time() - t0
        if it["resolved"]:
            print(f"  [{it['i']:>3}] {secs:4.1f}s  -> {clip(it['resolved'], 62)}")
            print(f"        canonical_key: {rules.canonical_key(it['resolved'])[:64]}")
        else:
            print(f"  [{it['i']:>3}] {secs:4.1f}s  FAILED: {clip(err, 60)}")
        time.sleep(a.pace)

    rule("STAGE 5 -- FETCHING AND EXTRACTING THE ARTICLE BODY")
    with httpx.Client(timeout=30, follow_redirects=True,
                      headers={"User-Agent": UA}) as c:
        for it in picked:
            if not it.get("resolved"):
                continue
            t0 = time.time()
            try:
                r = c.get(it["resolved"])
                text = trafilatura.extract(r.text, include_comments=False,
                                           include_tables=False) or ""
                it["body"], code = text, r.status_code
            except Exception as ex:
                it["body"], code = "", f"ERR {type(ex).__name__}"
            secs = time.time() - t0
            n = len(it["body"])
            verdict = "OK" if n >= 400 else ("THIN" if n else "FAILED")
            print(f"\n  [{it['i']:>3}] HTTP {code}  {secs:4.1f}s  {n:>6} chars  {verdict}")
            print(f"        {clip(it['title'], 68)}")
            if n and n < 400:
                print("        NOTE: under 400 chars -- dedup will never compare this item.")
            if it["body"]:
                body = it["body"] if a.full else clip(it["body"], 300)
                for line in body.splitlines():
                    print(f"        | {line}")

    rule("STAGE 6 -- RE-SCORING, NOW WITH THE BODY")
    print("  Left: title only (stage 3). Right: title + body. The body is what")
    print("  Apps Script never sees.\n")
    print(f"  {'#':>3}  {'title-only':>12}   {'with body':>12}   change")
    for it in picked:
        if not it.get("body"):
            continue
        before = it["title_score"]
        after = rules.score(kw, it["title"], it["desc"], it["body"], has_ai=False)
        ur_after = rules.urgent(kw, it["title"], it["desc"], it["body"])
        d = after["score"] - before["score"]
        print(f"  {it['i']:>3}  {before['band']:>7}{before['score']:>5}   "
              f"{after['band']:>7}{after['score']:>5}   "
              f"{d:+d}" + ("   URGENT NOW" if ur_after and not it["title_urgent"] else ""))
        new_terms = [t for t in after["terms"] if t not in before["terms"]]
        if new_terms:
            print(f"       found only in the body: {clip(', '.join(new_terms), 52)}")
        it["final"] = after
        if ur_after:
            print(f"       urgent: {ur_after.get('target')} / "
                  f"{','.join(ur_after.get('groups', []))}")

    rule("STAGE 7 -- DEDUP: EVERY PAIR, AND WHAT WOULD MERGE")
    have = [it for it in picked if it.get("body")]
    print(f"  threshold {rules.DEDUP_THRESHOLD}  |  4-gram Jaccard on the first "
          f"{rules.BODY_CAP} chars  |  cross-host only\n")
    if len(have) < 2:
        print("  need at least 2 extracted bodies to compare "
              "-- raise --resolve and run again")
    else:
        grams = {it["i"]: rules.sim_grams(it["body"][:rules.BODY_CAP]) for it in have}
        hosts = {it["i"]: rules.host_of(it.get("resolved") or "") for it in have}
        for x in range(len(have)):
            for y in range(x + 1, len(have)):
                p, q = have[x]["i"], have[y]["i"]
                if hosts[p] == hosts[q]:
                    print(f"  {p:>3} + {q:<3}  skipped, same host ({hosts[p]})")
                    continue
                s = rules.jaccard(grams[p], grams[q])
                verdict = ("MERGE" if s >= rules.DEDUP_THRESHOLD else
                           "grey band" if s >= 0.20 else "")
                print(f"  {p:>3} + {q:<3}  {s:.3f}  {verdict}")

    rule("DONE -- nothing was written, nothing was sent")
    print("  corpus.db was never opened. No Telegram, no database, no state.")
    print()
    print("  Try next:")
    print('    python trace.py --query "திருவள்ளூர்" --lang ta --window 1d')
    print('    python trace.py --query "தவெக" --lang ta --window 6h --resolve 0')
    print('    python trace.py --query "Tamil Nadu AI minister" --window 7d')


if __name__ == "__main__":
    main()