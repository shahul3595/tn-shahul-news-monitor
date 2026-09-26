#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 1 tests. Synthetic data in a temporary folder; no network; nothing is sent.
corpus.db, collect.log, .env and the Google Sheet are never touched.

    python test_phase1.py              run everything (about half a minute)
    python test_phase1.py -k urgent    only tests whose name contains "urgent"
"""

import csv
import io
import itertools
import json
import logging
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
import types
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

import rules    # noqa: E402
import alerts   # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="tnintel_test_"))
T0 = datetime(2026, 10, 15, 2, 0, tzinfo=timezone.utc)          # 07:30 IST, monsoon
NO_ENV = {}                                                      # never the real .env

# alerts log to "collect.alerts"; capture it instead of printing or writing collect.log
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


# --------------------------------------------------------------------------
# synthetic corpus
# --------------------------------------------------------------------------

def collect_schema():
    src = (HERE / "collect.py").read_text(encoding="utf-8")
    return re.search(r'SCHEMA = """(.*?)"""', src, re.S).group(1)


SOURCES = [("gn_ta_velachery", "Google News TA - velachery", "google_news"),
           ("gn_en_velachery", "Google News EN - velachery", "google_news"),
           ("yt_polimer", "Polimer News", "youtube"),
           ("gn_ta_tvk", "Google News TA - thaveka", "google_news")]
_seq = itertools.count(1)


def make_db(name, migrate=True):
    path = TMP / name
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.executescript(collect_schema())
    con.executemany("INSERT INTO sources (source_id, name, kind) VALUES (?,?,?)", SOURCES)
    con.commit()
    if migrate:
        alerts.migrate(con)
    return con


def ts(minutes):
    return alerts.iso(T0 + timedelta(minutes=minutes))


def add(con, title, body="", desc="", host="dinamalar.com", publisher=None, minutes=0, pub_minutes=None,
        url=None, source="gn_ta_velachery", resolve="RESOLVED", extract=None, link=None):
    n = next(_seq)
    publisher = publisher if publisher is not None else host.split(".")[0].title()
    url = url or f"https://www.{host}/news/{n}"
    if extract is None:
        extract = "OK" if len(body) >= 400 else ("THIN" if body else "FAILED")
    link = link or f"https://news.google.com/rss/articles/TOKEN{n}?oc=5"
    raw = json.dumps({"title": title, "source": {"href": f"https://www.{host}", "title": publisher}})
    cur = con.execute(
        """INSERT INTO items (item_key, source_id, link, title, description, publisher, published_at,
               discovered_at, raw_payload, resolve_status, resolved_url, extract_status, extract_host,
               extract_chars, extract_text)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"key{n}", source, link, f"{title} - {publisher}" if publisher else title, desc, publisher,
         ts(minutes if pub_minutes is None else pub_minutes), ts(minutes), raw, resolve,
         None if resolve == "FAILED" else url, extract, host, len(body), body or None))
    con.commit()
    return cur.lastrowid


FLOOD = ("சென்னை: வேளச்சேரியில் நேற்று இரவு பெய்த கனமழையால் விஜயநகர், ராம் நகர், டான்சி நகர் உள்ளிட்ட "
         "தாழ்வான பகுதிகளில் மழைநீர் தேங்கியது. பல வீடுகளுக்குள் தண்ணீர் புகுந்ததால் பொதுமக்கள் கடும் "
         "அவதியடைந்தனர். மாநகராட்சி ஊழியர்கள் மோட்டார் பம்புகள் மூலம் நீரை அகற்றும் பணியில் இரவு முழுவதும் "
         "ஈடுபட்டனர். நாராயணபுரம் ஏரி நிரம்பியதால் உபரி நீர் திறந்து விடப்பட்டது. வேளச்சேரி - தாம்பரம் "
         "சாலையில் போக்குவரத்து பாதிக்கப்பட்டது. அடுத்த இரண்டு நாட்களுக்கு கனமழை தொடரும் என சென்னை வானிலை "
         "ஆய்வு மையம் தெரிவித்துள்ளது. பாதிக்கப்பட்ட மக்கள் தங்குவதற்காக அருகிலுள்ள பள்ளிகளில் முகாம்கள் "
         "அமைக்கப்பட்டுள்ளன.")


def flood_variant(k):
    """Same story, lightly re-edited by another desk: similarity well above 0.45."""
    heads = ["சென்னை: ", "வேளச்சேரி, அக்.15: ", "சென்னை, அக்.15 (செய்தியாளர்): ", "தென் சென்னை: "]
    tails = ["", " மாநகராட்சி ஆணையர் நேரில் ஆய்வு செய்தார்.", " அமைச்சர்கள் பார்வையிட்டனர்.",
             " மக்கள் பாதுகாப்பாக இருக்க அறிவுறுத்தப்பட்டனர்."]
    return heads[k % 4] + FLOOD[len("சென்னை: "):] + tails[k % 4]


POLITICS = ("சென்னை: தமிழக வெற்றிக் கழகத்தின் மாவட்ட நிர்வாகிகள் கூட்டம் பனையூரில் உள்ள கட்சி அலுவலகத்தில் "
            "நடைபெற்றது. இதில் வரவிருக்கும் உள்ளாட்சித் தேர்தலுக்கான பணிகள் குறித்து விரிவாக ஆலோசிக்கப்பட்டது. "
            "ஒவ்வொரு வார்டிலும் பூத் கமிட்டிகளை வலுப்படுத்த வேண்டும் என்று நிர்வாகிகளுக்கு அறிவுறுத்தப்பட்டது. "
            "உறுப்பினர் சேர்க்கை முகாம்களை அடுத்த மாதம் முதல் தொடங்கவும் முடிவு செய்யப்பட்டது. கூட்டத்தில் "
            "பல்வேறு மாவட்டங்களைச் சேர்ந்த நிர்வாகிகள் கலந்து கொண்டு தங்கள் கருத்துகளைத் தெரிவித்தனர்.")

KW = rules.keywords_from_file()


def queue(con, kind="URGENT"):
    return con.execute("SELECT * FROM alert_queue WHERE kind=? ORDER BY id", (kind,)).fetchall()


def process(con, minutes, has_ai=True):
    return alerts.process_ready(con, KW, T0 + timedelta(minutes=minutes), has_ai=has_ai)


def set_cutoff(con, minutes=-600):
    alerts.rt_set(con, "alert_cutoff_at", ts(minutes))
    con.commit()


class Scripted:
    """A Telegram double that returns the results it is given, in order."""

    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def send(self, text, plain=False):
        self.calls.append(("send", text, plain))
        return self.results.pop(0) if self.results else alerts.Result(True, message_id=len(self.calls) + 500)

    def edit(self, message_id, text, plain=False):
        self.calls.append(("edit", message_id, text, plain))
        return self.results.pop(0) if self.results else alerts.Result(True, message_id=message_id)


def deliver(con, transport, minutes):
    return alerts.deliver(con, transport, now_fn=lambda: T0 + timedelta(minutes=minutes), budget_s=5,
                          sleep=lambda s: None, pace_s=0)


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------

@test
def similarity_is_identical_to_the_calibration_code():
    import analyse
    samples = ["", "Velachery!!  flood  ", "வேளச்சேரியில்\u200c மழைநீர்\u200d தேங்கியது — 3.5 செ.மீ.",
               "Minister R. Kumar's AI-policy (2026) & <b>html</b>", FLOOD, POLITICS, "ொ vs ொ",
               "ஸ்ரீ 123_abc  \n\t tabs"]
    for s in samples:
        check(analyse.norm(s) == rules.sim_norm(s), f"norm differs on {s[:30]!r}")
        check(analyse.grams(s) == rules.sim_grams(s), f"grams differ on {s[:30]!r}")
    for a, b in itertools.combinations(samples, 2):
        check(analyse.jaccard(analyse.grams(a), analyse.grams(b)) == rules.jaccard(rules.sim_grams(a),
              rules.sim_grams(b)), "jaccard differs")
    check(rules.DEDUP_THRESHOLD == 0.45 and rules.BODY_CAP == 1200, "calibrated constants changed")


