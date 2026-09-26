#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 1 rule engine -- a port of the Apps Script's score_ / band_ / buildMatcher_.

Pure logic. Nothing here sends alerts or writes items; alerts.py and collect.py
call into it. The vocabulary comes from a Google Sheet published as CSV (one term
per row), cached in SQLite, with keywords_seed.csv as the last resort.

Sheet columns:  tier, term, language, weight, group, role, active

  tier    constituency  district  area          places. Only the best one scores.
          civic  crime                          count only once a place matched
          portfolio  party                      count on their own
          collision                             other people: subtract, or drop outright
                                                when named next to minister / MLA
          penalty                               always subtract (former MLA, predecessor)
          veto                                  drop the item (film news)
  role    anchor    an ordinary term
          context   civic/crime term (the default for those tiers)
          urgent    civic/crime term that can also fire an urgent alert
          exclude   a phrase to ignore before matching ("adyar ananda bhavan"
                    must not count as Adyar)
  weight  integer. Blank uses the tier default.
  group   civic/crime: the kind of emergency an urgent term reports (flood, fire, death ...);
          a new kind in an ongoing story sends an UPDATE alert.
          places: name the group after the place, and give every spelling of that place the
          same group (velachery, வேளச்சேரி -> group "velachery"). Alerts treat one group as one
          place, so a Tamil video and an English report of the same flood become one alert.
          A spelling left without such a group counts as a place of its own.
  term    end an English term with * to match word endings: flood* = floods, flooded.
          Tamil: write the ordinary form. The engine also matches inflections:
            ends in ம்   (ஆதம்பாக்கம், மரணம்)   -> ஆதம்பாக்கத்தில், மரணமடைந்தார்
            ends in ்    (திருவான்மியூர், மறியல்) -> திருவான்மியூரில், மறியலில்
            ends in ு    (அடையாறு, தீ விபத்து)   -> அடையாற்றில், தீ விபத்தில்
          Anything else is a prefix that never splits a letter from its vowel sign: the
          stem வெள்ள matches வெள்ளம் and வெள்ளப்பெருக்கு, not வெள்ளி or வெள்ளை.
          Words of one or two letters (பலி, கொலை, மனு, ஏரி) must END where the word ends,
          apart from the letters Tamil itself adds: the glide before a case ending
          (பலியானார், மனுவை), the plural (கொலைகள், மனுக்கள்), a doubled consonant before
          the next word (கொலைச் சம்பவம்), the dative (கொலைக்கு). So பலி is a death in
          இருவர் பலி and பலியாகினர், and not in பலித்தது or பலிக்கும் ("came true").
          To match a longer form of a short word, add that form as its own row.
          A term found inside a longer word that another row of the same tier matches does
          not count: with தற்கொலை in the sheet, கொலை no longer fires inside it.
