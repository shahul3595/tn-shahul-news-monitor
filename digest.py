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
     urgent), published within the last 36 hours, one per story (event).
  2. Ranking: Gemini reads headline + first lines of each candidate and gives a
     category (mention, constituency, district, portfolio, political, opportunity,
     none) and a priority (1 urgent, 2 standard, 3 background). Without a key, over
     quota, or on any error, the keyword rules decide instead.
  3. Selection: up to 5 per category first; if a category has fewer, its spare places
     go to the next-best items from other categories, up to DIGEST_MAX in total
     (default 5 x the number of categories = 30). Priority-1 civic items (floods,
     deaths, protests) go in an URGENT block at the top.
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

CATEGORIES = ["mention", "constituency", "district", "portfolio", "political", "opportunity"]
LABELS = {"mention": "MENTIONS", "constituency": "VELACHERY", "district": "THIRUVALLUR",
          "portfolio": "AI / IT / DIGITAL", "political": "POLITICAL", "opportunity": "OPPORTUNITIES"}
NICE = {"mention": "Mentions", "constituency": "Velachery", "district": "Thiruvallur",
        "portfolio": "AI / IT / Digital", "political": "Political", "opportunity": "Opportunities"}
ICONS = {"mention": "🗣", "constituency": "📍", "district": "🏛", "portfolio": "💻",
         "political": "🏳", "opportunity": "🎯"}
URGENT_CATEGORIES = ("constituency", "district")

PER_CATEGORY = 5              # first pass
MAX_TOTAL = PER_CATEGORY * len(CATEGORIES)
PUBLISHED_WITHIN_H = 36       # a 14-day query can surface old stories; the brief is about now
WEB_WINDOW_H = 36             # the web page is a rolling edition of everything kept this recently
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
                ("ai_impact", "INTEGER"), ("story_key", "TEXT")]
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
              "DIGEST_WEB_MAX", "DIGEST_WEB_PER_CATEGORY", "DIGEST_WEB_HOURS", "FEEDBACK_URL", "FEEDBACK_CSV_URL"):
        if k not in env and os.environ.get(k):
            env[k] = os.environ[k]
    return env


def migrate(con):
    alerts.ensure_migrated(con)
    for ddl in SCHEMA:
        con.execute(ddl)
    have = {r[1] for r in con.execute("PRAGMA table_info(items)")}
    for name, decl in ITEM_COLUMNS:
        if name not in have:
            con.execute(f"ALTER TABLE items ADD COLUMN {name} {decl}")
    con.execute("CREATE INDEX IF NOT EXISTS ix_items_digested ON items(digested_at)")
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
       i.ai_sentiment, i.ai_impact, i.story_key,
       CASE WHEN i.raw_payload LIKE '%youtube_api%' THEN i.raw_payload END AS yt_payload,
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


class _Union:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def join(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.p[a] = b


def _merge(cands, links):
    """Collapse candidates into stories. links(i, j) -> True when two are the same story.
    The representative is the best-ranked report; outlets are pooled."""
    n = len(cands)
    u = _Union(n)
    for i in range(n):
        for j in range(i + 1, n):
            if links(cands[i], cands[j]):
                u.join(i, j)
    groups = defaultdict(list)
    for i, c in enumerate(cands):
        groups[u.find(i)].append(c)
    out = []
    for members in groups.values():
        rep = max(members, key=lambda c: (-(c.get("priority") or 9), c.get("urgent") or 0, c.get("score") or 0,
                                           c.get("published_at") or ""))
        rep["sources"] = list(dict.fromkeys(s for m in members for s in m.get("sources") or []))
        rep["merged"] = [m["id"] for m in members]
        # every report of the story, the representative first, one per url
        seen, reports = set(), []
        for m in [rep] + [m for m in members if m is not rep]:
            for r in m.get("reports") or []:
                if r["url"] not in seen:
                    seen.add(r["url"])
                    reports.append(r)
        rep["reports"] = reports
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
                         "lang": "Tamil" if re.search(r"[\u0B80-\u0BFF]", r["title"] or "") else "English"}]
        d["tags"] = alerts._j(r["target_tags"], [])
        d["ntitle"] = norm_title(r["title"], r["publisher"])
        d["tgrams"] = frozenset(rules.sim_grams(d["ntitle"])) if len(d["ntitle"]) >= 12 else frozenset()
        cands.append(d)
    # Same event (body match), or the same headline word for word: one story. The same-outlet
    # copies and Tamil/English pairs that the body match cannot join are caught here.
    reps = _merge(cands, lambda a, b: (a["event_id"] is not None and a["event_id"] == b["event_id"])
                  or (len(a["ntitle"]) >= 12 and a["ntitle"] == b["ntitle"]))
    return reps, [r["id"] for r in rows]


