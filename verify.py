#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Runs the two checks that were sitting on the 'do it in a browser' list.

    python verify.py

Answers:
  1. Does the Google News `when:` operator work, and how much staleness does it remove?
  2. Which YouTube channel IDs are real news channels?

Read-only. Touches nothing in corpus.db.
"""

import re
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import httpx
import feedparser

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PROBE_QUERIES = [
    ("en", "Velachery"),
    ("en", "Tamilaga Vettri Kazhagam"),
    ("ta", "\u0BB5\u0BC7\u0BB3\u0B9A\u0BCD\u0B9A\u0BC7\u0BB0\u0BBF"),   # velachery
    ("ta", "\u0BA4\u0BB5\u0BC6\u0B95"),                                  # thaveka
]

# Left column = what we resolved from the @handle and confirmed carried news.
# Right column = what the new spec asserts. Both get tested.
CHANNELS = [
    ("Polimer News",        "UC8Z-VjXBtDJTvq6aqkIskPg", "UCnU_bxeU2Uepz3oI_hC18AQ"),
    ("Puthiya Thalaimurai", "UCmyKnNRH0wH-r8I-ceP-dsg", "UCw9v9_J_fI_2fGzF6u7cTqw"),
    ("Thanthi TV",          "UCFYqIFxANnSDsnz6qDH01Hw", "UCdtAupzV3j0WV_upfftwZ0A"),
    ("Sun News",            "",                          "UC4_pA_Q7o_uXy_Jp7QfO83Q"),
]


def say(s=""):
    try:
        print(s)
    except UnicodeEncodeError:
        print(s.encode("ascii", "replace").decode())


def feed_url(lang, q, when=None):
    query = q + (f" when:{when}" if when else "")
    tail = "hl=ta&gl=IN&ceid=IN%3Ata" if lang == "ta" else "hl=en-IN&gl=IN&ceid=IN%3Aen"
    return f"https://news.google.com/rss/search?q={quote(query)}&{tail}"


def age_profile(entries):
    now = datetime.now(timezone.utc)
    ages = []
    for e in entries:
        st = e.get("published_parsed")
        if st:
            ages.append((now - datetime(*st[:6], tzinfo=timezone.utc)).days)
    if not ages:
        return None
    ages.sort()
    fresh = sum(1 for a in ages if a <= 2)
    return {"n": len(ages), "median": ages[len(ages) // 2], "max": max(ages),
            "fresh": fresh, "pct_fresh": 100 * fresh / len(ages)}


def test_when(client):
    say("=" * 76)
    say("  CHECK 1 -- does the `when:` operator fix the 59% staleness?")
    say("=" * 76)
    say("  Your corpus was 59% older than a month. If `when:` works, these numbers")
    say("  collapse and your resolver load drops with them.\n")

    verdicts = []
    for lang, q in PROBE_QUERIES:
        say(f"  [{lang}] {q}")
        row = {}
        for label, when in (("plain", None), ("when:1d", "1d"), ("when:7d", "7d")):
            try:
                r = client.get(feed_url(lang, q, when))
                p = age_profile(feedparser.parse(r.content).entries)
                row[label] = p
                if p:
                    say(f"      {label:<9} {p['n']:>3} items   median age "
                        f"{p['median']:>4}d   oldest {p['max']:>5}d   "
                        f"{p['pct_fresh']:>5.1f}% from last 48h")
                else:
                    say(f"      {label:<9} no dated items returned")
            except Exception as ex:
                say(f"      {label:<9} ERROR {type(ex).__name__}")
            time.sleep(1.5)
        if row.get("plain") and row.get("when:1d"):
            a, b = row["plain"], row["when:1d"]
            works = b["median"] <= 2 and b["median"] < a["median"]
            verdicts.append(works)
            say(f"      -> {'WORKS' if works else 'no effect'}: median age "
                f"{a['median']}d -> {b['median']}d, items {a['n']} -> {b['n']}")
        say()

    if verdicts and all(verdicts):
        say("  VERDICT: `when:` is honoured. Add it to every query in sources.json.")
        say("  Use when:1d for fast sources, when:7d for slow ones like Pallikaranai.")
    elif any(verdicts):
        say("  VERDICT: mixed. Apply `when:` only to the queries where it worked.")
    else:
        say("  VERDICT: `when:` ignored. Fall back to filtering on published_at at")
        say("  ingest -- store everything, but only alert on items under 48h old.")


def test_channels(client):
    say("\n" + "=" * 76)
    say("  CHECK 2 -- which YouTube channel IDs are real news channels?")
    say("=" * 76)
    say("  A wrong ID collects silently. Thanthi already cost us a day of TV serials.\n")

    good = {}
    for name, ours, spec in CHANNELS:
        say(f"  {name}")
        for label, cid in (("verified", ours), ("new spec", spec)):
            if not cid:
                say(f"      {label:<9} (none)")
                continue
            try:
                r = client.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
                if r.status_code != 200:
                    say(f"      {label:<9} {cid}  HTTP {r.status_code} -- DEAD ID")
                    continue
                f = feedparser.parse(r.content)
                ch = f.feed.get("title", "?")
                titles = [e.get("title", "") for e in f.entries[:3]]
                say(f"      {label:<9} {cid}")
                say(f"                channel: {ch}   ({len(f.entries)} entries)")
                for t in titles:
                    say(f"                  - {t[:62]}")
                good.setdefault(name, []).append((label, cid, ch, titles))
            except Exception as ex:
                say(f"      {label:<9} {cid}  ERROR {type(ex).__name__}")
            time.sleep(1.2)
        say()

    say("  Read the sample titles. Pick the ID whose latest uploads are NEWS, not")
    say("  serials or entertainment, and put it in sources.json with enabled: true.")


CANDIDATE_HANDLES = {
    "Thanthi TV (news, not Thanthi One)":
        ["ThanthiTVNews", "thanthitv", "ThanthiTVOfficial", "ThanthiTVLive",
         "ThanthiTv", "dailythanthi"],
    "Sun News":
        ["SunNewsTamil", "sunnews", "SunNewsOfficial", "SunNewsTV", "SunNews24x7"],
    "News7 Tamil":
        ["News7Tamil", "news7tamilprime"],
}


def find_channels(client):
    """Probe handles for the channels we still do not have a working id for."""
    say("\n" + "=" * 76)
    say("  CHECK 3 -- finding ids for the channels still missing")
    say("=" * 76)
    for label, handles in CANDIDATE_HANDLES.items():
        say(f"\n  {label}")
        for h in handles:
            try:
                r = client.get(f"https://www.youtube.com/@{h}")
                if r.status_code != 200:
                    say(f"      @{h:<22} HTTP {r.status_code}")
                    time.sleep(0.8)
                    continue
                m = re.search(r'"(?:channelId|externalId)":"(UC[0-9A-Za-z_-]{22})"', r.text)
                if not m:
                    say(f"      @{h:<22} no channelId in page")
                    time.sleep(0.8)
                    continue
                cid = m.group(1)
                f = feedparser.parse(client.get(
                    f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}").content)
                ch = f.feed.get("title", "?")
                say(f"      @{h:<22} {cid}   -> {ch}")
                for e in f.entries[:2]:
                    say(f"           - {e.get('title','')[:60]}")
            except Exception as ex:
                say(f"      @{h:<22} {type(ex).__name__}")
            time.sleep(1.2)
    say("\n  Copy the id whose uploads are news bulletins into sources.json.")


def main():
    with httpx.Client(timeout=25.0, follow_redirects=True,
                      headers={"User-Agent": UA}) as c:
        if "--find" in sys.argv:
            find_channels(c)
            return
        test_when(c)
        test_channels(c)
        find_channels(c)


if __name__ == "__main__":
    main()
