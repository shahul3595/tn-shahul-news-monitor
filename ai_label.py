#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI labelling. Does the two jobs that were waiting on manual judgement.

    python ai_label.py --dupes       label dupe_pairs.csv   (SAME_EVENT_y_n)
    python ai_label.py --relevance   label label_me.csv     (relevance + category)
    python ai_label.py --dupes --limit 40    cheap first run

Needs GEMINI_API_KEY in a .env file next to this script. Get one free at
aistudio.google.com -- no credit card. Then:  echo GEMINI_API_KEY=xxx > .env

Everything is cached in ai_cache.json, so re-running costs nothing for items
already done. Nothing in corpus.db is modified.
"""

import os
import csv
import json
import time
import re
import hashlib
import sqlite3
import argparse
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
DB = HERE / "corpus.db"
CACHE = HERE / "ai_cache.json"

# Model names change. If this 404s, check aistudio.google.com for the current
# free-tier flash model and set GEMINI_MODEL in .env.
MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"

BATCH = 8
PAUSE = 4.0          # seconds between calls; free tier is RPM-limited
BODY_CHARS = 700     # Tamil tokenises ~4x worse than English -- keep this small


def load_env():
    """Read .env. PowerShell's `echo >` writes UTF-16 with a BOM, so decode leniently
    and strip stray quotes -- the Apps Script hit the same thing (see cleanKey_)."""
    global MODEL
    env = HERE / ".env"
    key = os.environ.get("GEMINI_API_KEY", "")
    if env.exists():
        raw = env.read_bytes()
        for enc in ("utf-8-sig", "utf-16", "utf-8", "latin-1"):
            try:
                text = raw.decode(enc)
                if "GEMINI" in text:
                    break
            except Exception:
                continue
        else:
            text = ""
        for line in text.splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'").strip()
            if k.strip() == "GEMINI_API_KEY" and v:
                key = v
            elif k.strip() == "GEMINI_MODEL" and v:
                MODEL = v
    return key


def load_key():
    return load_env()


def stratify(rows, limit, keyfn):
    """Spread a --limit sample evenly across groups.

    Taking the head of a sorted file is never representative: label_me.csv is
    ordered by source_id, so the first 40 rows were 40 Pallikaranai headlines.
    """
    from collections import defaultdict
    import itertools
    groups = defaultdict(list)
    for r in rows:
        groups[keyfn(r)].append(r)
    out, keys = [], sorted(groups, key=str)
    for r in itertools.chain.from_iterable(
            itertools.zip_longest(*(groups[k] for k in keys))):
        if r is not None:
            out.append(r)
        if len(out) >= limit:
            break
    print(f"  sampling {len(out)} across {len(keys)} groups: "
          f"{', '.join(str(k) for k in keys[:8])}")
    return out


def cache_load():
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def cache_save(c):
    CACHE.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")


def ask(key, prompt, schema):
    """One Gemini call with enforced JSON output. Backs off on 429."""
    url = ENDPOINT.format(m=MODEL)
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                             "responseSchema": schema},
    }
    delay = PAUSE
    for attempt in range(5):
        try:
            r = httpx.post(url, headers={"x-goog-api-key": key}, json=body, timeout=120)
            if r.status_code == 429:
                print(f"      rate limited, waiting {delay:.0f}s")
                time.sleep(delay)
                delay = min(delay * 2, 120)
                continue
            if r.status_code in (400, 403, 404):
                print(f"      HTTP {r.status_code}: {r.text.replace(chr(10),' ')[:160]}")
                print("      -> run:  python ai_label.py --pick")
                return None
            r.raise_for_status()
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(txt)
        except Exception as ex:
            print(f"      attempt {attempt+1}: {type(ex).__name__}: {str(ex)[:90]}")
            time.sleep(delay)
            delay = min(delay * 2, 120)
    return None


# ---------------------------------------------------------------------------
# dedup labelling
# ---------------------------------------------------------------------------

DUPE_SCHEMA = {
    "type": "ARRAY",
    "items": {"type": "OBJECT", "properties": {
        "pair": {"type": "INTEGER"},
        "same_event": {"type": "STRING", "enum": ["y", "n"]},
        "reason": {"type": "STRING"}},
        "required": ["pair", "same_event", "reason"]},
}

DUPE_PROMPT = """You are comparing pairs of Tamil and English news articles from Tamil Nadu.