def candidates(con, now):
    """The Telegram delta: kept items not yet covered by a brief."""
    last = parse_ts(rt_get(con, "last_digest_at"))
    since = max(now - timedelta(hours=WINDOW_CAP_H), last) if last else now - timedelta(hours=WINDOW_CAP_H)
    reps, ids = _load(con, "i.digested_at IS NULL", since, now - timedelta(hours=PUBLISHED_WITHIN_H))
    return reps, ids, since


def edition(con, now, hours=WEB_WINDOW_H):
    """The web page: everything kept and published in the last `hours`, covered or not, so
    the page is always a full edition rather than the last few arrivals."""
    reps, ids = _load(con, "1=1", now - timedelta(hours=hours + 24), now - timedelta(hours=hours))
    return reps, ids


_STOP = {"chennai", "tamil", "nadu", "tamilnadu", "india", "news", "ias", "ips", "minister", "govt", "government",
         "the", "and", "for", "with", "over", "after", "from", "sept", "sep", "oct", "nov", "dec", "jan", "police", "collector"}
_NUM = re.compile(r"\d[\d,.]*")
_LATIN = re.compile(r"[A-Za-z][A-Za-z.\-]{2,}")
_MONTHS = {"jan": "1", "feb": "2", "mar": "3", "apr": "4", "may": "5", "jun": "6", "jul": "7", "aug": "8",
           "sep": "9", "sept": "9", "oct": "10", "nov": "11", "dec": "12",
           "ஜனவரி": "1", "பிப்ரவரி": "2", "மார்ச்": "3", "ஏப்ரல்": "4", "மே": "5", "ஜூன்": "6", "ஜூலை": "7",
           "ஆகஸ்ட்": "8", "செப்டம்பர்": "9", "அக்டோபர்": "10", "நவம்பர்": "11", "டிசம்பர்": "12"}


def entity_tokens(c):
    """Language-independent handles of a story: amounts and counts, dates, and Latin-script
    proper names that Tamil outlets keep in Latin (CMRL, Alstom, acronyms). Returns
    (strong_numbers, dates, names)."""
    text = f"{_title(c)} {_snippet(c)}"
    strong, dates, names = set(), set(), set()
    for m in _NUM.finditer(text):
        n = m.group(0).replace(",", "").rstrip(".")
        if n.isdigit() and 1 <= int(n) <= 31 and len(n) <= 2:
            dates.add("d" + n)                        # a day of the month, or a small count
        elif len(n) >= 2:
            strong.add(n)                             # 450, 2500, 33305, 2026, 10.5
    low = text.lower()
    for w, mnum in _MONTHS.items():
        if w in low:
            dates.add("m" + mnum)
    for m in re.finditer(r"\b[A-Z][A-Za-z.\-]{3,}\b", text):
        w = m.group(0).lower().strip(".-")
        if w not in _STOP:
            names.add(w)
    return strong, dates, names


def cross_lingual_link(a, b):
    """A Tamil and an English report within a day, same category, sharing an amount plus a
    date or a name, or two names, or two amounts. Sharing only 'October' and '10' is not
    enough: every story with a deadline that day would merge."""
    sa, da, na = a["etoks"]
    sb, db, nb = b["etoks"]
    strong, dates, names = sa & sb, da & db, na & nb
    return bool(len(strong) >= 2 or (len(strong) >= 1 and (dates or names)) or len(names) >= 2)


def _is_tamil(c):
    return bool(re.search(r"[\u0B80-\u0BFF]", c["title"] or ""))


def merge_stories(cands):
    """After ranking: Gemini's story numbers, near-identical headlines, and -- for a Tamil and
    an English report -- shared dates, amounts and Latin-script names finish the job, so one
    event framed three ways by three outlets is one candidate with one category."""
    for c in cands:
        if "etoks" not in c:
            c["etoks"] = entity_tokens(c)

    def same(a, b):
        if a.get("story") and a.get("story") == b.get("story"):
            return True
        pa, pb = parse_ts(a["published_at"]), parse_ts(b["published_at"])
        close = not (pa and pb) or abs((pa - pb).total_seconds()) <= 24 * 3600
        if close and _is_tamil(a) != _is_tamil(b) and a["category"] == b["category"] and cross_lingual_link(a, b):
            return True
        if a["tgrams"] and b["tgrams"] and len(a["ntitle"]) >= 25 and len(b["ntitle"]) >= 25:
            pa, pb = parse_ts(a["published_at"]), parse_ts(b["published_at"])
            close = not (pa and pb) or abs((pa - pb).total_seconds()) <= 24 * 3600
            return close and rules.jaccard(a["tgrams"], b["tgrams"]) >= TITLE_SIM
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
mention      - names or quotes this R. Kumar, or criticism/praise of him
constituency - civic issues, flooding, sewage, drains, roads, lakes, MRTS, crime or law-and-order
               in Velachery, Adyar, Besant Nagar, Thiruvanmiyur, Tharamani, Adambakkam,
               Pallikaranai -- whether or not he is named