def _m(term, text):
    return bool(rules.Term("constituency", term, 3, "", "anchor").spans(rules.norm_match(text)))


@test
def tamil_places_match_in_their_case_forms():
    for term, text in [("வேளச்சேரி", "வேளச்சேரியில் மழைநீர் தேங்கியது"),
                       ("திருவான்மியூர்", "திருவான்மியூரில் மழைநீர் தேங்கியது"),
                       ("ஆதம்பாக்கம்", "ஆதம்பாக்கத்தில் வெள்ளம்"), ("திருவள்ளூர்", "திருவள்ளூரில் கனமழை"),
                       ("திருவள்ளூர்", "திருவள்ளூர் மாவட்டம்"), ("பெசன்ட் நகர்", "பெசன்ட் நகரில்"),
                       ("அடையாறு", "அடையாற்றில் வெள்ளப்பெருக்கு"), ("சோழவரம்", "சோழவரத்தில்"),
                       ("மறியல்", "சாலை மறியலில் ஈடுபட்டனர்"), ("தீ விபத்து", "தீ விபத்தில் இருவர்"),
                       ("மரணம்", "மின்சாரம் தாக்கி மரணமடைந்தார்"), ("சடலம்", "சடலமாக மீட்பு"),
                       ("மின்வெட்டு", "மின்வெட்டால் அவதி"), ("வெள்ள", "வெள்ளம் சூழ்ந்தது")]:
        check(_m(term, text), f"{term} should match in {text}")


@test
def tamil_stems_do_not_leak_into_other_words():
    for term, text in [("வெள்ள", "வெள்ளிக்கிழமை"), ("வெள்ள", "வெள்ளை நிற"), ("பள்ளம்", "பள்ளி மாணவர்கள்"),
                       ("பள்ளம்", "பள்ளிக்கரணை"), ("மனு", "மனைவி"), ("மனு", "மனம் விட்டு")]:
        check(not _m(term, text), f"{term} must not match in {text}")
    check(_m("பள்ளம்", "சாலையில் பள்ளத்தில் விழுந்தது"), "பள்ளம் should still match பள்ளத்தில்")


@test
def english_terms_wildcards_and_masks():
    check(_m("velachery", "Velachery's lake") and not _m("velachery", "velacherynews"), "word boundary")
    t = rules.Term("civic", "flood*", 2, "flood", "urgent")
    for s in ("floods", "flooded roads", "Flooding"):
        check(t.spans(rules.norm_match(s)), f"flood* should match {s}")
    sc = rules.score(KW, "Adyar Ananda Bhavan opens new outlet", has_ai=True)
    check("constituency" not in sc["tags"], f"exclude mask failed: {sc}")


@test
def scoring_rules_from_the_spec():
    S = lambda t, **k: rules.score(KW, t, has_ai=True, **k)           # noqa: E731
    check(S("Heavy flooding in Mumbai suburbs")["score"] == 0, "unanchored civic words must score 0")
    check(S("Avadi MLA Ramesh Kumar visits Velachery")["band"] == "DROP", "collision veto is absolute")
    check(S("Minister R. Kumar and Avadi MLA Ramesh Kumar visit Velachery")["veto"] is None,
          "naming our R. Kumar lifts the collision veto")
    check("mention" in S("IT Minister R. Kumar launches AI policy")["tags"], "initials + title = mention")
    check("mention" in S("அமைச்சர் ஆர்.குமாரின் அறிவுறுத்தலின் பேரில்")["tags"], "Tamil inflected mention")
    check(S("Jana Nayagan trailer breaks records")["band"] == "DROP", "film veto")
    check(S("ஜனநாயகன் திரை விமர்சனம்")["band"] == "DROP", "film review veto")
    check(S("அமைச்சர் ஆர்.குமார் மீது எதிர்க்கட்சிகள் விமர்சனம்")["band"] != "DROP",
          "criticism of the minister must not be vetoed")
    check(S("Container trailer overturns near Ponneri, 2 dead")["veto"] is None, "trailer lorry is not film news")
    check(S("वेलाचेरी में बाढ़ Velachery")["veto"] is None, "strong anchor survives the foreign-script veto")
    # BUILD_PHASE1 as written: party weight 1, AI gate at 2. A TVK story naming no person or
    # place is dropped before the briefing ever sees it -- flagged for the label data to settle.
    check(S("தவெக மாவட்ட நிர்வாகிகள் கூட்டம்")["band"] == "DROP", "spec: a party-only story scores 1")


@test
def urgent_predicate():
    U = lambda t, d="", b="": rules.urgent(KW, t, d, b)                # noqa: E731
    u = U("Waterlogging reported near Narayanapuram Lake in Velachery")
    check(u and u["target"] == "VELACHERY" and u["zone"] == "title" and "flood" in u["groups"], f"title: {u}")
    u = U("சென்னையில் கனமழை", b="சென்னை: நேற்று இரவு பெய்த கனமழையால் திருவான்மியூரில் மழைநீர் தேங்கியது.")
    check(u and u["zone"] == "body" and "திருவான்மியூர்" in u["anchors"], f"Tamil locative in body: {u}")
    check(not U("Chennai roundup", b="Velachery residents met about parking. " + "x " * 200 + "Mumbai flooding."),
          "far-apart place and trigger must not fire")
    check(not U("வேளச்சேரியில் வெள்ளிக்கிழமை குடிநீர் நிறுத்தம்"), "Friday is not a flood")
    u = U("Container trailer overturns near Ponneri, 2 dead")
    check(u and u["target"] == "THIRUVALLUR", "urgent ignores the scoring veto")
    check(not U("Today's news | Polimer", d="Evening bulletin.\n\n#Chennai #Rain #Flood #Velachery"),
          "hashtag blocks must not fire")
    u = U("x", d='<a href="https://news.google.com/rss/articles/x?oc=5">வேளச்சேரியில் மழைநீர் தேங்கியது</a>'
                 '&nbsp;&nbsp;<font color="#6f6f6f">Dinamalar</font>')
    check(u and "<" not in u["excerpt"] and "href" not in u["excerpt"], f"Google News HTML desc: {u}")


def _kw_from(rows):
    text = "tier,term,language,weight,group,role,active\n" + "\n".join(rows) + "\n"
    parsed, problems = rules.parse_keywords_csv(text)
    check(not problems, problems)
    return rules.Keywords(parsed, origin="test", problems=problems)


