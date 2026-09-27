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
    """Answers by looking at the title: 'X:' prefix sets the category, '!' sets priority 1,
    '~' priority 3, '#7' story 7, '(+)' positive, '(-)' critical. Handles the cluster and
    summary calls too."""

    def __init__(self, fail=False, shared=None):
        self.calls, self.fail, self.model = 0, fail, "fake"
        self.dead = None
        self.kinds = []
        self.shared = shared or {}           # cluster tag -> the "shared" names the fake claims

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
            m = re.search(r"#(\d+)", t)
            out[c["id"]] = {"category": cat, "priority": 1 if "!" in t else (3 if "~" in t else 2), "reason": "fake",
                            "story": f"{self.calls}:{m.group(1)}" if m else None,
                            "impact": 9 if "!" in t else 5,
                            "sentiment": "positive" if "(+)" in t else ("critical" if "(-)" in t or "!" in t else "neutral")}
        return out

    def call(self, prompt, schema, label="call"):
        self.calls += 1
        self.kinds.append(label)
        if self.fail:
            self.dead = "simulated failure"
            return None
        if label == "summary":               # two takeaways per section, each on the section's first story
            groups = []
            for name, body in re.findall(r"^## (.+?) \(.*?\n((?:\[.*\n?)+)", prompt, re.M):
                n = int(re.match(r"\[(\d+)\]", body).group(1))
                groups.append({"section": name, "takeaways": [{"text": f"{name} takeaway one.", "story": n},
                                                              {"text": f"{name} takeaway two.", "story": n}]})
            return {"groups": groups}
        if label == "cluster":               # items whose titles share a '@word' tag are one story
            items = re.findall(r"^(\d+)\. \[.*?\] (.*)$", prompt, re.M)
            by_tag = {}
            for n, t in items:
                m = re.search(r"@(\w+)", t)
                if m:
                    by_tag.setdefault(m.group(1), []).append(int(n))
            return {"groups": [{"items": ns, "shared": self.shared.get(tag, tag)} for tag, ns in by_tag.items() if len(ns) > 1]}
        return None


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
def the_web_edition_is_rolling_not_the_telegram_delta():
    import web
    con = make_db("edition.db")
    old = add(con, "Con story: covered by the morning brief", hours_ago=5)
    con.execute("UPDATE items SET digested_at=?, ai_category='constituency', ai_priority=2, ai_processed_at=?, "
                "ai_sentiment='critical', ai_impact=7 WHERE id=?",
                (alerts.iso(T0 - timedelta(hours=1)), alerts.iso(T0 - timedelta(hours=1)), old))
    add(con, "Con story: too old for the page", hours_ago=40)
    add(con, "Dis story: brand new", hours_ago=0, tags=["district"])
    con.commit()
    cands, *_ = digest.candidates(con, T0)
    check([c["title"] for c in cands] == ["Dis story: brand new"], "Telegram sees only the new item")
    eds = web.publish(con, {}, T0, None, docs=TMP / "docs-ed")
    titles = sorted(c["title"] for c in eds["latest"]["chosen"])
    check(titles == ["Con story: covered by the morning brief", "Dis story: brand new"], f"the page keeps both: {titles}")
    byt = {c["title"]: c for c in eds["latest"]["cands"]}
    check(byt["Con story: covered by the morning brief"]["by"] == "gemini (cached)" and
          byt["Con story: covered by the morning brief"]["sentiment"] == "critical", "stored verdict and sentiment reused")
    check(byt["Dis story: brand new"]["by"] == "rules", "unranked item ranked by the rules on the page")
    check("2026-10-15" in eds and "2026-10-14" in eds, "today's and yesterday's dated editions")
    data = json.loads((TMP / "docs-ed" / "data" / "latest.json").read_text(encoding="utf-8"))
    check({s["sentiment"] for s in data["stories"]} == {"critical", "neutral"}, data["stories"])