district     - Thiruvallur district administration, collectorate, review meetings, Poondi
               reservoir, Gummidipoondi, Ponneri, Avadi, Ambattur, Poonamallee, Tiruttani
portfolio    - AI, IT, digital governance, ELCOT, TIDEL, StartupTN, data centres, GCC
               investment, IT corridor, and rival-state (Karnataka/Telangana) IT policy
political    - attacks or alliances specifically touching his seat, district or department.
               GENERIC TVK vs DMK vs AIADMK battles are "none"
opportunity  - schemes, inaugurations, foundation stones, summits he could attend
none         - irrelevant, a DIFFERENT person named Kumar (R.B. Udhayakumar, C.T.R. Nirmal Kumar,
               Ramesh Kumar of Avadi, actor Sarathkumar, Praveen/Vinoth/Ashok/Santhosh Kumar, Kumar
               Sanu, Akshay Kumar ...) unless our R. Kumar is also named or the story is about
               his constituency, film or cinema news, real estate listings, hotel
               or motor-trade press releases, exam-prep content, a place with the same name
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

Return one object per item, using the item numbers given.

ITEMS:
{payload}"""

RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {"type": "OBJECT", "properties": {
        "n": {"type": "INTEGER"},
        "category": {"type": "STRING", "enum": CATEGORIES + ["none"]},
        "priority": {"type": "INTEGER"},
        "reason": {"type": "STRING"},
        "story": {"type": "INTEGER"},
        "impact": {"type": "INTEGER"},
        "sentiment": {"type": "STRING", "enum": list(SENTIMENTS)}},
        "required": ["n", "category", "priority", "reason", "story", "impact", "sentiment"]},
}


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
            if time.monotonic() - self.t0 > AI_TIME_BUDGET_S:
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
                out[batch[i]["id"]] = _verdict(it, f"{self.calls}:")
        return out


CLUSTER_PROMPT = """Below are news items from Tamil Nadu in Tamil and English, collected over about a day.
Group the items that report the SAME real-world event: the same announcement, press
conference, order, inauguration, incident or statement -- even when one is in Tamil and one
in English, and however differently they are headlined. Look at names, places, dates,
amounts and what actually happened. Different events on the same topic (two separate
floods, two statements on different days, a recurring daily column) are NOT the same.
When torn, keep them apart: merging two events hides news.

Return one object per item with a "story" number: the same number for every item in a
group (use the lowest item number of the group); an item on its own gets its own number.