@test
def short_tamil_words_end_where_the_word_ends():
    pali, kolai = rules.Term("crime", "பலி", 2, "death", "urgent"), rules.Term("crime", "கொலை", 2, "death", "urgent")
    for term, text, want in [
            (pali, "விபத்தில் இருவர் பலி", True), (pali, "விபத்தில் 3 பேர் பலியாகினர்", True),
            (pali, "லாரி மோதி முதியவர் பலியானார்", True), (pali, "உயிர்ப்பலிக்கு காரணமான ஓட்டுநர்", True),
            (pali, "பலி.", True), (pali, "கணிப்பு பலித்தது", False), (pali, "முயற்சிகள் பலிக்கும்", False),
            (kolai, "கொலை வழக்கு", True), (kolai, "கொலையில் இருவர் கைது", True), (kolai, "தொடர் கொலைகள்", True),
            (kolai, "கொலைச் சம்பவம்", True), (kolai, "கொலைக்கு காரணம்", True), (kolai, "இரட்டைக்கொலை", True),
            (rules.Term("civic", "மனு", 2, "civic", "context"), "மனுக்கள் அளிப்பு", True),
            (rules.Term("civic", "மனு", 2, "civic", "context"), "மனுவை பெற்றார்", True),
            (rules.Term("civic", "கொசு", 2, "civic", "context"), "கொசுத் தொல்லை", True),
            (rules.Term("civic", "ஏரி", 2, "civic", "context"), "ஏரியில் கழிவுநீர்", True)]:
        check(bool(term.spans(rules.norm_match(text))) == want, f"{term.term} in {text}: expected {want}")
    long_term = rules.Term("civic", "மீட்கப்பட்ட", 2, "evacuation", "urgent")
    check(not long_term.short and long_term.spans(rules.norm_match("20 பேர் மீட்கப்பட்டனர்")), "3+ letters unchanged")


@test
def a_word_inside_a_longer_matched_word_does_not_count():
    kw = _kw_from(["crime,கொலை,ta,2,death,urgent,TRUE", "crime,தற்கொலை,ta,2,suicide,urgent,TRUE",
                   "civic,வெள்ள,ta,2,flood,urgent,TRUE", "civic,வெள்ள நீர்,ta,2,evacuation,urgent,TRUE",
                   "constituency,வேளச்சேரி,ta,3,velachery,anchor,TRUE"])
    h = kw.hits(rules.norm_match("வேளச்சேரியில் தூக்கிட்டு தற்கொலை"))
    check([t.term for t, _ in h["crime"]] == ["தற்கொலை"], "கொலை inside தற்கொலை is dropped")
    h = kw.hits(rules.norm_match("வேளச்சேரியில் இரட்டைக்கொலை; மற்றொருவர் தற்கொலை"))
    check(sorted(t.term for t, _ in h["crime"]) == ["கொலை", "தற்கொலை"], "a separate murder in the same text counts")
    h = kw.hits(rules.norm_match("வேளச்சேரியில் வெள்ள நீர் தேங்கியது"))
    check(sorted(t.term for t, _ in h["civic"]) == ["வெள்ள", "வெள்ள நீர்"], "a whole word of a phrase still counts")


@test
def review_homographs_do_not_alert_and_real_reports_still_do():
    U = lambda t: (rules.urgent(KW, t) or {}).get("groups")              # noqa: E731
    for t in ("திருவள்ளூர் தேர்தல் கணிப்பு பலித்தது", "திருவள்ளூர் தவெக நிர்வாகி கட்சியிலிருந்து வெளியேற்றப்பட்டார்",
              "வேளச்சேரியில் ஆக்கிரமிப்பு நில மீட்பு பணி", "திருவள்ளூர் ராசிபலன்: முயற்சிகள் பலிக்கும்",
              "Contesting from Velachery would be political suicide", "வேளச்சேரியில் போட்டியிடுவது அரசியல் தற்கொலை"):
        check(U(t) is None, f"must not alert: {t} -> {U(t)}")
    for t, groups in [("பொன்னேரி அருகே லாரி கவிழ்ந்து இருவர் பலி", ["death"]),
                      ("ஆவடியில் லாரி மோதி முதியவர் பலியானார்", ["death"]),
                      ("ஆவடியில் இரட்டைக்கொலை", ["death"]), ("பொன்னேரியில் ஆணவக்கொலை", ["death"]),
                      ("ஆவடியில் இளம்பெண் தூக்கிட்டு தற்கொலை", ["suicide"]),
                      ("Velachery: student dies by suicide in hostel", ["suicide"]),
                      ("வேளச்சேரியில் வெள்ளம்: பொதுமக்கள் பாதுகாப்பான இடத்திற்கு அழைத்துச் செல்லப்பட்டனர்", ["evacuation", "flood"]),
                      ("திருவள்ளூரில் வெள்ளத்தில் சிக்கிய 20 பேர் மீட்கப்பட்டனர்", ["evacuation", "flood"]),
                      ("வேளச்சேரியில் நிவாரண முகாம்களில் 500 பேர் தங்கவைப்பு", ["evacuation"])]:
        check(U(t) == groups, f"{t}: expected {groups}, got {U(t)}")
    rows = {(r["tier"], r["term"]) for r in KW.rows}
    check(("civic", "வெளியேற்ற") not in rows and ("civic", "மீட்பு") not in rows, "ambiguous rows removed")


@test
def suicide_alerts_are_labelled_and_carry_no_excerpt():
    u = rules.urgent(KW, "ஆவடியில் இளம்பெண் தூக்கிட்டு தற்கொலை", "",
                     "ஆவடியில் இளம்பெண் தூக்கிட்டு தற்கொலை செய்து கொண்டார். போலீசார் விசாரணை நடத்தி வருகின்றனர்.")
    text = alerts.render_urgent(_row(title="ஆவடியில் இளம்பெண் தூக்கிட்டு தற்கொலை - Dinamalar"), u)
    check(text.startswith("◼️ <b>P1 — THIRUVALLUR · SUICIDE</b>"), text[:60])
    check("போலீசார்" not in text and "Excerpt withheld for suicide reports." in text, text)
    _valid_telegram_html(text)
    flood = alerts.render_urgent(_row(), {"target": "VELACHERY", "groups": ["flood"], "excerpt": "மழைநீர் தேங்கியது"})
    check(flood.startswith("🚨") and "மழைநீர் தேங்கியது" in flood, "other alerts unchanged")


@test
def canonical_key_and_hosts():
    ck = rules.canonical_key
    check(ck("https://www.youtube.com/watch?v=aaaaaaaaaaa&t=3") != ck("https://youtu.be/bbbbbbbbbbb"), "yt ids")
    check(ck("https://youtu.be/bbbbbbbbbbb") == ck("https://m.youtube.com/watch?v=bbbbbbbbbbb"), "yt forms")
    check(ck("https://dinamalar.com/news_detail.asp?id=111") != ck("https://dinamalar.com/news_detail.asp?id=112"),
          "?id= articles are different articles")
    check(ck("https://www.thehindu.com/a.ece/amp/?utm_source=x") == ck("https://m.thehindu.com/a.ece"), "amp/utm")
    check(not rules.host_in("newsx.com", ("x.com",)) and rules.host_in("m.facebook.com", ("facebook.com",)), "host_in")
    check(rules.cut("வெள்ளி", 5) == "வெள்…", f"cut splits a letter: {rules.cut('வெள்ளி', 5)!r}")


@test
def keyword_sheet_failures_fall_back_safely():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE runtime (k TEXT PRIMARY KEY, v TEXT)")

    class R:
        def __init__(self, code, content):
            self.status_code, self.content = code, content

    class C:
        def __init__(self, r):
            self.r = r

        def get(self, *a, **k):
            if isinstance(self.r, Exception):
                raise self.r
            return self.r
    seed = (HERE / "keywords_seed.csv").read_bytes()
    try:
        rules._memo.update(kw=None, checked=0.0)
        check(rules.get_keywords(con, C(R(200, seed)), "https://s", force=True).origin == "sheet", "sheet")
        check(rules.get_keywords(con, C(R(200, b"<html>sign in</html>")), "https://s", force=True).origin == "sheet",
              "an HTML login page keeps the last good sheet")
        rules._memo.update(kw=None, checked=0.0)
        k = rules.get_keywords(con, C(ConnectionError("down")), "https://s", force=True)
        check(k.origin == "cache" and len(k.rows) == len(KW.rows), "restart while the sheet is down uses the cache")
        k = rules.get_keywords(con, C(R(200, b"tier,term,role\nconstituency,velachery,anchor\n")), "https://s", force=True)
        check(k.origin == "cache", "a sheet with no urgent terms is rejected")
    finally:
        rules._memo.update(kw=None, checked=0.0)


