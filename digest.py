#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
digest.py -- the twice-daily Telegram brief (step 3).

    python digest.py                     send the brief for this time of day
    python digest.py --slot morning      force the morning / evening heading
    python digest.py --dry-run           build it, print it, send nothing, mark nothing
    python digest.py --status            what the next brief would contain right now

What it does, in order:
  1. Candidates: items scored since the last brief that the rules kept (or flagged
     urgent), published within the last 24 hours, one per story (event).
  2. Ranking: Gemini reads headline + first lines of each candidate and gives a
     category (mention, constituency, district, portfolio, political, opportunity,
     none) and a priority (1 urgent, 2 standard, 3 background). Without a key, over
     quota, or on any error, the keyword rules decide instead.
  2b. Merging: reports of one event become one candidate. Gemini's story numbers and
     near-identical headlines only PROPOSE a link; it is accepted when the two reports
     are within a day, from different outlets and share an identifying headline word
     (a Tamil/English pair: when Gemini grouped them AND gave both the same category).
     Numbers and dates never link anything, links never chain, a card pools at most 8
     reports, one per outlet.
  3. Selection: every mention of him and every link the office added, uncapped; then up
     to 5 per category; if a category has fewer, its spare places go to the next-best
     items from other categories, up to DIGEST_MAX in total (default 5 x the number of
     categories = 30). Priority-1 civic items (floods, deaths, protests) go in an URGENT
     block at the top.
  4. One Telegram post, HTML, split only when longer than Telegram allows.
  5. Every candidate -- chosen or not -- is marked so it never appears again.

Settings (environment or .env): GEMINI_API_KEY, GEMINI_MODEL, TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID, DIGEST_MAX, DIGEST_PER_CATEGORY, DIGEST_AI_MAX_CALLS.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import rules
import alerts
from alerts import IST, esc, esc_attr, fmt_ist, iso, parse_ts, rt_get, rt_set, utcnow

HERE = Path(__file__).resolve().parent
DB = HERE / "corpus.db"
log = logging.getLogger("collect.digest")

# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

AI_CATEGORIES = ["mention", "constituency", "district", "portfolio", "political", "opportunity"]
CATEGORIES = AI_CATEGORIES + ["added"]        # "added": links the office handed in that fit no section
LABELS = {"mention": "MENTIONS", "constituency": "VELACHERY", "district": "THIRUVALLUR",
          "portfolio": "AI / IT / DIGITAL", "political": "POLITICAL", "opportunity": "OPPORTUNITIES",
          "added": "ADDED BY THE OFFICE"}
NICE = {"mention": "Mentions", "constituency": "Velachery", "district": "Thiruvallur",
        "portfolio": "AI / IT / Digital", "political": "Political", "opportunity": "Opportunities",
        "added": "Added by the office"}
ICONS = {"mention": "🗣", "constituency": "📍", "district": "🏛", "portfolio": "💻",
         "political": "🏳", "opportunity": "🎯", "added": "📌"}
URGENT_CATEGORIES = ("constituency", "district")

PER_CATEGORY = 5              # first pass
MENTION_CAP = 40              # mentions are never capped by PER_CATEGORY or the total; this is the sanity ceiling
MAX_TOTAL = PER_CATEGORY * len(CATEGORIES)
PUBLISHED_WITHIN_H = 24       # a 14-day query can surface old stories; the brief is about now
WEB_WINDOW_H = 24             # the web page is a rolling edition of everything kept this recently
RERUN_SIM = 0.8               # a headline or body this close to one covered the day before is a re-run
WEB_PER_CATEGORY = 10         # web page: 10 per category first, then fill to WEB_MAX (60)
WINDOW_CAP_H = 48             # never look further back than this, even on the first send
AI_MAX_CALLS_PER_DAY = 80     # a normal day uses ~10-15: ranking, one cluster and one summary per edition
AI_BATCH = 60                 # items per Gemini call (~25k tokens with Tamil)
AI_PAUSE_S = 4.0              # free tier is per-minute limited
AI_TIME_BUDGET_S = 420        # after this, the rest is ranked by the rules
SNIPPET_CHARS = 220
TITLE_CHARS = 120
TG_LIMIT = 3900               # UTF-16 units; Telegram allows 4096
MODEL_DEFAULT = "gemini-flash-lite-latest"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS digests (id INTEGER PRIMARY KEY, slot TEXT, created_at TEXT, sent_at TEXT, "
    "status TEXT, candidates INTEGER, chosen INTEGER, ai_calls INTEGER, message_ids TEXT, body TEXT)",
]
ITEM_COLUMNS = [("digested_at", "TEXT"), ("digest_id", "INTEGER"), ("ai_sentiment", "TEXT"),
                ("ai_impact", "INTEGER"), ("story_key", "TEXT"), ("ai_takeaways", "TEXT"), ("manual", "INTEGER DEFAULT 0")]
SENTIMENTS = ("positive", "neutral", "critical")


def settings(env):
    def num(k, d):
        try:
            return int(env.get(k) or d)
        except ValueError:
            return d
    per = max(1, num("DIGEST_PER_CATEGORY", PER_CATEGORY))
    wper = max(1, num("DIGEST_WEB_PER_CATEGORY", WEB_PER_CATEGORY))
    return {"per": per, "max": max(per, num("DIGEST_MAX", per * len(CATEGORIES))),
            "web_per": wper, "web_max": max(wper, num("DIGEST_WEB_MAX", wper * len(CATEGORIES))),
            "web_hours": num("DIGEST_WEB_HOURS", WEB_WINDOW_H),
            "ai_calls": num("DIGEST_AI_MAX_CALLS", AI_MAX_CALLS_PER_DAY),
            "model": (env.get("GEMINI_MODEL") or MODEL_DEFAULT).strip(),
            "feedback_url": (env.get("FEEDBACK_URL") or "").strip(),
            "feedback_csv": (env.get("FEEDBACK_CSV_URL") or "").strip()}


def load_env():
    env = rules.load_env()
    for k in ("GEMINI_MODEL", "DIGEST_MAX", "DIGEST_PER_CATEGORY", "DIGEST_AI_MAX_CALLS",
              "DIGEST_WEB_MAX", "DIGEST_WEB_PER_CATEGORY", "DIGEST_WEB_HOURS", "FEEDBACK_URL", "FEEDBACK_CSV_URL",
              "MINISTER_NAMES", "ADMIN_PIN"):
        if k not in env and os.environ.get(k):
            env[k] = os.environ[k]
    if env.get("MINISTER_NAMES"):
        os.environ["MINISTER_NAMES"] = env["MINISTER_NAMES"]
    return env


def sheet_settings(con, env):
    """The office's 'Settings' tab, as the collector stored it (cfg:KEY rows in runtime):
    MINISTER_NAMES, DIGEST_* sizes, GEMINI_MODEL, FEEDBACK_URL. The sheet wins over the
    repository variable, so there is one place to look."""
    try:
        rows = con.execute("SELECT k, v FROM runtime WHERE k LIKE 'cfg:%'").fetchall()
    except Exception:
        return {}
    out = {}
    for k, v in rows:
        key = k[4:]
        if v and key in ("MINISTER_NAMES", "GEMINI_MODEL", "DIGEST_PER_CATEGORY", "DIGEST_MAX", "DIGEST_WEB_PER_CATEGORY",
                         "DIGEST_WEB_MAX", "DIGEST_WEB_HOURS", "FEEDBACK_URL", "BLOCKED_HOSTS", "TELEGRAM_ADMIN_IDS"):
            env[key] = v
            os.environ[key] = v
            out[key] = v
    return out


def migrate(con):
    alerts.ensure_migrated(con)
    for ddl in SCHEMA:
        con.execute(ddl)
    have = {r[1] for r in con.execute("PRAGMA table_info(items)")}
    for name, decl in ITEM_COLUMNS:
        if name not in have:
            con.execute(f"ALTER TABLE items ADD COLUMN {name} {decl}")
    con.execute("CREATE INDEX IF NOT EXISTS ix_items_digested ON items(digested_at)")
    if rt_get(con, "story_keys") != "v2":
        # keys written before step 7 looked like "3:12" -- batch 3, story 12 -- and the same
        # key came up again in the next run on unrelated items. Forget them; the next
        # cluster call writes keys that carry the edition and a hash of its items.
        con.execute("UPDATE items SET story_key=NULL WHERE story_key IS NOT NULL AND story_key NOT LIKE '%@%' "
                    "AND story_key NOT LIKE 'r%'")
        rt_set(con, "story_keys", "v2")
    con.commit()


def slot_for(now):
    return "morning" if now.astimezone(IST).hour < 12 else "evening"


# --------------------------------------------------------------------------
# 1. candidates
# --------------------------------------------------------------------------

CAND_SQL = """
SELECT i.id, i.title, i.description, i.publisher, i.published_at, i.discovered_at, i.rules_at,
       i.resolve_status, i.resolved_url, i.link, i.extract_host, i.extract_status, i.extract_text,
       i.score, i.band, i.urgent, i.target_tags, i.matched_terms, i.event_id,
       i.ai_category, i.ai_priority, i.ai_reason, i.ai_processed_at, i.image_url,
       i.ai_sentiment, i.ai_impact, i.story_key, i.ai_takeaways, coalesce(i.manual, 0) AS manual,
       CASE WHEN i.raw_payload LIKE '%youtube_api%' THEN i.raw_payload END AS yt_payload,
       CASE WHEN coalesce(i.manual, 0) = 1 THEN i.raw_payload END AS manual_payload,
       s.name AS source_name
FROM items i LEFT JOIN sources s ON s.source_id = i.source_id
WHERE {scope}
  AND i.rules_at IS NOT NULL AND i.rules_at >= ?
  AND (i.band IN ('AUTO_KEEP', 'AI', 'KEYWORD_KEEP') OR i.urgent = 1)
  AND coalesce(i.published_at, i.discovered_at) >= ?
ORDER BY i.id
"""


