#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 1 alerting -- the urgent path, the event model and the Telegram queue.

collect.py calls maybe_tick() between items. A tick
  1. scores items that have finished resolving/extracting   items.score, band, urgent, canonical_key
  2. files them into events at body similarity 0.45         events, items.event_id
  3. queues an urgent alert, or records why it did not      alert_queue PENDING / SUPPRESSED
  4. delivers the queue to Telegram at ~1 message/second    alert_queue SENT / FAILED / EXPIRED
Processing and delivery keep separate status, and an error in either is logged and
skipped -- it never stops collection.

    python alerts.py --backtest        replay the whole corpus through this code; sends nothing
    python alerts.py --test-telegram   send one message full of Tamil and HTML traps
    python alerts.py --status          queue, recent alerts, open events, latency
    python alerts.py --once            one tick now (the collector normally does this)
    python alerts.py --run             tick every minute on its own

Nothing reaches Telegram until TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are in .env.
Until then every alert is marked DRY_RUN and written to the log instead.
TELEGRAM_DRY_RUN=1 forces that even with a token.
"""

import argparse
import csv
import html
import json
import logging
import re
import signal
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import rules

HERE = Path(__file__).resolve().parent
DB = HERE / "corpus.db"
BACKTEST_DB = HERE / "backtest.db"
REPORT = HERE / "backtest_report.txt"
LABELS = HERE / "label_me.csv"

log = logging.getLogger("collect.alerts")
IST = timezone(timedelta(hours=5, minutes=30))

# --------------------------------------------------------------------------
# tunables -- the backtest prints the ones that matter
# --------------------------------------------------------------------------

TICK_MIN_INTERVAL_S = 45      # collect.py may call maybe_tick() as often as it likes
TICK_PROCESS_BUDGET_S = 20    # never hold the collector longer than this per tick
READY_BATCH = 300
URGENT_MAX_AGE_H = 6          # BUILD_PHASE1 section 3: under 6 hours old by published_at
EVENT_QUIET_H = 6             # OPEN -> QUIET with no new member
EVENT_CLOSE_H = 24            # -> CLOSED with no new member
EVENT_MAX_SPAN_H = 24         # an event closes this long after its first report even if
                              # still active, so a week-long flood re-alerts daily
EVENT_MIN_BODY = 400          # the 0.45 calibration only compared bodies this long
URGENT_PLACE_WINDOW_H = 3     # reports that cannot be body-compared -- videos, failed extractions,
                              # the other language -- fold into an alert for the same place and
                              # emergency sent this recently, instead of each buzzing the phone
ALERT_EXPIRE_H = 12           # an undelivered alert older than this is not sent
DELAY_NOTE_MIN = 30           # alerts delivered later than this say so
TG_PACE_S = 1.1
TG_MAX_ATTEMPTS = 6
TG_LIMIT = 4000               # Telegram allows 4096; measured on the HTML, in UTF-16 units
DELIVER_BUDGET_S = 25
SENDING_STALE_MIN = 10
BACKTEST_SLOT_MIN = 5         # replay granularity, roughly the live tick cadence

LIVE = ("PENDING", "SENDING", "SENT", "DRY_RUN")      # delivered, or will be
# Suicide reports alert under their own heading, without the siren, and without the press
# excerpt -- Tamil reports often put the method in the lede, and it adds nothing to the
# office's response. To take suicide out of the urgent path entirely, set its sheet rows'
# role to "context": they still reach the briefing.
QUIET_GROUPS = ("suicide",)
SCHEMA_VERSION = "phase1-v1"

# --------------------------------------------------------------------------
# schema -- all additive; BUILD_PHASE1 section 1 plus the columns delivery needs
# --------------------------------------------------------------------------

ITEM_COLUMNS = [
    ("canonical_key", "TEXT"), ("score", "INTEGER"), ("matched_terms", "TEXT"),
    ("target_tags", "TEXT"), ("band", "TEXT"), ("urgent", "INTEGER DEFAULT 0"),
    ("ai_category", "TEXT"), ("ai_priority", "INTEGER"), ("ai_summary", "TEXT"),
    ("ai_reason", "TEXT"), ("ai_model", "TEXT"), ("ai_processed_at", "TEXT"),
    ("event_id", "INTEGER"), ("rules_at", "TEXT"),
]
EVENT_COLUMNS = [
    ("first_seen_at", "TEXT"), ("last_seen_at", "TEXT"), ("status", "TEXT DEFAULT 'OPEN'"),
    ("target", "TEXT"), ("canonical_topic", "TEXT"), ("rep_item_id", "INTEGER"),
    ("priority", "INTEGER"), ("members", "INTEGER DEFAULT 1"),
]
QUEUE_COLUMNS = [
    ("item_id", "INTEGER"), ("event_id", "INTEGER"), ("kind", "TEXT"), ("priority", "INTEGER"),
    ("body", "TEXT"), ("status", "TEXT DEFAULT 'PENDING'"), ("attempts", "INTEGER DEFAULT 0"),
    ("created_at", "TEXT"), ("sent_at", "TEXT"), ("error", "TEXT"),
    # beyond the spec: why an alert was suppressed, what matched, what it edits
    ("reason", "TEXT"), ("detail", "TEXT"), ("ref_id", "INTEGER"), ("message_id", "INTEGER"),
    ("next_attempt_at", "TEXT"), ("claimed_at", "TEXT"),
]
TABLES = [
    "CREATE TABLE IF NOT EXISTS runtime (k TEXT PRIMARY KEY, v TEXT)",
    "CREATE TABLE IF NOT EXISTS events (event_id INTEGER PRIMARY KEY)",
    "CREATE TABLE IF NOT EXISTS alert_queue (id INTEGER PRIMARY KEY)",
    "CREATE TABLE IF NOT EXISTS ai_budget (day TEXT PRIMARY KEY, calls INTEGER DEFAULT 0, "
    "tokens INTEGER DEFAULT 0)",
    rules.KEYWORDS_SCHEMA.strip().rstrip(";"),
]
INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_items_canon ON items(canonical_key)",
    "CREATE INDEX IF NOT EXISTS ix_items_event ON items(event_id)",
    "CREATE INDEX IF NOT EXISTS ix_items_band ON items(band)",
    "CREATE INDEX IF NOT EXISTS ix_events_status ON events(status)",
    "CREATE INDEX IF NOT EXISTS ix_queue_status ON alert_queue(status, priority, id)",
    "CREATE INDEX IF NOT EXISTS ix_queue_event ON alert_queue(event_id)",
    "CREATE INDEX IF NOT EXISTS ix_queue_item ON alert_queue(item_id)",
]


def _columns(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def migrate(con):
    """Idempotent. Needs collect.py's items table; never drops or rewrites anything."""
    if con.in_transaction:
        con.commit()
    if "items" not in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        raise RuntimeError("no items table -- run 'python collect.py --init' first")
    for ddl in TABLES:
        con.execute(ddl)
    added = []
    for table, cols in (("items", ITEM_COLUMNS), ("events", EVENT_COLUMNS),
                        ("alert_queue", QUEUE_COLUMNS)):
        have = _columns(con, table)
        for name, decl in cols:
            if name not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                added.append(f"{table}.{name}")
    for ddl in INDEXES:
        con.execute(ddl)
    rt_set(con, "schema_phase1", SCHEMA_VERSION)
    con.commit()
    if added and len(added) > 3:
        log.info(f"alerts: schema upgraded, {len(added)} columns added")
    return added


_migrated = set()


def ensure_migrated(con):
    if con.row_factory is None:
        con.row_factory = sqlite3.Row
    k = _db_key(con)
    if k in _migrated:
        return
    if rt_get(con, "schema_phase1") != SCHEMA_VERSION or not {"band", "event_id"} <= _columns(con, "items"):
        migrate(con)
    _migrated.add(k)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_ist(s):
    dt = parse_ts(s)
    if not dt:
        return "time unknown"
    dt = dt.astimezone(IST)
    return f"{dt.day} {dt:%b}, {dt:%H:%M} IST"


def rt_get(con, k, default=None):
    try:
        r = con.execute("SELECT v FROM runtime WHERE k=?", (k,)).fetchone()
    except sqlite3.OperationalError:
        return default
    return r[0] if r else default