@test
def env_file_quirks_from_windows_editors():
    cases = {"powershell.env": "TELEGRAM_BOT_TOKEN=123:abc\r\nTELEGRAM_CHAT_ID=-100999\r\n".encode("utf-16"),
             "notepad.env": "\ufeffTELEGRAM_BOT_TOKEN = \"123:abc\"\nTELEGRAM_CHAT_ID='-100999'\n".encode("utf-8"),
             "comments.env": b"# telegram\nexport TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=-100999   # the channel\n"}
    for name, raw in cases.items():
        path = TMP / name
        path.write_bytes(raw)
        env = rules.load_env(path)
        check(env.get("TELEGRAM_BOT_TOKEN") == "123:abc" and env.get("TELEGRAM_CHAT_ID") == "-100999", f"{name}: {env}")


# --------------------------------------------------------------------------
# rendering and the Telegram client
# --------------------------------------------------------------------------

def _row(**kw):
    base = {"id": 1, "link": "https://news.google.com/rss/articles/T?oc=5", "title": "t - Dinamalar",
            "publisher": "Dinamalar", "published_at": ts(0), "discovered_at": ts(3), "resolve_status": "RESOLVED",
            "resolved_url": "https://www.dinamalar.com/news?id=1&x=2", "extract_host": "dinamalar.com",
            "source_name": "Google News TA"}
    base.update(kw)
    return base


def _valid_telegram_html(text):
    """Only b/i/a tags, and every & < > outside them escaped."""
    stripped = re.sub(r'</?(b|i)>|<a href="[^"<>]*">|</a>', "", text)
    check("<" not in stripped and ">" not in stripped, f"unescaped angle bracket in {stripped[:200]!r}")
    for m in re.finditer("&", stripped):
        check(re.match(r"&(amp|lt|gt|quot);", stripped[m.start():]), f"bare & at {stripped[m.start():m.start()+20]!r}")


@test
def render_escapes_tamil_and_html_traps():
    title = "வேளச்சேரியில் மழை (கனமழை) - 3.5 செ.மீ. & <உடனடி> \"எச்சரிக்கை\""
    row = _row(title=title + " - Dinamalar", resolved_url='https://www.dinamalar.com/n?id=1&b=<2>"x')
    d = {"target": "VELACHERY", "groups": ["flood"], "triggers": ["தேங்கி"], "anchors": ["வேளச்சேரி"],
         "excerpt": "A&B <50 செ.மீ.> (முதல்) 'உடனடி' நடவடிக்கை - 2-வது தெரு."}
    text = alerts.render_urgent(row, d, ["Dinamalar", "The Hindu", "தினத்தந்தி"])
    _valid_telegram_html(text)
    check("&lt;உடனடி&gt;" in text and "(கனமழை) - 3.5 செ.மீ. &amp;" in text, "title escaped verbatim")
    check('href="https://www.dinamalar.com/n?id=1&amp;b=&lt;2&gt;&quot;x"' in text, "href escaped")
    check("Also reported by: The Hindu, தினத்தந்தி  (3 sources)" in text, "sources line")
    plain = alerts.html_to_plain(text)
    check('Read original: https://www.dinamalar.com/n?id=1&b=<2>"x' in plain and "<b>" not in plain, "plain fallback")


@test
def render_respects_the_length_limit_and_keeps_the_link():
    row = _row(title=("வேளச்சேரி வெள்ளம் " * 400) + " - Dinamalar")
    d = {"target": "VELACHERY", "groups": ["flood"], "triggers": ["flood"], "anchors": ["velachery"],
         "excerpt": "மழைநீர் தேங்கியது. " * 800}
    text = alerts.render_urgent(row, d)
    check(rules.utf16_len(text) <= alerts.TG_LIMIT, f"too long: {rules.utf16_len(text)}")
    check(text.endswith('<a href="https://www.dinamalar.com/news?id=1&amp;x=2">Read original</a>'), "link kept")
    _valid_telegram_html(text)
    for part in re.findall(r"<(?:b|i)>(.*?)</(?:b|i)>", text, re.S):
        check(not part or unicodedata.category(part[0]) not in ("Mn", "Mc"), "a cut left an orphan vowel sign")
    note = alerts.with_delay_note("x", ts(0), T0 + timedelta(minutes=95))
    check(note.startswith("⏱ <i>Delayed: queued 1h 35m ago</i>"), note)


class _Resp:
    def __init__(self, code, data=None, text=""):
        self.status_code, self._data, self.text = code, data, text

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


class _Client:
    def __init__(self, r):
        self.r, self.calls = r, []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        if isinstance(self.r, Exception):
            raise self.r
        return self.r


@test
def telegram_errors_are_classified_and_the_token_never_leaks():
    tok = "123456:SECRETSECRET"

    def call(r, plain=False):
        c = _Client(r)
        return alerts.Telegram(tok, "-100123", c).send("<b>x</b>", plain=plain), c
    res, c = call(_Resp(200, {"ok": True, "result": {"message_id": 77}}))
    check(res.ok and res.message_id == 77, repr(res))
    check(c.calls[0][1]["parse_mode"] == "HTML" and c.calls[0][1]["link_preview_options"] == {"is_disabled": True},
          "payload")
    check("parse_mode" not in call(_Resp(200, {"ok": True, "result": {"message_id": 1}}), plain=True)[1].calls[0][1],
          "plain has no parse_mode")
    cases = [(_Resp(429, {"ok": False, "description": "Too Many Requests: retry after 7",
                          "parameters": {"retry_after": 7}}), "rate"),
             (_Resp(400, {"ok": False, "description": "Bad Request: can't parse entities: Unsupported start tag"}), "parse"),
             (_Resp(400, {"ok": False, "description": "Bad Request: chat not found"}), "config"),
             (_Resp(401, {"ok": False, "description": "Unauthorized"}), "config"),
             (_Resp(403, {"ok": False, "description": "Forbidden: bot is not a member of the channel chat"}), "config"),
             (_Resp(502, None, "<html>Bad Gateway</html>"), "server"),
             (_Resp(400, {"ok": False, "description": "Bad Request: message is not modified"}), "not_modified"),
             (_Resp(400, {"ok": False, "description": "Bad Request: message is too long"}), "bad")]
    for r, kind in cases:
        res, _ = call(r)
        check(res.kind == kind, f"expected {kind}, got {res}")
    check(call(cases[0][0])[0].retry_after == 7, "retry_after")
    res, _ = call(ConnectionError(f"failed to reach https://api.telegram.org/bot{tok}/sendMessage"))
    check(res.kind == "network" and tok not in res.error and "<token>" in res.error, f"token leaked: {res.error}")


# --------------------------------------------------------------------------
# schema, bootstrap, processing
# --------------------------------------------------------------------------

@test
def migration_is_additive_and_idempotent():
    con = make_db("mig.db", migrate=False)
    iid = add(con, "old Phase 0 row", body=FLOOD)
    before = dict(con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone())
    added = alerts.migrate(con)
    check(len(added) >= 20, f"columns added: {added}")
    check(alerts.migrate(con) == [], "second migration must add nothing")
    after = dict(con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone())
    check(all(after[k] == v for k, v in before.items()), "existing values changed")
    for t in ("events", "alert_queue", "ai_budget", "keywords"):
        check(con.execute("SELECT count(*) FROM sqlite_master WHERE name=?", (t,)).fetchone()[0] == 1, t)
    bare = sqlite3.connect(":memory:")
    try:
        alerts.migrate(bare)
        check(False, "migrating a database without items must refuse")
    except RuntimeError:
        pass