_PUNCT = re.compile(r"[^\w\s஀-௿]+")


def norm_title(title, publisher=""):
    """For grouping: outlet suffix off, ASCII lowercased, punctuation and zero-width marks out."""
    t = rules.norm_match(rules.clean_title(title or "", publisher or ""))
    return " ".join(_PUNCT.sub(" ", t).split())


TITLE_SIM = 0.5               # 4-gram Jaccard on normalised titles; well above the 0.35 "topic" band
REPORTS_MAX = 8               # outlets shown behind a card; further reports are absorbed, never new cards


def _best_first(c):
    """Priority, urgency and score first; among equals the fuller article, then the latest --
    so the card's own link is the most detailed, most recent report of the story."""
    ts = parse_ts(c.get("published_at"))
    detail = len(c["extract_text"] or "") if c.get("extract_status") in ("OK", "THIN") and c.get("extract_text") else 0
    return (c.get("priority") or 9, -(c.get("urgent") or 0), -(c.get("score") or 0), -detail, -(ts.timestamp() if ts else 0))


def _merge(cands, links):
    """Collapse candidates into stories. links(rep, c) -> True when c reports rep's story.

    Anchored, not transitive: candidates are taken best first, and each one joins the first
    story whose REPRESENTATIVE it links to, or starts its own. A chain A~B, B~C, C~D can
    therefore never pull A and D together (the union-find this replaces did exactly that,
    and once it did, a handful of loose links could swallow twenty unrelated items).
    A story takes every report that links to it -- a big story is one card with many
    outlets, never many cards. Behind the card: one report per outlet (the outlet's most
    detailed, then latest), the representative's own first, at most REPORTS_MAX."""
    stories = []
    for c in sorted(cands, key=_best_first):
        for members in stories:
            if links(members[0], c):
                members.append(c)
                break
        else:
            stories.append([c])
    out = []
    for members in stories:
        rep = members[0]
        rep["sources"] = list(dict.fromkeys(s for m in members for s in m.get("sources") or []))
        rep["merged"] = [m["id"] for m in members]
        own = (rep.get("reports") or [None])[0]
        best = {}
        for m in members:
            for r in m.get("reports") or []:
                cur = best.get(r["outlet"])
                if cur is None or (r.get("detail", 0), r.get("ts", "")) > (cur.get("detail", 0), cur.get("ts", "")):
                    best[r["outlet"]] = r
        if own is not None:
            best[own["outlet"]] = own                 # the card's own link is the representative's
        reports = ([own] if own is not None else []) + [r for o, r in best.items() if own is None or o != own["outlet"]]
        rep["reports"] = reports[:REPORTS_MAX]
        out.append(rep)
    return out


def _load(con, scope, since, pub_since):
    rows = con.execute(CAND_SQL.format(scope=scope), (iso(since), iso(pub_since))).fetchall()
    cands = []
    for r in rows:
        d = dict(r)
        d["sources"] = [alerts.outlet_name(r)]
        d["reports"] = [{"outlet": alerts.outlet_name(r), "url": alerts.display_url(r),
                         "title": rules.clean_title(r["title"] or "", r["publisher"] or ""),
                         "lang": "Tamil" if re.search(r"[\u0B80-\u0BFF]", r["title"] or "") else "English",
                         "detail": len(r["extract_text"] or "") if r["extract_status"] in ("OK", "THIN") else 0,
                         "ts": r["published_at"] or r["discovered_at"] or ""}]
        d["tags"] = alerts._j(r["target_tags"], [])
        d["ntitle"] = norm_title(r["title"], r["publisher"])
        d["tgrams"] = frozenset(rules.sim_grams(d["ntitle"])) if len(d["ntitle"]) >= 12 else frozenset()
        cands.append(d)
    # Same event (body match), the same headline word for word, or one headline inside the
    # other (a Facebook or YouTube copy carries the post text around the headline): one story.
    reps = _merge(cands, lambda a, b: (a["event_id"] is not None and a["event_id"] == b["event_id"])
                  or (len(a["ntitle"]) >= 12 and a["ntitle"] == b["ntitle"])
                  or (len(a["ntitle"]) >= 30 and len(b["ntitle"]) >= 30 and (a["ntitle"] in b["ntitle"] or b["ntitle"] in a["ntitle"])))
    return reps, [r["id"] for r in rows]


def candidates(con, now):
    """The Telegram delta: kept items not yet covered by a brief."""
    last = parse_ts(rt_get(con, "last_digest_at"))
    since = max(now - timedelta(hours=WINDOW_CAP_H), last) if last else now - timedelta(hours=WINDOW_CAP_H)
    reps, ids = _load(con, "i.digested_at IS NULL", since, now - timedelta(hours=PUBLISHED_WITHIN_H))
    reps, _ = drop_reruns(con, reps, now - timedelta(hours=PUBLISHED_WITHIN_H))
    return reps, ids, since


def edition(con, now, hours=WEB_WINDOW_H):
    """The web page: everything kept and published in the last `hours`, covered or not, so
    the page is always a full edition rather than the last few arrivals."""
    reps, ids = _load(con, "1=1", now - timedelta(hours=hours + 24), now - timedelta(hours=hours))
    return reps, ids


# Words that never identify a story on their own: places every report shares, the words of
# the news trade, and the KINDS of event (a murder, a flood, a protest). Two floods share
# "flood"; only a shared victim, locality, official or company makes them one flood.
_CORE_STOP = set("""chennai tamil nadu tamilnadu india indian state states central centre news story live video watch
breaking update updates report reports today tonight yesterday tomorrow morning evening night week month
year years minister ministers ministry govt government collector collectorate corporation police cops
court high supreme order orders case cases issue issues work works project projects scheme schemes plan
plans people public residents road roads street water rain rains flood floods flooding protest protests
death deaths dead died dies killed kills murder murdered accident accidents arrested arrest arrests
attack attacked fire crore lakh lakhs rupees says said tells told will after before over amid near into
from with this that these those what when where which their there here about against between
without within under also more most other another first second third last next new latest
announce announces announced announcement inaugurate inaugurates inaugurated launch launches launched
opens opened meeting meet review reviews visit visits speech statement demand demands alleges
""".split())
_CORE_STOP_TA = set("""திரு திருமதி சென்னை சென்னையில் தமிழக தமிழகம் தமிழ்நாடு இந்தியா அரசு அரசின் அமைச்சர் முதல்வர்
ஆட்சியர் மாவட்ட மாவட்டம் மாநகராட்சி போலீஸ் போலீசார் காவல் நீதிமன்றம் உயர்நீதிமன்றம் மக்கள் பொதுமக்கள்
பணிகள் பணி திட்டம் திட்டங்கள் சாலை சாலைகள் தண்ணீர் மழை வெள்ளம் போராட்டம் மரணம் உயிரிழப்பு கொலை விபத்து
கைது தாக்குதல் தீ கோடி லட்சம் ரூபாய் இன்று நேற்று நாளை காலை மாலை இரவு வாரம் மாதம் ஆண்டு செய்தி செய்திகள்
வீடியோ நேரலை புதிய முதல் பிறகு பின்னர் மற்றும் என்று என எனவும் கூறினார் தெரிவித்தார் அறிவிப்பு அறிவித்தார்
திறப்பு திறந்து தொடக்கம் தொடங்கி ஆய்வு ஆய்வுக்கூட்டம் கூட்டம் பேட்டி பேச்சு கோரிக்கை குற்றச்சாட்டு
வழக்கு பிரச்சனை பிரச்சினை""".split())
_WORD = re.compile(r"[\u0B80-\u0BE5]+|[^\W\d_]+")   # a Tamil word (\w misses its vowel signs) or letters of any script; numbers never count
_TAMIL = re.compile(r"[஀-௿]")


def core_terms(c):
    """The words of a HEADLINE that can identify its story: names of people, places,
    organisations and things, 4+ letters, minus the generic words above (a Tamil word is
    generic when a generic word begins it: சென்னையில் is சென்னை). Numbers and dates are
    deliberately excluded: "October 10" or "450 crore" link nothing."""
    out = set()
    for w in _WORD.findall(_title(c)):
        if _TAMIL.search(w):
            if len(w) >= 4 and not any(w.startswith(s) for s in _CORE_STOP_TA):
                out.add("ta:" + w)
        else:
            low = w.lower()
            # 4+ letters, or a 3-letter acronym written in capitals (OMR, GST, NGT, ECR)
            if (len(low) >= 4 or (len(w) == 3 and w.isupper())) and low not in _CORE_STOP:
                out.add(low)
    return out


