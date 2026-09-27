#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Digest tests. Synthetic data in a temp folder; no network; nothing is sent.

    python test_digest.py
"""

import json
import logging
import re
import sqlite3
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

import rules     # noqa: E402
import alerts    # noqa: E402
import digest    # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="tndigest_"))
T0 = datetime(2026, 10, 15, 1, 30, tzinfo=timezone.utc)      # 07:00 IST
LOGS = []


class _Capture(logging.Handler):
    def emit(self, record):
        LOGS.append(record.getMessage())


_root = logging.getLogger("collect")
_root.setLevel(logging.INFO)
_root.addHandler(_Capture())
_root.propagate = False

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def check(cond, msg="check failed"):
    if not cond:
        raise AssertionError(msg)


def collect_schema():
    src = (HERE / "collect.py").read_text(encoding="utf-8")
    return re.search(r'SCHEMA = """(.*?)"""', src, re.S).group(1)


def make_db(name):
    path = TMP / name
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.executescript(collect_schema())
    con.execute("INSERT INTO sources (source_id, name, kind) VALUES ('gn', 'Google News', 'google_news')")
    con.commit()
    alerts.reset_caches()
    alerts.migrate(con)
    digest.migrate(con)
    return con


_n = [0]


def add(con, title, tags=(), score=5, band="KEYWORD_KEEP", urgent=0, hours_ago=2, host="dinamalar.com",
        event=None, body="", ai=None):
    _n[0] += 1
    n = _n[0]
    t = alerts.iso(T0 - timedelta(hours=hours_ago))
    cur = con.execute("""INSERT INTO items (item_key, source_id, link, title, publisher, published_at, discovered_at,
                   rules_at, resolve_status, resolved_url, extract_status, extract_host, extract_text,
                   score, band, urgent, target_tags, event_id, ai_category, ai_priority, ai_processed_at)
                   VALUES (?, 'gn', ?, ?, ?, ?, ?, ?, 'RESOLVED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (f"k{n}", f"https://news.google.com/rss/articles/T{n}", title, host.split(".")[0].title(),
                 t, t, t, f"https://www.{host}/news/{n}", "OK" if body else "FAILED", host, body or None,
                 score, band, urgent, json.dumps(list(tags)), event,
                 ai[0] if ai else None, ai[1] if ai else None, t if ai else None))
    con.commit()
    return cur.lastrowid


class FakeGemini:
    """Answers by looking at the title: 'X:' prefix sets the category, '!' sets priority 1."""

    def __init__(self, fail=False):
        self.calls, self.fail, self.model = 0, fail, "fake"
        self.dead = None

    def rank(self, batch):
        self.calls += 1
        if self.fail:
            self.dead = "simulated failure"
            return None
        out = {}
        for c in batch:
            t = c["title"]
            cat = "none"
            for k in digest.CATEGORIES:
                if t.lower().startswith(k[:3]):
                    cat = k
            pri = 1 if "!" in t else (3 if "~" in t else 2)
            m = re.search(r"#(\d+)", t)                       # '#7' in a title = story number 7
            out[c["id"]] = (cat, pri, "fake reason", f"{self.calls}:{m.group(1)}" if m else None)
        return out


WORDS = ("lake road bridge sewage metro school hospital market temple bus drain power water rain tender "
         "canal flyover garbage clinic library station stadium beach park bank".split())


def t(prefix, i):
    """Distinct synthetic headlines: similar-looking titles would otherwise merge as one
    story, which is exactly what the merge tests check."""
    import random
    words = random.Random(f"{prefix}-{i}").sample(WORDS, 6)
    return f"{prefix} story {i}: " + " ".join(words)


def build(con, gemini, now=None, per=5, total=30):
    now = now or T0
    cands, all_ids, since = digest.candidates(con, now)
    st = digest.rank_all(con, cands, gemini, now)
    before = len(cands)
    cands = digest.merge_stories(cands)
    st["merged"] = before - len(cands)
    urgent, sections, left = digest.select(cands, per, total)
    chosen = urgent + [c for cat in digest.CATEGORIES for c in sections[cat]]
    return cands, chosen, urgent, sections, left, st


# --------------------------------------------------------------------------