ITEMS:
{payload}"""

CLUSTER_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "n": {"type": "INTEGER"}, "story": {"type": "INTEGER"}}, "required": ["n", "story"]}}


def cluster(gemini, con, cands, key_prefix):
    """One call over a whole edition: {id: story_key}. Stores the keys on the items so a
    later run links the same reports again without asking."""
    if gemini is None or not cands or len(cands) < 2:
        return 0
    lines = [f"{n}. [{alerts.outlet_name(c)}] {_title(c)}\n   {_snippet(c)}" for n, c in enumerate(cands, 1)]
    data = gemini.call(CLUSTER_PROMPT.format(payload="\n".join(lines)), CLUSTER_SCHEMA, "cluster")
    if not isinstance(data, list):
        return 0
    groups = Counter()
    for it in data:
        i, g = _int(it.get("n"), 1, len(cands), 0) - 1, _int(it.get("story"), 1, len(cands), 0)
        if i >= 0 and g:
            groups[g] += 1
            cands[i]["story"] = f"{key_prefix}:{g}"
    for c in cands:
        if c.get("story", "").startswith(key_prefix):
            con.execute("UPDATE items SET story_key=? WHERE id=?", (c["story"], c["id"]))
    con.commit()
    return sum(1 for g, n in groups.items() if n > 1)


def _int(v, lo, hi, default):
    try:
        return min(hi, max(lo, int(v)))
    except (TypeError, ValueError):
        return default


def _verdict(it, story_prefix):
    cat = it.get("category") if it.get("category") in CATEGORIES + ["none"] else "none"
    story = _int(it.get("story"), 0, 10 ** 6, 0)
    return {"category": cat, "priority": _int(it.get("priority"), 1, 3, 2),
            "reason": str(it.get("reason", ""))[:200],
            "story": f"{story_prefix}{story}" if story else None,
            "impact": _int(it.get("impact"), 1, 10, 5),
            "sentiment": it.get("sentiment") if it.get("sentiment") in SENTIMENTS else "neutral"}


def rule_rank(c):
    """The keyword fallback: category from the rule tags, priority from urgency and band."""
    tags = c.get("tags") or []
    cat = "none"
    for tag, name in (("mention", "mention"), ("constituency", "constituency"), ("district", "district"),
                      ("portfolio", "portfolio"), ("party", "political")):
        if tag in tags:
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
            "impact": impact, "sentiment": sent}


def _apply(c, v, by):
    c["category"], c["priority"], c["reason"], c["by"] = v["category"], v["priority"], v["reason"], by
    c["impact"], c["sentiment"] = v.get("impact") or 5, v.get("sentiment") or "neutral"
    if v.get("story"):
        c["story"] = v["story"]


def rank_all(con, cands, gemini, now):
    """Fills category, priority, reason, impact, sentiment, by. Reuses Gemini's stored verdict
    on an item when there is one (a re-run costs nothing)."""
    todo = []
    for c in cands:
        if c["ai_processed_at"] and c["ai_category"]:
            _apply(c, {"category": c["ai_category"], "priority": c["ai_priority"] or 2, "reason": c["ai_reason"] or "",
                       "impact": c.get("ai_impact") or 5, "sentiment": c.get("ai_sentiment") or "neutral",
                       "story": c.get("story_key")}, "gemini (cached)")
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
                                   ai_processed_at=?, ai_impact=?, ai_sentiment=?,
                                   story_key=coalesce(story_key, ?) WHERE id=?""",
                                (c["category"], c["priority"], c["reason"], gemini.model, iso(now),
                                 c["impact"], c["sentiment"], c.get("story"), c["id"]))
                    st["gemini"] += 1
            con.commit()
            if k + AI_BATCH < len(todo):
                time.sleep(AI_PAUSE_S)
    for c in cands:
        if "category" not in c:
            _apply(c, rule_rank(c), "rules")
            st["rules"] += 1
    if gemini is not None and gemini.dead:
        log.warning(f"gemini: stopped -- {gemini.dead}; {st['rules']} items ranked by the rules")
    return st


# --------------------------------------------------------------------------
# 3. selection
# --------------------------------------------------------------------------

def _order(c):
    return (c["priority"], -(c.get("urgent") or 0), -(c.get("impact") or 0), -(c.get("score") or 0),
            c["published_at"] or "")


def select(cands, per=PER_CATEGORY, total=MAX_TOTAL):
    """5 per category first (priority 1 and 2 only); then spare places go to the next-best
    leftovers from any category, priority 3 included, until `total`. Returns
    (urgent_block, {category: [items]}, leftovers)."""
    pool = [c for c in cands if c["category"] != "none"]
    by_cat = defaultdict(list)
    for c in sorted(pool, key=_order):
        by_cat[c["category"]].append(c)
    chosen, leftovers = [], []
    for cat in CATEGORIES:
        head = [c for c in by_cat.get(cat, []) if c["priority"] <= 2][:per]
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
    gemini = None
    key = (env.get("GEMINI_API_KEY") or "").strip()
    if key and cands:
        gemini = Gemini(key, cfg["model"], con, cfg["ai_calls"], transport_client)
    st = rank_all(con, cands, gemini, now)
    before = len(cands)
    cands = merge_stories(cands)
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
        eds = web.publish(con, env, now, b.get("gemini"))
        b["edition"] = eds["latest"]
        log.info(f"digest: web editions written -- latest {len(eds['latest']['chosen'])} stories from "
                 f"{len(eds['latest']['ids'])} items; " + ", ".join(f"{k} {len(v['chosen'])}" for k, v in eds.items() if k != "latest"))
    except Exception:
        log.exception("digest: could not write the web page (the brief itself is unaffected)")


def cmd_backfill(con, env, days, now=None):
    """Dated web editions for the past `days` days from what the database already holds."""
    import web
    now = now or utcnow()
    migrate(con)
    cfg = settings(env)
    key = (env.get("GEMINI_API_KEY") or "").strip()
    gemini = Gemini(key, cfg["model"], con, cfg["ai_calls"]) if key else None
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
            f"{'; ' + str(b['edition']['stats'].get('clusters', 0)) + ' cross-report clusters' if b['edition']['stats'].get('clusters') else ''} |"]
           if b.get("edition") else []) + [""])


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
    a = ap.parse_args()
    if not DB.exists():
        print("no corpus.db -- run 'python collect.py --init' first")
        return 1
    alerts._setup_logging(HERE / "digest.log")
    env = load_env()
    con = alerts._connect(DB)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
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