def _stem_en(w):
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    for suf in ("ing", "es", "ed", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[:-len(suf)]
    return w


def shared_core(a, b):
    """How many identifying words two headlines share. English: the same word or its plural /
    tense form (deepfake, deepfakes; advisory, advisories). Tamil: the same word, or one is
    the other plus a short case suffix (ஆவடி / ஆவடியில்), never a longer compound."""
    ta, tb = a["core"], b["core"]
    en_a = {_stem_en(x) for x in ta if not x.startswith("ta:")}
    en_b = {_stem_en(x) for x in tb if not x.startswith("ta:")}
    n = len(en_a & en_b)
    xs = [x[3:] for x in ta if x.startswith("ta:")]
    ys = [y[3:] for y in tb if y.startswith("ta:")]
    for x in xs:
        for y in ys:
            s, l = (x, y) if len(x) <= len(y) else (y, x)
            if l.startswith(s) and len(l) - len(s) <= 5:
                n += 1
                break
    return n


def share_core(a, b):
    return shared_core(a, b) >= 1


def _is_tamil(c):
    return bool(_TAMIL.search(c["title"] or ""))


def _confirm(a, b, why):
    """The gate every proposed link goes through.
    cluster  -- Gemini's edition-wide grouping, already held to the names it cited: accepted
                outright in one category (one event, one card per category); across categories
                only when the headlines share an identifying word.
    gemini   -- a story number from a ranking batch (no evidence check): within a day, same
                language with a shared identifying word, or Tamil/English in one category.
    title    -- near-identical headlines: within a day, with a shared identifying word.
    The same outlet twice is fine: an update and its first report are one story, and the
    card keeps the outlet's most detailed link."""
    if why == "cluster":
        return a.get("category") == b.get("category") or (_is_tamil(a) == _is_tamil(b) and share_core(a, b))
    pa, pb = parse_ts(a.get("published_at")), parse_ts(b.get("published_at"))
    if pa and pb and abs((pa - pb).total_seconds()) > 24 * 3600:
        return False
    if _is_tamil(a) != _is_tamil(b):
        return why == "gemini" and a.get("category") == b.get("category")
    return share_core(a, b)


def merge_stories(cands, con=None):
    """After ranking: Gemini's story keys and near-identical headlines propose links; the
    gate above accepts or refuses each one, and _merge never chains them. One event framed
    three ways by three outlets is one candidate; two events that merely rhyme stay two."""
    kw = vocabulary(con)
    for c in cands:
        if "core" not in c:
            c["core"] = core_terms(c)
            c["places"] = places(_title(c), kw)

    def same(a, b):
        # two headlines naming different places are two stories, whoever says otherwise
        # (a Velachery flood and an Adyar flood on the same evening)
        if a.get("places") and b.get("places") and not (a["places"] & b["places"]):
            return False
        if a.get("story") and a.get("story") == b.get("story"):
            return _confirm(a, b, "cluster" if "@" in a["story"] else "gemini")
        if a["tgrams"] and b["tgrams"] and len(a["ntitle"]) >= 25 and len(b["ntitle"]) >= 25 \
                and rules.jaccard(a["tgrams"], b["tgrams"]) >= TITLE_SIM:
            return _confirm(a, b, "title")
        # no Gemini grouping (budget spent, key missing): two reports in one category and one
        # language, within a day, that share three identifying words and a sixth of their
        # headline -- or two words and a quarter -- are one story. "MeitY advisory on
        # deepfakes / manipulated content" five ways passes (3-5 words, 0.24-0.39 overlap); a
        # murder and a sewage overflow, or two bag-of-words coincidences, never do.
        if a.get("category") == b.get("category") and a.get("category") != "none" and _is_tamil(a) == _is_tamil(b) \
                and a["tgrams"] and b["tgrams"]:
            pa, pb = parse_ts(a.get("published_at")), parse_ts(b.get("published_at"))
            if pa and pb and abs((pa - pb).total_seconds()) > 24 * 3600:
                return False
            n, j = shared_core(a, b), rules.jaccard(a["tgrams"], b["tgrams"])
            return (n >= 3 and j >= 0.15) or (n >= 2 and j >= 0.25)
        return False
    return _merge(cands, same)


# --------------------------------------------------------------------------
# 2. ranking -- Gemini, or the rules
# --------------------------------------------------------------------------

PROMPT = """You triage Tamil and English news for the office of R. Kumar, MLA for Velachery
(Chennai AC 26), Minister for Artificial Intelligence, Information Technology and Digital
Services, Government of Tamil Nadu, and District In-Charge Minister for Thiruvallur. He belongs
to Tamilaga Vettri Kazhagam (TVK), the governing party.

Give each item ONE category:
mention      - names or quotes THIS R. Kumar ("R. Kumar", "ஆர். குமார்", "Minister R. Kumar"),
               or criticism/praise of him. ONLY when the text literally names him. Minister
               Rajkumar (Housing), Minister Rajeev, Rajendra Kumar, R.B. Udhayakumar, C.T.R.
               Nirmal Kumar, Ramesh Kumar (Avadi MLA), Sarathkumar, or any other initial + Kumar
               is a DIFFERENT person: never "mention", never written as R. Kumar anywhere.
constituency - civic issues, flooding, sewage, drains, roads, lakes, MRTS, crime or law-and-order
               in Velachery assembly constituency ONLY: Velachery, Taramani/Tharamani, Adyar,
               Besant Nagar, Thiruvanmiyur, Pallikaranai, Adambakkam (and their colonies: Baby
               Nagar, Vijaya Nagar, Ram Nagar, Kakkan Bridge, Indira Nagar, Kasturba Nagar)
               -- whether or not he is named. NOT constituency, ever: Ambattur (that is
               "district"), Thoraipakkam, Sholinganallur, Perungudi, OMR beyond Thoraipakkam,
               Tambaram, Chromepet, Pallavaram, GST Road, Guindy, Saidapet, Porur, Medavakkam
district     - Thiruvallur district administration, collectorate, review meetings, Poondi
               reservoir, Gummidipoondi, Ponneri, Avadi, Ambattur, Poonamallee, Tiruttani
portfolio    - AI, IT, digital governance, ELCOT, TIDEL, StartupTN, data centres, GCC
               investment, IT corridor, and rival-state (Karnataka/Telangana) IT policy
political    - attacks or alliances specifically touching his seat (Velachery), his district
               (Thiruvallur) or his department (AI / IT). GENERIC party politics -- TVK vs DMK vs
               AIADMK battles, by-election results and celebrations elsewhere (Madurantakam,
               Dharapuram, Panaiyur ...), cadre meetings, alliance talk -- is "none"
opportunity  - schemes, inaugurations, foundation stones, summits he could attend
none         - irrelevant, a DIFFERENT person named Kumar (R.B. Udhayakumar, C.T.R. Nirmal Kumar,
               Ramesh Kumar of Avadi, actor Sarathkumar, Praveen/Vinoth/Ashok/Santhosh Kumar, Kumar
               Sanu, Akshay Kumar ...) unless our R. Kumar is also named or the story is about
               his constituency, film or cinema news, commercial real estate (office-space or
               rental guides, flats for sale, price-per-sq-ft trends; land governance --
               encroachment, acquisition, patta, poramboke -- is NOT this), hotel or
               motor-trade press releases, exam-prep content, a place with the same name
               outside Tamil Nadu

Priority: 1 immediate (flooding now, a death, a major protest, an urgent official statement in
his areas), 2 standard news worth reading today, 3 background.
Impact, 1-10: how much this matters to his office today. 9-10 a crisis or a decision he must act
on; 6-8 something he will be asked about; 3-5 worth knowing; 1-2 trivia.
Sentiment for his office: "positive" (schemes, achievements, new infrastructure, IT investment,
praise), "neutral" (routine civic updates, notices, court orders, general reporting),
"critical" (protests, civic failures, accidents, deaths, opposition attacks, power cuts,
waterlogging, criticism of him or the government).

Rules: a story about another district's collector or another state's IT minister is "none".
Local murders, fatal accidents, chain-snatching, sewage overflows, road cave-ins, tree falls and
major protests in Velachery, Adyar, Besant Nagar, Thiruvanmiyur, Tharamani, Adambakkam or
Pallikaranai are NEVER "none": they are "constituency", priority 1 or 2, whether or not he is
named. The same in Thiruvallur district towns is "district".
If your reason says the item is generic, unclear or has no link to him or his areas, the
category must be "none".

Also give each item a "story" number: items that report the SAME event -- the same
inauguration, statement, incident or announcement, in Tamil or English, however differently
headlined -- share one story number (use the number of the first such item). An item about
its own event gets its own number.

And "takeaways": exactly 2 bullet notes in plain English, 10-15 words each, for a reader
who will not open the article -- the fact and the figure (who, where, what, how much, when),
then what it means or what comes next. Tamil items get English notes too. No preamble,
no bullet symbols, no repetition of the headline. Name people exactly as the item names
them (Minister Rajkumar stays Minister Rajkumar); write "R. Kumar" ONLY when the item does.

Return one object per item, using the item numbers given.

ITEMS:
{payload}"""

RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {"type": "OBJECT", "properties": {
        "n": {"type": "INTEGER"},
        "category": {"type": "STRING", "enum": AI_CATEGORIES + ["none"]},
        "priority": {"type": "INTEGER"},
        "reason": {"type": "STRING"},
        "story": {"type": "INTEGER"},
        "impact": {"type": "INTEGER"},
        "sentiment": {"type": "STRING", "enum": list(SENTIMENTS)},
        "takeaways": {"type": "ARRAY", "items": {"type": "STRING"}}},
        "required": ["n", "category", "priority", "reason", "story", "impact", "sentiment", "takeaways"]},
}

TAKEAWAYS_PROMPT = """For each news item below write "takeaways": exactly 2 bullet notes in plain English,
10-15 words each, for a reader in the office of R. Kumar (MLA Velachery, Tamil Nadu Minister for
AI, IT and Digital Services, in-charge minister Thiruvallur) who will not open the article --
the fact and the figure (who, where, what, how much, when), then what it means or what comes
next. Tamil items get English notes. No preamble, no bullet symbols, no repetition of the
headline. Name people exactly as the item names them; write "R. Kumar" ONLY when the item
does -- an item marked [does not name R. Kumar] must not mention him at all.
Return one object per item, using the item numbers given.

ITEMS:
{payload}"""

TAKEAWAYS_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "n": {"type": "INTEGER"}, "takeaways": {"type": "ARRAY", "items": {"type": "STRING"}}},
    "required": ["n", "takeaways"]}}


def _takeaways(v):
    """Two clean notes, or []: strings only, bullets and numbering stripped, 160 chars each."""
    out = []
    for s in v if isinstance(v, list) else []:
        s = re.sub(r"^[\s•\-–*\d.)]+", "", str(s)).strip()
        if s:
            out.append(s[:160])
    return out[:2]