"""

import csv
import html
import io
import json
import os
import re
import time
import hashlib
import logging
import unicodedata
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qsl, urlencode

HERE = Path(__file__).resolve().parent
SEED_CSV = HERE / "keywords_seed.csv"
SOURCES_JSON = HERE / "sources.json"

log = logging.getLogger("collect.rules")

# --------------------------------------------------------------------------
# tunables -- each one is a measurement waiting to happen; see alerts.py --backtest
# --------------------------------------------------------------------------

AUTO_KEEP = 7                 # band thresholds, from BUILD_PHASE1.md
AI_MIN = 2
KEYWORD_ONLY_KEEP = 4
NEAR_WEIGHT = 2               # "minister" near "kumar"
SCORE_BODY_CHARS = 1200       # body lede added to title+description for scoring
URGENT_PROXIMITY = 160        # place and emergency term this close in the body
URGENT_LEDE = 400             # title names the place, lede names the emergency
COLLISION_WINDOW = 40         # other Kumar this close to minister/MLA = absolute veto
STRONG_WEIGHT = 3
KEYWORDS_REFRESH_S = 300      # published sheets refresh ~5 min anyway

# body similarity -- copied from analyse.py so the 0.45 calibration stays valid
BODY_CAP = 1200
NGRAM = 4
DEDUP_THRESHOLD = 0.45

DEFAULT_BLOCKED_HOSTS = ("theprint.in", "fuelcarmagazine.com", "tamil.getlokalapp.com")

TIERS = ("constituency", "district", "area", "civic", "crime", "portfolio", "party",
         "collision", "penalty", "veto")
ROLES = ("anchor", "context", "urgent", "exclude")
PLACE_TIERS = ("constituency", "district", "area")
URGENT_PLACE_TIERS = ("constituency", "district")
CONTEXT_TIERS = ("civic", "crime")
TIER_DEFAULT_WEIGHT = {"constituency": 3, "district": 3, "area": 1, "civic": 2, "crime": 2,
                       "portfolio": 1, "party": 1, "collision": -4, "penalty": -3, "veto": 0}
TARGET_LABEL = {"constituency": "VELACHERY", "district": "THIRUVALLUR"}

# Structural patterns about the one person being monitored. Not sheet material.
NEAR_EN = (re.compile(r"minister[^.\n]{0,40}(?<![a-z0-9])kumar(?![a-z0-9])"),
           re.compile(r"(?<![a-z0-9])kumar(?![a-z0-9])[^.\n]{0,40}minister"))
# Tamil case endings replace the final pulli: அமைச்சரின், குமாருக்கு, குமாரை
_TA_END = "(?:\u0BCD|\u0BBF|\u0BC1|\u0BC8|\u0BBE)"
NEAR_TA = (re.compile(r"அமைச்சர" + _TA_END + r"[^\n]{0,40}குமார" + _TA_END),
           re.compile(r"குமார" + _TA_END + r"[^\n]{0,40}அமைச்சர" + _TA_END))
# "Minister R. Kumar": the Apps Script pattern excluded '.', so it only matched "R Kumar"
INITIALS = re.compile(r"(?<![a-z0-9])([a-z])\.\s?")
TITLE_WORDS_EN = re.compile(r"(?<![a-z0-9])(minister\w*|mla|m\.l\.a\.?)(?![a-z0-9])")
TITLE_WORDS_TA = ("அமைச்சர", "எம்எல்ஏ", "எம்.எல்.ஏ", "சட்டமன்ற உறுப்பினர")   # stems
TARGET_EN = re.compile(r"(?<![a-z0-9])r\.?\s?kumar(?![a-z0-9])")
TARGET_TA = re.compile(r"ஆர்\.?\s?குமார" + _TA_END)
FOREIGN_SCRIPT = re.compile(r"[\u0900-\u097F\u0C00-\u0C7F\u0C80-\u0CFF\u0D00-\u0D7F]")

COMBINING = ("Mn", "Mc", "Me")
VIRAMA, U_SIGN, MA = "\u0BCD", "\u0BC1", "\u0BAE"
TA_FRONT_VOWEL = "\u0BBF\u0BC0\u0BC6\u0BC7\u0BC8"          # ி ீ ெ ே ை take the glide ய, others வ
TA_GLIDE_Y, TA_GLIDE_V = "\u0BAF", "\u0BB5"
TA_HARD = "\u0B95\u0B9A\u0BA4\u0BAA"                         # க ச த ப
TA_PLURAL = ("\u0B95\u0BB3", "\u0B95\u0BCD\u0B95\u0BB3")        # கள், க்கள்
TA_DATIVE = "\u0B95\u0BCD\u0B95\u0BC1"                       # க்கு
HASHTAG = re.compile(r"#[\w\u0B80-\u0BFF]+")


# --------------------------------------------------------------------------
# .env -- same lenient decoding as ai_label.py (PowerShell writes UTF-16)
# --------------------------------------------------------------------------

def load_env(path=None):
    path = Path(path) if path else HERE / ".env"
    out = {}
    if path.exists():
        raw = path.read_bytes()
        text = ""
        for enc in ("utf-8-sig", "utf-16", "utf-8", "latin-1"):
            try:
                t = raw.decode(enc)
            except UnicodeDecodeError:
                continue
            if "=" in t and "\x00" not in t:
                text = t
                break
        for line in text.splitlines():
            line = line.strip().lstrip("\ufeff")
            if "=" not in line or line.startswith("#"):
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k.lower().startswith("export "):
                k = k[7:].strip()
            if v[:1] not in ("'", '"'):
                v = re.split(r"\s+#", v, maxsplit=1)[0]      # TELEGRAM_CHAT_ID=-100123  # channel
            v = v.strip().strip('"').strip("'").strip()
            if k and v:
                out[k] = v
    for k in ("GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
              "KEYWORDS_CSV_URL", "TELEGRAM_DRY_RUN", "YOUTUBE_API_KEY"):
        if k not in out and os.environ.get(k):
            out[k] = os.environ[k]
    return out


# --------------------------------------------------------------------------
# text normalisation -- a SEPARATE copy for matching; originals stay verbatim
# --------------------------------------------------------------------------

_ZW = {0x200B: None, 0x200C: None, 0x200D: None, 0xFEFF: None}
_ASCII_LOWER = {c: c + 32 for c in range(65, 91)}


def norm_map(s):
    """NFC, strip zero-width joiners, lowercase ASCII, collapse whitespace.

    Returns (normalised, nfc_original, index_map) where index_map[k] is the index in
    nfc_original of normalised character k -- so a match can be quoted verbatim."""
    nfc = unicodedata.normalize("NFC", s or "")
    out, idx, prev_space = [], [], True
    for i, ch in enumerate(nfc):
        o = ord(ch)
        if o in _ZW:
            continue
        if ch.isspace():
            if not prev_space:
                out.append(" ")
                idx.append(i)
            prev_space = True
            continue
        out.append(chr(_ASCII_LOWER.get(o, o)))
        idx.append(i)
        prev_space = False
    if out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), nfc, idx


def norm_match(s):
    return norm_map(s)[0]


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def strip_urls(s):
    return re.sub(r"https?://\S+", " ", s or "")


def clean_title(title, publisher=""):
    """Google News titles arrive as 'Headline - Outlet'. Port of cleanTitle_."""
    s = (title or "").strip()
    if publisher and s.endswith(" - " + publisher.strip()):
        return s[: -len(publisher.strip()) - 3].strip()
    i = s.rfind(" - ")
    if i > 20 and len(s) - i < 45:
        s = s[:i]
    return s.strip()


def cut(s, n, ellipsis="…"):
    """Truncate without splitting a Tamil letter from its vowel sign."""
    if len(s) <= n:
        return s
    i = n
    while i > 0 and unicodedata.category(s[i]) in COMBINING:
        i -= 1
    j = s.rfind(" ", 0, i)
    if j > n * 0.6:
        i = j
    return s[:i].rstrip() + ellipsis


def utf16_len(s):
    return len(s.encode("utf-16-le")) // 2


# --------------------------------------------------------------------------
# body similarity -- VERBATIM from analyse.py; test_phase1.py asserts they agree
# --------------------------------------------------------------------------

def sim_norm(s):
    s = unicodedata.normalize("NFC", s or "")
    s = s.replace("\u200c", "").replace("\u200d", "")
    s = re.sub(r"[^\w\u0B80-\u0BFF]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def sim_grams(s, n=NGRAM):
    s = sim_norm(s)
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


def jaccard(a, b):
    if not a or not b:
        return 0.0
    i = len(a & b)
    return i / (len(a) + len(b) - i)


# --------------------------------------------------------------------------
# urls and hosts
# --------------------------------------------------------------------------

TRACKING_PARAMS = {"fbclid", "gclid", "igshid", "ref", "ref_src", "share", "oc", "amp",
                   "outputtype", "cmpid", "cmp", "ito", "s_cid", "mc_cid", "mc_eid"}
_YT_ID = re.compile(r"^[\w-]{11}$")


def youtube_id(url):
    u = urlparse(url or "")
    host = (u.hostname or "").lower()
    if host == "youtu.be":
        vid = u.path.strip("/").split("/")[0]
        return vid if _YT_ID.match(vid) else None
    if host == "youtube.com" or host.endswith(".youtube.com"):
        q = dict(parse_qsl(u.query))
        if _YT_ID.match(q.get("v", "")):
            return q["v"]
        m = re.match(r"^/(?:shorts|live|embed)/([\w-]{11})", u.path)
        return m.group(1) if m else None
    return None


def canonical_key(url):
    """The BUILD_PHASE1 canonical_key, plus two fixes.

    The specced version drops the query string, which collapses every YouTube video
    to 'youtube.com/watch' and every ?id= article on a host to one key. Phase 0 never
    saw this: its measurement only covered RESOLVED rows, and channel videos are
    SKIPPED. So: YouTube keys on the video id, and non-tracking query params stay."""
    if not url:
        return None
    vid = youtube_id(url)
    if vid:
        return f"youtube.com/watch?v={vid}"
    u = urlparse(url.strip())
    host = (u.hostname or "").lower()
    for p in ("www.", "m.", "amp."):
        if host.startswith(p):
            host = host[len(p):]
    path = re.sub(r"/amp/?$|\.amp$|/$", "", u.path)
    keep = sorted((k.lower(), v) for k, v in parse_qsl(u.query, keep_blank_values=True)
                  if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS)
    key = f"{host}{path}".lower()
    return key + ("?" + urlencode(keep) if keep else "")


def host_of(url):
    h = (urlparse(url or "").hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def host_in(host, suffixes):
    """Dot-boundary suffix match. Plain substring matching (the Phase 0 code) treats
    newsx.com as x.com and would skip it as social media."""
    host = (host or "").lower().rstrip(".")
    return any(host == s or host.endswith("." + s) for s in suffixes)


def blocked_hosts():
    try:
        cfg = json.loads(SOURCES_JSON.read_text(encoding="utf-8"))
        hosts = cfg.get("blocked_hosts")
        if isinstance(hosts, list) and hosts:
            return tuple(h.strip().lower() for h in hosts if h.strip())
    except Exception:
        pass
    return DEFAULT_BLOCKED_HOSTS


# --------------------------------------------------------------------------
# terms and the keyword set
# --------------------------------------------------------------------------

def tamil_stem(n):
    """(stem, open_end) for a normalised Tamil term.

    Case endings rewrite the last letter, so the written form is never a substring
    of the forms a news sentence uses: திருவான்மியூர் -> திருவான்மியூரில்,
    ஆதம்பாக்கம் -> ஆதம்பாக்கத்தில், தீ விபத்து -> தீ விபத்தில். Measured on the
    seed sheet: 63 of 111 Tamil terms, including Thiruvallur itself, missed these."""
    def letters(x):
        return sum(1 for ch in x if unicodedata.category(ch) == "Lo")
    if n.endswith(MA + VIRAMA) and letters(n[:-2]) >= 3:
        return n[:-2], False          # -am noun: stem ends in a bare consonant, and the
                                      # vowel-sign rule still keeps பள்ள(ம்) out of பள்ளி
    if n.endswith(VIRAMA) and letters(n[:-1]) >= 3:
        return n[:-1], True           # pulli becomes a vowel sign: -ரில், -ரை, -ரும்
    if n.endswith(U_SIGN) and letters(n[:-1]) >= 3:
        return n[:-1], True           # u drops before a vowel: -த்தில், -ற்றில்
    return n, False


def _in_word(ch):
    return unicodedata.category(ch)[0] in "LM"


def short_word_ends(text, j, glide):
    """May a one- or two-letter Tamil term that matched up to text[j] count? Only if the
    word ends there, or continues with a letter Tamil adds to the SAME word -- the glide,
    the plural, a doubled consonant before the next word, the dative. Anything else is a
    different word that happens to start the same way: பலி-த்தது, பலி-க்கும்."""
    n = len(text)
    if j >= n or not _in_word(text[j]):
        return True                                   # இருவர் பலி
    if text[j] == glide:
        return True                                   # பலியானார், கொலையில், மனுவை
    if text.startswith(TA_PLURAL, j):
        return True                                   # கொலைகள், மனுக்கள்
    if text[j] in TA_HARD and text[j + 1:j + 2] == VIRAMA and (j + 2 >= n or not _in_word(text[j + 2])):
        return True                                   # கொலைச் சம்பவம்
    if text.startswith(TA_DATIVE, j) and (j + 4 >= n or not _in_word(text[j + 4])):
        return True                                   # கொலைக்கு காரணம்
    return False


class Term:
    __slots__ = ("tier", "term", "weight", "group", "role", "norm", "rx", "open_end", "short", "glide")

    def __init__(self, tier, term, weight, group, role):
        self.tier, self.term, self.weight, self.group, self.role = tier, term, weight, group, role
        n = norm_match(term)
        wildcard = n.endswith("*")
        n = n.rstrip("*").strip()
        self.open_end, self.short, self.glide = False, False, ""
        if n.isascii():
            tail = r"[a-z0-9]*" if wildcard else ""
            self.rx = re.compile(r"(?<![a-z0-9])" + re.escape(n) + tail + r"(?![a-z0-9])")
        else:
            self.rx = None
            n, self.open_end = tamil_stem(n)
            if not self.open_end and n and sum(1 for ch in n if unicodedata.category(ch) == "Lo") < 3:
                self.short = True
                self.glide = TA_GLIDE_Y if n[-1] in TA_FRONT_VOWEL else TA_GLIDE_V
        self.norm = n

    @property
    def label(self):
        return self.term.rstrip("*").strip()

    def spans(self, text):
        if not self.norm:
            return []
        if self.rx is not None:
            return [m.span() for m in self.rx.finditer(text)]
        out, n, i = [], len(self.norm), text.find(self.norm)
        while i != -1:
            j = i + n
            # substring, because Tamil has no word boundaries -- but a match may not end
            # in the middle of a letter: வெள்ள inside வெள்ளி is followed by a vowel sign.
            # Stems cut from an inflecting ending take any continuation; one- and
            # two-letter words must end where the word ends.
            if self.open_end:
                ok = True
            elif self.short:
                ok = short_word_ends(text, j, self.glide)
            else:
                ok = j >= len(text) or unicodedata.category(text[j]) not in COMBINING
            if ok:
                out.append((i, j))
            i = text.find(self.norm, i + 1)
        return out


def parse_keywords_csv(text):
    """Returns (rows, problems). A row is a dict with typed fields."""
    rows, problems, seen = [], [], set()
    if not text or "<html" in text[:500].lower():
        return [], ["not CSV (an HTML page -- is the sheet published to web as CSV?)"]
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        return [], ["empty"]
    fields = {f.strip().lower(): f for f in reader.fieldnames if f}
    if "tier" not in fields or "term" not in fields:
        return [], [f"missing tier/term columns, got {reader.fieldnames}"]

    def get(r, k):
        f = fields.get(k)
        return (r.get(f) or "").strip() if f else ""

    for n, r in enumerate(reader, start=2):
        tier, term = get(r, "tier").lower(), get(r, "term")
        if not tier and not term:
            continue
        active = get(r, "active").lower()
        if active in ("false", "no", "n", "0"):
            continue
        if tier not in TIERS:
            problems.append(f"row {n}: unknown tier '{tier}'")
            continue
        if not term:
            problems.append(f"row {n}: empty term")
            continue
        role = get(r, "role").lower() or ("context" if tier in CONTEXT_TIERS else "anchor")
        if role not in ROLES:
            problems.append(f"row {n}: unknown role '{role}', treated as anchor")
            role = "anchor"
        if role == "urgent" and tier not in CONTEXT_TIERS:
            problems.append(f"row {n}: role urgent only works on civic/crime, '{term}' treated as anchor")
            role = "anchor"
        w = get(r, "weight")
        try:
            weight = int(float(w)) if w else TIER_DEFAULT_WEIGHT[tier]
        except ValueError:
            problems.append(f"row {n}: weight '{w}' is not a number")
            continue
        key = (tier, norm_match(term), role)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"tier": tier, "term": term, "language": get(r, "language"),
                     "weight": weight, "group": get(r, "group"), "role": role})
    return rows, problems


def sanity_problem(rows):
    """A sheet that would silently switch off the urgent path is rejected."""
    if not any(r["tier"] in URGENT_PLACE_TIERS and r["role"] != "exclude" for r in rows):
        return "no active constituency or district term"
    if not any(r["role"] == "urgent" for r in rows):
        return "no active urgent term"
    return None


class Keywords:
    def __init__(self, rows, origin="?", problems=()):
        self.origin, self.problems = origin, list(problems)
        self.rows = rows
        self.hash = hashlib.sha1(json.dumps(rows, ensure_ascii=False, sort_keys=True)
                                 .encode("utf-8")).hexdigest()[:12]
        self.terms = [Term(r["tier"], r["term"], r["weight"], r["group"], r["role"])
                      for r in rows if r["role"] != "exclude"]
        self.excludes = [Term(r["tier"], r["term"], 0, r["group"], "exclude")
                         for r in rows if r["role"] == "exclude"]

    def mask(self, text):
        if not self.excludes:
            return text
        chars = list(text)
        for t in self.excludes:
            for a, b in t.spans(text):
                chars[a:b] = " " * (b - a)
        return "".join(chars)

    def hits(self, text):
        """{tier: [(Term, [spans])]} over a normalised, masked text. A term found inside a
        longer word that another term of the same tier matched does not count: கொலை inside
        தற்கொலை is not a murder. A term that is a whole word of a longer phrase still does:
        வெள்ள in வெள்ள நீர் வெளியேற்ற is still a flood."""
        out = {}
        for t in self.terms:
            sp = t.spans(text)
            if sp:
                out.setdefault(t.tier, []).append((t, sp))
        for tier, found in out.items():
            if len(found) > 1:
                out[tier] = _drop_buried(text, found)
        return {tier: found for tier, found in out.items() if found}

    def summary(self):
        c = {}
        for r in self.rows:
            k = f"{r['tier']}/{r['role']}"
            c[k] = c.get(k, 0) + 1
        return c


_SPACE = re.compile(r"\s")


def _drop_buried(text, found):
    spans = [s for _, sp in found for s in sp]

    def buried(a, b):
        return any(A <= a and b <= B and B - A > b - a and not _SPACE.search(text[A:a])
                   and not _SPACE.search(text[b:B]) for A, B in spans)
    kept = []
    for t, sp in found:
        keep = [s for s in sp if not buried(*s)]
        if keep:
            kept.append((t, keep))
    return kept


def keywords_from_file(path=SEED_CSV):
    rows, problems = parse_keywords_csv(Path(path).read_text(encoding="utf-8-sig"))
    return Keywords(rows, origin=f"file:{Path(path).name}", problems=problems)


def _seed_or_none(why):
    """The seed file is not shipped to the public repository; without it, the sheet and
    the cache, there is no vocabulary. Return None rather than scoring everything DROP --
    unscored items simply wait for the next tick that has one."""
    if SEED_CSV.exists():
        kw = keywords_from_file()
        log.warning(f"keywords: {why}, using {SEED_CSV.name}")
        return kw
    log.error(f"keywords: {why}, and there is no {SEED_CSV.name} here -- NOTHING is scored until the "
              f"sheet answers (check KEYWORDS_CSV_URL and that the sheet is published as CSV)")
    return None


# --------------------------------------------------------------------------
# keyword source: published sheet -> SQLite cache -> seed file
# --------------------------------------------------------------------------

_memo = {"kw": None, "checked": 0.0}

KEYWORDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS keywords (
    tier TEXT, term TEXT, language TEXT, weight INTEGER, grp TEXT, role TEXT
);
"""