For each pair decide whether both articles report THE SAME underlying real-world event.

Same event ("y"): the same statement, the same incident, the same announcement, the
same meeting -- even if the headlines are worded completely differently, even if one
is Tamil and one is English.

Different event ("n"): the same topic or the same people, but different occasions.
Two separate statements by the same politician on different days are DIFFERENT.
A recurring daily column or news digest is DIFFERENT each day. Two floods in the
same area at different times are DIFFERENT.

Be strict. When genuinely torn, answer "n" -- wrongly merging two events hides news,
which is worse than showing one story twice.

Return one object per pair, using the pair numbers given.

PAIRS:
{payload}"""


def label_dupes(key, limit):
    path = HERE / "dupe_pairs.csv"
    if not path.exists():
        print("dupe_pairs.csv not found -- run: python analyse.py --dupes")
        return
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    if not rows:
        print("dupe_pairs.csv is empty")
        return

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    bodies = {}
    for r in con.execute("SELECT id, extract_text FROM items WHERE extract_text IS NOT NULL"):
        bodies[str(r["id"])] = (r["extract_text"] or "")[:BODY_CHARS]
    con.close()

    cache = cache_load()
    todo = [r for r in rows if not (r.get("SAME_EVENT_y_n") or "").strip()]
    if limit:
        todo = stratify(todo, limit, lambda r: round(float(r["body_sim"]) * 10) / 10)
    print(f"{len(rows)} pairs total, {len(todo)} to label "
          f"(batch {BATCH}, ~{-(-len(todo)//BATCH)} calls)\n")

    done = 0
    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        lines, keys = [], []
        for n, r in enumerate(chunk, 1):
            ck = hashlib.md5(f"d|{r['id_a']}|{r['id_b']}".encode()).hexdigest()
            keys.append(ck)
            if ck in cache:
                continue
            lines.append(
                f"--- PAIR {n} (published {r.get('hours_apart','?')}h apart) ---\n"
                f"A [{r['host_a']}] {r['title_a']}\n{bodies.get(r['id_a'],'')}\n\n"
                f"B [{r['host_b']}] {r['title_b']}\n{bodies.get(r['id_b'],'')}\n")

        if lines:
            print(f"  batch {start//BATCH + 1}: {len(lines)} pairs -> Gemini")
            res = ask(key, DUPE_PROMPT.format(payload="\n".join(lines)), DUPE_SCHEMA)
            if res:
                for item in res:
                    i = int(item.get("pair", 0)) - 1
                    if 0 <= i < len(chunk):
                        cache[keys[i]] = {"v": item.get("same_event", "n"),
                                          "why": item.get("reason", "")[:160]}
                cache_save(cache)
            time.sleep(PAUSE)

        for r, ck in zip(chunk, keys):
            if ck in cache:
                r["SAME_EVENT_y_n"] = cache[ck]["v"]
                r["AI_REASON"] = cache[ck]["why"]
                done += 1

    cols = list(rows[0].keys())
    if "AI_REASON" not in cols:
        cols.append("AI_REASON")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})

    y = sum(1 for r in rows if (r.get("SAME_EVENT_y_n") or "").strip() == "y")
    n = sum(1 for r in rows if (r.get("SAME_EVENT_y_n") or "").strip() == "n")
    print(f"\n  labelled {done} pairs -> {y} same-event, {n} different")
    print(f"  written to {path}")
    print("\n  Now run:  python analyse.py --score")


# ---------------------------------------------------------------------------
# relevance labelling
# ---------------------------------------------------------------------------

REL_SCHEMA = {
    "type": "ARRAY",
    "items": {"type": "OBJECT", "properties": {
        "n": {"type": "INTEGER"},
        "relevant": {"type": "STRING", "enum": ["y", "n"]},
        "category": {"type": "STRING", "enum": ["mention", "constituency", "district",
                                                "portfolio", "political", "opportunity", "none"]},
        "priority": {"type": "INTEGER"},
        "reason": {"type": "STRING"}},
        "required": ["n", "relevant", "category", "priority", "reason"]},
}

REL_PROMPT = """You triage Tamil and English news for the office of R. Kumar, MLA for
Velachery (Chennai AC 26), Minister for Artificial Intelligence, Information Technology
and Digital Services, Government of Tamil Nadu, and District In-Charge Minister for
Thiruvallur. He belongs to Tamilaga Vettri Kazhagam (TVK).