def _snippet(c):
    body = c["extract_text"] if c["extract_status"] in ("OK", "THIN") and c["extract_text"] else ""
    text = body or rules.strip_urls(rules.strip_html(c["description"] or ""))
    return rules.cut(" ".join(text.split()), SNIPPET_CHARS)


def _title(c):
    return rules.clean_title(c["title"] or "", c["publisher"] or "") or "(no title)"


class Gemini:
    """One JSON call per batch, with the free-tier guard from the Apps Script."""

    def __init__(self, key, model, con, max_calls, client=None):
        self.key, self.model, self.con, self.max_calls = key, model, con, max_calls
        self.calls = 0
        self.dead = None
        self.t0 = time.monotonic()
        self._client = client
        self.run = utcnow().strftime("%Y%m%d%H%M%S")   # story keys from this run never collide with another run's
        self.budget_s = AI_TIME_BUDGET_S

    @property
    def client(self):
        if self._client is None:
            import httpx
            self._client = httpx.Client(timeout=120.0)
        return self._client

    def _today_calls(self):
        day = utcnow().strftime("%Y-%m-%d")
        r = self.con.execute("SELECT calls FROM ai_budget WHERE day=?", (day,)).fetchone()
        return day, (r[0] if r else 0)

    def _count(self, day):
        self.con.execute("INSERT INTO ai_budget (day, calls, tokens) VALUES (?, 1, 0) "
                         "ON CONFLICT(day) DO UPDATE SET calls = calls + 1", (day,))
        self.con.commit()
        self.calls += 1

    def call(self, prompt, schema, label="call"):
        """One JSON call. Returns the parsed JSON, or None (and sets self.dead when it is
        pointless to keep trying this run)."""
        if self.dead:
            return None
        day, used = self._today_calls()
        if used >= self.max_calls:
            self.dead = f"daily budget of {self.max_calls} Gemini calls used"
            return None
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                                     "responseSchema": schema}}
        delay = AI_PAUSE_S
        for attempt in range(4):
            if time.monotonic() - self.t0 > self.budget_s:
                self.dead = "Gemini time budget spent"
                return None
            try:
                self._count(day)
                r = self.client.post(ENDPOINT.format(m=self.model), headers={"x-goog-api-key": self.key}, json=body)
                if r.status_code == 429:
                    log.warning(f"gemini {label}: rate limited, waiting {delay:.0f}s")
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                if r.status_code in (400, 401, 403, 404):
                    self.dead = f"HTTP {r.status_code}: {r.text[:120].replace(chr(10), ' ')}"
                    log.error(f"gemini {label}: {self.dead} -- check GEMINI_API_KEY / GEMINI_MODEL")
                    return None
                r.raise_for_status()
                return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
            except Exception as ex:
                log.warning(f"gemini {label}: attempt {attempt + 1}: {type(ex).__name__}: {str(ex)[:100]}")
                time.sleep(delay)
                delay = min(delay * 2, 60)
        self.dead = "Gemini kept failing"
        return None

    def rank(self, batch):
        """batch: candidate dicts. Returns {id: verdict dict} or None."""
        lines = [f"{n}. [{alerts.outlet_name(c)}] {_title(c)}\n   {_snippet(c)}" for n, c in enumerate(batch, 1)]
        data = self.call(PROMPT.format(payload="\n".join(lines)), RESPONSE_SCHEMA, "rank")
        if data is None:
            return None
        out = {}
        for it in data if isinstance(data, list) else []:
            try:
                i = int(it.get("n", 0)) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(batch):
                out[batch[i]["id"]] = _verdict(it, f"r{self.run}.{self.calls}:")
        return out


CLUSTER_PROMPT = """Below are news items from Tamil Nadu in Tamil and English, collected over about a day.
Find the items that report the SAME specific incident or announcement: the same murder, the
same inauguration, the same court order, the same press statement -- in Tamil or English,
however differently headlined. Two items belong together ONLY when BOTH are true:
1. they name the same core entities -- the same victim or accused, the same official, the
   same locality or building, the same company or scheme;
2. they describe the same thing happening at the same time.
NOT the same: two incidents of the same kind (two murders, two floods, two protests, two
accidents) in different places or with different people; the same subject on different
days; a follow-up, a reaction or an analysis of an event; a daily column; two items that
merely share a district, a party or a minister's name.
When torn, leave the item out of the group. A missed merge shows a story twice; a wrong
merge hides a story from the office. A big story may have ten or more reports: that is
one group, however large.

Return only the groups (2 or more items each). For each group give the item numbers and
"shared": the core names the items have in common, written exactly as they appear in the
items (Tamil names in Tamil, English names in English), for example
"Avadi, Ramesh, Sekar Nagar" or "ஆவடி, ரமேஷ், Avadi". Items that stand alone are not listed.

ITEMS:
{payload}"""

CLUSTER_SCHEMA = {"type": "OBJECT", "properties": {"groups": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "items": {"type": "ARRAY", "items": {"type": "INTEGER"}}, "shared": {"type": "STRING"}},
    "required": ["items", "shared"]}}}, "required": ["groups"]}

def _has_evidence(c, shared):
    """Gemini's "shared" names must actually occur in the item -- checked in the item's own
    script, so an English item is held to the Latin names and a Tamil item to the Tamil ones.
    Names the items "have in common" are by definition in every item, so a member must carry
    them all (or at least two of them, for a long list with one odd spelling). "Avadi,
    Kanchipuram" claimed for a murder in Avadi and a bank in Kanchipuram fails on both.
    No names in the item's script: nothing to hold it to, the gate in _confirm remains."""
    text = f"{_title(c)} {_snippet(c)}".lower()
    tamil = _is_tamil(c)
    words = {w.lower() for w in _WORD.findall(shared or "") if len(w) >= 3 and bool(_TAMIL.search(w)) == tamil}
    hits = sum(1 for w in words if w in text)
    return not words or hits == len(words) or hits >= 2


def cluster(gemini, con, cands, key_prefix):
    """One call over a whole edition. Gemini proposes groups with the names that prove them;
    a group that names nothing, or whose names do not occur in a member, is thinned or
    dropped here, before merge_stories applies its own gate. Size is no objection: a
    story ten outlets report is one group. Every item gets a
    key (its group's, or one of its own) so a later run links the same reports again
    without asking and no older key survives on it. Returns the number of groups kept."""
    if gemini is None or not cands or len(cands) < 2:
        return 0
    lines = [f"{n}. [{alerts.outlet_name(c)}] {_title(c)}\n   {_snippet(c)}" for n, c in enumerate(cands, 1)]
    data = gemini.call(CLUSTER_PROMPT.format(payload="\n".join(lines)), CLUSTER_SCHEMA, "cluster")
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        return 0
    taken, kept, dropped, refused = set(), 0, Counter(), []
    for c in cands:
        c["story"] = None
    for g in data["groups"]:
        nums = sorted({_int(n, 1, len(cands), 0) for n in (g.get("items") or []) if _int(n, 1, len(cands), 0)})
        nums = [n for n in nums if n not in taken]
        shared = str(g.get("shared") or "").strip()
        if len(nums) < 2:
            continue
        if not shared:
            dropped["no shared names"] += 1
            refused.append({"why": "no shared names", "shared": "", "titles": [_title(cands[n - 1])[:80] for n in nums[:4]]})
            continue
        proven = [n for n in nums if _has_evidence(cands[n - 1], shared)]
        if len(proven) < len(nums):
            refused.append({"why": "names not in the items" if len(proven) < 2 else "thinned",
                            "shared": shared[:80], "titles": [_title(cands[n - 1])[:80] for n in nums if n not in proven][:4]})
        nums = proven
        if len(nums) < 2:
            dropped["names not in the items"] += 1
            continue
        for n in nums:
            cands[n - 1]["story"] = f"{key_prefix}:{nums[0]}"
            taken.add(n)
        kept += 1
    for n, c in enumerate(cands, 1):
        if not c["story"]:
            c["story"] = f"{key_prefix}:s{n}"
        con.execute("UPDATE items SET story_key=? WHERE id=?", (c["story"], c["id"]))
    con.commit()
    if dropped:
        log.info("cluster: refused " + ", ".join(f"{n} group(s) {why}" for why, n in dropped.items()))
    cluster.last_refused = refused
    return kept


def _int(v, lo, hi, default):
    try:
        return min(hi, max(lo, int(v)))
    except (TypeError, ValueError):
        return default


def _verdict(it, story_prefix):
    cat = it.get("category") if it.get("category") in AI_CATEGORIES + ["none"] else "none"
    story = _int(it.get("story"), 0, 10 ** 6, 0)
    return {"category": cat, "priority": _int(it.get("priority"), 1, 3, 2),
            "reason": str(it.get("reason", ""))[:200],
            "story": f"{story_prefix}{story}" if story else None,
            "impact": _int(it.get("impact"), 1, 10, 5),
            "sentiment": it.get("sentiment") if it.get("sentiment") in SENTIMENTS else "neutral",
            "takeaways": _takeaways(it.get("takeaways"))}


def rule_rank(c):
    """The keyword fallback: category from the rule tags, priority from urgency and band."""
    tags = c.get("tags") or []
    cat = "none"
    for tag, name in (("mention", "mention"), ("constituency", "constituency"), ("district", "district"),
                      ("portfolio", "portfolio"), ("party", "political")):
        if tag in tags and (tag != "mention" or names_in(_text(c), tags)):
            cat = name
            break
    if cat == "none" and c.get("urgent"):
        cat = "constituency"
    if c.get("urgent"):
        pri, impact, sent = 1, 8, "critical"
    elif c.get("band") == "AUTO_KEEP":
        pri, impact, sent = 2, 6, "neutral"
    else:
        pri, impact, sent = 3, 4, "neutral"
    return {"category": cat, "priority": pri, "reason": "keyword rules", "story": None,
            "impact": impact, "sentiment": sent, "takeaways": []}