@test
def five_per_category_then_the_balance_goes_elsewhere():
    con = make_db("select.db")
    for i in range(9):
        add(con, t("Con", i))            # constituency: 9 candidates
    for i in range(2):
        add(con, t("Dis", i))            # district: 2
    for i in range(12):
        add(con, t("Por", i))            # portfolio: 12
    for i in range(3):
        add(con, t("Non", i))            # none: never shown
    cands, chosen, urgent, sections, left, st = build(con, FakeGemini(), total=15)
    counts = {k: len(v) for k, v in sections.items() if v}
    check(len(chosen) == 15, f"total capped at 15, got {len(chosen)}")
    check(counts["district"] == 2, "district has only 2 -- its spare places move")
    check(counts["constituency"] + counts["portfolio"] == 13, f"the balance filled from the others: {counts}")
    check(counts["constituency"] >= 5 and counts["portfolio"] >= 5, "each got its 5 first")
    check(all(c["category"] != "none" for c in chosen), "no 'none' items")
    # with the full 30 cap everything but 'none' fits
    cands, chosen, *_ = build(con, FakeGemini(), total=30)
    check(len(chosen) == 23, f"all 23 real stories fit under 30, got {len(chosen)}")


@test
def urgent_civic_items_go_on_top_and_priority_3_only_fills_gaps():
    con = make_db("urgent.db")
    a = add(con, "Con! Velachery flooded", urgent=1, tags=["constituency"])
    add(con, "Con story calm")
    add(con, "Por~ background piece")
    for i in range(6):
        add(con, t("Dis", i))
    cands, chosen, urgent, sections, left, st = build(con, FakeGemini(), per=5, total=9)
    check([c["id"] for c in urgent] == [a], "the flood is the urgent block")
    check(a not in [c["id"] for c in sections["constituency"]], "and not repeated in its section")
    ids = [c["id"] for c in chosen]
    check(len(ids) == 9, f"9 chosen: {len(ids)}")
    check(len(sections["district"]) == 6, "the 6th district story took the first spare place (priority 2)")
    check(any("background" in c["title"] for c in chosen), "the priority-3 item filled the last place")
    _, chosen8, *_ = build(con, FakeGemini(), per=5, total=8)
    check(not any("background" in c["title"] for c in chosen8), "with one place fewer, priority 3 loses it")
    # priority 3 never takes a place in the first pass
    con2 = make_db("p3.db")
    add(con2, "Por~ old background")
    add(con2, "Por fresh news")
    _, chosen, *_ = build(con2, FakeGemini(), per=1, total=1)
    check(chosen[0]["title"] == "Por fresh news", "priority 2 beats priority 3 for the one place")


@test
def nothing_repeats_across_sends_and_unchosen_items_are_not_carried_over():
    con = make_db("repeat.db")
    for i in range(8):
        add(con, t("Con", i))
    rec = alerts.Recorder()
    env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}
    saved = digest.Gemini
    digest.Gemini = lambda *a, **k: FakeGemini()
    try:
        b1 = digest.run(con, dict(env, GEMINI_API_KEY="x", DIGEST_MAX="3", DIGEST_PER_CATEGORY="3"),
                        now=T0, slot="morning", transport=rec)
        check(len(b1["chosen"]) == 3 and len(rec.sent) == 1, "first brief: 3 items, one message")
        check(con.execute("SELECT count(*) FROM items WHERE digested_at IS NOT NULL").fetchone()[0] == 8,
              "all 8 candidates marked, chosen or not")
        add(con, "Con story new", hours_ago=0)
        b2 = digest.run(con, dict(env, GEMINI_API_KEY="x"), now=T0 + timedelta(hours=11), slot="evening",
                        transport=rec)
        titles = [c["title"] for c in b2["chosen"]]
        check(titles == ["Con story new"], f"only the new story: {titles}")
    finally:
        digest.Gemini = saved


@test
def without_gemini_the_rules_rank_and_on_failure_they_take_over():
    con = make_db("fallback.db")
    add(con, "வேளச்சேரியில் வெள்ளம்", tags=["constituency"], urgent=1)
    add(con, "Thiruvallur collector meeting", tags=["district"], band="AUTO_KEEP")
    add(con, "TN AI mission", tags=["portfolio"])
    add(con, "TVK vs DMK spat", tags=["party"])
    cands, chosen, urgent, sections, left, st = build(con, None)
    cats = {c["title"]: (c["category"], c["priority"]) for c in cands}
    check(cats["வேளச்சேரியில் வெள்ளம்"] == ("constituency", 1), cats)
    check(cats["Thiruvallur collector meeting"] == ("district", 2), cats)
    check(cats["TVK vs DMK spat"] == ("political", 3), cats)
    check(len(urgent) == 1, "rule-urgent item is on top")
    check(st["rules"] == 4 and not st.get("gemini"), st)
    cands, chosen, *_ , st = build(con, FakeGemini(fail=True))
    check(st["rules"] == 4, "gemini failed -> every item ranked by the rules")
    check(any("stopped" in m for m in LOGS), "and the log says so")