@test
def cluster_call_merges_tamil_and_english_reports_and_is_not_repeated():
    import web
    con = make_db("cluster.db")
    add(con, "Con story @drain: Oct 10 set as deadline for drain desilting in Chennai", host="newindianexpress.com")
    add(con, "Con story @drain: சென்னையில் வடிகால் பணிகள் அக்டோபர் 10-க்குள்: ககன்தீப் சிங் பேடி", host="maalaimalar.com")
    add(con, "Con story: Velachery lake desilting tender floated", host="dtnext.in")
    g = FakeGemini(shared={"drain": "drain desilting, வடிகால்"})
    eds = web.publish(con, {"GEMINI_API_KEY": "x"}, T0, g, docs=TMP / "docs-cl")
    lat = eds["latest"]
    check(len(lat["chosen"]) == 2, f"two stories, not three: {[c['title'][:30] for c in lat['chosen']]}")
    drain = next(c for c in lat["chosen"] if "@drain" in c["title"])
    check(len(drain["sources"]) == 2, "the Tamil and English reports pooled")
    check(len(drain["reports"]) == 2 and {r["lang"] for r in drain["reports"]} == {"Tamil", "English"}, drain["reports"])
    data = json.loads((TMP / "docs-cl" / "data" / "latest.json").read_text(encoding="utf-8"))
    st = next(x for x in data["stories"] if "@drain" in x["title"])
    check(len(st["reports"]) == 1 and st["reports"][0]["url"] != st["url"] and st["reports"][0]["outlet"], "the other outlet, with its own link")
    check(lat["stats"].get("clusters") == 1, lat["stats"])
    keys = con.execute("SELECT story_key FROM items WHERE title LIKE '%@drain%'").fetchall()
    check(keys[0][0] and keys[0][0] == keys[1][0], "story keys stored on both items")
    n = g.calls
    eds2 = web.publish(con, {"GEMINI_API_KEY": "x"}, T0 + timedelta(minutes=30), g, docs=TMP / "docs-cl")
    check(len(eds2["latest"]["chosen"]) == 2, "still merged from the stored keys")
    check(g.calls == n, "no new cluster or summary calls when nothing changed")