def _apply(c, v, by):
    c["category"], c["priority"], c["reason"], c["by"] = v["category"], v["priority"], v["reason"], by
    c["impact"], c["sentiment"] = v.get("impact") or 5, v.get("sentiment") or "neutral"
    c["takeaways"] = v.get("takeaways") or []
    if v.get("story"):
        c["story"] = v["story"]


# --------------------------------------------------------------------------
# 2a. guards -- what Gemini may not decide on its own
# --------------------------------------------------------------------------

MINISTER_NAMES_DEFAULT = ("R. Kumar|R Kumar|Kumar.R|Kumar. R|ஆர். குமார்|ஆர்.குமார்|ஆர் குமார்|குமார்.ரா|குமார் ரா|"
                          "Dr. குமார்.ரா|Dr. Kumar.R|kumarr|"
                          "அமைச்சர் ஆர். குமார்|அமைச்சர் குமார்|அமைச்சர் Dr. குமார்|Dr. குமார்|டாக்டர் குமார்|Dr. Kumar|Minister Kumar")
# forms other people share (another state's Minister Kumar, any Dr Kumar): they count only
# next to a Tamil Nadu anchor
GENERIC_FORMS = ("அமைச்சர் குமார்", "minister kumar", "dr. குமார்", "டாக்டர் குமார்", "dr. kumar", "அமைச்சர் dr. குமார்")
_TN_ANCHOR = re.compile(r"tamil ?nadu|chennai|velachery|thiruvallur|tiruvallur|elcot|tidel|startuptn|"
                        r"தமிழக|தமிழ்நாடு|சென்னை|வேளச்சேரி|திருவள்ளூர்", re.I)
_LATIN_RE = re.compile(r"[A-Za-z]")

# Built-in place vocabularies, grouped so a Tamil and an English spelling are one place. The
# keyword sheet remains the master list (its place groups are used too); these are the floor.
AC26 = {"velachery": ("velachery", "வேளச்சேரி"), "taramani": ("taramani", "tharamani", "தரமணி"),
        "adyar": ("adyar", "அடையாறு", "அடையார்"), "besant nagar": ("besant nagar", "பெசன்ட் நகர்", "பெசண்ட் நகர்"),
        "thiruvanmiyur": ("thiruvanmiyur", "tiruvanmiyur", "திருவான்மியூர்"), "pallikaranai": ("pallikaranai", "பள்ளிக்கரணை"),
        "adambakkam": ("adambakkam", "ஆதம்பாக்கம்"), "baby nagar": ("baby nagar", "பேபி நகர்"),
        "vijaya nagar": ("vijaya nagar", "vijayanagar", "விஜயநகர்", "விஜய நகர்"), "ram nagar": ("ram nagar", "ராம் நகர்"),
        "kakkan bridge": ("kakkan bridge", "காக்கன்"), "indira nagar": ("indira nagar", "இந்திரா நகர்"),
        "kasturba nagar": ("kasturba nagar", "கஸ்தூரிபா நகர்"), "guru nanak": ("guru nanak",)}
NOT_AC26 = {"ambattur": ("ambattur", "ஆம்பத்தூர்"), "thoraipakkam": ("thoraipakkam", "thuraipakkam", "துரைப்பாக்கம்"),
            "sholinganallur": ("sholinganallur", "சோழிங்கநல்லூர்"), "perungudi": ("perungudi", "பெருங்குடி"),
            "tambaram": ("tambaram", "தாம்பரம்"), "chromepet": ("chromepet", "chrompet", "குரோம்பேட்டை"),
            "pallavaram": ("pallavaram", "பல்லாவரம்"), "gst road": ("gst road", "ஜிஎஸ்டி சாலை"), "guindy": ("guindy", "கிண்டி"),
            "saidapet": ("saidapet", "சைதாப்பேட்டை"), "porur": ("porur", "போரூர்"), "medavakkam": ("medavakkam", "மேடவாக்கம்"),
            "karapakkam": ("karapakkam",), "navalur": ("navalur",), "siruseri": ("siruseri",), "kelambakkam": ("kelambakkam",)}
DISTRICT = {"thiruvallur": ("thiruvallur", "tiruvallur", "திருவள்ளூர்"), "avadi": ("avadi", "ஆவடி"), "ambattur": ("ambattur", "ஆம்பத்தூர்"),
            "poonamallee": ("poonamallee", "பூந்தமல்லி"), "gummidipoondi": ("gummidipoondi", "கும்மிடிப்பூண்டி"),
            "ponneri": ("ponneri", "பொன்னேரி"), "tiruttani": ("tiruttani", "திருத்தணி"), "poondi": ("poondi", "பூண்டி")}
AC26_WORDS = tuple(w for ws in AC26.values() for w in ws)
NOT_AC26_WORDS = tuple(w for ws in NOT_AC26.values() for w in ws)
DISTRICT_PLACES = tuple(w for ws in DISTRICT.values() for w in ws)
PORTFOLIO_WORDS = re.compile(r"\b(ai|artificial intelligence|it\b|information technology|elcot|tidel|startuptn|"
                             r"data cent|digital|semiconductor|gcc)\b|செயற்கை நுண்ணறிவு|தகவல் தொழில்நுட்ப|எல்காட்|டைடல்", re.I)
# commercial property marketing -- never news; land GOVERNANCE is, and bypasses this
COMMERCIAL_RE = re.compile(r"office space|office rent|rent guide|rental guide|per sq\.? ?ft|sq\.? ?ft rent|"
                           r"flats? for sale|plots? for sale|\b[1-5] ?bhk\b|luxury (?:apartment|villa|flat)|"
                           r"price trends?|ready[- ]to[- ]move|real estate (?:guide|listing|prices?)|"
                           r"விற்பனைக்கு|வாடகைக்கு|சதுர அடி", re.I)
GOVERNANCE_RE = re.compile(r"collector|corporation|encroach|acquisition|acquire|demoli|\blake\b|patta|poramboke|"
                           r"cmda|tahsildar|revenue|eviction|court|tribunal|ngt\b|"
                           r"ஆட்சியர்|மாநகராட்சி|ஆக்கிரமிப்பு|கையகப்படுத்த|இடிப்பு|இடிக்க|ஏரி|பட்டா|புறம்போக்கு|வருவாய்|நீதிமன்ற", re.I)

_NAME_CACHE = {}


def _form_re(form):
    """One name form -> a regex that also takes Tamil case endings (குமாரின், குமாருக்கு) and
    optional dots/spaces between the parts; never a longer name (Kumaraswamy, குமாரசாமி)."""
    pieces = [p for p in re.split(r"([.\s]+)", form.strip()) if p]
    pat = ""
    for p in pieces:
        if re.fullmatch(r"[.\s]+", p):
            # a dot in the form is required ("Kumar.R" never matches "Praveen Kumar R"); a
            # space may be any amount of space
            pat += r"\.\s*" if "." in p else r"\s+"
        elif _TAMIL.search(p):
            # a final pulli may be any case sign (குமார் / குமாரின் / குமாருக்கு); a consonant
            # there is another name (குமாரசாமி, குமாரன்) and does not match; a token without
            # a pulli (ரா) must end there, so குமார் ரா is not குமார் ராஜன்
            pat += re.escape(p.rstrip("\u0BCD")) + (rules._TA_END if p.endswith("\u0BCD") else r"(?![\u0B80-\u0BE5])")
        else:
            pat += re.escape(p.lower())
    if not _TAMIL.search(form):
        pat = r"(?<![a-z0-9])" + pat + r"(?![a-z0-9])"
    else:
        pat = r"(?<![\u0B80-\u0BE5])" + pat
    return re.compile(pat, re.I)


def name_forms():
    raw = (os.environ.get("MINISTER_NAMES") or MINISTER_NAMES_DEFAULT).strip() or MINISTER_NAMES_DEFAULT
    if raw not in _NAME_CACHE:
        forms = [f.strip() for f in raw.split("|") if f.strip()]
        _NAME_CACHE[raw] = [(f, _form_re(f), f.lower() in GENERIC_FORMS) for f in forms]
    return _NAME_CACHE[raw]


def names_in(text, tags=(), strict=False):
    """Does this text name the minister? A generic form ("அமைச்சர் குமார்", "Minister Kumar")
    counts only next to a Tamil Nadu anchor -- a place, his department, or a sheet tag --
    unless `strict` (used on Gemini's own notes, where any such phrase is the problem)."""
    text = text or ""
    low = text.lower()
    anchored = bool(_TN_ANCHOR.search(text)) or bool({"constituency", "district", "portfolio"} & set(tags or ()))
    for _form, rx, generic in name_forms():
        if rx.search(low if not _TAMIL.search(rx.pattern) else text):
            if not generic or strict or anchored:
                return True
    return False


def _ta_stem(w):
    """A Tamil place inflects at its end: அடையாறு -> அடையாறில், ஆதம்பாக்கம் -> ஆதம்பாக்கத்தில்.
    Match the stem, so the locality is found in any case."""
    if _TAMIL.search(w):
        if w.endswith("ம்"):
            return w[:-2]
        if w.endswith(("ு", "்")):
            return w[:-1]
    return w


def _has_any(text, words):
    low = (text or "").lower()
    return any(_ta_stem(w) in low for w in words)


def _ac26(text):
    return _has_any(text, AC26_WORDS)


_VOCAB = {}


def vocabulary(con):
    """The keyword sheet as cached in the database (what the collector scored with), for
    place groups; None when there is none."""
    k = id(con)
    if k not in _VOCAB:
        rows = rules._cached_rows(con) if con is not None else []
        _VOCAB[k] = rules.Keywords(rows, origin="cache") if rows else None
    return _VOCAB[k]