@test
def gemini_verdicts_are_cached_and_reused():
    con = make_db("cache.db")
    add(con, "Con story fresh")
    add(con, "Ignored title", ai=("district", 1))
    g = FakeGemini()
    cands, chosen, *_ , st = build(con, g)
    check(g.calls == 1 and st["gemini"] == 1, "one call for the one un-ranked item")
    byt = {c["title"]: c for c in cands}
    check(byt["Ignored title"]["category"] == "district" and byt["Ignored title"]["by"] == "gemini (cached)")
    check(con.execute("SELECT ai_category FROM items WHERE title='Con story fresh'").fetchone()[0] == "constituency",
          "the verdict is stored on the item")


@test
def one_story_per_event_with_a_source_count():
    con = make_db("event.db")
    con.execute("INSERT INTO events (event_id, status) VALUES (7, 'OPEN')")
    add(con, "Con story A", event=7, score=4, host="dinamalar.com")
    add(con, "Con story B", event=7, score=6, host="dailythanthi.com")
    add(con, "Con story C", event=7, score=5, host="maalaimalar.com")
    cands, chosen, *_ = build(con, FakeGemini())
    check(len(cands) == 1 and cands[0]["title"] == "Con story B", "the best-scored report represents the event")
    check(len(cands[0]["sources"]) == 3, "three outlets counted")
    line = digest._line(cands[0])
    check("+2" in line, f"shown as +2: {line}")


@test
def same_headline_and_same_story_collapse_to_one_candidate():
    con = make_db("merge.db")
    # word-for-word the same headline from two feeds, different urls, no event
    add(con, "Con story: Metro line to Velachery opens on Oct 11", host="etvbharat.com")
    add(con, "Con story: Metro line to Velachery opens on Oct 11", host="etvbharat.com")
    add(con, "Con story: Metro line to Velachery opens on Oct 11 - ETV Bharat", host="dinamani.com")
    cands, *_ = digest.candidates(con, T0)
    check(len(cands) == 1 and len(cands[0]["sources"]) == 2, f"one story, outlets pooled: {len(cands)}")
    # near-identical headlines merge; Gemini's story number merges the differently framed one
    con = make_db("merge2.db")
    add(con, "Dis story #4: PM inaugurates Chennai Metro extension to Poonamallee", host="thehindu.com")
    add(con, "Dis story #4: PM inaugurates Chennai Metro extension to Poonamallee today", host="dtnext.in")
    add(con, "Pol story #4: Opposition slams timing of Metro launch", host="news18.com")
    add(con, "Opp story: StartupTN summit", host="yourstory.com")
    cands, chosen, urgent, sections, left, st = build(con, FakeGemini())
    check(len(cands) == 2, f"two stories after merging: {[c['title'] for c in cands]}")
    metro = next(c for c in cands if "Metro" in c["title"])
    check(len(metro["sources"]) == 3, "three outlets pooled")
    check(sum(len(v) for v in sections.values()) + len(urgent) == 2, "and the story appears once, in one category")
    check(st["merged"] == 2, st)
    # a real story is never swallowed by a merely similar-topic headline
    con = make_db("merge3.db")
    add(con, "Con story: Velachery lake desilting begins", host="thehindu.com")
    add(con, "Con story: Velachery flyover work delayed", host="dtnext.in")
    cands, *_ = digest.candidates(con, T0)
    cands = digest.merge_stories(cands)
    check(len(cands) == 2, "different stories stay separate")