Categorise each headline:

mention      - directly names or quotes this R. Kumar, or criticism/praise of him
constituency - civic issues, flooding, sewage, drains, roads, lakes, MRTS, crime or
               law-and-order in Velachery, Adyar, Besant Nagar, Thiruvanmiyur,
               Tharamani, Adambakkam, Pallikaranai -- whether or not he is named
district     - Thiruvallur district administration, collectorate, review meetings,
               Poondi reservoir, Gummidipoondi, Ponneri, Avadi, Ambattur, Poonamallee
portfolio    - AI, IT, digital governance, ELCOT, TIDEL, StartupTN, data centres, GCC
               investment, IT corridor, and rival-state (Karnataka/Telangana) IT policy
political    - attacks or alliances specifically touching his seat, district or
               department. GENERIC TVK vs DMK vs AIADMK battles are "none"
opportunity  - schemes, inaugurations, foundation stones, summits he could attend
none         - irrelevant, a DIFFERENT person named Kumar, film or cinema news, real
               estate listings, hotel or motor-trade press releases, exam-prep content

Priority: 1 immediate (flooding now, a death, a protest, an urgent official statement),
2 standard news, 3 background.

Critical: a DIFFERENT Kumar (Ramesh Kumar of Avadi, Sarath Kumar, Nirmal Kumar, and so
on) is "none". Local murders, fatal accidents and major protests in Velachery or
Thiruvallur are never "none" -- they are "constituency" or "district" at priority 1 or 2.