def places(text, kw=None):
    """The place groups a text names -- built-in localities and the sheet's place tiers --
    so two reports naming different places are never one story."""
    low = (text or "").lower()
    out = set()
    for table in (AC26, NOT_AC26, DISTRICT):
        for grp, words in table.items():
            if any(_ta_stem(w) in low for w in words):
                out.add(grp)
    if kw is not None:
        try:
            for tier, found in kw.hits(rules.norm_match(text or "")).items():
                if tier in rules.PLACE_TIERS:
                    out.update((term.group or term.term).lower() for term, _ in found)
        except Exception:
            pass
    return out


def _text(c):
    return f"{_title(c)} {_snippet(c)}"


def _refile(c, exclude):
    """The keyword rules' next-best category for a card a guard has rejected: only a
    category whose own evidence is present, never the one just rejected."""
    tags = set(c.get("tags") or [])
    text = _text(c)
    order = [("mention", lambda: names_in(text, tags)),
             ("constituency", lambda: (_ac26(_title(c)) or ("constituency" in tags and not _has_any(_title(c), NOT_AC26_WORDS))
                                       or (_ac26(text) and not _has_any(_title(c), NOT_AC26_WORDS)))),
             ("district", lambda: "district" in tags or _has_any(text, DISTRICT_PLACES)),
             ("portfolio", lambda: "portfolio" in tags or bool(PORTFOLIO_WORDS.search(text))),
             ("political", lambda: "party" in tags and _anchored(c))]
    for cat, ok in order:
        if cat not in exclude and ok():
            return cat
    return "none"


def _anchored(c):
    """Something of his in the article: his name, an AC-26 locality, a Thiruvallur-district
    place, or his portfolio -- by the sheet's tags or the built-in lists."""
    tags = set(c.get("tags") or [])
    text = _text(c)
    return bool({"constituency", "district", "portfolio"} & tags) or names_in(text, tags) \
        or _ac26(text) or _has_any(text, DISTRICT_PLACES) or bool(PORTFOLIO_WORDS.search(text))


def guard(c):
    """Re-files a verdict the article cannot support. Returns the reason, or None. Priority,
    impact and sentiment stay as ranked; only the category (and tainted notes) change."""
    text, title, tags = _text(c), _title(c), set(c.get("tags") or [])
    why = None
    cat = c.get("category") or "none"
    if cat != "none" and COMMERCIAL_RE.search(text) and not GOVERNANCE_RE.search(text):
        cat, why = "none", "commercial listing"
    elif cat == "mention" and not names_in(text, tags):
        cat, why = _refile(c, {"mention"}), "name not in article"
    elif cat == "constituency" and not _ac26(title):
        if _has_any(title, NOT_AC26_WORDS):
            cat, why = _refile(c, {"constituency"}), "outside Velachery (headline)"
        elif not (_ac26(text) or "constituency" in tags):
            cat, why = _refile(c, {"constituency"}), "no Velachery locality in article"
    elif cat == "political" and not _anchored(c):
        cat, why = _refile(c, {"political"}), "generic politics"
    elif cat not in ("mention", "none") and names_in(text, tags):
        cat, why = "mention", "names him"          # mention outranks every other section, urgent included
    if why:
        c["category"], c["guard"] = cat, why
        c["reason"] = f"{c.get('reason', '')} [guard: {why} -> {cat}]"[:240]
    # notes that put his name in an article that does not carry it are thrown away and
    # written again with the article marked (fill_takeaways)
    if c.get("takeaways") and not names_in(text, tags):
        if any(names_in(n, (), strict=True) for n in c["takeaways"]):
            c["takeaways"], c["notes_note"] = [], "does not name R. Kumar"
            c["guard"] = c.get("guard") or "notes named him"
    return why


def _place_manual(c):
    """A link the office handed in is shown whatever the verdict: in the category it asked
    for ('url | district'), else Gemini's, else the rules', else the 📌 section."""
    asked = alerts._j(c.get("manual_payload"), {}) or {}
    if asked.get("category") in AI_CATEGORIES:
        c["category"] = asked["category"]
    elif c.get("category") in (None, "none", "added"):
        c["category"] = _refile(c, set())
        if c["category"] == "none":
            c["category"] = "added"
    if asked.get("sentiment") in SENTIMENTS:
        c["sentiment"] = asked["sentiment"]
    c["reason"] = (c.get("reason") or "") + " [added by the office]"


def rank_all(con, cands, gemini, now):
    """Fills category, priority, reason, impact, sentiment, by. Reuses Gemini's stored verdict
    on an item when there is one (a re-run costs nothing). Every verdict, live or cached,
    then passes the guards above."""
    todo = []
    for c in cands:
        if c["ai_processed_at"] and c["ai_category"]:
            _apply(c, {"category": c["ai_category"], "priority": c["ai_priority"] or 2, "reason": c["ai_reason"] or "",
                       "impact": c.get("ai_impact") or 5, "sentiment": c.get("ai_sentiment") or "neutral",
                       "story": c.get("story_key"), "takeaways": alerts._j(c.get("ai_takeaways"), [])}, "gemini (cached)")
        else:
            todo.append(c)
    st = Counter(cached=len(cands) - len(todo)) if len(cands) > len(todo) else Counter()
    if gemini is not None:
        for k in range(0, len(todo), AI_BATCH):
            batch = todo[k:k + AI_BATCH]
            res = gemini.rank(batch)
            if res is None:
                break
            for c in batch:
                if c["id"] in res:
                    _apply(c, res[c["id"]], "gemini")
                    con.execute("""UPDATE items SET ai_category=?, ai_priority=?, ai_reason=?, ai_model=?,
                                   ai_processed_at=?, ai_impact=?, ai_sentiment=?, ai_takeaways=?,
                                   story_key=coalesce(story_key, ?) WHERE id=?""",
                                (c["category"], c["priority"], c["reason"], gemini.model, iso(now),
                                 c["impact"], c["sentiment"], json.dumps(c["takeaways"], ensure_ascii=False) if c["takeaways"] else None,
                                 c.get("story"), c["id"]))
                    st["gemini"] += 1
            con.commit()
            if k + AI_BATCH < len(todo):
                time.sleep(AI_PAUSE_S)
    for c in cands:
        if "category" not in c:
            _apply(c, rule_rank(c), "rules")
            st["rules"] += 1
    for c in cands:
        why = guard(c)
        if why:
            st["guarded"] += 1
            st.setdefault("guards", Counter())[why] += 1
        if c.get("manual"):
            _place_manual(c)
            st["manual"] += 1
    if gemini is not None and gemini.dead:
        log.warning(f"gemini: stopped -- {gemini.dead}; {st['rules']} items ranked by the rules")
    return st


def fill_takeaways(gemini, con, cands, now):
    """Notes for chosen stories that have none yet -- items ranked before notes existed, or by
    the rules. One call per 40, stored on the item; returns how many were filled."""
    todo = [c for c in cands if not c.get("takeaways")]
    if gemini is None or not todo:
        return 0
    done = 0
    for k in range(0, len(todo), 40):
        batch = todo[k:k + 40]
        lines = [f"{n}. [{alerts.outlet_name(c)}]{' [does not name R. Kumar]' if c.get('notes_note') else ''} {_title(c)}\n   {_snippet(c)}"
                 for n, c in enumerate(batch, 1)]
        data = gemini.call(TAKEAWAYS_PROMPT.format(payload="\n".join(lines)), TAKEAWAYS_SCHEMA, "takeaways")
        if not isinstance(data, list):
            break
        for it in data:
            i = _int(it.get("n"), 1, len(batch), 0) - 1
            notes = _takeaways(it.get("takeaways")) if i >= 0 else []
            if notes and batch[i].get("notes_note") and any(names_in(n, (), strict=True) for n in notes):
                notes = []                            # it did it again; the card shows the snippet instead
            if notes:
                batch[i]["takeaways"] = notes
                con.execute("UPDATE items SET ai_takeaways=? WHERE id=?", (json.dumps(notes, ensure_ascii=False), batch[i]["id"]))
                done += 1
        con.commit()
    return done


def drop_reruns(con, cands, since, days=2):
    """Stale re-runs out: a candidate whose headline or body is RERUN_SIM-similar to a kept
    item published in the `days` before the window is yesterday's story syndicated again,
    not news. A development on the same event has its own headline (an arrest, an inquiry,
    a statement) and stays. Returns (kept, dropped_count)."""
    rows = con.execute("""SELECT title, publisher, extract_text, extract_status FROM items
                          WHERE coalesce(published_at, discovered_at) >= ? AND coalesce(published_at, discovered_at) < ?
                            AND (band IN ('AUTO_KEEP', 'AI', 'KEYWORD_KEEP') OR urgent = 1)""",
                       (iso(since - timedelta(days=days)), iso(since))).fetchall()
    if not rows or not cands:
        return cands, 0
    prev = []
    for r in rows:
        nt = norm_title(r["title"], r["publisher"])
        body = (r["extract_text"] or "")[:rules.BODY_CAP] if r["extract_status"] == "OK" else ""
        prev.append((nt, frozenset(rules.sim_grams(nt)) if len(nt) >= 25 else frozenset(),
                     frozenset(rules.sim_grams(body)) if len(body) >= 300 else frozenset()))
    kept, dropped = [], 0
    for c in cands:
        if c.get("manual"):
            kept.append(c)
            continue
        body = (c["extract_text"] or "")[:rules.BODY_CAP] if c["extract_status"] == "OK" else ""
        bg = frozenset(rules.sim_grams(body)) if len(body) >= 300 else frozenset()
        stale = False
        for nt, tg, pg in prev:
            # the same body under a new headline still needs the headlines to rhyme a little
            # (0.3, the topic band), so boilerplate-heavy pages never make strangers re-runs
            if (len(c["ntitle"]) >= 12 and c["ntitle"] == nt) \
                    or (c["tgrams"] and tg and rules.jaccard(c["tgrams"], tg) >= RERUN_SIM) \
                    or (bg and pg and rules.jaccard(bg, pg) >= RERUN_SIM
                        and c["tgrams"] and tg and rules.jaccard(c["tgrams"], tg) >= 0.3):
                stale = True
                break
        if stale:
            dropped += 1
        else:
            kept.append(c)
    return kept, dropped