@test
def merging_is_strict_numbers_never_link_and_links_never_chain():
    import web
    con = make_db("strict.db")
    # 1. A Tamil/English pair Gemini groups: merged only with the same category. Numbers and
    #    dates on their own (October 10 here) link nothing any more.
    add(con, "Con story @g1: Oct 10 set as deadline for drain desilting in Chennai", host="newindianexpress.com")
    add(con, "Dis story @g1: வடிகால் பணிகள் அக்டோபர் 10-க்குள்: ககன்தீப் சிங் பேடி", host="maalaimalar.com")
    # 2. Two different incidents Gemini lumps under one tag (tags carry a digit so they
    #    are not headline words themselves) (the "+19 more outlets" bug):
    #    same category, no headline word in common -> refused.
    add(con, "Dis story @g2: Avadi shopkeeper Ramesh murdered over land dispute", host="dtnext.in")
    add(con, "Dis story @g2: Sewage floods Kanchipuram bank branch, customers turned away", host="thehindu.com")
    # 3. The same outlet twice under one tag -> two stories (a follow-up, or Gemini's slip).
    add(con, "Con story @g3: Velachery lake desilting begins near Ram Nagar", host="dinamalar.com")
    add(con, "Con story @g3: Velachery lake desilting: residents of Ram Nagar want silt removed", host="dinamalar.com")
    # 4. A genuine same-language pair: shared names, different outlets -> one card, one report per outlet.
    add(con, "Con story @g4: Tharamani MRTS station gets new lift after 4 years", host="thehindu.com")
    add(con, "Con story @g4: New lift at Tharamani MRTS station opens", host="dtnext.in")
    add(con, "Con story @g4: Tharamani MRTS lift finally working, say commuters", host="dtnext.in")
    g = FakeGemini(shared={"g1": "drain, வடிகால்", "g2": "Avadi, Kanchipuram", "g3": "Velachery, Ram Nagar",
                           "g4": "Tharamani, MRTS"})
    eds = web.publish(con, {"GEMINI_API_KEY": "x"}, T0, g, docs=TMP / "docs-strict")
    titles = sorted(c["title"] for c in eds["latest"]["cands"])
    drain = [t for t in titles if "@g1" in t]
    check(len(drain) == 2, f"different categories: the Tamil/English pair stays apart: {drain}")
    lump = [t for t in titles if "@g2" in t]
    check(len(lump) == 2, f"Avadi murder and Kanchipuram sewage never merge: {lump}")
    twice = [t for t in titles if "@g3" in t]
    check(len(twice) == 2, f"the same outlet twice is two stories: {twice}")
    ok = [c for c in eds["latest"]["cands"] if "@g4" in c["title"]]
    check(len(ok) == 1 and sorted(ok[0]["sources"]) == ["Dtnext", "Thehindu"], f"the real pair merged: {[c['sources'] for c in ok]}")
    check(len(ok[0]["reports"]) == 2 and len({r["outlet"] for r in ok[0]["reports"]}) == 2, f"one report per outlet: {ok[0]['reports']}")
    # 5. A Gemini group that is too big, or whose "shared" names are not in the items, is refused whole.
    con = make_db("strict2.db")
    for i in range(7):
        add(con, f"Dis story @g5: {t('Big', i)} Tiruttani", host=f"outlet{i}.com")
    add(con, "Dis story @g6: Ponneri bridge work stalls, Tiruttani lorry drivers protest", host="thehindu.com")
    add(con, "Dis story @g6: Ambattur estate power cut for six hours, Tiruttani", host="dtnext.in")
    g = FakeGemini(shared={"g5": "Tiruttani", "g6": "Gummidipoondi"})
    eds = web.publish(con, {"GEMINI_API_KEY": "x"}, T0, g, docs=TMP / "docs-strict2")
    check(len(eds["latest"]["cands"]) == 9, f"a 7-item group and an unproven group are refused: {len(eds['latest']['cands'])}")
    # 6. Links never chain: A~B and B~C do not make A~C. Three items, two gemini keys of which
    #    only the middle one is shared by both neighbours -- in _merge each joins the REP only.
    mk = lambda i, title, story, outlet: {"id": i, "title": title, "publisher": "", "description": "", "extract_status": "FAILED",
                                          "extract_text": None, "published_at": "2026-10-15T05:00:00+00:00", "category": "district",
                                          "priority": 2, "urgent": 0, "score": 10 - i, "sources": [outlet], "story": story,
                                          "reports": [{"outlet": outlet, "url": f"u{i}", "title": title, "lang": "English"}],
                                          "ntitle": digest.norm_title(title), "tgrams": frozenset()}
    a = mk(1, "Poondi reservoir level rises after rain", "k1", "A")
    b = mk(2, "Poondi reservoir: Gummidipoondi farmers ask for water", "k1", "B")
    c = mk(3, "Gummidipoondi SIPCOT unit fined for effluent", "k1", "C")
    out = digest.merge_stories([a, b, c])
    check(len(out) == 2 and sorted(out[0]["sources"]) == ["A", "B"], f"B joins A; C shares nothing with the rep A: {[o['sources'] for o in out]}")
    # 7. numbers alone: "450 crore" and "2 km" in both, no name in common -> apart
    e = mk(4, "Rs 450 crore radial road bridge sanctioned, 2 km long", "k9", "E")
    f = mk(5, "450 crore for 2 km of new pipelines in Ambattur", "k9", "F")
    check(len(digest.merge_stories([e, f])) == 2, "shared amounts never link")
    ct = digest.core_terms({"title": "Avadi: ஆவடியில் கடை உரிமையாளர் கொலை", "publisher": ""})
    check(ct == {"avadi", "ta:ஆவடியில்", "ta:உரிமையாளர்"}, f"names kept, generic words and short words out: {ct}")
    check(digest.share_core({"core": {"ta:ஆவடி"}}, {"core": {"ta:ஆவடியில்"}}), "a case suffix does not hide a shared Tamil name")
    check(not digest.share_core({"core": {"ta:திருவள்ளூர்"}}, {"core": {"ta:திருவொற்றியூர்"}}), "Tiruvallur is not Tiruvottiyur")
    check(not digest.share_core({"core": {"ta:பூண்டி"}}, {"core": {"ta:பூண்டிகுளம்பாளையம்"}}), "a longer compound is another name")