HEADLINES:
{payload}"""


def label_relevance(key, limit):
    path = HERE / "label_me.csv"
    if not path.exists():
        print("label_me.csv not found -- run: python analyse.py --label")
        return
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    cache = cache_load()
    todo = [r for r in rows if not (r.get("RELEVANT_y_n") or "").strip()]
    if limit:
        todo = stratify(todo, limit, lambda r: r.get("source_id", ""))
    print(f"{len(rows)} rows total, {len(todo)} to label "
          f"(batch {BATCH*2}, ~{-(-len(todo)//(BATCH*2))} calls)\n")

    B = BATCH * 2
    for start in range(0, len(todo), B):
        chunk = todo[start:start + B]
        lines, keys = [], []
        for n, r in enumerate(chunk, 1):
            ck = hashlib.md5(f"r|{r['id']}".encode()).hexdigest()
            keys.append(ck)
            if ck not in cache:
                lines.append(f"{n}. [{r['host']}] {r['title']}")
        if lines:
            print(f"  batch {start//B + 1}: {len(lines)} headlines -> Gemini")
            res = ask(key, REL_PROMPT.format(payload="\n".join(lines)), REL_SCHEMA)
            if res:
                for item in res:
                    i = int(item.get("n", 0)) - 1
                    if 0 <= i < len(chunk):
                        cache[keys[i]] = {"v": item.get("relevant", "n"),
                                          "cat": item.get("category", "none"),
                                          "pri": item.get("priority", 3),
                                          "why": item.get("reason", "")[:140]}
                cache_save(cache)
            time.sleep(PAUSE)
        for r, ck in zip(chunk, keys):
            if ck in cache:
                d = cache[ck]
                r["RELEVANT_y_n"] = d["v"]
                r["TARGET_tvk_velachery_none"] = d["cat"]
                r["AI_PRIORITY"] = d["pri"]
                r["NOTE"] = d["why"]

    cols = list(rows[0].keys())
    if "AI_PRIORITY" not in cols:
        cols.append("AI_PRIORITY")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})

    import collections
    cats = collections.Counter(r.get("TARGET_tvk_velachery_none", "") for r in rows
                               if r.get("RELEVANT_y_n"))
    print("\n  category breakdown:")
    for k, v in cats.most_common():
        print(f"    {k or '(blank)':<14} {v}")
    print(f"\n  written to {path}")


# Model families that cannot do text classification. Borrowed from the Apps Script.
NOT_TEXT = re.compile(r"embedding|image|tts|audio|veo|imagen|lyria|robotics|live|"
                      r"banana|computer-use|deep-research|transcribe|antigravity")

# Cheapest first. The "-latest" aliases survive model retirements, which is why the
# Apps Script uses them instead of pinning a version number.
PREFERRED = ["gemini-flash-lite-latest", "gemini-3.1-flash-lite", "gemini-3.5-flash-lite",
             "gemini-2.5-flash-lite", "gemini-flash-latest", "gemini-2.5-flash",
             "gemini-3.5-flash", "gemini-3-flash-preview"]


def fetch_models(key):
    try:
        r = httpx.get("https://generativelanguage.googleapis.com/v1beta/models",
                      headers={"x-goog-api-key": key},
                      params={"pageSize": 200}, timeout=60)
        r.raise_for_status()
        return r.json().get("models", [])
    except Exception as ex:
        print(f"could not list models: {type(ex).__name__}: {str(ex)[:140]}")
        return []


def candidates(key):
    avail = {m["name"].replace("models/", "") for m in fetch_models(key)
             if "generateContent" in m.get("supportedGenerationMethods", [])}
    out = [n for n in PREFERRED if n in avail]
    out += sorted(n for n in avail
                  if n not in out and not NOT_TEXT.search(n) and "flash" in n)
    out += sorted(n for n in avail
                  if n not in out and not NOT_TEXT.search(n))
    return out


def list_models(key):
    c = candidates(key)
    if not c:
        print("no text-capable models visible to this key")
        return
    print(f"{len(c)} text-capable models, cheapest first:\n")
    for n in c[:14]:
        print(f"  {n}")
    print("\nBeing listed does NOT mean callable -- run:  python ai_label.py --pick")


def pick_model(key):
    """Test candidates with a real one-token request and save the first that answers."""
    c = candidates(key)
    if not c:
        print("no candidates")
        return
    print(f"testing up to 8 of {len(c)} candidates with a real request:\n")
    for name in c[:8]:
        try:
            r = httpx.post(ENDPOINT.format(m=name), headers={"x-goog-api-key": key},
                           json={"contents": [{"role": "user",
                                               "parts": [{"text": "Reply with: ok"}]}],
                                 "generationConfig": {"temperature": 0}}, timeout=60)
            if r.status_code == 200:
                print(f"  {name:<34} HTTP 200  -> SELECTED")
                env = HERE / ".env"
                lines = [l for l in (env.read_text(encoding="utf-8").splitlines()
                                     if env.exists() else [])
                         if not l.strip().startswith("GEMINI_MODEL")]
                lines.append(f"GEMINI_MODEL={name}")
                env.write_text("\n".join(lines) + "\n", encoding="utf-8")
                print(f"\n  written to .env. Now run:  python ai_label.py --dupes --limit 24")
                return
            msg = r.text.replace("\n", " ")[:88]
            print(f"  {name:<34} HTTP {r.status_code}  {msg}")
        except Exception as ex:
            print(f"  {name:<34} {type(ex).__name__}: {str(ex)[:60]}")
    print("\nNothing answered. Check the key is valid and unrestricted.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dupes", action="store_true")
    ap.add_argument("--relevance", action="store_true")
    ap.add_argument("--models", action="store_true",
                    help="list text-capable models, cheapest first")
    ap.add_argument("--pick", action="store_true",
                    help="test candidates for real and save the winner to .env")
    ap.add_argument("--limit", type=int, default=0,
                    help="only label this many, to test cheaply first")
    a = ap.parse_args()

    key = load_key()
    if not key:
        print("No GEMINI_API_KEY found.")
        print("  1. get a free key at aistudio.google.com (no credit card)")
        print("  2. create a file called .env next to this script containing:")
        print("     GEMINI_API_KEY=your_key_here")
        return
    if a.models:
        return list_models(key)
    if a.pick:
        return pick_model(key)
    print(f"model: {MODEL}\n")

    if a.dupes:
        label_dupes(key, a.limit)
    elif a.relevance:
        label_relevance(key, a.limit)
    else:
        print("pick one: --pick, --models, --dupes, or --relevance")


if __name__ == "__main__":
    main()