# --------------------------------------------------------------------------
# 3. selection
# --------------------------------------------------------------------------

def _order(c):
    return (c["priority"], -(c.get("urgent") or 0), -(c.get("impact") or 0), -(c.get("score") or 0),
            c["published_at"] or "")


UNCAPPED = ("mention", "added")   # everything about him, and everything the office added, is shown


def select(cands, per=PER_CATEGORY, total=MAX_TOTAL):
    """5 per category first (priority 1 and 2 only); then spare places go to the next-best
    leftovers from any category, priority 3 included, until `total`. Mentions and the office's
    own links are never capped: 15 reports naming him are 15 cards. Returns
    (urgent_block, {category: [items]}, leftovers)."""
    pool = [c for c in cands if c["category"] != "none"]
    by_cat = defaultdict(list)
    seen_story = {}                      # (category, story key) -> the card; one event, one card per category
    # always on, never capped: the office's own links, and every mention of him -- a day he
    # is in the news 15 times is a day the office wants all 15 (MENTION_CAP is a sanity ceiling)
    manual = [c for c in pool if c.get("manual")]
    mentions = sorted([c for c in pool if c["category"] == "mention" and not c.get("manual")], key=_order)[:MENTION_CAP]
    forced = manual + mentions
    pool = [c for c in pool if c not in forced]
    for c in sorted(pool, key=_order):
        k = (c["category"], c.get("story")) if c.get("story") and "@" in c["story"] else None
        if k in seen_story:
            first = seen_story[k]
            first["sources"] = list(dict.fromkeys(first["sources"] + c.get("sources", [])))
            first["merged"] = first.get("merged", [first["id"]]) + c.get("merged", [c["id"]])
            have = {r["outlet"] for r in first["reports"]}
            first["reports"] = (first["reports"] + [r for r in c.get("reports") or [] if r["outlet"] not in have])[:REPORTS_MAX]
            continue
        if k:
            seen_story[k] = c
        by_cat[c["category"]].append(c)
    chosen, leftovers = list(forced), []
    for cat in CATEGORIES:
        head = list(by_cat.get(cat, [])) if cat in UNCAPPED else [c for c in by_cat.get(cat, []) if c["priority"] <= 2][:per]
        chosen += head
        leftovers += [c for c in by_cat.get(cat, []) if c not in head]
    leftovers.sort(key=_order)
    room = max(0, total - len(chosen))
    chosen += leftovers[:room]
    leftovers = leftovers[room:]
    urgent = [c for c in chosen if c["priority"] == 1 and c["category"] in URGENT_CATEGORIES
              and (c.get("urgent") or c["by"].startswith("gemini"))]
    urgent.sort(key=_order)
    sections = {cat: [c for c in chosen if c["category"] == cat and c not in urgent] for cat in CATEGORIES}
    for cat in sections:
        sections[cat].sort(key=_order)
    return urgent, sections, leftovers


# --------------------------------------------------------------------------
# 4. rendering
# --------------------------------------------------------------------------

def _line(c, mark=True):
    t = rules.cut(_title(c), TITLE_CHARS)
    url = alerts.display_url(c)
    outlet = alerts.outlet_name(c)
    extra = ""
    if len(c.get("sources") or []) > 1:
        extra = f" +{len(c['sources']) - 1}"
    mark = "🚨 " if (mark and c.get("urgent") and c["priority"] == 1) else ""
    return f"• {mark}<a href=\"{esc_attr(url)}\">{esc(t)}</a> — {esc(outlet)}{extra}"


def render(slot, now, urgent, sections, n_total, n_cands, by_gemini):
    day = now.astimezone(IST)
    head = f"{'🌅' if slot == 'morning' else '🌆'} <b>{'Morning' if slot == 'morning' else 'Evening'} brief · {day.day} {day:%b} · {n_total} items</b>"
    blocks = [[head, f"<i>{n_cands} stories considered{'' if by_gemini or not n_cands else ' · ranked by keywords (no AI)'}</i>"]]
    if urgent:
        blocks.append([f"🚨 <b>URGENT ({len(urgent)})</b>"] + [_line(c, mark=False) for c in urgent])
    for cat in CATEGORIES:
        items = sections.get(cat) or []
        if items:
            blocks.append([f"{ICONS[cat]} <b>{LABELS[cat]} ({len(items)})</b>"] + [_line(c) for c in items])
    if n_total == 0:
        blocks.append(["Nothing worth reporting since the last brief."])
    # pack blocks into messages under the Telegram limit; split a block only if it must be
    messages, cur = [], []

    def fits(lines):
        return rules.utf16_len("\n".join(lines)) <= TG_LIMIT

    def flush():
        if cur:
            messages.append("\n".join(cur))
            cur.clear()

    for b in blocks:
        gap = [""] if cur else []
        if fits(cur + gap + b):
            cur.extend(gap + b)
            continue
        if cur and fits(b):                 # the block fits on its own: start a new message
            flush()
            cur.extend(b)
            continue
        head = b[0]                          # too long even alone: continue it across messages
        cur.extend(gap + [head])
        for line in b[1:]:
            if not fits(cur + [line]):
                flush()
                cur.append(head + " (contd)")
            cur.append(line)
    flush()
    if len(messages) > 1:
        messages = [f"{m}\n\n<i>({k + 1}/{len(messages)})</i>" for k, m in enumerate(messages)]
    return messages


# --------------------------------------------------------------------------
# 5. the run
# --------------------------------------------------------------------------

def build(con, env, now, slot, transport_client=None):
    cfg = settings(env)
    cands, all_ids, since = candidates(con, now)
    key = (env.get("GEMINI_API_KEY") or "").strip()
    # even with nothing new for Telegram the web editions are rebuilt afterwards, and they
    # need Gemini for the clusters and the briefing (an empty delta used to leave a stale one)
    gemini = Gemini(key, cfg["model"], con, cfg["ai_calls"], transport_client) if key else None
    st = rank_all(con, cands, gemini, now)
    before = len(cands)
    cands = merge_stories(cands, con)
    st["merged"] = before - len(cands)
    urgent, sections, leftovers = select(cands, cfg["per"], cfg["max"])
    chosen = urgent + [c for cat in CATEGORIES for c in sections[cat]]
    messages = render(slot, now, urgent, sections, len(chosen), len(cands), bool(st.get("gemini")))
    return {"cands": cands, "all_ids": all_ids, "since": since, "chosen": chosen, "leftovers": leftovers,
            "urgent": urgent, "sections": sections, "gemini": gemini,
            "messages": messages, "stats": st, "ai_calls": gemini.calls if gemini else 0,
            "ai_note": gemini.dead if gemini else ("no GEMINI_API_KEY" if not key else None)}


# --------------------------------------------------------------------------
# 4b. the web page (GitHub Pages serves docs/)
# --------------------------------------------------------------------------

DOCS = HERE / "docs"
REASONS = (("unrelated", "Unrelated to constituency / portfolio"), ("category", "Wrong category"),
           ("old", "Duplicate / old"), ("spam", "Spam / noise"))


def _image_for(c):
    if c.get("image_url"):                     # '' means the page was checked and has none
        return c["image_url"]
    if c.get("yt_payload"):
        try:
            th = (json.loads(c["yt_payload"]).get("snippet") or {}).get("thumbnails") or {}
            for k in ("medium", "high", "default"):
                if th.get(k, {}).get("url"):
                    return th[k]["url"]
        except ValueError:
            pass
    return None



def run(con, env, now=None, slot=None, dry_run=False, transport="auto", client=None):
    now = now or utcnow()
    slot = slot or slot_for(now)
    migrate(con)
    b = build(con, env, now, slot, client)
    for m in b["messages"]:
        log.info("digest:\n" + alerts.html_to_plain(m))
    log.info(f"digest: {len(b['cands'])} stories from {len(b['all_ids'])} items since {fmt_ist(iso(b['since']))}; "
             f"{len(b['chosen'])} chosen, {len(b['leftovers'])} left out; ranked by "
             + ", ".join(f"{k} {v}" for k, v in b["stats"].items())
             + (f"; gemini note: {b['ai_note']}" if b["ai_note"] else ""))
    if dry_run:
        _pages(con, env, slot, now, b)
        log.info("digest: DRY RUN -- nothing sent, nothing marked; web page written to docs/ for preview")
        return b
    tr = alerts.make_transport(env, client) if transport == "auto" else transport
    if tr is None:
        log.warning("digest: Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) -- nothing sent, "
                    "nothing marked; the same stories will be offered again next time")
        return b
    cur = con.execute("INSERT INTO digests (slot, created_at, status, candidates, chosen, ai_calls, body) "
                      "VALUES (?, ?, 'SENDING', ?, ?, ?, ?)",
                      (slot, iso(now), len(b["cands"]), len(b["chosen"]), b["ai_calls"], "\n\n".join(b["messages"])))
    did = cur.lastrowid
    con.commit()
    ids = []
    for k, m in enumerate(b["messages"]):
        res = tr.send(m)
        if res.kind == "parse":
            log.error(f"digest: Telegram rejected the HTML ({res.error}); sending plain text")
            res = tr.send(alerts.html_to_plain(m), plain=True)
        if not res.ok:
            log.error(f"digest: send failed ({res.kind}: {res.error}); nothing marked, will retry next run")
            con.execute("UPDATE digests SET status='FAILED', message_ids=? WHERE id=?", (json.dumps(ids), did))
            con.commit()
            return b
        ids.append(res.message_id)
        if k + 1 < len(b["messages"]):
            time.sleep(alerts.TG_PACE_S)
    ts = iso(now)
    chosen_ids = {c["id"] for c in b["chosen"]}
    for k in range(0, len(b["all_ids"]), 500):
        chunk = b["all_ids"][k:k + 500]
        con.execute(f"UPDATE items SET digested_at=? WHERE id IN ({','.join('?' * len(chunk))})", (ts, *chunk))
    if chosen_ids:
        con.execute(f"UPDATE items SET digest_id=? WHERE id IN ({','.join('?' * len(chosen_ids))})",
                    (did, *chosen_ids))
    con.execute("UPDATE digests SET status='SENT', sent_at=?, message_ids=? WHERE id=?", (ts, json.dumps(ids), did))
    rt_set(con, "last_digest_at", ts)
    con.commit()
    log.info(f"digest: sent {len(b['messages'])} message(s), {len(b['chosen'])} items; "
             f"{len(b['all_ids'])} items marked as covered")
    _pages(con, env, slot, now, b)
    return b


