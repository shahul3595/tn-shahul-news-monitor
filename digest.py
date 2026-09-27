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
import sys
import time
from collections import Counter, defaultdict
from datetime import timedelta
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
ICONS = {"mention": "🗣", "constituency": "📍", "district": "🏛", "portfolio": "💻",
         "political": "🏳", "opportunity": "🎯"}
URGENT_CATEGORIES = ("constituency", "district")

PER_CATEGORY = 5              # first pass
MAX_TOTAL = PER_CATEGORY * len(CATEGORIES)
PUBLISHED_WITHIN_H = 36       # a 14-day query can surface old stories; the brief is about now
WINDOW_CAP_H = 48             # never look further back than this, even on the first send
AI_MAX_CALLS_PER_DAY = 40
AI_BATCH = 30                 # items per Gemini call
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
ITEM_COLUMNS = [("digested_at", "TEXT"), ("digest_id", "INTEGER")]


def settings(env):
    def num(k, d):
        try:
            return int(env.get(k) or d)
        except ValueError:
            return d
    per = max(1, num("DIGEST_PER_CATEGORY", PER_CATEGORY))
    return {"per": per, "max": max(per, num("DIGEST_MAX", per * len(CATEGORIES))),
            "ai_calls": num("DIGEST_AI_MAX_CALLS", AI_MAX_CALLS_PER_DAY),
            "model": (env.get("GEMINI_MODEL") or MODEL_DEFAULT).strip()}


def load_env():
    env = rules.load_env()
    for k in ("GEMINI_MODEL", "DIGEST_MAX", "DIGEST_PER_CATEGORY", "DIGEST_AI_MAX_CALLS"):
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
       i.ai_category, i.ai_priority, i.ai_reason, i.ai_processed_at, s.name AS source_name
FROM items i LEFT JOIN sources s ON s.source_id = i.source_id
WHERE i.digested_at IS NULL
  AND i.rules_at IS NOT NULL AND i.rules_at >= ?
  AND (i.band IN ('AUTO_KEEP', 'AI', 'KEYWORD_KEEP') OR i.urgent = 1)
  AND coalesce(i.published_at, i.discovered_at) >= ?
ORDER BY i.id
"""


def candidates(con, now):
    last = parse_ts(rt_get(con, "last_digest_at"))
    since = max(now - timedelta(hours=WINDOW_CAP_H), last) if last else now - timedelta(hours=WINDOW_CAP_H)
    pub_since = now - timedelta(hours=PUBLISHED_WITHIN_H)
    rows = con.execute(CAND_SQL, (iso(since), iso(pub_since))).fetchall()
    # one per story: the event's best-scored, then most recent, report
    groups = defaultdict(list)
    for r in rows:
        groups[r["event_id"] if r["event_id"] is not None else f"item:{r['id']}"].append(r)
    reps, all_ids = [], [r["id"] for r in rows]
    for members in groups.values():
        rep = max(members, key=lambda r: (r["urgent"] or 0, r["score"] or 0, r["published_at"] or ""))
        d = dict(rep)
        d["sources"] = list(dict.fromkeys(alerts.outlet_name(m) for m in members))
        d["tags"] = alerts._j(rep["target_tags"], [])
        reps.append(d)
    return reps, all_ids, since


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
none         - irrelevant, a DIFFERENT person named Kumar (Ramesh Kumar of Avadi, Sarath Kumar,
               Nirmal Kumar, Udhayakumar ...), film or cinema news, real estate listings, hotel
               or motor-trade press releases, exam-prep content, a place with the same name
               outside Tamil Nadu

Priority: 1 immediate (flooding now, a death, a major protest, an urgent official statement in
his areas), 2 standard news worth reading today, 3 background.

Rules: a story about another district's collector or another state's IT minister is "none".
Local murders, fatal accidents and major protests in Velachery or Thiruvallur are never "none".
If your reason says the item is generic, unclear or has no link to him or his areas, the
category must be "none". Return one object per item, using the item numbers given.

ITEMS:
{payload}"""

RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {"type": "OBJECT", "properties": {
        "n": {"type": "INTEGER"},
        "category": {"type": "STRING", "enum": CATEGORIES + ["none"]},
        "priority": {"type": "INTEGER"},
        "reason": {"type": "STRING"}},
        "required": ["n", "category", "priority", "reason"]},
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

    def rank(self, batch):
        """batch: list of candidate dicts. Returns {id: (category, priority, reason)} or None."""
        if self.dead:
            return None
        day, used = self._today_calls()
        if used >= self.max_calls:
            self.dead = f"daily budget of {self.max_calls} Gemini calls used"
            return None
        if time.monotonic() - self.t0 > AI_TIME_BUDGET_S:
            self.dead = "Gemini time budget spent"
            return None
        lines = [f"{n}. [{alerts.outlet_name(c)}] {_title(c)}\n   {_snippet(c)}" for n, c in enumerate(batch, 1)]
        body = {"contents": [{"role": "user", "parts": [{"text": PROMPT.format(payload='\n'.join(lines))}]}],
                "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                                     "responseSchema": RESPONSE_SCHEMA}}
        delay = AI_PAUSE_S
        for attempt in range(4):
            if time.monotonic() - self.t0 > AI_TIME_BUDGET_S:
                self.dead = "Gemini time budget spent"
                return None
            try:
                self._count(day)
                r = self.client.post(ENDPOINT.format(m=self.model), headers={"x-goog-api-key": self.key}, json=body)
                if r.status_code == 429:
                    log.warning(f"gemini: rate limited, waiting {delay:.0f}s")
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                if r.status_code in (400, 401, 403, 404):
                    self.dead = f"HTTP {r.status_code}: {r.text[:120].replace(chr(10), ' ')}"
                    log.error(f"gemini: {self.dead} -- check GEMINI_API_KEY / GEMINI_MODEL")
                    return None
                r.raise_for_status()
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                out = {}
                for it in json.loads(text):
                    i = int(it.get("n", 0)) - 1
                    if 0 <= i < len(batch):
                        cat = it.get("category") if it.get("category") in CATEGORIES + ["none"] else "none"
                        try:
                            pri = min(3, max(1, int(it.get("priority", 2))))
                        except (TypeError, ValueError):
                            pri = 2
                        out[batch[i]["id"]] = (cat, pri, str(it.get("reason", ""))[:200])
                return out
            except Exception as ex:
                log.warning(f"gemini: attempt {attempt + 1}: {type(ex).__name__}: {str(ex)[:100]}")
                time.sleep(delay)
                delay = min(delay * 2, 60)
        self.dead = "Gemini kept failing"
        return None


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
        pri = 1
    elif c.get("band") == "AUTO_KEEP":
        pri = 2
    else:
        pri = 3
    return cat, pri, "keyword rules"


def rank_all(con, cands, gemini, now):
    """Fills c['category'], c['priority'], c['reason'], c['by']. Reuses Gemini's earlier
    verdict on an item when one is stored (a re-run costs nothing)."""
    todo = []
    for c in cands:
        if c["ai_processed_at"] and c["ai_category"]:
            c["category"], c["priority"], c["reason"], c["by"] = (c["ai_category"], c["ai_priority"] or 2,
                                                                  c["ai_reason"] or "", "gemini (cached)")
        else:
            todo.append(c)
    st = Counter()
    if gemini is not None:
        for k in range(0, len(todo), AI_BATCH):
            batch = todo[k:k + AI_BATCH]
            res = gemini.rank(batch)
            if res is None:
                break
            for c in batch:
                if c["id"] in res:
                    c["category"], c["priority"], c["reason"] = res[c["id"]]
                    c["by"] = "gemini"
                    con.execute("""UPDATE items SET ai_category=?, ai_priority=?, ai_reason=?, ai_model=?,
                                   ai_processed_at=? WHERE id=?""",
                                (c["category"], c["priority"], c["reason"], gemini.model, iso(now), c["id"]))
                    st["gemini"] += 1
            con.commit()
            if k + AI_BATCH < len(todo):
                time.sleep(AI_PAUSE_S)
    for c in cands:
        if "category" not in c:
            c["category"], c["priority"], c["reason"] = rule_rank(c)
            c["by"] = "rules"
            st["rules"] += 1
    if gemini is not None and gemini.dead:
        log.warning(f"gemini: stopped -- {gemini.dead}; {st['rules']} items ranked by the rules")
    return st


# --------------------------------------------------------------------------
# 3. selection
# --------------------------------------------------------------------------

def _order(c):
    return (c["priority"], -(c.get("urgent") or 0), -(c.get("score") or 0), c["published_at"] or "")


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
    urgent, sections, leftovers = select(cands, cfg["per"], cfg["max"])
    chosen = urgent + [c for cat in CATEGORIES for c in sections[cat]]
    messages = render(slot, now, urgent, sections, len(chosen), len(cands), bool(st.get("gemini")))
    return {"cands": cands, "all_ids": all_ids, "since": since, "chosen": chosen, "leftovers": leftovers,
            "messages": messages, "stats": st, "ai_calls": gemini.calls if gemini else 0,
            "ai_note": gemini.dead if gemini else ("no GEMINI_API_KEY" if not key else None)}


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
        log.info("digest: DRY RUN -- nothing sent, nothing marked")
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
    return b


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
        b = run(con, env, slot=None if a.slot == "auto" else a.slot, dry_run=a.dry_run)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write("```\n" + "\n\n".join(alerts.html_to_plain(m) for m in b["messages"]) + "\n```\n")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