def rt_set(con, k, v):
    con.execute("INSERT INTO runtime (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, None if v is None else str(v)))


def _db_key(con):
    try:
        path = con.execute("PRAGMA database_list").fetchone()[2]
    except Exception:
        path = ""
    return path or f"memory:{id(con)}"


def _begin(con):
    if con.in_transaction:
        con.commit()
    con.execute("BEGIN IMMEDIATE")


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def esc_attr(s):
    return esc(s).replace('"', "&quot;")


def html_to_plain(text):
    """Last resort when Telegram rejects our HTML: keep the link visible."""
    t = re.sub(r'<a href="([^"]*)">(.*?)</a>', lambda m: f"{m.group(2)}: {m.group(1)}", text or "",
               flags=re.S)
    t = re.sub(r"</?(?:b|i|u|s|code|pre)>", "", t)
    return html.unescape(t)


def _j(v, default):
    try:
        return json.loads(v) if v else default
    except ValueError:
        return default


def _publisher_host(raw_payload):
    """The Google News <source url> -- known before the token is decoded."""
    if not raw_payload:
        return ""
    try:
        src = json.loads(raw_payload).get("source") or {}
        return rules.host_of(src.get("href", ""))
    except (ValueError, AttributeError):
        m = re.search(r'"source":\s*\{[^}]*"href":\s*"([^"]+)"', raw_payload)
        return rules.host_of(m.group(1)) if m else ""


def outlet_name(row):
    keys = row.keys()
    for k in ("publisher", "source_name"):
        if k in keys and (row[k] or "").strip():
            return row[k].strip()
    return (row["extract_host"] if "extract_host" in keys else "") or rules.host_of(display_url(row)) or "?"


def display_url(row):
    if row["resolve_status"] in ("RESOLVED", "SKIPPED", "BLOCKED") and row["resolved_url"]:
        return row["resolved_url"]
    return row["link"] or ""


TARGET_FROM_TAG = (("constituency", "VELACHERY"), ("district", "THIRUVALLUR"), ("mention", "MENTION"),
                   ("portfolio", "PORTFOLIO"), ("party", "PARTY"), ("area", "AREA"))


def _target_from_tags(tags):
    for tag, label in TARGET_FROM_TAG:
        if tag in (tags or []):
            return label
    return None


# --------------------------------------------------------------------------
# 1-3. processing: score, event, urgent decision
# --------------------------------------------------------------------------

READY_SQL = """
SELECT i.id, i.source_id, i.link, i.title, i.description, i.publisher,
       i.published_at, i.discovered_at, i.resolve_status, i.resolved_url,
       i.extract_status, i.extract_host, i.extract_text,
       CASE WHEN i.resolved_url IS NULL THEN i.raw_payload END AS raw_payload,
       s.name AS source_name
FROM items i LEFT JOIN sources s ON s.source_id = i.source_id
WHERE i.band IS NULL
  AND (i.extract_status <> 'PENDING' OR i.resolve_status IN ('FAILED', 'BLOCKED'))
  AND i.discovered_at <= ?
ORDER BY i.discovered_at, i.id
LIMIT ?
"""

RENDER_SQL = """
SELECT i.id, i.link, i.title, i.publisher, i.published_at, i.discovered_at, i.resolve_status,
       i.resolved_url, i.extract_host, s.name AS source_name
FROM items i LEFT JOIN sources s ON s.source_id = i.source_id WHERE i.id = ?
"""


def ensure_cutoff(con, now=None):
    """Bootstrap: everything collected before the first tick is scored but never alerted."""
    v = rt_get(con, "alert_cutoff_at")
    if v:
        return v
    v = iso(now or utcnow())
    rt_set(con, "alert_cutoff_at", v)
    con.commit()
    older = con.execute("SELECT count(*) FROM items WHERE discovered_at < ?", (v,)).fetchone()[0]
    log.info(f"alerts: first run -- alert cutoff set to {fmt_ist(v)}; the {older} items "
             f"already collected will be scored but never alerted")
    return v


def refresh_events(con, now):
    """OPEN -> QUIET after 6h without a new member; CLOSED after 24h without one, or 24h
    after the first report. A later report on a CLOSED event starts a new event."""
    close = iso(now - timedelta(hours=EVENT_CLOSE_H))
    span = iso(now - timedelta(hours=EVENT_MAX_SPAN_H))
    quiet = iso(now - timedelta(hours=EVENT_QUIET_H))
    _begin(con)
    con.execute("""UPDATE events SET status='CLOSED' WHERE status<>'CLOSED'
                   AND (last_seen_at < ? OR first_seen_at < ?)""", (close, span))
    con.execute("UPDATE events SET status='QUIET' WHERE status='OPEN' AND last_seen_at < ?", (quiet,))
    con.commit()


def process_ready(con, kw, now, has_ai, blocked=None, limit=READY_BATCH, budget_s=None):
    blocked = blocked or rules.blocked_hosts()
    cutoff = rt_get(con, "alert_cutoff_at")
    refresh_events(con, now)
    rows = con.execute(READY_SQL, (iso(now), limit)).fetchall()
    st = Counter()
    t0 = time.monotonic()
    for row in rows:
        if budget_s is not None and time.monotonic() - t0 > budget_s:
            st["deferred"] += 1
            continue
        try:
            _begin(con)
            if con.execute("SELECT band FROM items WHERE id=?", (row["id"],)).fetchone()[0] is not None:
                con.commit()                      # another process got there first
                continue
            process_item(con, kw, row, now, has_ai, blocked, cutoff, st)
            con.commit()
        except Exception:
            con.rollback()
            log.exception(f"alerts: item {row['id']} could not be processed; marked band=ERROR")
            try:
                con.execute("UPDATE items SET band='ERROR', rules_at=? WHERE id=?", (iso(now), row["id"]))
                con.commit()
            except sqlite3.Error:
                con.rollback()
            st["error"] += 1
    return st


def process_item(con, kw, row, now, has_ai, blocked, cutoff, st):
    title = rules.clean_title(row["title"] or "", row["publisher"] or "")
    desc = row["description"] or ""
    body = (row["extract_text"] or "") if row["extract_status"] in ("OK", "THIN") else ""
    url = row["resolved_url"] if row["resolve_status"] in ("RESOLVED", "SKIPPED", "BLOCKED") else None
    ckey = rules.canonical_key(url) if url else None
    host = rules.host_of(url) if url else _publisher_host(row["raw_payload"])
    ts = iso(now)

    if row["resolve_status"] == "BLOCKED" or (host and rules.host_in(host, blocked)):
        con.execute("""UPDATE items SET band='DROP', score=0, urgent=0, matched_terms=?,
                       target_tags='[]', canonical_key=?, rules_at=? WHERE id=?""",
                    (json.dumps(["✖blocked-host:" + host], ensure_ascii=False), ckey, ts, row["id"]))
        st["blocked"] += 1
        return

    sc = rules.score(kw, title, desc, body, has_ai=has_ai)
    u = rules.urgent(kw, title, desc, body)
    con.execute("""UPDATE items SET score=?, band=?, matched_terms=?, target_tags=?, urgent=?,
                   canonical_key=?, rules_at=? WHERE id=?""",
                (sc["score"], sc["band"], json.dumps(sc["terms"], ensure_ascii=False),
                 json.dumps(sc["tags"]), 1 if u else 0, ckey, ts, row["id"]))
    st["scored"] += 1
    st["band:" + sc["band"]] += 1
    if u:
        st["urgent"] += 1
    if not u and sc["band"] == "DROP":
        return

    event_id, joined = assign_event(con, row, title, body, ckey, u, sc)
    folded_into = None
    if u and cutoff and row["discovered_at"] >= cutoff:
        _status, folded_into = decide_urgent(con, kw, row, u, sc, event_id, ckey, now, st)
    if joined:
        refresh_sources(con, event_id, now)
    if folded_into:
        refresh_alert(con, folded_into, now)


# ---- events ---------------------------------------------------------------

_GRAMS = {}


def reset_caches():
    _GRAMS.clear()
    _migrated.clear()


def _grams(con, dbk, ids):
    missing = [i for i in ids if (dbk, i) not in _GRAMS]
    for k in range(0, len(missing), 300):
        chunk = missing[k:k + 300]
        q = (f"SELECT id, substr(extract_text, 1, {rules.BODY_CAP}) t FROM items "
             f"WHERE id IN ({','.join('?' * len(chunk))})")
        for r in con.execute(q, chunk):
            _GRAMS[(dbk, r[0])] = frozenset(rules.sim_grams(r[1] or ""))


def _similar_event(con, item_id, host, body):
    """Best open event with a cross-host member at body similarity >= 0.45."""
    dbk = _db_key(con)
    g = frozenset(rules.sim_grams(body[:rules.BODY_CAP]))
    cands = [c for c in con.execute(
        """SELECT i.id, i.event_id, i.extract_host FROM items i JOIN events e ON e.event_id = i.event_id
           WHERE e.status <> 'CLOSED' AND i.extract_status = 'OK' AND i.id <> ?""", (item_id,))
        if (c[2] or "") != host]
    _grams(con, dbk, [c[0] for c in cands])
    best, lg = None, len(g)
    for cid, eid, _h in cands:
        cg = _GRAMS.get((dbk, cid))
        if not cg or not lg or min(lg, len(cg)) < rules.DEDUP_THRESHOLD * max(lg, len(cg)):
            continue                              # Jaccard cannot reach 0.45
        s = rules.jaccard(g, cg)
        if s >= rules.DEDUP_THRESHOLD and (best is None or s > best[0]):
            best = (s, eid)
    if len(_GRAMS) > 50000:
        _GRAMS.clear()
    _GRAMS[(dbk, item_id)] = g
    return best[1] if best else None


def assign_event(con, row, title, body, ckey, u, sc):
    """Returns (event_id, joined_existing). Articles are never deleted, only associated."""
    ts = row["discovered_at"]
    target = u["target"] if u else _target_from_tags(sc["tags"])
    prio = 1 if u else None
    eid = None
    if ckey:                                      # the same article under another token
        r = con.execute("""SELECT i.event_id FROM items i JOIN events e ON e.event_id = i.event_id
                           WHERE i.canonical_key = ? AND i.id <> ? AND e.status <> 'CLOSED'
                           ORDER BY i.id DESC LIMIT 1""", (ckey, row["id"])).fetchone()
        eid = r[0] if r else None
    if eid is None and row["extract_status"] == "OK" and len(body) >= EVENT_MIN_BODY:
        eid = _similar_event(con, row["id"], row["extract_host"] or "", body)
    if eid is not None:
        con.execute("UPDATE items SET event_id=? WHERE id=?", (eid, row["id"]))
        con.execute("""UPDATE events SET members = members + 1, status = 'OPEN',
                         last_seen_at = CASE WHEN last_seen_at IS NULL OR last_seen_at < ? THEN ?
                                             ELSE last_seen_at END,
                         target = coalesce(target, ?),
                         priority = CASE WHEN ? IS NOT NULL AND (priority IS NULL OR ? < priority)
                                         THEN ? ELSE priority END
                       WHERE event_id = ?""", (ts, ts, target, prio, prio, prio, eid))
        return eid, True
    cur = con.execute("""INSERT INTO events (first_seen_at, last_seen_at, status, target, canonical_topic,
                                            rep_item_id, priority, members)
                         VALUES (?, ?, 'OPEN', ?, ?, ?, ?, 1)""",
                      (ts, ts, target, title[:300], row["id"], prio))
    con.execute("UPDATE items SET event_id=? WHERE id=?", (cur.lastrowid, row["id"]))
    return cur.lastrowid, False


def event_sources(con, event_id):
    rows = con.execute("""SELECT i.publisher, i.extract_host, i.resolve_status, i.resolved_url, i.link,
                                 s.name AS source_name
                          FROM items i LEFT JOIN sources s ON s.source_id = i.source_id
                          WHERE i.event_id = ? ORDER BY i.discovered_at, i.id""", (event_id,)).fetchall()
    return list(dict.fromkeys(outlet_name(r) for r in rows))


# ---- urgent decision ------------------------------------------------------

_PLACES = {}


def place_ids(kw, labels):
    """One identity per place. Spellings count as one place when their sheet group is named
    after the place (group 'velachery' holds velachery, வேளச்சேரி ...); a spelling whose
    group is not a place name stands alone -- which errs towards alerting twice."""
    m = _PLACES.get(kw.hash)
    if m is None:
        anchors = [t for t in kw.terms if t.tier in rules.URGENT_PLACE_TIERS and t.role == "anchor"]
        members = defaultdict(set)
        for t in anchors:
            members[rules.norm_match(t.group or "")].add(rules.norm_match(t.label))
        m = {}
        for t in anchors:
            g = rules.norm_match(t.group or "")
            m[t.label.lower()] = f"{t.tier}:{g if g and g in members[g] else rules.norm_match(t.label)}"
        _PLACES.clear()
        _PLACES[kw.hash] = m
    return sorted({m.get(x.lower(), rules.norm_match(x)) for x in labels})


def decide_urgent(con, kw, row, u, sc, event_id, ckey, now, st):
    """Returns (status, id of the alert this report was folded into, if any)."""
    ref = parse_ts(row["published_at"]) or parse_ts(row["discovered_at"])
    age_h = (now - ref).total_seconds() / 3600 if ref else 0.0
    detail = dict(u, places=place_ids(kw, u["anchors"]), score=sc["score"], band=sc["band"],
                  age_h=round(age_h, 2))
    ph = ",".join("?" * len(LIVE))
    status, reason, ref_id = "PENDING", None, None

    if age_h > URGENT_MAX_AGE_H:
        status, reason = "SUPPRESSED", f"stale: published {age_h:.1f}h before processing"
    if status == "PENDING" and ckey:
        d = con.execute(f"""SELECT q.id FROM alert_queue q JOIN items i ON i.id = q.item_id
                            WHERE q.kind = 'URGENT' AND q.status IN ({ph}) AND i.canonical_key = ?
                            LIMIT 1""", (*LIVE, ckey)).fetchone()
        if d:
            status, reason = "SUPPRESSED", f"same url as alert #{d[0]}"
    in_event = False
    if status == "PENDING" and event_id:
        prior = con.execute(f"""SELECT id, detail FROM alert_queue WHERE kind = 'URGENT'
                                AND event_id = ? AND status IN ({ph}) ORDER BY id""",
                            (event_id, *LIVE)).fetchall()
        if prior:
            in_event = True
            groups, targets = set(), set()
            for p in prior:
                d = _j(p[1], {})
                groups.update(d.get("groups") or [])
                targets.add(d.get("target"))
            new = sorted(set(u["groups"]) - groups)
            if u["target"] not in targets:
                new.append(u["target"].lower())
            if new:                               # break-out: casualty, evacuation, new district...
                detail["update"] = new
                reason = "new in this event: " + ", ".join(new)
            else:
                status, reason = "SUPPRESSED", f"same event as alert #{prior[0][0]}"
    if status == "PENDING" and not in_event:
        since = iso(now - timedelta(hours=URGENT_PLACE_WINDOW_H))
        near = []
        for p in con.execute(f"""SELECT id, detail FROM alert_queue WHERE kind = 'URGENT' AND status IN ({ph})
                                 AND created_at >= ? ORDER BY id""", (*LIVE, since)):
            d = _j(p[1], {})
            if d.get("target") == u["target"] and set(d.get("places") or []) & set(detail["places"]):
                near.append((p[0], d))
        if near:
            seen = set().union(*(set(d.get("groups") or []) for _, d in near))
            new = sorted(set(u["groups"]) - seen)
            ref_id = near[-1][0]
            if new:                               # same place, a new kind of emergency
                detail["update"] = new
                reason = f"new at this place since alert #{ref_id}: " + ", ".join(new)
            else:
                status = "SUPPRESSED"
                reason = f"same place and emergency as alert #{ref_id} (within {URGENT_PLACE_WINDOW_H}h)"

    body = None
    if status == "PENDING":
        body = render_urgent(con.execute(RENDER_SQL, (row["id"],)).fetchone(), detail,
                             event_sources(con, event_id) if event_id else None)
    cur = con.execute("""INSERT INTO alert_queue (item_id, event_id, kind, priority, body, status, attempts,
                                                 created_at, reason, detail, ref_id)
                         VALUES (?, ?, 'URGENT', 1, ?, ?, 0, ?, ?, ?, ?)""",
                      (row["id"], event_id, body, status, iso(now), reason,
                       json.dumps(detail, ensure_ascii=False), ref_id))
    st["queued" if status == "PENDING" else "suppressed"] += 1
    title = rules.cut(rules.clean_title(row["title"] or "", row["publisher"] or ""), 70)
    log.info(f"alerts: #{cur.lastrowid} {status:<10} {u['target']}/{'/'.join(u['groups'])} "
             f"{title}{'  (' + reason + ')' if reason else ''}")
    return status, (ref_id if status == "SUPPRESSED" else None)


def related_reports(con, alert_id):
    """Reports folded into an alert by the same-place window: outlet and headline, because
    unlike a body match they might be a separate incident at the same place."""
    rows = con.execute("""SELECT i.title, i.publisher, i.extract_host, i.resolve_status, i.resolved_url, i.link,
                                 s.name AS source_name
                          FROM alert_queue q JOIN items i ON i.id = q.item_id
                          LEFT JOIN sources s ON s.source_id = i.source_id
                          WHERE q.kind = 'URGENT' AND q.ref_id = ? AND q.status = 'SUPPRESSED'
                          ORDER BY q.id""", (alert_id,)).fetchall()
    return [(outlet_name(r), rules.clean_title(r["title"] or "", r["publisher"] or "")) for r in rows]


def refresh_sources(con, event_id, now):
    """Keep the event's alert showing how many outlets carry it."""
    a = con.execute("""SELECT id FROM alert_queue WHERE kind = 'URGENT' AND event_id = ?
                       AND status IN ('PENDING', 'SENDING', 'SENT') ORDER BY id DESC LIMIT 1""",
                    (event_id,)).fetchone()
    if a:
        refresh_alert(con, a[0], now)


def refresh_alert(con, alert_id, now):
    """Re-render an alert with its current sources and related reports: rewrite it while it
    is pending, or queue one silent edit once it is sent. One event, many sources -- not
    many messages."""
    a = con.execute("SELECT id, status, item_id, event_id, detail, body FROM alert_queue WHERE id = ?",
                    (alert_id,)).fetchone()
    if not a or a["status"] not in ("PENDING", "SENDING", "SENT"):
        return
    item = con.execute(RENDER_SQL, (a["item_id"],)).fetchone()
    body = render_urgent(item, _j(a["detail"], {}), event_sources(con, a["event_id"]) if a["event_id"] else None,
                         related_reports(con, a["id"]))
    if a["status"] == "PENDING":
        con.execute("UPDATE alert_queue SET body=? WHERE id=?", (body, a["id"]))
        return
    last = con.execute("""SELECT body FROM alert_queue WHERE kind = 'EDIT' AND ref_id = ? AND status = 'SENT'
                          ORDER BY id DESC LIMIT 1""", (a["id"],)).fetchone()
    if body == (last[0] if last else a["body"]):
        return
    e = con.execute("SELECT id FROM alert_queue WHERE kind = 'EDIT' AND ref_id = ? AND status = 'PENDING'",
                    (a["id"],)).fetchone()
    if e:
        con.execute("UPDATE alert_queue SET body=? WHERE id=?", (body, e[0]))
    else:
        con.execute("""INSERT INTO alert_queue (item_id, event_id, kind, priority, body, status, attempts,
                                               created_at, ref_id, reason)
                       VALUES (?, ?, 'EDIT', 2, ?, 'PENDING', 0, ?, ?, 'sources changed')""",
                    (a["item_id"], a["event_id"], body, iso(now), a["id"]))


# --------------------------------------------------------------------------
# rendering -- Telegram HTML parse mode
# --------------------------------------------------------------------------

def render_urgent(row, detail, sources=None, related=None, max_len=TG_LIMIT):
    target = detail.get("target") or "ALERT"
    groups = [g.upper() for g in detail.get("groups") or []]
    quiet = bool(set(detail.get("groups") or []) & set(QUIET_GROUPS))
    if detail.get("update"):
        head = f"🔁 <b>P1 — {esc(target)} · UPDATE: {esc(', '.join(x.upper() for x in detail['update']))}</b>"
    else:
        head = (f"{'◼️' if quiet else '🚨'} <b>P1 — {esc(target)}"
                f"{' · ' + esc('/'.join(groups)) if groups else ''}</b>")
    title = rules.clean_title(row["title"] or "", row["publisher"] or "") or "(no title)"
    outlet = outlet_name(row)
    url = display_url(row)
    matched = ", ".join(dict.fromkeys((detail.get("triggers") or []) + (detail.get("anchors") or [])))
    others = [s for s in (sources or []) if s != outlet]
    also = None
    if others:
        shown = ", ".join(others[:6]) + (f" +{len(others) - 6}" if len(others) > 6 else "")
        also = f"Also reported by: {esc(shown)}  ({len(others) + 1} sources)"
    excerpt = "" if quiet else (detail.get("excerpt") or "")
    rel = []
    if related:
        rel = [f"Related reports ({len(related)}):"]
        rel += [f"• {esc(o)}: {esc(rules.cut(t, 90))}" for o, t in related[:5]]
        if len(related) > 5:
            rel.append(f"• +{len(related) - 5} more")

    def build(t, ex):
        parts = [head, f"<b>{esc(t)}</b>", "", f"Source: {esc(outlet)} · {fmt_ist(row['published_at'] or row['discovered_at'])}"]
        if also:
            parts.append(also)
        parts += rel
        if matched:
            parts.append(f"Matched: {esc(matched)}")
        if ex:
            parts += ["", f"<i>{esc(ex)}</i>"]
        elif quiet:
            parts += ["", "<i>Excerpt withheld for suicide reports.</i>"]
        parts += ["", f'<a href="{esc_attr(url)}">Read original</a>']
        return "\n".join(parts)

    text = build(title, excerpt)
    n = len(excerpt)
    while rules.utf16_len(text) > max_len and n > 20:          # the excerpt goes first
        n = int(n * 0.8)
        text = build(title, rules.cut(excerpt, n))
    n = len(title)
    while rules.utf16_len(text) > max_len and n > 20:          # then the title; never the link
        n = int(n * 0.8)
        text = build(rules.cut(title, n), "")
    return text


def with_delay_note(body, created_at, now):
    c = parse_ts(created_at)
    if not c or (now - c).total_seconds() < DELAY_NOTE_MIN * 60:
        return body
    m = int((now - c).total_seconds() // 60)
    return f"⏱ <i>Delayed: queued {m // 60}h {m % 60:02d}m ago</i>\n" + body


# --------------------------------------------------------------------------
# 4. delivery
# --------------------------------------------------------------------------

class Result:
    __slots__ = ("ok", "message_id", "retry_after", "error", "kind")

    def __init__(self, ok, message_id=None, retry_after=None, error=None, kind=None):
        self.ok, self.message_id, self.retry_after, self.error, self.kind = ok, message_id, retry_after, error, kind

    def __repr__(self):
        return f"Result(ok={self.ok}, id={self.message_id}, kind={self.kind}, error={self.error!r})"


class Telegram:
    API = "https://api.telegram.org"

    def __init__(self, token, chat_id, client=None, timeout=20.0):
        self.token, self.chat_id, self.timeout = token.strip(), str(chat_id).strip(), timeout
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import httpx
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def redact(self, s):
        return (s or "").replace(self.token, "<token>") if self.token else (s or "")

    def _call(self, method, payload):
        try:
            r = self.client.post(f"{self.API}/bot{self.token}/{method}", json=payload, timeout=self.timeout)
        except Exception as ex:
            return Result(False, error=self.redact(f"{type(ex).__name__}: {ex}")[:300], kind="network")
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 200 and data.get("ok"):
            res = data.get("result")
            return Result(True, message_id=res.get("message_id") if isinstance(res, dict) else None)
        desc = self.redact(str(data.get("description") or r.text[:200]))
        low, code = desc.lower(), r.status_code
        if code == 429:
            retry = (data.get("parameters") or {}).get("retry_after") or 30
            return Result(False, retry_after=int(retry), error=desc, kind="rate")
        if "can't parse entities" in low or "can not parse entities" in low:
            return Result(False, error=desc, kind="parse")
        if "message is not modified" in low:
            return Result(False, error=desc, kind="not_modified")
        if "message to edit not found" in low or "message can't be edited" in low:
            return Result(False, error=desc, kind="gone")
        if code in (401, 403, 404) or "chat not found" in low:
            return Result(False, error=f"HTTP {code}: {desc}", kind="config")
        if code >= 500 or code == 0:
            return Result(False, error=f"HTTP {code}: {desc}", kind="server")
        return Result(False, error=f"HTTP {code}: {desc}", kind="bad")

    def send(self, text, plain=False):
        p = {"chat_id": self.chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        if not plain:
            p["parse_mode"] = "HTML"
        return self._call("sendMessage", p)

    def edit(self, message_id, text, plain=False):
        p = {"chat_id": self.chat_id, "message_id": message_id, "text": text,
             "link_preview_options": {"is_disabled": True}}
        if not plain:
            p["parse_mode"] = "HTML"
        return self._call("editMessageText", p)


class Recorder:
    """Stands in for Telegram in the backtest and the tests."""

    def __init__(self):
        self.sent, self.edits, self._id = [], [], 1000

    def send(self, text, plain=False):
        self._id += 1
        self.sent.append((self._id, text, plain))
        return Result(True, message_id=self._id)

    def edit(self, message_id, text, plain=False):
        self.edits.append((message_id, text, plain))
        return Result(True, message_id=message_id)


def make_transport(env, client=None):
    token, chat = (env.get("TELEGRAM_BOT_TOKEN") or "").strip(), (env.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat or (env.get("TELEGRAM_DRY_RUN") or "").strip().lower() in ("1", "true", "yes"):
        return None
    return Telegram(token, chat, client)


def _finish(con, rid, status, now, **cols):
    sets = ", ".join(f"{k}=?" for k in cols)
    con.execute(f"UPDATE alert_queue SET status=?, claimed_at=NULL{', ' + sets if sets else ''} WHERE id=?",
                (status, *cols.values(), rid))
    con.commit()


def deliver(con, transport, now_fn=utcnow, budget_s=DELIVER_BUDGET_S, sleep=time.sleep, pace_s=TG_PACE_S):
    st = Counter()
    now = now_fn()
    _begin(con)
    # a send interrupted by a crash goes back to the queue: a rare duplicate beats a lost alert
    con.execute("""UPDATE alert_queue SET status='PENDING', claimed_at=NULL
                   WHERE status='SENDING' AND claimed_at < ?""", (iso(now - timedelta(minutes=SENDING_STALE_MIN)),))
    st["expired"] += con.execute(
        """UPDATE alert_queue SET status='EXPIRED',
                  reason = coalesce(reason || '; ', '') || 'not delivered within ' || ? || 'h'
           WHERE status='PENDING' AND created_at < ?""",
        (ALERT_EXPIRE_H, iso(now - timedelta(hours=ALERT_EXPIRE_H)))).rowcount
    con.commit()
    hold = parse_ts(rt_get(con, "tg_hold_until"))
    if transport is not None and hold and now < hold:
        st["held"] += 1
        return st

    deadline, last = time.monotonic() + budget_s, 0.0
    while time.monotonic() < deadline:
        now = now_fn()
        row = con.execute("""SELECT * FROM alert_queue WHERE status='PENDING'
                             AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                             ORDER BY priority, CASE kind WHEN 'URGENT' THEN 0 WHEN 'EDIT' THEN 1 ELSE 2 END, id
                             LIMIT 1""", (iso(now),)).fetchone()
        if not row:
            break
        ref = None
        if row["kind"] == "EDIT":
            ref = con.execute("SELECT status, message_id FROM alert_queue WHERE id=?", (row["ref_id"],)).fetchone()
            rs = ref["status"] if ref else None
            if rs in ("PENDING", "SENDING"):
                _finish(con, row["id"], "PENDING", now, next_attempt_at=iso(now + timedelta(seconds=30)))
                continue
            if rs == "DRY_RUN" or (transport is None and rs == "SENT"):
                _finish(con, row["id"], "DRY_RUN", now, sent_at=iso(now))
                continue
            if rs != "SENT" or not ref["message_id"]:
                _finish(con, row["id"], "SUPPRESSED", now, reason="original alert was not delivered")
                continue

        _begin(con)
        claimed = con.execute("UPDATE alert_queue SET status='SENDING', claimed_at=? WHERE id=? AND status='PENDING'",
                              (iso(now), row["id"])).rowcount
        con.commit()
        if claimed != 1:
            continue
        if transport is None:
            _finish(con, row["id"], "DRY_RUN", now, sent_at=iso(now))
            if row["kind"] == "URGENT":
                log.info("alerts: DRY RUN (no Telegram token) -- would send:\n"
                         + rules.cut(html_to_plain(row["body"]), 600))
            st["dry_run"] += 1
            continue

        wait = pace_s - (time.monotonic() - last)
        if wait > 0 and last:
            sleep(wait)
        if row["kind"] == "EDIT":
            text = row["body"]
            res = transport.edit(ref["message_id"], text)
        else:
            text = with_delay_note(row["body"], row["created_at"], now)
            res = transport.send(text)
        note = None
        if res.kind == "parse":                   # never lose an alert to an escaping bug
            note = f"HTML rejected ({res.error}); delivered as plain text"
            log.error(f"alerts: #{row['id']} {note}")
            plain = html_to_plain(text)
            res = transport.edit(ref["message_id"], plain, plain=True) if row["kind"] == "EDIT" \
                else transport.send(plain, plain=True)
        last = time.monotonic()
        if _handle(con, row, res, now_fn(), note, st):
            break
    return st


def _handle(con, row, res, now, note, st):
    """Record one delivery outcome. True means stop delivering for this tick."""
    rid = row["id"]
    if res.ok or res.kind == "not_modified":
        _finish(con, rid, "SENT", now, sent_at=iso(now), message_id=res.message_id or row["message_id"],
                attempts=row["attempts"] + 1, error=note)
        st["sent" if row["kind"] == "URGENT" else "edited"] += 1
        return False
    if res.kind == "rate":
        until = now + timedelta(seconds=max(1, res.retry_after or 30))
        _finish(con, rid, "PENDING", now, next_attempt_at=iso(until), error=res.error)
        rt_set(con, "tg_hold_until", iso(until))
        con.commit()
        st["rate_limited"] += 1
        return True
    if res.kind == "network":
        _finish(con, rid, "PENDING", now, next_attempt_at=iso(now + timedelta(seconds=60)), error=res.error)
        st["network"] += 1
        return True
    if res.kind == "config":
        until = now + timedelta(minutes=15)
        _finish(con, rid, "PENDING", now, next_attempt_at=iso(until), error=res.error)
        rt_set(con, "tg_hold_until", iso(until))
        con.commit()
        log.error(f"alerts: Telegram refused the bot or chat ({res.error}). Check TELEGRAM_BOT_TOKEN, "
                  f"TELEGRAM_CHAT_ID, and that the bot is an admin of the channel. Retrying in 15 min.")
        st["config"] += 1
        return True
    attempts = row["attempts"] + 1
    if res.kind == "server" and attempts < TG_MAX_ATTEMPTS:
        until = now + timedelta(seconds=min(900, 30 * 2 ** attempts))
        _finish(con, rid, "PENDING", now, attempts=attempts, next_attempt_at=iso(until), error=res.error)
        st["retry"] += 1
        return True
    _finish(con, rid, "FAILED", now, attempts=attempts, error=res.error)
    log.error(f"alerts: #{rid} {row['kind']} failed permanently: {res.error}")
    st["failed"] += 1
    return False


# --------------------------------------------------------------------------
# the tick
# --------------------------------------------------------------------------

_last = {"tick": 0.0, "warned_dry": 0.0}


def tick(con, client=None, env=None, now=None, transport="auto", deliver_budget_s=DELIVER_BUDGET_S):
    env = env if env is not None else rules.load_env()
    now = now or utcnow()
    ensure_migrated(con)
    ensure_cutoff(con, now)
    kw = rules.get_keywords(con, client, env.get("KEYWORDS_CSV_URL"))
    if kw is None:                      # no sheet, no cache, no seed file: items wait unscored
        st = Counter(unscored=1)
    else:
        st = process_ready(con, kw, now, has_ai=bool(env.get("GEMINI_API_KEY")),
                           budget_s=TICK_PROCESS_BUDGET_S)
    tr = make_transport(env, client) if transport == "auto" else transport
    if tr is None and time.time() - _last["warned_dry"] > 3600 and st.get("queued"):
        log.warning("alerts: Telegram is not configured -- alerts are DRY_RUN and only logged. "
                    "Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env")
        _last["warned_dry"] = time.time()
    st.update(deliver(con, tr, budget_s=deliver_budget_s))
    rt_set(con, "last_tick_at", iso(now))
    con.commit()
    busy = {k: v for k, v in st.items() if v and k not in ("band:DROP",)}
    if any(k in busy for k in ("urgent", "queued", "suppressed", "sent", "edited", "failed", "error",
                               "dry_run", "rate_limited", "config", "network", "expired")):
        log.info("alerts: " + ", ".join(f"{k} {v}" for k, v in sorted(busy.items())))
    return st


def maybe_tick(con, client=None, env=None, force=False):
    """Safe to call between every collector step. Never raises."""
    if not force and time.monotonic() - _last["tick"] < TICK_MIN_INTERVAL_S:
        return None
    _last["tick"] = time.monotonic()
    try:
        return tick(con, client, env)
    except Exception:
        log.exception("alerts: tick failed; collection continues")
        try:
            if con.in_transaction:
                con.rollback()
        except sqlite3.Error:
            pass
        return None


# --------------------------------------------------------------------------
# status / health
# --------------------------------------------------------------------------

def _ago(s, now):
    dt = parse_ts(s)
    if not dt:
        return "never"
    m = (now - dt).total_seconds() / 60
    return f"{m / 60:.1f}h ago" if m >= 90 else f"{m:.0f} min ago"


def status_lines(con, env=None, now=None):
    now = now or utcnow()
    env = env if env is not None else rules.load_env()
    out = []
    if rt_get(con, "schema_phase1") is None:
        return ["ALERTS  not set up yet -- run 'python collect.py --init' (or start the collector)"]
    cutoff = rt_get(con, "alert_cutoff_at")
    kwo = rt_get(con, "keywords_origin") or ("sheet" if env.get("KEYWORDS_CSV_URL") else "seed file")
    tg = "LIVE" if make_transport(env) else "DRY RUN (no TELEGRAM_BOT_TOKEN/CHAT_ID, or TELEGRAM_DRY_RUN=1)"
    hold = parse_ts(rt_get(con, "tg_hold_until"))
    if hold and hold > now:
        tg += f", paused until {fmt_ist(iso(hold))}"
    out.append(f"ALERTS  telegram {tg}")
    out.append(f"  cutoff {fmt_ist(cutoff) if cutoff else 'not set'} · last tick {_ago(rt_get(con, 'last_tick_at'), now)}"
               f" · keywords from {kwo}")
    r = con.execute("""SELECT
        sum(CASE WHEN band IS NULL AND (extract_status <> 'PENDING' OR resolve_status IN ('FAILED','BLOCKED')) THEN 1 ELSE 0 END) ready,
        sum(CASE WHEN band IS NULL AND NOT (extract_status <> 'PENDING' OR resolve_status IN ('FAILED','BLOCKED')) THEN 1 ELSE 0 END) waiting,
        sum(CASE WHEN band = 'ERROR' THEN 1 ELSE 0 END) errors FROM items""").fetchone()
    out.append(f"  items: {r[0] or 0} ready for rules, {r[1] or 0} still resolving/extracting, {r[2] or 0} errors")
    day = iso(now - timedelta(hours=24))
    b = dict(con.execute("SELECT band, count(*) FROM items WHERE rules_at >= ? GROUP BY band", (day,)).fetchall())
    u = con.execute("SELECT count(*) FROM items WHERE rules_at >= ? AND urgent = 1", (day,)).fetchone()[0]
    out.append(f"  last 24h: {sum(b.values())} scored · AUTO_KEEP {b.get('AUTO_KEEP', 0)} · AI {b.get('AI', 0)}"
               f" · KEYWORD_KEEP {b.get('KEYWORD_KEEP', 0)} · urgent {u}")
    q = con.execute("SELECT kind, status, count(*) FROM alert_queue GROUP BY kind, status ORDER BY kind, status").fetchall()
    if q:
        by = defaultdict(list)
        for k, s, c in q:
            by[k].append(f"{s.lower()} {c}")
        out.append("  queue: " + " | ".join(f"{k} " + ", ".join(v) for k, v in by.items()))
    recent = con.execute("""SELECT q.id, q.status, q.created_at, q.reason, q.detail, q.error, i.title, i.publisher
                            FROM alert_queue q LEFT JOIN items i ON i.id = q.item_id
                            WHERE q.kind = 'URGENT' ORDER BY q.id DESC LIMIT 8""").fetchall()
    if recent:
        out.append("  recent urgent decisions:")
        for x in recent:
            d = _j(x["detail"], {})
            t = rules.cut(rules.clean_title(x["title"] or "", x["publisher"] or ""), 52)
            why = x["reason"] or x["error"] or ""
            out.append(f"    #{x['id']:<5} {fmt_ist(x['created_at']):<18} {x['status']:<10} "
                       f"{(d.get('target') or '')[:11]:<11} {t}{'  -- ' + rules.cut(why, 50) if why else ''}")
    ev = dict(con.execute("SELECT status, count(*) FROM events GROUP BY status").fetchall())
    multi = con.execute("SELECT count(*) FROM events WHERE status <> 'CLOSED' AND members > 1").fetchone()[0]
    out.append(f"  events: {ev.get('OPEN', 0)} open, {ev.get('QUIET', 0)} quiet ({multi} with 2+ reports), "
               f"{ev.get('CLOSED', 0)} closed")
    lat = con.execute("""SELECT q.sent_at, i.published_at, i.discovered_at FROM alert_queue q JOIN items i ON i.id = q.item_id
                         WHERE q.kind = 'URGENT' AND q.status = 'SENT' AND q.sent_at >= ?""",
                      (iso(now - timedelta(days=7)),)).fetchall()
    if lat:
        dd = [(parse_ts(s) - parse_ts(d)).total_seconds() / 60 for s, _p, d in lat if parse_ts(s) and parse_ts(d)]
        pp = [(parse_ts(s) - parse_ts(p)).total_seconds() / 60 for s, p, _d in lat if parse_ts(s) and parse_ts(p)]
        out.append(f"  latency over {len(lat)} sent alerts (7 days): collected -> sent median "
                   f"{statistics.median(dd):.0f} min; published -> sent median {statistics.median(pp):.0f} min")
    return out


# --------------------------------------------------------------------------
# backtest -- the live code, replayed over a copy of the corpus
# --------------------------------------------------------------------------

def _connect(path, readonly=False):
    if readonly:
        # as_uri() percent-encodes: Windows profile paths often contain spaces
        con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
    else:
        con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def _pct(values, q):
    v = sorted(values)
    if not v:
        return 0.0
    return v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))]


def _phase0_key(url):
    """analyse.canon_url, verbatim -- to show what the query-string fix changes."""
    from urllib.parse import urlparse
    if not url:
        return ""
    u = urlparse(url)
    host = (u.hostname or "").lower()
    for p in ("www.", "m.", "amp."):
        if host.startswith(p):
            host = host[len(p):]
    path = re.sub(r"/amp/?$|\.amp$|/$", "", u.path)
    return f"{host}{path}".lower()


def replay(src_path, dst_path, env, client=None, slot_min=BACKTEST_SLOT_MIN, progress=True):
    """Copy the corpus, clear Phase 1 state, and run the real tick code slot by slot with
    the clock set to each slot. Returns (con, kw, recorder)."""
    src = _connect(src_path, readonly=True)
    dst_path = Path(dst_path)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(dst_path) + suffix)
        try:
            p.unlink(missing_ok=True)
        except PermissionError:
            raise RuntimeError(f"{p.name} is open in another program (DB Browser?) -- close it and run again")
    dst = sqlite3.connect(dst_path)
    src.backup(dst)
    src.close()
    dst.close()

    con = _connect(dst_path)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("PRAGMA synchronous=OFF")
    reset_caches()
    migrate(con)
    con.execute("""UPDATE items SET band=NULL, score=NULL, matched_terms=NULL, target_tags=NULL, urgent=0,
                   event_id=NULL, rules_at=NULL""")
    con.execute("DELETE FROM events")
    con.execute("DELETE FROM alert_queue")
    rt_set(con, "alert_cutoff_at", "1970-01-01T00:00:00+00:00")
    rt_set(con, "tg_hold_until", None)
    con.commit()

    rules._memo.update(kw=None, checked=0.0)
    kw = rules.get_keywords(con, client, env.get("KEYWORDS_CSV_URL"), force=True)
    rec = Recorder()
    first, last = con.execute("SELECT min(discovered_at), max(discovered_at) FROM items").fetchone()
    t, end = parse_ts(first), parse_ts(last)
    if not t:
        return con, kw, rec
    step = timedelta(minutes=slot_min)
    t = t.replace(second=0) + step
    total = (end - t) / step + 1
    n = 0
    while t <= end + step:
        while True:
            st = process_ready(con, kw, t, has_ai=True)
            deliver(con, rec, now_fn=lambda t=t: t, budget_s=1e9, sleep=lambda s: None, pace_s=0)
            if sum(v for k, v in st.items() if k in ("scored", "blocked", "error")) < READY_BATCH:
                break
        n += 1
        if progress and n % 200 == 0:
            print(f"  replayed {n}/{int(total)} slots ({fmt_ist(iso(t))})", flush=True)
        t += step
    return con, kw, rec