def _pages(con, env, slot, now, b):
    try:
        import web
        eds = web.publish(con, env, now, b.get("gemini"), brief=b.get("stats"))
        b["edition"] = eds["latest"]
        log.info(f"digest: web editions written -- latest {len(eds['latest']['chosen'])} stories from "
                 f"{len(eds['latest']['ids'])} items; " + ", ".join(f"{k} {len(v['chosen'])}" for k, v in eds.items() if k != "latest"))
    except Exception:
        log.exception("digest: could not write the web page (the brief itself is unaffected)")


def cmd_pages(con, env, now=None):
    """Rebuild the web editions and the page without sending or marking anything -- after
    the office adds links, or to republish after a settings change."""
    now = now or utcnow()
    migrate(con)
    b = build(con, env, now, slot_for(now))
    _pages(con, env, slot_for(now), now, b)
    log.info(f"pages: rebuilt; next brief would carry {len(b['chosen'])} of {len(b['cands'])} stories"
             + (f"; gemini note: {b['ai_note']}" if b["ai_note"] else ""))
    return b


def cmd_backfill(con, env, days, now=None):
    """Dated web editions for the past `days` days from what the database already holds."""
    import web
    now = now or utcnow()
    migrate(con)
    cfg = settings(env)
    key = (env.get("GEMINI_API_KEY") or "").strip()
    gemini = Gemini(key, cfg["model"], con, cfg["ai_calls"]) if key else None
    if gemini is not None:
        gemini.budget_s = AI_TIME_BUDGET_S * 2 + 60     # 15 minutes: a 14-day rebuild must not stop half way
    eds = web.backfill(con, env, now, days, gemini)
    web.publish(con, env, now, gemini)                        # index + page pick the new days up
    calls = gemini.calls if gemini else 0
    log.info(f"backfill: {len(eds)} editions written, {calls} Gemini calls"
             + (f" (stopped: {gemini.dead})" if gemini and gemini.dead else ""))
    return eds


def feedback_review(env, now=None, days=7):
    """The Feedback tab (published as CSV) as an LLM-ready block for the run page."""
    url = settings(env)["feedback_csv"]
    if not url:
        return ""
    now = now or utcnow()
    try:
        import csv
        import io
        import httpx
        r = httpx.get(url, timeout=20.0, follow_redirects=True)
        if r.status_code != 200:
            return f"### Reader feedback\n\n_could not read the Feedback tab: HTTP {r.status_code}_\n"
        rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig", errors="replace"))))
    except Exception as ex:
        return f"### Reader feedback\n\n_could not read the Feedback tab: {type(ex).__name__}_\n"
    since = now - timedelta(days=days)
    kept = []
    for row in rows:
        w = (row.get("when") or "").strip()
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(w[:19], fmt).replace(tzinfo=IST)
                break
            except ValueError:
                continue
        if dt is None or dt >= since:
            kept.append(row)
    if not kept:
        return f"### Reader feedback (last {days} days)\n\n_none_\n"
    L = [f"### Reader feedback (last {days} days) — paste this block to an LLM for review", "", "```"]
    reasons = dict(REASONS)
    for row in kept:
        t = (row.get("type") or "").strip()
        if t == "missing":
            L.append(f"MISSING  {row.get('when', '')[:16]}  {row.get('url', '')}  {row.get('notes', '')}".rstrip())
        else:
            L.append(f"{'UP  ' if t == 'up' else 'DOWN'}  {row.get('when', '')[:16]}  [{row.get('category', '')}/"
                     f"{row.get('outlet', '')}]  {row.get('title', '')}"
                     + (f"  -- {reasons.get(row.get('reason', ''), row.get('reason', ''))}" if t == "down" else "")
                     + f"  {row.get('url', '')}")
    c = Counter((r.get("type") or "") for r in kept)
    L += ["```", f"_{c.get('up', 0)} up · {c.get('down', 0)} down · {c.get('missing', 0)} missing-story reports_", ""]
    return "\n".join(L)


def diagnostics(b):
    st = b["stats"]
    live, cached, by_rules = st.get("gemini", 0), st.get("cached", 0), st.get("rules", 0)
    status = "not used (no GEMINI_API_KEY)" if b["ai_note"] == "no GEMINI_API_KEY" else \
             (f"stopped early: {b['ai_note']}" if b["ai_note"] else ("ok" if live or cached else "nothing to rank"))
    return "\n".join([
        "### Brief diagnostics", "",
        "| | |", "|---|---|",
        f"| Items in the window | {len(b['all_ids'])} |",
        f"| Stories after merging same-event / same-headline reports | {len(b['cands'])} (merged away {st.get('merged', 0)} more after ranking) |",
        f"| Ranked by Gemini | {live} live, {cached} cached |",
        f"| Ranked by keyword rules (fallback) | {by_rules} |",
        f"| Gemini calls this run | {b['ai_calls']} — {status} |",
        f"| Chosen for the brief | {len(b['chosen'])} (left out: {len(b['leftovers'])}) |"]
        + ([f"| Web edition | {len(b['edition']['chosen'])} stories from {len(b['edition']['ids'])} items in the rolling window"
            f"{'; ' + str(b['edition']['stats'].get('clusters', 0)) + ' cross-report clusters' if b['edition']['stats'].get('clusters') else ''}"
            f"{'; ' + str(b['edition']['stats'].get('reruns', 0)) + ' re-runs dropped' if b['edition']['stats'].get('reruns') else ''} |"]
           if b.get("edition") else [])
        + (["| Guards re-filed | " + ", ".join(f"{n} ({why})" for why, n in sorted(g.items())) + " |"]
           if (g := {**(st.get("guards") or {}), **((b.get("edition") or {}).get("stats", {}).get("guards") or {})}) else [])
        + [""]
        + ([("### Gemini groups refused or thinned\n\n" + "\n".join(
                f"- **{r['why']}** — shared: _{r.get('shared') or '–'}_ — " + " · ".join(r.get("titles") or [])
                for r in (b.get("edition") or {}).get("stats", {}).get("refused") or []) + "\n")]
           if (b.get("edition") or {}).get("stats", {}).get("refused") else []))


def status_lines(con, env, now=None):
    now = now or utcnow()
    migrate(con)
    b = build(con, env, now, slot_for(now))
    out = [f"DIGEST  next {slot_for(now)} brief would carry {len(b['chosen'])} of {len(b['cands'])} stories "
           f"(since {fmt_ist(iso(b['since']))})"]
    last = con.execute("SELECT slot, sent_at, chosen, status FROM digests ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        out.append(f"  last: {last['slot']} {fmt_ist(last['sent_at'] or '')} {last['status']} {last['chosen']} items")
    return out + ["  " + line for m in b["messages"] for line in alerts.html_to_plain(m).splitlines()]


def main():
    alerts._console_utf8()
    ap = argparse.ArgumentParser(description="twice-daily Telegram brief")
    ap.add_argument("--slot", choices=["auto", "morning", "evening"], default="auto")
    ap.add_argument("--dry-run", action="store_true", help="build and print; send nothing, mark nothing")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--backfill", type=int, metavar="DAYS", help="write web editions for the past DAYS days")
    ap.add_argument("--pages", action="store_true", help="rebuild the web page only; send nothing, mark nothing")
    a = ap.parse_args()
    if not DB.exists():
        print("no corpus.db -- run 'python collect.py --init' first")
        return 1
    alerts._setup_logging(HERE / "digest.log")
    env = load_env()
    con = alerts._connect(DB)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    sheet_settings(con, env)
    try:
        if a.status:
            print("\n".join(status_lines(con, env)))
            return 0
        if a.backfill:
            eds = cmd_backfill(con, env, max(1, min(30, a.backfill)))
            summary = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary:
                with open(summary, "a", encoding="utf-8") as f:
                    f.write("### Backfill\n\n" + "\n".join(f"- {k}: {len(v['chosen'])} stories from {len(v['ids'])} items"
                                                            for k, v in eds.items()) + "\n")
            return 0
        if a.pages:
            b = cmd_pages(con, env)
            summary = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary:
                with open(summary, "a", encoding="utf-8") as f:
                    f.write("### Web page rebuilt (nothing sent)\n\n" + diagnostics(b) + "\n")
            return 0
        b = run(con, env, slot=None if a.slot == "auto" else a.slot, dry_run=a.dry_run)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(diagnostics(b) + "\n```\n" + "\n\n".join(alerts.html_to_plain(m) for m in b["messages"]) + "\n```\n"
                        + feedback_review(env))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