@test
def the_web_page_is_a_rolling_edition_not_the_telegram_delta():
    con = make_db("edition.db")
    old = add(con, "Con story: covered by the morning brief", hours_ago=5)
    con.execute("UPDATE items SET digested_at=?, ai_category='constituency', ai_priority=2, ai_processed_at=? WHERE id=?",
                (alerts.iso(T0 - timedelta(hours=1)), alerts.iso(T0 - timedelta(hours=1)), old))
    add(con, "Con story: too old for the page", hours_ago=40)
    add(con, "Dis story: brand new", hours_ago=0, tags=["district"])
    con.commit()
    cands, *_ = digest.candidates(con, T0)
    check([c["title"] for c in cands] == ["Dis story: brand new"], "Telegram sees only the new item")
    ed = digest.build_edition(con, {}, T0)
    titles = sorted(c["title"] for c in ed["chosen"])
    check(titles == ["Con story: covered by the morning brief", "Dis story: brand new"], f"the page keeps both: {titles}")
    byt = {c["title"]: c for c in ed["cands"]}
    check(byt["Con story: covered by the morning brief"]["by"] == "gemini (cached)", "stored verdict reused, no calls")
    check(byt["Dis story: brand new"]["by"] == "rules", "unranked item ranked by the rules on the page")


@test
def the_web_page_has_cards_search_data_feedback_and_editions():
    import shutil
    con = make_db("page.db")
    a = add(con, "Con! <Flood> & drains in Velachery", body="வேளச்சேரியில் மழைநீர் தேங்கியது. " * 20)
    con.execute("UPDATE items SET image_url='https://img.example.com/flood.jpg?a=1&b=2' WHERE id=?", (a,))
    con.execute("""UPDATE items SET raw_payload=?, resolved_url='https://www.youtube.com/watch?v=abcdefghijk'
                   WHERE id=?""", (json.dumps({"via": "youtube_api", "snippet": {"thumbnails": {"medium": {"url": "https://i.ytimg.com/vi/x/mq.jpg"}}}}),
                                    add(con, "Por story video", host="youtube.com")))
    con.commit()
    docs = TMP / "docs"
    shutil.rmtree(docs, ignore_errors=True)
    saved = digest.Gemini
    digest.Gemini = lambda *a, **k: FakeGemini()
    try:
        digest.run(con, {"GEMINI_API_KEY": "x"}, now=T0, slot="morning", dry_run=True)     # ranks + caches
    finally:
        digest.Gemini = saved
    ed = digest.build_edition(con, {}, T0)
    digest.write_pages(ed, "morning", T0, docs, feedback_url="https://script.google.com/macros/s/X/exec")
    digest.write_pages(ed, "evening", T0 + timedelta(hours=11), docs, feedback_url="https://script.google.com/macros/s/X/exec")
    html = (docs / "index.html").read_text(encoding="utf-8")
    check("&lt;Flood&gt; &amp; drains" in html and "<Flood>" not in html, "escaped headline")
    check('src="https://img.example.com/flood.jpg?a=1&amp;b=2"' in html, "og:image thumbnail")
    check("i.ytimg.com" in html, "youtube thumbnail from the API payload")
    check('class="chip u">URGENT' in html, "urgent chip")
    check('data-text="' in html and 'data-cat="constituency"' in html, "search data on the cards")
    check('class="fb" data-id=' in html and 'data-r="unrelated"' in html, "feedback buttons with reasons")
    check('id="missing"' in html and 'data-feedback="https://script.google.com/macros/s/X/exec"' in html, "missing-news form + endpoint")
    check('<select id="edition"' in html and "briefs/2026-10-15-evening.html" in html, "edition selector")
    check("2026-10-15-morning.html" not in html, "one entry per day: the later edition of the day")
    check('maximum-scale=1.0' in html and "min-height:44px" in html, "mobile viewport and touch targets")
    arch = (docs / "briefs" / "2026-10-15-evening.html").read_text(encoding="utf-8")
    check('value="../index.html"' in arch and 'value="../briefs/2026-10-15-evening.html" selected' in arch, "archive pages link back")
    check((docs / ".nojekyll").exists())
    plain = digest.write_pages(ed, "morning", T0, docs)                     # no endpoint set
    check('data-feedback=""' in plain.read_text(encoding="utf-8"), "without an endpoint the page carries none")