@test
def summary_is_cached_until_the_top_stories_change():
    import web
    con = make_db("summary.db")
    add(con, "Con story (+) scheme launched")
    g = FakeGemini()
    eds = web.publish(con, {"GEMINI_API_KEY": "x"}, T0, g, docs=TMP / "docs-sum")
    sid = con.execute("SELECT id FROM items").fetchone()[0]
    check(eds["latest"]["summary"] == [{"cat": "constituency", "bullets": [{"text": "Velachery takeaway one.", "id": sid},
                                                                            {"text": "Velachery takeaway two.", "id": sid}]}],
          eds["latest"]["summary"])
    page = (TMP / "docs-sum" / "index.html").read_text(encoding="utf-8")
    check('id="story-\'+s.id' in page and "data-story" in page and "function jump(" in page, "cards carry anchors, takeaways link to them")
    n = g.kinds.count("summary")
    web.publish(con, {"GEMINI_API_KEY": "x"}, T0 + timedelta(minutes=5), g, docs=TMP / "docs-sum")
    check(g.kinds.count("summary") == n, "same top stories, no new summary call")
    add(con, "Dis story (-) flood!", hours_ago=0)
    web.publish(con, {"GEMINI_API_KEY": "x"}, T0 + timedelta(minutes=10), g, docs=TMP / "docs-sum")
    check(g.kinds.count("summary") > n, "a new top story refreshes the summary")


@test
def backfill_writes_dated_editions_and_the_index():
    import web
    con = make_db("backfill.db")
    for k in range(1, 9):
        add(con, f"Con story day{k} {WORDS[k]} {WORDS[k + 3]}", hours_ago=24 * k - 6, tags=["constituency"])
    docs = TMP / "docs-bf"
    eds = web.backfill(con, {}, T0, 7, None, docs=docs)
    check(len(eds) == 7 and all(len(e["chosen"]) == 1 for e in eds.values()), {k: len(v["chosen"]) for k, v in eds.items()})
    web.publish(con, {}, T0, None, docs=docs)
    idx = json.loads((docs / "data" / "index.json").read_text(encoding="utf-8"))
    keys = [e["key"] for e in idx["editions"]]
    check(keys[0] == "latest" and len(keys) == 8 and keys[1] == "2026-10-15", keys)
    check((docs / "data" / "2026-10-08.json").exists(), "the 7th day back exists")
    day = next(e for e in idx["editions"] if e["key"] == "2026-10-14")
    check(day["counts"] == {"constituency": {"positive": 0, "neutral": 1, "critical": 0}} and day["total"] == 1, day)


@test
def the_page_embeds_the_latest_edition_and_the_app_markup():
    import web
    con = make_db("page.db")
    a = add(con, "Con! <Flood> & drains in Velachery", body="வேளச்சேரியில் மழைநீர் தேங்கியது. " * 20)
    con.execute("UPDATE items SET image_url='https://img.example.com/flood.jpg?a=1&b=2' WHERE id=?", (a,))
    con.commit()
    docs = TMP / "docs-page"
    web.publish(con, {"GEMINI_API_KEY": "x", "FEEDBACK_URL": "https://script.google.com/macros/s/X/exec"}, T0, FakeGemini(), docs=docs)
    html = (docs / "index.html").read_text(encoding="utf-8")
    data = json.loads((docs / "data" / "latest.json").read_text(encoding="utf-8"))
    s = data["stories"][0]
    check(s["title"] == "Con! <Flood> & drains in Velachery" and s["urgent"] and s["sentiment"] == "critical" and s["impact"] == 9, s)
    check(s["image"].startswith("https://img.example.com/flood.jpg"), "image carried in the data")
    check('id="dashboard"' in html and 'id="drawer"' in html and 'id="top"' in html and 'id="missing"' in html, "app markup")
    check('data-feedback="https://script.google.com/macros/s/X/exec"' in html, "feedback endpoint")
    check('<script id="data" type="application/json">' in html and "Urgent takeaway one." in html, "latest edition and summary embedded")
    data_sum = data["summary"]
    check(data_sum[0]["cat"] == "urgent" and len(data_sum[0]["bullets"]) == 2, data_sum)
    check("</script>" not in html.split('<script id="data" type="application/json">')[1].split("</script>")[0], "safe embedding")
    check("wa.me" in web.JS and "maximum-scale=1.0" in html and "min-height:44px" in html, "share, viewport, touch targets")
    check('id="dash-week"' in html and "function matrix" in web.JS and 'class="more"' in web.JS, "7-day matrix and outlet expander")


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
    check(g.call("prompt", {}, "x") is None and "budget" in g.dead, "daily cap respected")


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