def reason_kind(status, reason):
    r = (reason or "").lower()
    for key, label in (("stale", "stale"), ("same url", "same url"), ("same event", "same event"),
                       ("same place", "same place"), ("not delivered", "expired")):
        if key in r:
            return label
    return (status or "?").lower()


def backtest_report(con, kw, labels_path=LABELS):
    L = []
    p = L.append
    items = con.execute("SELECT count(*) FROM items").fetchone()[0]
    first, last = con.execute("SELECT min(discovered_at), max(discovered_at) FROM items").fetchone()
    days = max(1e-9, (parse_ts(last) - parse_ts(first)).total_seconds() / 86400) if first else 0
    p("TN INTEL -- URGENT PATH BACKTEST (nothing was sent; corpus.db was only read)")
    p(f"corpus: {items} items, {fmt_ist(first)} -> {fmt_ist(last)} ({days:.1f} days)")
    p(f"keywords: {kw.origin}, {len(kw.rows)} rows, hash {kw.hash}")
    p(f"rules: urgent max age {URGENT_MAX_AGE_H}h, body proximity {rules.URGENT_PROXIMITY} chars, event "
      f"similarity {rules.DEDUP_THRESHOLD}, quiet {EVENT_QUIET_H}h, close {EVENT_CLOSE_H}h, "
      f"max span {EVENT_MAX_SPAN_H}h")

    # 1 bands
    p("\n1. WHAT THE RULE ENGINE KEPT")
    bands = dict(con.execute("SELECT coalesce(band,'(unscored)'), count(*) FROM items GROUP BY 1").fetchall())
    for b in ("AUTO_KEEP", "AI", "KEYWORD_KEEP", "DROP", "ERROR", "(unscored)"):
        if bands.get(b):
            p(f"   {b:<12} {bands[b]:>6}  {100 * bands[b] / max(1, items):5.1f}%")
    ko = con.execute("SELECT count(*) FROM items WHERE band <> 'DROP' AND score >= ?",
                     (rules.KEYWORD_ONLY_KEEP,)).fetchone()[0]
    p(f"   with no AI key the keyword-only fallback would keep {ko} (score >= {rules.KEYWORD_ONLY_KEEP})")
    p(f"\n   {'source':<24}{'items':>7}{'kept':>7}{'urgent':>8}")
    for r in con.execute("""SELECT source_id, count(*) n, sum(CASE WHEN band IN ('AUTO_KEEP','AI','KEYWORD_KEEP') THEN 1 ELSE 0 END) k,
                                   sum(urgent) u FROM items GROUP BY source_id ORDER BY n DESC"""):
        p(f"   {r[0] or '?':<24}{r[1]:>7}{r[2] or 0:>7}{r[3] or 0:>8}")
    vetoes = Counter()
    for (mt,) in con.execute("SELECT matched_terms FROM items WHERE band='DROP' AND matched_terms LIKE '%✖%'"):
        for t in _j(mt, []):
            if t.startswith("✖"):
                vetoes[t[1:]] += 1
    if vetoes:
        p("   drops by veto: " + ", ".join(f"{k} {v}" for k, v in vetoes.most_common(12)))

    # 2 per day
    p("\n2. URGENT ALERTS PER DAY (IST)")
    rows = con.execute("""SELECT q.*, i.title, i.publisher, i.source_id, i.extract_host, i.published_at,
                                 i.discovered_at, i.resolve_status, i.resolved_url, i.link, s.name AS source_name
                          FROM alert_queue q JOIN items i ON i.id = q.item_id
                          LEFT JOIN sources s ON s.source_id = i.source_id
                          WHERE q.kind = 'URGENT' ORDER BY q.id""").fetchall()
    per = defaultdict(Counter)
    for r in rows:
        day = parse_ts(r["created_at"]).astimezone(IST).date()
        if r["status"] in ("SENT", "DRY_RUN"):
            per[day]["update" if _j(r["detail"], {}).get("update") else "new"] += 1
        else:
            per[day][reason_kind(r["status"], r["reason"])] += 1
    reasons = sorted({k for c in per.values() for k in c if k not in ("new", "update")})
    p(f"   {'day':<12}{'new':>5}{'update':>8}" + "".join(f"{k:>12}" for k in reasons)
      + ("   <- suppressed" if reasons else ""))
    over = 0
    for day in sorted(per):
        c = per[day]
        flag = "   <-- over 10" if c["new"] + c["update"] > 10 else ""
        over += bool(flag)
        p(f"   {day:%a %d %b}  {c['new']:>5}{c['update']:>8}" + "".join(f"{c[k]:>12}" for k in reasons) + flag)
    sent = [r for r in rows if r["status"] in ("SENT", "DRY_RUN")]
    p(f"   total {len(sent)} alerts over {days:.1f} days = {len(sent) / max(days, 1e-9):.1f}/day. "
      + ("Some days exceed ~10 -- section 4 shows which words did it." if over
         else "Within the ~10/day budget."))

    # 3 every alert
    p("\n3. EVERY ALERT THAT WOULD HAVE GONE OUT -- real emergency, or noise?")
    for r in sent:
        d = _j(r["detail"], {})
        t = rules.clean_title(r["title"] or "", r["publisher"] or "")
        tag = "UPDATE " + ",".join(d.get("update") or []) if d.get("update") else "/".join(d.get("groups") or [])
        p(f"   #{r['id']:<5} {fmt_ist(r['created_at']):<17} {d.get('target', ''):<11} {tag:<18} "
          f"[{outlet_name(r)[:22]}]")
        p(f"          {rules.cut(t, 100)}")
        p(f"          matched {', '.join((d.get('triggers') or []) + (d.get('anchors') or []))} · in {d.get('zone')}"
          f" · {d.get('age_h', 0):.1f}h old")
    if not sent:
        p("   none")

    # 4 trigger words
    p("\n4. WHICH SHEET TERMS FIRED (all urgent decisions, sent / suppressed)")
    trig, anch = defaultdict(Counter), defaultdict(Counter)
    for r in rows:
        d = _j(r["detail"], {})
        k = "sent" if r["status"] in ("SENT", "DRY_RUN") else "supp"
        for t in d.get("triggers") or []:
            trig[t][k] += 1
        for t in d.get("anchors") or []:
            anch[t][k] += 1
    for name, table in (("trigger", trig), ("place", anch)):
        top = sorted(table.items(), key=lambda kv: -(kv[1]["sent"] + kv[1]["supp"]))[:20]
        if top:
            p(f"   {name}s: " + "; ".join(f"{t} {c['sent']}/{c['supp']}" for t, c in top))

    # 5 suppressed samples
    p("\n5. SUPPRESSED -- samples by reason")
    by = defaultdict(list)
    for r in rows:
        if r["status"] not in ("SENT", "DRY_RUN"):
            by[reason_kind(r["status"], r["reason"])].append(r)
    for reason, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        p(f"   {reason} ({len(rs)})")
        for r in rs[:4]:
            p(f"      {rules.cut(rules.clean_title(r['title'] or '', r['publisher'] or ''), 80)}  -- {r['reason']}")
    if not by:
        p("   none")

    # 6 lag
    p("\n6. TIMESTAMPS -- minutes from published_at to collection (does the 6h window fit?)")
    lag = defaultdict(list)
    for sid, pub, disc, urg in con.execute("SELECT source_id, published_at, discovered_at, urgent FROM items"):
        a, b = parse_ts(pub), parse_ts(disc)
        if a and b:
            m = (b - a).total_seconds() / 60
            lag[sid].append(m)
            if urg:
                lag["(urgent items)"].append(m)
    p(f"   {'source':<24}{'n':>6}{'median':>8}{'p90':>8}{'>6h':>6}{'future':>8}")
    for sid in sorted(lag, key=lambda s: (s != "(urgent items)", s)):
        v = lag[sid]
        p(f"   {sid:<24}{len(v):>6}{statistics.median(v):>8.0f}{_pct(v, 0.9):>8.0f}"
          f"{sum(1 for x in v if x > 360):>6}{sum(1 for x in v if x < -15):>8}")
    hosts = defaultdict(list)
    for host, pub, disc in con.execute("SELECT extract_host, published_at, discovered_at FROM items"):
        a, b = parse_ts(pub), parse_ts(disc)
        if a and b:
            hosts[host or "?"].append((b - a).total_seconds() / 60)
    odd = sorted(((h, v) for h, v in hosts.items() if sum(1 for x in v if x < -15) >= max(2, len(v) // 10)),
                 key=lambda hv: -len(hv[1]))
    shifted = sorted(((h, v) for h, v in hosts.items() if len(v) >= 10 and 270 <= statistics.median(v) <= 390),
                     key=lambda hv: -len(hv[1]))
    p("   Clock or timezone faults show up per outlet, not per query:")
    p("   - published AFTER it was collected (IST digits labelled UTC): harmless for the 6h rule")
    for h, v in odd[:10]:
        p(f"      {h:<30} {sum(1 for x in v if x < -15)}/{len(v)} future-dated, median {statistics.median(v):.0f} min")
    if not odd:
        p("      none")
    p("   - typically ~5.5h old on arrival (UTC digits labelled IST): its urgent items get ~30 min before")
    p("     the 6h rule calls them stale -- check these by opening a few articles")
    for h, v in shifted[:10]:
        p(f"      {h:<30} median {statistics.median(v):.0f} min over {len(v)} items")
    if not shifted:
        p("      none")

    # 7 canonical key
    p("\n7. CANONICAL KEY -- Phase 0 key vs the query-string fix")
    urls = [r[0] for r in con.execute("""SELECT resolved_url FROM items WHERE resolved_url IS NOT NULL
                                          AND resolve_status IN ('RESOLVED','SKIPPED')""")]
    old, new = Counter(_phase0_key(u) for u in urls), Counter(rules.canonical_key(u) for u in urls)
    p(f"   {len(urls)} urls -> Phase 0 key {len(old)} distinct ({len(urls) - len(old)} merged), "
      f"new key {len(new)} distinct ({len(urls) - len(new)} merged)")
    wrong = defaultdict(set)
    for u in urls:
        wrong[_phase0_key(u)].add(rules.canonical_key(u))
    bad = {k: v for k, v in wrong.items() if len(v) > 1}
    if bad:
        p(f"   the Phase 0 key would have merged {sum(len(v) for v in bad.values())} different articles "
          f"into {len(bad)} key{'s' if len(bad) != 1 else ''}, e.g.:")
        for k, v in sorted(bad.items(), key=lambda kv: -len(kv[1]))[:4]:
            p(f"      {k[:60]}  <- {len(v)} distinct articles")

    # 8 events
    p("\n8. EVENTS")
    ne = con.execute("SELECT count(*) FROM events").fetchone()[0]
    multi = con.execute("""SELECT e.event_id, e.members, e.canonical_topic, e.first_seen_at, e.target,
                                  count(DISTINCT i.extract_host) hosts
                           FROM events e JOIN items i ON i.event_id = e.event_id
                           GROUP BY e.event_id HAVING e.members > 1 ORDER BY hosts DESC, e.members DESC""").fetchall()
    p(f"   {ne} events; {len(multi)} have 2+ reports ({sum(m['members'] - 1 for m in multi)} reports folded in)")
    for m in multi[:8]:
        p(f"      {m['members']} reports / {m['hosts']} outlets  {fmt_ist(m['first_seen_at']):<17} "
          f"{(m['target'] or ''):<11} {rules.cut(m['canonical_topic'] or '', 60)}")

    # 9 labels
    p("\n9. AGAINST label_me.csv (Gemini's reading of headlines -- a rough check, not ground truth)")
    L.extend(label_lines(con, labels_path))
    return L


def label_lines(con, path):
    out = []
    p = out.append
    if not Path(path).exists():
        return ["   label_me.csv not found next to the script -- skipped"]
    try:
        rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    except Exception as ex:
        return [f"   could not read label_me.csv: {type(ex).__name__}"]
    lab = {}
    for r in rows:
        v = (r.get("RELEVANT_y_n") or "").strip().lower()
        if v in ("y", "n") and (r.get("id") or "").strip().isdigit():
            lab[int(r["id"])] = (v == "y", (r.get("TARGET_tvk_velachery_none") or "").strip().lower() or "none",
                                 (r.get("AI_PRIORITY") or "").strip())
    if not lab:
        return ["   no labelled rows (RELEVANT_y_n empty) -- skipped"]
    ids = list(lab)
    got = {}
    for k in range(0, len(ids), 500):
        chunk = ids[k:k + 500]
        for r in con.execute(f"""SELECT id, band, score, target_tags, urgent, title, publisher FROM items
                                 WHERE id IN ({','.join('?' * len(chunk))})""", chunk):
            got[r["id"]] = r
    both = [i for i in ids if i in got]
    p(f"   {len(lab)} labelled rows, {len(both)} found in this corpus")
    if not both:
        return out
    gate = {i for i in both if got[i]["band"] in ("AUTO_KEEP", "AI", "KEYWORD_KEEP")}
    konly = {i for i in both if got[i]["band"] != "DROP" and (got[i]["score"] or 0) >= rules.KEYWORD_ONLY_KEEP}
    rel = {i for i in both if lab[i][0]}

    def pr(sel):
        tp = len(sel & rel)
        return (f"recall {100 * tp / len(rel):.0f}% ({tp}/{len(rel)}), "
                f"precision {100 * tp / len(sel):.0f}% ({tp}/{len(sel)})") if rel and sel else "n/a"

    p(f"   AI gate (score >= {rules.AI_MIN}, no veto):  {pr(gate)}")
    p(f"   keyword-only fallback (score >= {rules.KEYWORD_ONLY_KEEP}): {pr(konly)}")
    tagmap = {"constituency": "constituency", "district": "district", "portfolio": "portfolio",
              "mention": "mention", "political": "party"}
    p(f"\n   {'label category':<15}{'n':>5}{'passes gate':>13}{'tagged':>9}")
    cats = Counter(lab[i][1] for i in both)
    for c, n in cats.most_common():
        ids_c = [i for i in both if lab[i][1] == c]
        g = sum(1 for i in ids_c if i in gate)
        tg = tagmap.get(c)
        tagged = sum(1 for i in ids_c if tg and tg in _j(got[i]["target_tags"], [])) if tg else None
        p(f"   {c:<15}{n:>5}{100 * g / n:>12.0f}%{'' if tagged is None else f'{100 * tagged / n:>8.0f}%':>9}")
    p(f"\n   {'rule tag':<15}{'items':>6}{'labelled relevant':>19}{'same category':>15}")
    for c, tg in tagmap.items():
        ids_t = [i for i in both if tg in _j(got[i]["target_tags"], [])]
        if ids_t:
            p(f"   {tg:<15}{len(ids_t):>6}{100 * sum(1 for i in ids_t if lab[i][0]) / len(ids_t):>18.0f}%"
              f"{100 * sum(1 for i in ids_t if lab[i][1] == c) / len(ids_t):>14.0f}%")
    urg = [i for i in both if got[i]["urgent"]]
    p1 = [i for i in both if lab[i][2] == "1" and lab[i][1] in ("constituency", "district")]
    p(f"\n   urgent-flagged: {len(urg)}; Gemini priority 1 in constituency/district: {len(p1)}; overlap "
      f"{len(set(urg) & set(p1))}")
    for title, sel in (("urgent but labelled not relevant (possible false alarms):",
                        [i for i in urg if not lab[i][0]]),
                       ("priority-1 constituency/district items the urgent path did NOT flag:",
                        [i for i in p1 if not got[i]["urgent"]]),
                       ("labelled relevant but dropped (terms the sheet may be missing):",
                        [i for i in rel if got[i]["band"] == "DROP"])):
        if sel:
            p(f"   {title}")
            for i in sel[:12]:
                r = got[i]
                p(f"      #{i:<6} {lab[i][1]:<13} score {r['score']:<3} "
                  f"{rules.cut(rules.clean_title(r['title'] or '', r['publisher'] or ''), 70)}")
            if len(sel) > 12:
                p(f"      ... and {len(sel) - 12} more")
    return out


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def _console_utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            if (s.encoding or "").lower().replace("-", "") != "utf8" and not s.isatty():
                s.reconfigure(encoding="utf-8", errors="replace")
            else:
                s.reconfigure(errors="replace")
        except Exception:
            pass


def _setup_logging(logfile):
    root = logging.getLogger("collect")
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s"))
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    root.addHandler(fh)
    root.addHandler(sh)


def cmd_backtest(db=DB, out=REPORT, work=BACKTEST_DB, labels=LABELS, env=None):
    env = env if env is not None else rules.load_env()
    if not Path(db).exists():
        print(f"{db} not found")
        return None
    client = None
    if env.get("KEYWORDS_CSV_URL"):
        import httpx
        client = httpx.Client(timeout=20.0, follow_redirects=True)
    t0 = time.time()
    print(f"replaying {Path(db).name} into {Path(work).name} (the original is only read) ...", flush=True)
    try:
        con, kw, _rec = replay(db, work, env, client)
    except RuntimeError as ex:
        print(ex)
        return None
    if not con.execute("SELECT count(*) FROM items").fetchone()[0]:
        print(f"{Path(db).name} has no items yet -- let the collector run first")
        con.close()
        return None
    lines = backtest_report(con, kw, labels)
    lines.append(f"\n(replay took {time.time() - t0:.0f}s; the replayed database is kept as {Path(work).name} "
                 f"for follow-up questions)")
    Path(out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nwritten to {out} -- upload or paste that file")
    con.close()
    return lines


TEST_TITLE = "வேளச்சேரியில் மழை (கனமழை) - 3.5 செ.மீ. & <உடனடி> \"எச்சரிக்கை\""
TEST_EXCERPT = ("சென்னை: வேளச்சேரி, திருவான்மியூர் பகுதிகளில் நேற்றிரவு 3.5 செ.மீ. மழை பதிவானது. "
                "விஜயநகர் 2-வது தெரு (முதல் பிளாக்) & தாழ்வான பகுதிகளில் <50 செ.மீ.> நீர் தேங்கியது; "
                "மாநகராட்சி A&B குழுக்கள் 'உடனடி' நடவடிக்கை.")


def cmd_test_telegram(env=None):
    env = env if env is not None else rules.load_env()
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be in .env:\n"
              "  1. In Telegram, message @BotFather, send /newbot, copy the token\n"
              "  2. Create the private channel, add the bot as an administrator\n"
              "  3. Post anything in the channel, then open\n"
              "     https://api.telegram.org/bot<token>/getUpdates  and copy chat.id (starts -100)")
        return False
    row = {"id": 0, "link": "https://news.google.com/rss/articles/TEST?oc=5&hl=ta", "title": TEST_TITLE + " - Dinamalar",
           "publisher": "Dinamalar", "published_at": iso(utcnow()), "discovered_at": iso(utcnow()),
           "resolve_status": "RESOLVED", "resolved_url": "https://www.dinamalar.com/news/test?id=1&amp=0&x=<y>",
           "extract_host": "dinamalar.com", "source_name": "Google News TA"}

    detail = {"target": "VELACHERY", "groups": ["flood"], "triggers": ["தேங்கி", "waterlog"],
              "anchors": ["வேளச்சேரி", "திருவான்மியூர்"], "excerpt": TEST_EXCERPT}
    text = "🧪 <b>TEST</b> — not a real alert\n" + render_urgent(row, detail, ["Dinamalar", "The Hindu", "தினத்தந்தி"])
    tg = Telegram(token, chat)
    print("sending HTML test message ...")
    res = tg.send(text)
    print(f"  {res}")
    if res.kind == "parse":
        print("  Telegram rejected the HTML -- this is a bug in the escaping; paste this output")
        print(f"  plain-text fallback: {tg.send(html_to_plain(text), plain=True)}")
        return False
    if not res.ok:
        print({"config": "  check the token, the chat id, and that the bot is an admin of the channel",
               "network": "  no connection to api.telegram.org",
               "rate": "  rate limited -- wait a minute"}.get(res.kind, "  unexpected -- paste this output"))
        return False
    time.sleep(1.2)
    ed = tg.edit(res.message_id, text.replace("not a real alert", "not a real alert (edit works ✓)"))
    print(f"  edit: {ed}")
    print("OK -- check the channel: Tamil intact, & < > \" ( ) . - all visible, link opens, no preview")
    return res.ok


def cmd_run(once=False):
    import httpx
    con = _connect(DB)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    client = httpx.Client(timeout=25.0, follow_redirects=True)
    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    log.info("alerts: running standalone (Ctrl+C to stop)")
    try:
        while not stop["now"]:
            maybe_tick(con, client, force=True)
            if once:
                break
            for _ in range(60):
                if stop["now"]:
                    break
                time.sleep(1)
    finally:
        client.close()
        con.close()


def main():
    _console_utf8()
    ap = argparse.ArgumentParser(description="Phase 1 alerting")
    ap.add_argument("--backtest", action="store_true", help="replay the corpus through the urgent path; sends nothing")
    ap.add_argument("--db", default=str(DB), help="corpus to replay (default corpus.db)")
    ap.add_argument("--test-telegram", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()

    if a.backtest:
        logging.getLogger("collect").addHandler(logging.NullHandler())
        return cmd_backtest(Path(a.db))
    if a.test_telegram:
        return cmd_test_telegram()
    if not DB.exists():
        print("no corpus.db -- run 'python collect.py --init' first")
        return None
    if a.status:
        con = _connect(DB, readonly=True)
        print("\n".join(status_lines(con)))
        return None
    _setup_logging(HERE / "alerts.log")
    if a.once or a.run:
        return cmd_run(once=a.once)
    ap.print_help()
    return None


if __name__ == "__main__":
    main()