@test
def rendering_escapes_html_and_stays_under_the_limit():
    con = make_db("render.db")
    for i in range(30):
        add(con, t("Con", i) + f": வேளச்சேரியில் <கனமழை> {i} & \"மழைநீர்\" தேங்கியது " + " ".join(
            __import__("random").Random(i).sample(["ஏரி", "சாலை", "பாலம்", "கழிவுநீர்", "மெட்ரோ", "பள்ளி", "மருத்துவமனை",
                                                    "சந்தை", "கோயில்", "பேருந்து", "வடிகால்", "மின்சாரம்", "தண்ணீர்", "மழை"], 8)),
            body="x" * 500)
    cands, chosen, urgent, sections, left, st = build(con, FakeGemini(), per=30, total=30)
    msgs = digest.render("morning", T0, urgent, sections, len(chosen), len(cands), True)
    check(len(msgs) >= 2, f"30 long Tamil lines need more than one message: {len(msgs)}")
    for m in msgs:
        check(rules.utf16_len(m) <= 4096, "under Telegram's limit")
        check("<கனமழை>" not in m and "&lt;கனமழை&gt;" in m, "angle brackets escaped")
        check("&amp;" in m, "ampersand escaped")
        check(re.search(r"\(\d/\d\)", m), "numbered parts")
        check(m.count("<a href=") == m.count("</a>"), "links balanced")
    joined = "\n".join(msgs)
    check(joined.count("<a href=") == 30, "every item has its link")
    check("Morning brief · 15 Oct · 30 items" in msgs[0], "heading")
    empty = digest.render("evening", T0, [], {c: [] for c in digest.CATEGORIES}, 0, 0, False)
    check(len(empty) == 1 and "Nothing worth reporting" in empty[0], "empty brief says so")


@test
def dry_run_and_missing_telegram_mark_nothing_and_send_nothing():
    con = make_db("dry.db")
    add(con, "Con story")
    rec = alerts.Recorder()
    saved = digest.Gemini
    digest.Gemini = lambda *a, **k: FakeGemini()
    try:
        digest.run(con, {"GEMINI_API_KEY": "x"}, now=T0, dry_run=True, transport=rec)
        check(not rec.sent, "dry run sends nothing")
        digest.run(con, {}, now=T0)                      # no token: auto transport is None
    finally:
        digest.Gemini = saved
    check(con.execute("SELECT count(*) FROM items WHERE digested_at IS NOT NULL").fetchone()[0] == 0,
          "nothing marked")
    check(any("not configured" in m for m in LOGS), "the log explains")


@test
def a_failed_send_marks_nothing_so_the_next_run_retries():
    con = make_db("fail.db")
    add(con, "Con story")

    class Broken:
        def send(self, text, plain=False):
            return alerts.Result(False, error="boom", kind="server")
    saved = digest.Gemini
    digest.Gemini = lambda *a, **k: FakeGemini()
    try:
        digest.run(con, {"GEMINI_API_KEY": "x", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"},
                   now=T0, transport=Broken())
    finally:
        digest.Gemini = saved
    check(con.execute("SELECT count(*) FROM items WHERE digested_at IS NOT NULL").fetchone()[0] == 0)
    check(con.execute("SELECT status FROM digests").fetchone()[0] == "FAILED")
    check(alerts.rt_get(con, "last_digest_at") is None, "window not advanced")


@test
def old_stories_and_dropped_items_are_not_candidates():
    con = make_db("window.db")
    add(con, "Con story old", hours_ago=40)                 # published 40 h ago
    add(con, "Con story dropped", band="DROP")
    add(con, "Con story dropped but urgent", band="DROP", urgent=1)
    add(con, "Con story fine")
    cands, *_ = digest.candidates(con, T0)
    titles = sorted(c["title"] for c in cands)
    check(titles == ["Con story dropped but urgent", "Con story fine"], titles)


@test
def slot_and_budget():
    check(digest.slot_for(T0) == "morning" and digest.slot_for(T0 + timedelta(hours=11)) == "evening")
    con = make_db("budget.db")
    g = digest.Gemini("key", "m", con, max_calls=1)
    con.execute("INSERT INTO ai_budget (day, calls, tokens) VALUES (?, 1, 0)", (digest.utcnow().strftime("%Y-%m-%d"),))
    con.commit()
    check(g.rank([{"id": 1}]) is None and "budget" in g.dead, "daily cap respected")


def main():
    only = sys.argv[sys.argv.index("-k") + 1] if "-k" in sys.argv else ""
    passed = failed = 0
    for fn in TESTS:
        if only and only not in fn.__name__:
            continue
        try:
            fn()
            passed += 1
            print(f"PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{passed + failed} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