@test
def bootstrap_scores_old_items_but_alerts_nothing():
    con = make_db("boot.db")
    now = alerts.utcnow()
    old = [add(con, "வேளச்சேரியில் மழைநீர் தேங்கியது", body=flood_variant(k), host=h, minutes=0)
           for k, h in enumerate(("dinamalar.com", "maalaimalar.com"))]
    con.execute("UPDATE items SET discovered_at=?, published_at=?", (alerts.iso(now - timedelta(minutes=10)),) * 2)
    con.commit()
    st = alerts.tick(con, env=NO_ENV, now=now, transport=None)
    check(alerts.rt_get(con, "alert_cutoff_at") == alerts.iso(now), "cutoff = first tick")
    check(st["urgent"] == 2 and not queue(con), f"old urgent items must not alert: {dict(st)}")
    check(all(con.execute("SELECT band FROM items WHERE id=?", (i,)).fetchone()[0] for i in old), "old items scored")
    fresh = add(con, "வேளச்சேரியில் மழைநீர் தேங்கியது", body=FLOOD, host="dailythanthi.com")
    con.execute("UPDATE items SET discovered_at=?, published_at=? WHERE id=?",
                (alerts.iso(now + timedelta(minutes=2)), alerts.iso(now + timedelta(minutes=1)), fresh))
    con.commit()
    alerts.tick(con, env=NO_ENV, now=now + timedelta(minutes=3), transport=None)
    q = queue(con)
    check(len(q) == 1 and q[0]["item_id"] == fresh, f"exactly the fresh item: {[dict(r) for r in q]}")
    check(q[0]["status"] == "SUPPRESSED" and "same event" in q[0]["reason"] or q[0]["status"] == "DRY_RUN",
          f"joins the bootstrap event (no prior alert) so it alerts, as DRY_RUN without a token: {dict(q[0])}")
    check(q[0]["status"] == "DRY_RUN", "no token means DRY_RUN, not a silent pending pile")
    check(any("DRY RUN" in m for m in LOGS), "dry run alerts are written to the log")


@test
def one_event_three_outlets_one_alert_and_an_edit():
    con = make_db("event.db")
    set_cutoff(con)
    a = add(con, "வேளச்சேரியில் கனமழை: தாழ்வான பகுதிகளில் மழைநீர் தேங்கியது", body=flood_variant(0),
            host="dinamalar.com", publisher="Dinamalar", minutes=0)
    process(con, 2)
    q = queue(con)
    check(len(q) == 1 and q[0]["status"] == "PENDING" and q[0]["item_id"] == a, "first report queues an alert")
    tg = Scripted()
    check(deliver(con, tg, 3)["sent"] == 1 and queue(con)[0]["message_id"], "delivered with a message id")
    add(con, "வேளச்சேரி பகுதிகளில் மழைநீர் தேங்கியதால் மக்கள் அவதி", body=flood_variant(1),
        host="maalaimalar.com", publisher="Maalaimalar", minutes=10)
    add(con, "Velachery: மழைநீர் தேங்கி மக்கள் தவிப்பு", body=flood_variant(2), host="dailythanthi.com",
        publisher="Daily Thanthi", minutes=20)
    process(con, 22)
    q = queue(con)
    check([r["status"] for r in q] == ["SENT", "SUPPRESSED", "SUPPRESSED"], [dict(r) for r in q])
    check(all("same event as alert #" in (r["reason"] or "") for r in q[1:]), "reason recorded")
    ev = con.execute("SELECT count(DISTINCT event_id) FROM items WHERE event_id IS NOT NULL").fetchone()[0]
    check(ev == 1, f"one event, got {ev}")
    edits = queue(con, "EDIT")
    check(len(edits) == 1 and edits[0]["status"] == "PENDING", "edits coalesce into one pending row")
    deliver(con, tg, 23)
    check(tg.calls[-1][0] == "edit" and "Maalaimalar, Daily Thanthi  (3 sources)" in tg.calls[-1][2],
          f"silent edit shows the source count: {tg.calls[-1]}")
    check(queue(con, "EDIT")[0]["status"] == "SENT", "edit recorded")


@test
def same_url_stale_and_breakout():
    con = make_db("breakout.db")
    set_cutoff(con)
    a = add(con, "வேளச்சேரியில் மழைநீர் தேங்கியது", body=flood_variant(0), host="dinamalar.com", minutes=0,
            url="https://www.dinamalar.com/news/555?utm_source=gn")
    process(con, 1)
    deliver(con, Scripted(), 2)
    add(con, "வேளச்சேரியில் மழைநீர் தேங்கியது", body=flood_variant(0), host="dinamalar.com", minutes=5,
        url="https://m.dinamalar.com/news/555")
    add(con, "Velachery flood: waterlogging in Vijayanagar", body="", host="dtnext.in", minutes=30, pub_minutes=-400)
    b = add(con, "வேளச்சேரியில் மின்சாரம் தாக்கி ஒருவர் பலி",
            body="வேளச்சேரியில் மழைநீர் தேங்கிய பகுதியில் மின்சாரம் தாக்கி ஒருவர் பலியானார். " + flood_variant(3),
            host="maalaimalar.com", minutes=40)
    process(con, 41)
    q = {r["item_id"]: r for r in queue(con)}
    reasons = [r["reason"] or "" for r in q.values()]
    check(any(x.startswith("same url as alert") for x in reasons), f"same article, other token: {reasons}")
    check(any(re.match(r"stale: published 7\.\dh", x) for x in reasons), f"stale: {reasons}")
    check(q[b]["status"] == "PENDING" and json.loads(q[b]["detail"])["update"] == ["death"], dict(q[b]))
    check("UPDATE: DEATH" in q[b]["body"], "update header")
    check(con.execute("SELECT event_id FROM items WHERE id=?", (a,)).fetchone()[0] ==
          con.execute("SELECT event_id FROM items WHERE id=?", (b,)).fetchone()[0], "break-out stays in the event")


@test
def blocked_hosts_youtube_and_a_poison_row():
    con = make_db("misc.db")
    set_cutoff(con)
    blk = add(con, "Velachery flood: waterlogging everywhere", host="theprint.in", minutes=0)
    yt_noise = add(con, "Evening bulletin | Polimer News", desc="State news.\n#Chennai #Rain #Flood #Velachery",
                   host="youtube.com", publisher="", source="yt_polimer", resolve="SKIPPED", extract="SKIPPED_VIDEO",
                   url="https://www.youtube.com/watch?v=aaaaaaaaaaa", link="https://www.youtube.com/watch?v=aaaaaaaaaaa")
    yt_real = add(con, "வேளச்சேரியில் கனமழை | Polimer News", desc="வேளச்சேரியில் மழைநீர் தேங்கியது. #Chennai",
                  host="youtube.com", publisher="", source="yt_polimer", resolve="SKIPPED", extract="SKIPPED_VIDEO",
                  url="https://www.youtube.com/watch?v=bbbbbbbbbbb", link="https://www.youtube.com/watch?v=bbbbbbbbbbb")
    poison = add(con, "POISON ROW", host="dinamalar.com", minutes=1)
    later = add(con, "Waterlogging in Velachery after overnight rain", host="dtnext.in", minutes=2)
    real_score = rules.score

    def boom(kw, title, *a, **k):
        if "POISON" in title:
            raise ValueError("synthetic failure")
        return real_score(kw, title, *a, **k)
    rules.score = boom
    try:
        st = process(con, 3)
    finally:
        rules.score = real_score
    band = lambda i: con.execute("SELECT band, urgent, event_id FROM items WHERE id=?", (i,)).fetchone()  # noqa
    check(band(blk)[0] == "DROP" and band(blk)[2] is None, "blocked host dropped, no event")
    check(band(yt_noise)[1] == 0, "hashtags alone do not make a video urgent")
    check(band(yt_real)[1] == 1, "a video whose description says it is urgent")
    check(band(poison)[0] == "ERROR" and st["error"] == 1, "a failing row is marked and skipped")
    check(band(later)[0] and band(later)[1] == 1, "processing continued past the poison row")
    body = {r["item_id"]: r["body"] for r in queue(con)}[yt_real]
    check("Source: Polimer News" in body and "youtube.com/watch?v=bbbbbbbbbbb" in body, "video outlet and link")