def _rt_set(con, k, v):
    con.execute("INSERT INTO runtime (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, str(v)))


def _cached_rows(con):
    try:
        return [{"tier": r[0], "term": r[1], "language": r[2] or "", "weight": r[3],
                 "group": r[4] or "", "role": r[5]}
                for r in con.execute("SELECT tier,term,language,weight,grp,role FROM keywords")]
    except Exception:
        return []


def get_keywords(con, client, url, force=False):
    """Never raises. A Sheets outage, a bad edit or no network must not stop collection."""
    now = time.time()
    kw = _memo["kw"]
    if kw is not None and not force and now - _memo["checked"] < KEYWORDS_REFRESH_S:
        return kw
    _memo["checked"] = now

    if not url:
        if kw is None or not kw.origin.startswith("file:"):
            kw = _seed_or_none("no KEYWORDS_CSV_URL set")
        _memo["kw"] = kw
        return kw

    con.execute(KEYWORDS_SCHEMA.strip().rstrip(";"))
    err = None
    try:
        r = client.get(url, timeout=20.0, follow_redirects=True)
        if r.status_code != 200:
            err = f"HTTP {r.status_code}"
        else:
            rows, problems = parse_keywords_csv(r.content.decode("utf-8-sig", errors="replace"))
            err = sanity_problem(rows) if rows else (problems[0] if problems else "no rows")
            if not err:
                fresh = Keywords(rows, origin="sheet", problems=problems)
                if kw is None or fresh.hash != kw.hash:
                    con.execute("DELETE FROM keywords")
                    con.executemany("INSERT INTO keywords VALUES (?,?,?,?,?,?)",
                                    [(x["tier"], x["term"], x["language"], x["weight"],
                                      x["group"], x["role"]) for x in rows])
                    old = len(kw.rows) if kw else 0
                    if old and len(rows) < old * 0.5:
                        log.warning(f"keywords: sheet shrank from {old} to {len(rows)} terms")
                    log.info(f"keywords: {len(rows)} terms from sheet"
                             f"{', ' + str(len(problems)) + ' row problems' if problems else ''}")
                _rt_set(con, "keywords_origin", "sheet")
                _rt_set(con, "keywords_fetched_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
                con.commit()
                _memo["kw"] = fresh
                return fresh
    except Exception as ex:
        err = f"{type(ex).__name__}"

    # keep the last good sheet copy; else the SQLite cache; else the seed file
    if kw is not None and kw.origin in ("sheet", "cache"):
        log.warning(f"keywords: sheet unusable ({err}), keeping the {kw.origin} copy")
        return kw
    rows = _cached_rows(con)
    if rows and not sanity_problem(rows):
        kw = Keywords(rows, origin="cache")
        log.warning(f"keywords: sheet unusable ({err}), using cached copy of {len(rows)} terms")
    elif kw is None:
        kw = _seed_or_none(f"sheet unusable ({err}) and no cache")
    _memo["kw"] = kw
    return kw


# --------------------------------------------------------------------------
# scoring -- port of score_ and band_
# --------------------------------------------------------------------------

def _best(hitlist):
    return max(hitlist, key=lambda h: h[0].weight) if hitlist else None


def _near(text):
    t = INITIALS.sub(r"\1 ", text)
    return any(p.search(t) for p in NEAR_EN) or any(p.search(text) for p in NEAR_TA)


def _title_word_spans(text):
    sp = [m.span() for m in TITLE_WORDS_EN.finditer(text)]
    for w in TITLE_WORDS_TA:
        i = text.find(w)
        while i != -1:
            sp.append((i, i + len(w)))
            i = text.find(w, i + 1)
    return sp


def _gap(a, b):
    return max(0, b[0] - a[1], a[0] - b[1])


def score_text(kw, title, desc="", body="", body_chars=SCORE_BODY_CHARS):
    raw = f"{title} — {strip_urls(strip_html(desc))[:400]} — {(body or '')[:body_chars]}"
    return kw.mask(norm_match(raw))


def score(kw, title, desc="", body="", has_ai=False, body_chars=SCORE_BODY_CHARS):
    """Returns dict(score, band, terms, tags, veto). Same structure as the Apps Script:
    one place tier; civic/crime only once placed; portfolio strong-or-weak; near;
    party; penalty; collision. Plus the three drops from BUILD_PHASE1 section 2."""
    text = score_text(kw, title, desc, body, body_chars)
    h = kw.hits(text)
    s, terms, tags, veto = 0, [], [], None

    places = [x for t in PLACE_TIERS for x in h.get(t, [])]
    best = _best(places)
    placed = best is not None
    strong = any(t.weight >= STRONG_WEIGHT for t, _ in h.get("constituency", []))
    if placed:
        s += best[0].weight
        terms += [t.term for t, _ in sorted(places, key=lambda x: -x[0].weight)[:3]]
        tags += sorted({t.tier for t, _ in places})

    ctx = [x for t in CONTEXT_TIERS for x in h.get(t, [])]
    if ctx and placed:
        s += _best(ctx)[0].weight
        terms += [t.term for t, _ in ctx[:4]]

    port = _best(h.get("portfolio", []))
    if port:
        s += port[0].weight
        terms.append(port[0].term)
        tags.append("portfolio")

    if _near(text):
        s += NEAR_WEIGHT
        terms.append("minister~kumar")
        tags.append("mention")

    party = _best(h.get("party", []))
    if party:
        s += party[0].weight
        terms.append(party[0].term)
        tags.append("party")

    pen = h.get("penalty", [])
    if pen:
        s -= max(abs(t.weight) for t, _ in pen)
        terms += ["-" + t.term for t, _ in pen[:2]]

    col = h.get("collision", [])
    if col:
        if not strong:
            s -= max(abs(t.weight) for t, _ in col)
        terms += ["!" + t.term for t, _ in col[:2]]
        # The known weakness: "Avadi MLA Ramesh Kumar visits Velachery" got through because
        # the Velachery anchor cancelled the penalty. Named next to a title, it is a
        # different office-holder -- unless our own R. Kumar is named as well.
        titles = _title_word_spans(text)
        if titles and not (TARGET_EN.search(text) or TARGET_TA.search(text)):
            for t, spans in col:
                if any(_gap(a, b) <= COLLISION_WINDOW for a in spans for b in titles):
                    veto = f"collision:{t.term}"
                    break

    if h.get("veto"):
        veto = veto or f"veto:{h['veto'][0][0].term}"
    if FOREIGN_SCRIPT.search(title or "") and not strong:
        veto = veto or "foreign-script"

    if veto:
        band = "DROP"
        terms.append("✖" + veto)
    elif s >= AUTO_KEEP:
        band = "AUTO_KEEP"
    elif s >= AI_MIN:
        band = "AI" if has_ai else ("KEYWORD_KEEP" if s >= KEYWORD_ONLY_KEEP else "DROP")
    else:
        band = "DROP"

    return {"score": s, "band": band, "terms": list(dict.fromkeys(terms))[:14],
            "tags": list(dict.fromkeys(tags)), "veto": veto}


# --------------------------------------------------------------------------
# urgent predicate -- deterministic, no AI, independent of the score
# --------------------------------------------------------------------------

def urgent(kw, title, desc="", body=""):
    """A constituency/district place AND an urgent term, close together.

    Deliberately ignores collision, penalty and veto: those decide whether an item is
    ABOUT our Kumar, not whether Velachery is flooding. A different minister touring a
    flood is still a flood, and 'container trailer overturns, 2 dead' must not be
    dropped as film news.

    Returns None, or dict(target, anchors, triggers, groups, zone, excerpt)."""
    tn = kw.mask(norm_match(title))
    # YouTube descriptions end in hashtag blocks (#Chennai #Rain #Velachery ...) that
    # would satisfy the proximity rule without the video being about either.
    zone_raw = strip_urls(body) if (body or "").strip() else strip_urls(strip_html(desc))
    zone_raw = HASHTAG.sub(" ", zone_raw)
    zn, znfc, zmap = norm_map(zone_raw)
    zn = kw.mask(zn)

    def split(text):
        hh = kw.hits(text)
        places = [x for t in URGENT_PLACE_TIERS for x in hh.get(t, [])]
        trig = [x for t in CONTEXT_TIERS for x in hh.get(t, []) if x[0].role == "urgent"]
        return places, trig

    tp, tt = split(tn)
    zp, zt = split(zn)
    lede = [(t, [s for s in sp if s[1] <= URGENT_LEDE]) for t, sp in zp + zt]
    lede_places = [(t, sp) for t, sp in lede if sp and t.tier in URGENT_PLACE_TIERS]
    lede_trig = [(t, sp) for t, sp in lede if sp and t.tier in CONTEXT_TIERS]

    chosen, span = None, None
    if tp and tt:
        chosen = (tp, tt, "title")
    elif tp and lede_trig:
        chosen = (tp, lede_trig, "title+lede")
        span = min(s for _, sp in lede_trig for s in sp)
    elif tt and lede_places:
        chosen = (lede_places, tt, "lede+title")
        span = min(s for _, sp in lede_places for s in sp)
    else:
        best = None
        for pt, psp in zp:
            for ut, usp in zt:
                for a in psp:
                    for b in usp:
                        g = _gap(a, b)
                        if g <= URGENT_PROXIMITY and (best is None or g < best[0]):
                            best = (g, pt, ut, (min(a[0], b[0]), max(a[1], b[1])))
        if best:
            _, pt, ut, span = best
            chosen = ([(pt, [])], [(ut, [])], "body")

    if not chosen:
        return None
    places, trig, zone = chosen
    top = max(places, key=lambda x: (x[0].tier == "constituency", x[0].weight))[0]

    excerpt = ""
    if zn:
        if span and zmap:
            a, b = zmap[min(span[0], len(zmap) - 1)], zmap[min(span[1], len(zmap)) - 1] + 1
            excerpt = _window(znfc, a, b, 300)
        else:
            excerpt = cut(re.sub(r"\s+", " ", znfc).strip(), 300)

    return {"target": TARGET_LABEL[top.tier],
            "anchors": list(dict.fromkeys(t.label for t, _ in places)),
            "triggers": list(dict.fromkeys(t.label for t, _ in trig)),
            "groups": sorted({(t.group or "urgent").lower() for t, _ in trig}),
            "zone": zone, "excerpt": excerpt}


def _window(text, a, b, width):
    pad = max(40, (width - (b - a)) // 2)
    s, e = max(0, a - pad), min(len(text), b + pad)
    while s > 0 and unicodedata.category(text[s]) in COMBINING:
        s -= 1
    if s > 0:
        k = text.find(" ", s, a)
        s = k + 1 if k != -1 else s
    while e < len(text) and unicodedata.category(text[e]) in COMBINING:
        e += 1
    if e < len(text):
        k = text.rfind(" ", b, e)
        e = k if k != -1 else e
    out = re.sub(r"\s+", " ", text[s:e]).strip()
    return ("…" if s > 0 else "") + out + ("…" if e < len(text) else "")