@test
def place_identity_follows_the_sheet_groups():
    ids = alerts.place_ids(KW, ["velachery", "வேளச்சேரி", "ponneri", "பொன்னேரி", "avadi"])
    check(ids == ["constituency:velachery", "district:avadi", "district:ponneri"], ids)
    rows = [r for r in KW.rows if r["tier"] in ("constituency", "district") and r["role"] == "anchor"]
    lone = [r["term"] for r in rows if rules.norm_match(r["group"] or "") not in
            {rules.norm_match(x["term"]) for x in rows if x["group"] == r["group"]}]
    check(not lone, f"seed place terms whose group is not a place name: {lone}")


@test
def reports_without_bodies_fold_into_the_alert_for_the_same_place():
    con = make_db("fold.db")
    set_cutoff(con)
    tg = Scripted()
    a = add(con, "வேளச்சேரியில் கனமழை: மழைநீர் தேங்கியது", body=flood_variant(0), host="dinamalar.com",
            publisher="Dinamalar", minutes=0)
    process(con, 1)
    deliver(con, tg, 2)
    video = add(con, "வேளச்சேரியில் கனமழை - மழைநீர் தேங்கியது | LIVE", desc="வேளச்சேரியில் மழைநீர் தேங்கியது.",
                host="youtube.com", publisher="", source="yt_polimer", resolve="SKIPPED", extract="SKIPPED_VIDEO",
                url="https://www.youtube.com/watch?v=ddddddddddd", link="https://www.youtube.com/watch?v=ddddddddddd",
                minutes=20)
    english = add(con, "Velachery flooding: residents wade through knee-deep water",
                  body="Heavy overnight rain flooded Velachery. " + "Residents of Vijayanagar waded through water. " * 12,
                  host="dtnext.in", publisher="DT Next", minutes=30, source="gn_en_velachery")
    failed = add(con, "வேளச்சேரியில் மழைநீர் தேங்கியது", host="maalaimalar.com", publisher="Maalaimalar",
                 minutes=35, resolve="FAILED", extract="PENDING")
    taramani = add(con, "Taramani link road waterlogged after rain", host="dtnext.in", publisher="DT Next", minutes=40,
                   source="gn_en_velachery")
    death = add(con, "வேளச்சேரியில் மழைநீரில் மின்சாரம் தாக்கி ஒருவர் பலி | LIVE", desc="", host="youtube.com",
                publisher="", source="yt_puthiya", resolve="SKIPPED", extract="SKIPPED_VIDEO",
                url="https://www.youtube.com/watch?v=eeeeeeeeeee", link="https://www.youtube.com/watch?v=eeeeeeeeeee",
                minutes=50)
    process(con, 51)
    q = {r["item_id"]: r for r in queue(con)}
    for i in (video, english, failed):
        check(q[i]["status"] == "SUPPRESSED" and q[i]["ref_id"] == q[a]["id"]
              and "same place and emergency" in q[i]["reason"], dict(q[i]))
    check(q[taramani]["status"] == "PENDING" and not json.loads(q[taramani]["detail"]).get("update"),
          "a different place in the constituency is its own alert")
    check(q[death]["status"] == "PENDING" and json.loads(q[death]["detail"])["update"] == ["death"],
          "a new kind of emergency at the same place is an UPDATE")
    deliver(con, tg, 52)
    edit = [c for c in tg.calls if c[0] == "edit"][-1][2]
    check("Related reports (3):" in edit and "• Polimer News: வேளச்சேரியில் கனமழை - மழைநீர் தேங்கியது | LIVE" in edit
          and "• DT Next: Velachery flooding" in edit and "• Maalaimalar:" in edit, edit)
    _valid_telegram_html(edit)
    later = add(con, "வேளச்சேரியில் மழைநீர் தேங்கியது | LIVE", desc="வேளச்சேரியில் மழைநீர் தேங்கியது.", host="youtube.com",
                publisher="", source="yt_thanthi", resolve="SKIPPED", extract="SKIPPED_VIDEO",
                url="https://www.youtube.com/watch?v=fffffffffff", link="https://www.youtube.com/watch?v=fffffffffff",
                minutes=200)
    process(con, 201)
    check({r["item_id"]: r for r in queue(con)}[later]["status"] == "PENDING",
          f"after {alerts.URGENT_PLACE_WINDOW_H}h the same place alerts again")


@test
def different_towns_in_the_district_alert_separately():
    con = make_db("towns.db")
    set_cutoff(con)
    one = add(con, "Container trailer overturns near Ponneri, 2 dead", host="dtnext.in", minutes=0)
    two = add(con, "Two killed as lorry rams bike in Avadi", host="dtnext.in", minutes=30)
    three = add(con, "பொன்னேரி அருகே கண்டெய்னர் லாரி கவிழ்ந்து இருவர் பலி", host="dailythanthi.com", minutes=45)
    process(con, 46)
    q = {r["item_id"]: r for r in queue(con)}
    check(q[one]["status"] == "PENDING" and q[two]["status"] == "PENDING", "Ponneri and Avadi are separate")
    check(q[three]["status"] == "SUPPRESSED" and q[three]["ref_id"] == q[one]["id"],
          f"the Tamil report of the Ponneri accident folds into it: {dict(q[three])}")


# --------------------------------------------------------------------------
# delivery failures
# --------------------------------------------------------------------------

def _one_pending(name):
    con = make_db(name)
    set_cutoff(con)
    add(con, "Waterlogging in Velachery after overnight rain", host="dtnext.in", minutes=0)
    process(con, 1)
    check(queue(con)[0]["status"] == "PENDING", "setup")
    return con


@test
def rate_limit_holds_the_whole_queue():
    con = _one_pending("rate.db")
    tg = Scripted(alerts.Result(False, retry_after=7, error="Too Many Requests", kind="rate"))
    st = deliver(con, tg, 2)
    r = queue(con)[0]
    check(st["rate_limited"] == 1 and r["status"] == "PENDING" and r["attempts"] == 0, dict(r))
    check(alerts.rt_get(con, "tg_hold_until") == alerts.iso(T0 + timedelta(minutes=2, seconds=7)), "hold recorded")
    tg2 = Scripted()
    check(deliver(con, tg2, 2.05)["held"] == 1 and not tg2.calls, "nothing sent during the hold")
    check(deliver(con, tg2, 2.2)["sent"] == 1, "sent once the hold passes")


@test
def network_loss_does_not_burn_attempts():
    con = _one_pending("net.db")
    deliver(con, Scripted(alerts.Result(False, error="ConnectError", kind="network")), 2)
    r = queue(con)[0]
    check(r["status"] == "PENDING" and r["attempts"] == 0 and r["next_attempt_at"] == ts(3), dict(r))
    check(deliver(con, Scripted(), 2.5)["sent"] == 0, "waits for next_attempt_at")
    check(deliver(con, Scripted(), 3)["sent"] == 1, "then sends")


@test
def rejected_html_is_resent_as_plain_text():
    con = _one_pending("parse.db")
    tg = Scripted(alerts.Result(False, error="can't parse entities", kind="parse"))
    deliver(con, tg, 2)
    r = queue(con)[0]
    check(r["status"] == "SENT" and "plain text" in (r["error"] or ""), dict(r))
    check(len(tg.calls) == 2 and tg.calls[1][2] is True and "https://www.dtnext.in/news/" in tg.calls[1][1],
          "second call is plain text and still carries the link")


@test
def config_errors_pause_server_errors_retry_then_fail():
    con = _one_pending("config.db")
    deliver(con, Scripted(alerts.Result(False, error="HTTP 403: Forbidden", kind="config")), 2)
    r = queue(con)[0]
    check(r["status"] == "PENDING" and r["attempts"] == 0, dict(r))
    check(alerts.rt_get(con, "tg_hold_until") == alerts.iso(T0 + timedelta(minutes=17)), "15 minute pause")
    check(any("bot is an admin" in m for m in LOGS), "tells the user what to check")

    con = _one_pending("server.db")
    minute = 2.0
    for _ in range(alerts.TG_MAX_ATTEMPTS):
        deliver(con, Scripted(alerts.Result(False, error="HTTP 502", kind="server")), minute)
        minute += 20
    r = queue(con)[0]
    check(r["status"] == "FAILED" and r["attempts"] == alerts.TG_MAX_ATTEMPTS, dict(r))


@test
def undelivered_alerts_expire_and_crashed_sends_are_retried():
    con = _one_pending("expire.db")
    check(deliver(con, None, 60 * 13)["expired"] == 1 and queue(con)[0]["status"] == "EXPIRED",
          "an alert still undelivered after 12h is not sent")
    con = _one_pending("crash.db")
    con.execute("UPDATE alert_queue SET status='SENDING', claimed_at=?", (ts(1),))
    con.commit()
    check(deliver(con, Scripted(), 5)["sent"] == 0, "a fresh SENDING row is left alone")
    check(deliver(con, Scripted(), 12)["sent"] == 1, "a SENDING row abandoned for 10 min is retried")
    check(queue(con)[0]["status"] == "SENT", "sent")
    con2 = _one_pending("late.db")
    tg = Scripted()
    deliver(con2, tg, 120)
    check(tg.calls[0][1].startswith("⏱ <i>Delayed: queued 1h 59m ago</i>"), tg.calls[0][1][:60])


# --------------------------------------------------------------------------
# event lifecycle
# --------------------------------------------------------------------------

@test
def a_three_day_flood_realerts_daily_not_every_report():
    con = make_db("monsoon.db")
    set_cutoff(con)
    hosts = ["dinamalar.com", "maalaimalar.com", "dailythanthi.com", "dinamani.com"]
    tg = Scripted()
    for k, m in enumerate(range(0, 72 * 60 + 1, 180)):                 # a report every 3 hours
        add(con, "வேளச்சேரியில் தொடர் மழை: மழைநீர் தேங்கியது", body=flood_variant(k), host=hosts[k % 4], minutes=m)
        process(con, m + 2)
        deliver(con, tg, m + 3)
    q = queue(con)
    sent = [r for r in q if r["status"] == "SENT"]
    times = [(alerts.parse_ts(r["created_at"]) - T0).total_seconds() / 3600 for r in sent]
    # an event closes 24h after its first report, so the report arriving on each new day
    # starts a fresh event and alerts: day 0, 1, 2 and 3
    check(len(q) == 25 and len(sent) == 4, f"expected 4 daily alerts from 25 reports, got {len(sent)} at {times}h")
    gaps = [b - a for a, b in zip(times, times[1:])]
    check(all(21 <= g <= alerts.EVENT_MAX_SPAN_H + 3.1 for g in gaps), f"about one alert a day: {gaps}")
    check(con.execute("SELECT count(*) FROM events").fetchone()[0] == 4, "one event per day")


@test
def events_go_quiet_then_close():
    con = make_db("life.db")
    set_cutoff(con)
    add(con, "வேளச்சேரியில் மழைநீர் தேங்கியது", body=flood_variant(0), host="dinamalar.com", minutes=0)
    process(con, 1)
    alerts.refresh_events(con, T0 + timedelta(hours=7))
    check(con.execute("SELECT status FROM events").fetchone()[0] == "QUIET", "quiet after 6h")
    add(con, "வேளச்சேரியில் மழைநீர் தேங்கியது", body=flood_variant(1), host="maalaimalar.com", minutes=8 * 60)
    process(con, 8 * 60 + 1)
    check(con.execute("SELECT status, members FROM events").fetchone()[:] == ("OPEN", 2), "a new report reopens it")
    alerts.refresh_events(con, T0 + timedelta(hours=25))
    check(con.execute("SELECT status FROM events").fetchone()[0] == "CLOSED", "closed a day after the first report")
    add(con, "வேளச்சேரியில் மழைநீர் தேங்கியது", body=flood_variant(2), host="dailythanthi.com", minutes=26 * 60)
    process(con, 26 * 60 + 1)
    check(con.execute("SELECT count(*) FROM events").fetchone()[0] == 2, "a report after CLOSED starts a new event")
    unrelated = add(con, "அமைச்சர் ஆர்.குமார் தவெக மாவட்ட நிர்வாகிகள் கூட்டத்தில் பங்கேற்பு", body=POLITICS,
                    host="vikatan.com", minutes=26 * 60 + 5, source="gn_ta_tvk")
    process(con, 26 * 60 + 6)
    e = con.execute("SELECT event_id FROM items WHERE id=?", (unrelated,)).fetchone()[0]
    check(e is not None and con.execute("SELECT members FROM events WHERE event_id=?", (e,)).fetchone()[0] == 1,
          "a different story is its own event")


# --------------------------------------------------------------------------
# backtest, status, and the collector hooks
# --------------------------------------------------------------------------

def synthetic_corpus(path):
    con = make_db(path.name, migrate=False)
    hosts = ["dinamalar.com", "maalaimalar.com", "dailythanthi.com", "dinamani.com", "vikatan.com"]
    ids = []
    for day in range(2):
        base = day * 1440
        for k in range(3):
            ids.append(add(con, "வேளச்சேரியில் கனமழை: மழைநீர் தேங்கியது", body=flood_variant(k + day),
                           host=hosts[(k + day) % 5], minutes=base + 60 + k * 25))
        ids.append(add(con, "தவெக மாவட்ட நிர்வாகிகள் கூட்டம் பனையூரில் நடைபெற்றது", body=POLITICS, host="vikatan.com",
                       minutes=base + 300, source="gn_ta_tvk"))
        ids.append(add(con, "IT Minister R. Kumar launches AI policy for Tamil Nadu", host="dtnext.in",
                       minutes=base + 400, source="gn_en_velachery"))
        ids.append(add(con, "Jana Nayagan trailer breaks records", host="cinema.vikatan.com", minutes=base + 500,
                       source="gn_ta_tvk"))
        ids.append(add(con, "Old story: Velachery flood relief", host="thehindu.com", minutes=base + 600,
                       pub_minutes=base - 900))
        ids.append(add(con, "Velachery flood: theprint", host="theprint.in", minutes=base + 610))
        ids.append(add(con, "Container trailer overturns near Ponneri, 2 dead", host="dtnext.in", minutes=base + 700,
                       source="gn_en_velachery"))
    con.close()
    labels = path.with_name("labels.csv")
    with open(labels, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "source_id", "published", "host", "status", "chars", "title", "RELEVANT_y_n",
                    "TARGET_tvk_velachery_none", "NOTE", "AI_PRIORITY"])
        cats = ["constituency", "constituency", "constituency", "political", "mention", "none", "constituency",
                "none", "district"]
        for i, c in zip(ids, cats * 2):
            w.writerow([i, "", "", "", "", "", "", "n" if c == "none" else "y", c, "", "1" if c in ("constituency", "district") else "3"])
    return labels


@test
def backtest_runs_end_to_end_and_never_touches_the_corpus():
    src = TMP / "corpus_synth.db"
    labels = synthetic_corpus(src)
    digest = src.read_bytes()
    out, work = TMP / "report.txt", TMP / "bt.db"
    buf = io.StringIO()
    real = sys.stdout
    sys.stdout = buf
    try:
        lines = alerts.cmd_backtest(src, out, work, labels, env=NO_ENV)
    finally:
        sys.stdout = real
    report = out.read_text(encoding="utf-8")
    check(src.read_bytes() == digest, "the source corpus must not change")
    for heading in ("1. WHAT THE RULE ENGINE KEPT", "2. URGENT ALERTS PER DAY", "3. EVERY ALERT", "4. WHICH SHEET",
                    "5. SUPPRESSED", "6. TIMESTAMPS", "7. CANONICAL KEY", "8. EVENTS", "9. AGAINST label_me.csv"):
        check(heading in report, f"missing section {heading}")
    check("total 4 alerts over" in report, "two flood events and two trailer accidents over two days: "
          + next((x for x in lines if "total" in x), ""))
    check("same event" in report and "stale" in report, "suppression reasons reported")
    check("AI gate" in report and "recall" in report, "label comparison ran")
    con = alerts._connect(work, readonly=True)
    check(any("queue:" in x for x in alerts.status_lines(con, env=NO_ENV)), "status works on a replayed db")
    con.close()


def _import_collect():
    import collect
    lg = logging.getLogger("collect")
    for h in list(lg.handlers):
        if not isinstance(h, _Capture):
            lg.removeHandler(h)
            h.close()
    return collect


@test
def collector_blocks_before_decoding_and_writes_canonical_keys():
    collect = _import_collect()
    check(collect.alerts is alerts and collect.PHASE1_ERROR is None, f"phase 1 import: {collect.PHASE1_ERROR}")
    con = make_db("collector.db", migrate=False)
    check(collect.phase1_setup(con), "setup migrates")
    collect._PHASE1["canon"] = None

    def gn(host, n):
        raw = json.dumps({"source": {"href": f"https://www.{host}", "title": host}})
        con.execute("""INSERT INTO items (item_key, source_id, link, title, published_at, discovered_at, raw_payload)
                       VALUES (?, 'gn_ta_velachery', ?, 't', ?, ?, ?)""",
                    (f"c{n}", f"https://news.google.com/rss/articles/C{n}", ts(0), ts(0), raw))
    gn("theprint.in", 1)
    gn("dinamalar.com", 2)
    gn("getlokalapp.com", 3)
    con.execute("""INSERT INTO items (item_key, source_id, link, title, published_at, discovered_at)
                   VALUES ('yt1', 'yt_polimer', 'https://www.youtube.com/watch?v=ccccccccccc', 'v', ?, ?)""", (ts(0), ts(0)))
    con.commit()
    decoded = {"https://news.google.com/rss/articles/C2": "https://www.dinamalar.com/news/9?utm_source=gn",
               "https://news.google.com/rss/articles/C3": "https://tamil.getlokalapp.com/story/1"}
    calls = []
    fake = types.ModuleType("googlenewsdecoder")

    def gnewsdecoder(link, interval=None):
        calls.append(link)
        return {"status": True, "decoded_url": decoded[link]}
    fake.gnewsdecoder = gnewsdecoder
    saved_mod, saved_time = sys.modules.get("googlenewsdecoder"), collect.time
    sys.modules["googlenewsdecoder"] = fake
    collect.time = types.SimpleNamespace(time=time.time, sleep=lambda s: None, monotonic=time.monotonic)
    ticks = []
    try:
        collect.drain_resolver(con, 30, tick=lambda: ticks.append(1))
    finally:
        collect.time = saved_time
        if saved_mod is None:
            sys.modules.pop("googlenewsdecoder", None)
        else:
            sys.modules["googlenewsdecoder"] = saved_mod
    rows = {r["item_key"]: r for r in con.execute("SELECT * FROM items")}
    check("https://news.google.com/rss/articles/C1" not in calls, "blocked outlet never decoded")
    check(rows["c1"]["resolve_status"] == "BLOCKED" and rows["c1"]["extract_status"] == "SKIPPED_BLOCKED", "c1")
    check(rows["c2"]["resolve_status"] == "RESOLVED" and rows["c2"]["canonical_key"] == "dinamalar.com/news/9", "c2")
    check(rows["c3"]["resolve_status"] == "BLOCKED", "blocked host found only after decoding")
    check(rows["yt1"]["canonical_key"] == "youtube.com/watch?v=ccccccccccc", "video key")
    check(ticks, "the resolver loop calls the alert tick")


@test
def collector_extractor_no_longer_mistakes_newsx_for_x():
    collect = _import_collect()
    con = make_db("extract.db")
    for n, url in enumerate(["https://www.newsx.com/national/story-1", "https://x.com/someone/status/1",
                             "https://theprint.in/india/story"]):
        con.execute("""INSERT INTO items (item_key, link, title, discovered_at, resolve_status, resolved_url)
                       VALUES (?, ?, 't', ?, 'RESOLVED', ?)""", (f"e{n}", url, ts(0), url))
    con.commit()
    page = "<html><body><article><p>" + ("Velachery residents reported waterlogging. " * 30) + "</p></article></body></html>"

    class C:
        def get(self, url, headers=None):
            return types.SimpleNamespace(status_code=200, text=page)
    saved = collect.time
    collect.time = types.SimpleNamespace(time=time.time, sleep=lambda s: None, monotonic=time.monotonic)
    try:
        collect.drain_extractor(con, C(), 30)
    finally:
        collect.time = saved
    st = {r["resolved_url"]: r["extract_status"] for r in con.execute("SELECT resolved_url, extract_status FROM items")}
    check(st["https://www.newsx.com/national/story-1"] in ("OK", "THIN"), f"newsx.com extracted: {st}")
    check(st["https://x.com/someone/status/1"] == "SKIPPED_SOCIAL", "x.com still skipped")
    check(st["https://theprint.in/india/story"] == "SKIPPED_BLOCKED", "blocked host not fetched")


@test
def a_broken_tick_never_stops_the_collector():
    con = make_db("broken.db")
    real = alerts.tick

    def explode(*a, **k):
        raise RuntimeError("synthetic")
    alerts.tick = explode
    try:
        check(alerts.maybe_tick(con, force=True) is None, "maybe_tick swallows the error")
    finally:
        alerts.tick = real
    check(any("tick failed" in m for m in LOGS), "and logs it")


# --------------------------------------------------------------------------

def main():
    only = sys.argv[sys.argv.index("-k") + 1] if "-k" in sys.argv else None
    chosen = [t for t in TESTS if not only or only in t.__name__]
    failed = 0
    t_all = time.time()
    for fn in chosen:
        alerts.reset_caches()
        rules._memo.update(kw=None, checked=0.0)
        t0 = time.time()
        try:
            fn()
            print(f"PASS  {fn.__name__}  ({time.time() - t0:.1f}s)")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(chosen) - failed}/{len(chosen)} passed in {time.time() - t_all:.0f}s")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
