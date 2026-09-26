#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Corpus collector -- Phase 0 acquisition with the Phase 1 hooks.

Runs the full acquisition path -- poll, resolve, fetch, extract -- and stores
everything including every failure. Between items it calls alerts.maybe_tick(),
which scores, groups and alerts (see alerts.py). Hosts in sources.json
"blocked_hosts" are skipped before their Google token is decoded. If the Phase 1
modules fail to import, collection carries on exactly as in Phase 0.

    python collect.py --init     create/upgrade the db and load sources.json
    python collect.py --once     one cycle, then exit
    python collect.py --once --minutes 18   one cycle sized for a scheduled run
    python collect.py            run until Ctrl+C
    python collect.py --stats    what the corpus looks like so far
    python collect.py --prune 21 forget items older than 21 days, shrink the db

Safe to kill and restart at any point. All state lives in the database.

On GitHub Actions (a fresh machine every run, wiped afterwards) the workflow
restores the database, runs --init, --once --minutes N and --prune, then saves
the database again. Settings read from the environment there:
    YOUTUBE_API_KEY    poll channels through the YouTube Data API (the RSS
                       feed endpoint is unreliable from cloud servers)
    KEEP_FAILED_HTML=0 do not store the html of failed extractions
    NODE_NAME          a label for this machine in the runtime table
"""

import os
import re
import sys
import json
import time
import signal
import sqlite3
import hashlib
import logging
import argparse
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse

import httpx
import feedparser
import trafilatura

# Phase 1. Collection must survive broken alerting code, so the import is guarded
# and every call into it is too.
try:
    import rules
    import alerts
    PHASE1_ERROR = None
except Exception as _ex:
    rules = alerts = None
    PHASE1_ERROR = f"{type(_ex).__name__}: {_ex}"

HERE = Path(__file__).resolve().parent
DB = HERE / "corpus.db"
SOURCES_JSON = HERE / "sources.json"
LOGFILE = HERE / "collect.log"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HTTP_TIMEOUT = 25.0

# Resolver pacing. Starts slow and speeds up while clean, backs off hard on 429.
# The point of the ramp is to find Google's threshold, so do not pin these.
RESOLVE_INTERVAL_START = 5.0
RESOLVE_INTERVAL_FLOOR = 1.0
RESOLVE_INTERVAL_CEIL = 120.0
RESOLVE_SPEEDUP_AFTER = 15      # consecutive successes before shaving the interval
RESOLVE_SPEEDUP_STEP = 0.5
RESOLVE_MAX_ATTEMPTS = 4

# Per cycle, seconds of wall clock to spend on each queue before polling again.
RESOLVE_BUDGET_S = 240
EXTRACT_BUDGET_S = 420          # extraction is the slower queue, give it more room
FETCH_DELAY_S = 2.0             # politeness between publisher fetches
EXTRACT_MAX_ATTEMPTS = 3
THIN_TEXT_CHARS = 400

# Hosts we never try to extract -- login walls, not worth the attempts.
SOCIAL_HOSTS = ("facebook.com", "instagram.com", "twitter.com", "x.com",
                "threads.net", "linkedin.com", "whatsapp.com", "t.me")

# Watch pages have no article body. The feed already gave us title + description
# (1500-2800 chars on your channels), so fetching these buys nothing.
VIDEO_HOSTS = ("youtube.com", "youtu.be", "dailymotion.com", "vimeo.com")

DEFAULT_BLOCKED_HOSTS = ("theprint.in", "fuelcarmagazine.com", "tamil.getlokalapp.com")

# YouTube Data API: 1 quota unit per channel poll against 10,000 free units a day.
YT_API = "https://www.googleapis.com/youtube/v3/playlistItems"
YT_API_MAX = 15                 # same window the RSS feed gave

STOP = False


def env():
    """Keys from .env (lenient parsing, see rules.load_env) or the environment."""
    out = {}
    if rules is not None:
        try:
            out = rules.load_env()
        except Exception:
            out = {}
    for k in ("YOUTUBE_API_KEY", "KEEP_FAILED_HTML", "NODE_NAME"):
        if k not in out and os.environ.get(k):
            out[k] = os.environ[k]
    return out


def keep_failed_html():
    return (env().get("KEEP_FAILED_HTML") or "1").strip().lower() not in ("0", "false", "no")


def _sigint(_s, _f):
    global STOP
    STOP = True
    log.info("stop requested, finishing current item")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def host_in(host, suffixes):
    """Dot-boundary suffix match. The old substring test treated newsx.com as x.com."""
    host = (host or "").lower().rstrip(".")
    return any(host == s or host.endswith("." + s) for s in suffixes)


def blocked_hosts():
    if rules is not None:
        try:
            return rules.blocked_hosts()
        except Exception:
            pass
    return DEFAULT_BLOCKED_HOSTS


def publisher_host(raw_payload):
    """Google News names the outlet (<source url>) before its token is decoded."""
    try:
        href = (json.loads(raw_payload or "{}").get("source") or {}).get("href") or ""
    except (ValueError, AttributeError):
        return ""
    h = (urlparse(href).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


_PHASE1 = {"canon": None}


def has_canon(con):
    if _PHASE1["canon"] is None:
        _PHASE1["canon"] = "canonical_key" in {r[1] for r in con.execute("PRAGMA table_info(items)")}
    return _PHASE1["canon"]


def set_canon(con, item_id, url):
    if rules is not None and has_canon(con):
        con.execute("UPDATE items SET canonical_key=? WHERE id=?", (rules.canonical_key(url), item_id))


def phase1_setup(con):
    if alerts is None:
        log.error(f"PHASE 1 NOT LOADED ({PHASE1_ERROR}) -- collecting only, no alerts")
        return False
    try:
        alerts.migrate(con)
        _PHASE1["canon"] = None
        return True
    except Exception as ex:
        log.error(f"phase 1 schema upgrade failed ({type(ex).__name__}: {ex}) -- collecting only")
        return False


def phase1_tick(con, client, force=False):
    if alerts is None:
        return
    try:
        alerts.maybe_tick(con, client, force=force)
    except Exception as ex:                      # maybe_tick already catches; belt and braces
        log.error(f"alerts tick crashed: {type(ex).__name__}: {ex}")


# --------------------------------------------------------------------------
# logging -- encoding must be explicit or Tamil kills the file handler
# --------------------------------------------------------------------------

log = logging.getLogger("collect")
log.setLevel(logging.INFO)
_fh = logging.FileHandler(LOGFILE, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s"))
log.addHandler(_fh)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
log.addHandler(_sh)


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=10000;

CREATE TABLE IF NOT EXISTS sources (
    source_id   TEXT PRIMARY KEY,
    name        TEXT,
    kind        TEXT,               -- google_news | youtube | rss | sitemap
    tier        TEXT,
    language    TEXT,
    feed_url    TEXT,
    requires_residential_ip INTEGER DEFAULT 1,
    poll_interval_s INTEGER DEFAULT 300,
    enabled     INTEGER DEFAULT 1,
    etag        TEXT,
    last_modified TEXT,
    last_polled_at TEXT,
    last_changed_at TEXT,
    failure_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY,
    item_key    TEXT UNIQUE,        -- google token / youtube video id / url hash
    source_id   TEXT,
    feed_url    TEXT,
    link        TEXT,               -- as it appeared in the feed
    title       TEXT,
    description TEXT,
    publisher   TEXT,               -- feed's <source> title, when present
    published_raw TEXT,             -- verbatim string from the feed
    published_at  TEXT,             -- parsed, UTC iso, NULL if unparseable
    discovered_at TEXT,
    language    TEXT,
    raw_payload TEXT,

    resolve_status TEXT DEFAULT 'PENDING',   -- PENDING RESOLVED FAILED SKIPPED
    resolved_url   TEXT,
    resolve_attempts INTEGER DEFAULT 0,
    resolve_ms     INTEGER,

    extract_status TEXT DEFAULT 'PENDING',   -- PENDING OK THIN FAILED SKIPPED_SOCIAL
    extract_host   TEXT,
    extract_http   INTEGER,
    extract_chars  INTEGER,
    extract_text   TEXT,
    extract_attempts INTEGER DEFAULT 0,
    extract_ms     INTEGER
);

CREATE INDEX IF NOT EXISTS ix_items_resolve ON items(resolve_status, resolve_attempts);
CREATE INDEX IF NOT EXISTS ix_items_extract ON items(extract_status, extract_attempts);
CREATE INDEX IF NOT EXISTS ix_items_disc    ON items(discovered_at);

-- every attempt, success or failure, kept for forensics
CREATE TABLE IF NOT EXISTS attempts (
    id        INTEGER PRIMARY KEY,
    item_id   INTEGER,
    kind      TEXT,                 -- resolve | fetch
    ts        TEXT,
    ok        INTEGER,
    http_status INTEGER,
    error_type  TEXT,
    error_text  TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS ix_attempts_item ON attempts(item_id, kind);

-- raw html kept ONLY for failed extractions
CREATE TABLE IF NOT EXISTS failed_html (
    item_id INTEGER PRIMARY KEY,
    ts      TEXT,
    html    TEXT
);

CREATE TABLE IF NOT EXISTS poll_runs (
    id         INTEGER PRIMARY KEY,
    ts         TEXT,
    source_id  TEXT,
    http_status INTEGER,
    not_modified INTEGER,
    entries    INTEGER,
    new_items  INTEGER,
    error      TEXT
);

-- the resolver rate experiment: every pacing change is recorded
CREATE TABLE IF NOT EXISTS rate_events (
    id       INTEGER PRIMARY KEY,
    ts       TEXT,
    event    TEXT,                  -- SPEEDUP | BACKOFF_429 | BACKOFF_ERR
    old_interval REAL,
    new_interval REAL,
    consecutive_ok INTEGER,
    note     TEXT
);

CREATE TABLE IF NOT EXISTS runtime (k TEXT PRIMARY KEY, v TEXT);
"""


def connect():
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def rt_get(con, k, default=None):
    r = con.execute("SELECT v FROM runtime WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def rt_set(con, k, v):
    con.execute("INSERT INTO runtime (k,v) VALUES (?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------

def feed_url_for(s):
    if s["kind"] == "google_news":
        q = quote(s["query"])
        if s.get("language") == "ta":
            return f"https://news.google.com/rss/search?q={q}&hl=ta&gl=IN&ceid=IN%3Ata"
        return f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN%3Aen"
    if s["kind"] == "youtube":
        cid = (s.get("channel_id") or "").strip()
        if not cid:
            return ""
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
    return s.get("url", "")


def cmd_init():
    con = connect()
    con.executescript(SCHEMA)

    if not SOURCES_JSON.exists():
        log.error("sources.json not found next to this script")
        return
    cfg = json.loads(SOURCES_JSON.read_text(encoding="utf-8"))

    n_on = 0
    for s in cfg["sources"]:
        url = feed_url_for(s)
        enabled = int(bool(s.get("enabled", True)) and bool(url))
        n_on += enabled
        con.execute("""
            INSERT INTO sources (source_id,name,kind,tier,language,feed_url,
                                 requires_residential_ip,poll_interval_s,enabled)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET
                name=excluded.name, kind=excluded.kind, tier=excluded.tier,
                language=excluded.language, feed_url=excluded.feed_url,
                requires_residential_ip=excluded.requires_residential_ip,
                poll_interval_s=excluded.poll_interval_s, enabled=excluded.enabled
        """, (s["source_id"], s["name"], s["kind"], s["tier"], s.get("language"),
              url, int(s.get("requires_residential_ip", True)),
              int(s.get("poll_interval_s", 300)), enabled))

    # A source deleted from sources.json used to keep polling forever: the upsert
    # never switched it off. Its items stay; only the polling stops.
    ids = [s["source_id"] for s in cfg["sources"]]
    gone = con.execute(f"""UPDATE sources SET enabled=0
                           WHERE enabled=1 AND source_id NOT IN ({','.join('?' * len(ids))})""", ids).rowcount
    if gone:
        log.info(f"{gone} source(s) no longer in sources.json switched off")

    if rt_get(con, "resolve_interval") is None:
        rt_set(con, "resolve_interval", RESOLVE_INTERVAL_START)
        rt_set(con, "consecutive_ok", 0)
    rt_set(con, "node", env().get("NODE_NAME") or "local")

    con.commit()
    log.info(f"schema ready, {len(cfg['sources'])} sources loaded, {n_on} enabled")
    if phase1_setup(con):
        cutoff = alerts.ensure_cutoff(con)
        log.info(f"phase 1 ready: alerting starts from {alerts.fmt_ist(cutoff)}; nothing collected "
                 f"earlier is ever alerted")
    for r in con.execute("SELECT source_id,enabled,poll_interval_s FROM sources ORDER BY enabled DESC, source_id"):
        log.info(f"   {'on ' if r['enabled'] else 'OFF'} {r['source_id']:<22} every {r['poll_interval_s']}s")
    con.close()


# --------------------------------------------------------------------------
# polling
# --------------------------------------------------------------------------

def make_item_key(link):
    if "news.google.com/rss/articles/" in link:
        return "gn:" + link.split("/articles/", 1)[1].split("?")[0][:120]
    m = re.search(r"(?:v=|youtu\.be/|video:)([\w-]{11})", link)
    if m:
        return "yt:" + m.group(1)
    return "u:" + hashlib.sha256(link.encode("utf-8")).hexdigest()[:32]


def parse_published(entry):
    raw = entry.get("published") or entry.get("updated") or ""
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    iso = None
    if st:
        try:
            iso = datetime(*st[:6], tzinfo=timezone.utc).isoformat(timespec="seconds")
        except Exception:
            iso = None
    return raw, iso


def _poll_failed(con, src, ts, ex):
    con.execute("INSERT INTO poll_runs (ts,source_id,error) VALUES (?,?,?)",
                (ts, src["source_id"], f"{type(ex).__name__}: {ex}"))
    # A connection-level error usually means the network is not up yet (waking
    # from sleep, router rebooting). Do NOT advance last_polled_at for those --
    # otherwise one failed poll on wake costs a full poll_interval of blindness.
    transient = isinstance(ex, (httpx.ConnectError, httpx.ConnectTimeout,
                                httpx.ReadTimeout, httpx.RemoteProtocolError))
    if transient:
        con.execute("UPDATE sources SET failure_count=failure_count+1 "
                    "WHERE source_id=?", (src["source_id"],))
    else:
        con.execute("UPDATE sources SET failure_count=failure_count+1, "
                    "last_polled_at=? WHERE source_id=?", (ts, src["source_id"]))
    con.commit()
    log.warning(f"  {src['source_id']}: {type(ex).__name__}: {str(ex)[:80]}"
                f"{' (will retry next cycle)' if transient else ''}")


def store_entries(con, src, entries, ts, http_status, headers=None):
    """entries: dicts with link, title, desc, publisher, raw_pub, iso_pub, payload."""
    new = 0
    for e in entries:
        link = e.get("link", "")
        if not link:
            continue
        try:
            con.execute("""
                INSERT INTO items (item_key,source_id,feed_url,link,title,description,
                                   publisher,published_raw,published_at,discovered_at,
                                   language,raw_payload)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (make_item_key(link), src["source_id"], src["feed_url"], link,
                  e.get("title", ""), e.get("desc", ""), e.get("publisher", ""),
                  e.get("raw_pub", ""), e.get("iso_pub"), ts, src["language"],
                  e.get("payload", "")[:20000]))
            new += 1
        except sqlite3.IntegrityError:
            pass  # already have it, from this feed or another query
    headers = headers or {}
    con.execute("INSERT INTO poll_runs (ts,source_id,http_status,not_modified,entries,new_items) "
                "VALUES (?,?,?,0,?,?)", (ts, src["source_id"], http_status, len(entries), new))
    con.execute("""UPDATE sources SET etag=?, last_modified=?, last_polled_at=?,
                   last_changed_at=CASE WHEN ?>0 THEN ? ELSE last_changed_at END,
                   failure_count=0 WHERE source_id=?""",
                (headers.get("etag"), headers.get("last-modified"), ts,
                 new, ts, src["source_id"]))
    con.commit()
    return new


def feed_entries(feed):
    out = []
    for e in feed.entries:
        raw_pub, iso_pub = parse_published(e)
        pub = e["source"].get("title", "") if isinstance(e.get("source"), dict) else ""
        out.append({"link": e.get("link", ""), "title": e.get("title", ""),
                    "desc": e.get("media_description") or e.get("summary") or e.get("description") or "",
                    "publisher": pub, "raw_pub": raw_pub, "iso_pub": iso_pub,
                    "payload": json.dumps(dict(e), ensure_ascii=False, default=str)})
    return out


def youtube_channel_id(feed_url):
    m = re.search(r"channel_id=(UC[\w-]{22})", feed_url or "")
    return m.group(1) if m else None


def poll_youtube_api(con, client, src, key, ts):
    """The channel's uploads playlist through the Data API. Returns new-item count,
    or None when the API could not be used (the caller then tries the RSS feed)."""
    cid = youtube_channel_id(src["feed_url"])
    if not cid:
        return None
    try:
        r = client.get(YT_API, params={"part": "snippet", "playlistId": "UU" + cid[2:],
                                       "maxResults": YT_API_MAX, "key": key})
    except Exception as ex:
        _poll_failed(con, src, ts, ex)
        return None
    if r.status_code != 200:
        reason = ""
        try:
            reason = (r.json().get("error") or {}).get("message", "")
        except ValueError:
            pass
        log.warning(f"  {src['source_id']:<22} YouTube API HTTP {r.status_code} {reason[:70]} -- trying the feed")
        if r.status_code == 403 and "quota" in reason.lower():
            log.error("  YouTube API daily quota is used up; channels fall back to the RSS feed until tomorrow")
        return None
    entries = []
    for it in r.json().get("items", []):
        sn = it.get("snippet") or {}
        vid = ((sn.get("resourceId") or {}).get("videoId")) or ""
        if not vid:
            continue
        raw_pub = sn.get("publishedAt") or ""
        try:
            iso_pub = datetime.fromisoformat(raw_pub.replace("Z", "+00:00")).astimezone(timezone.utc)\
                              .isoformat(timespec="seconds")
        except ValueError:
            iso_pub = None
        entries.append({"link": f"https://www.youtube.com/watch?v={vid}", "title": sn.get("title", ""),
                        "desc": sn.get("description", ""), "publisher": sn.get("channelTitle", ""),
                        "raw_pub": raw_pub, "iso_pub": iso_pub,
                        "payload": json.dumps({"via": "youtube_api", "snippet": sn}, ensure_ascii=False)})
    new = store_entries(con, src, entries, ts, 200)
    log.info(f"  {src['source_id']:<22} api  {len(entries):>3} entries  {new:>3} new")
    return new


def poll_source(con, client, src):
    ts = now()
    if src["kind"] == "youtube":
        key = (env().get("YOUTUBE_API_KEY") or "").strip()
        if key:
            n = poll_youtube_api(con, client, src, key, ts)
            if n is not None:
                return n

    headers = {"User-Agent": UA}
    if src["etag"]:
        headers["If-None-Match"] = src["etag"]
    if src["last_modified"]:
        headers["If-Modified-Since"] = src["last_modified"]
    try:
        r = client.get(src["feed_url"], headers=headers)
    except Exception as ex:
        _poll_failed(con, src, ts, ex)
        return 0

    if r.status_code == 304:
        con.execute("INSERT INTO poll_runs (ts,source_id,http_status,not_modified,entries,new_items) "
                    "VALUES (?,?,?,1,0,0)", (ts, src["source_id"], 304))
        con.execute("UPDATE sources SET last_polled_at=?, failure_count=0 WHERE source_id=?",
                    (ts, src["source_id"]))
        con.commit()
        log.info(f"  {src['source_id']:<22} 304 unchanged")
        return 0

    entries = feed_entries(feedparser.parse(r.content))
    new = store_entries(con, src, entries, ts, r.status_code, r.headers)
    log.info(f"  {src['source_id']:<22} {r.status_code}  {len(entries):>3} entries  {new:>3} new")
    return new


def poll_due(con, client):
    rows = con.execute("SELECT * FROM sources WHERE enabled=1 AND feed_url<>''").fetchall()
    total = 0
    for src in rows:
        if STOP:
            break
        due = True
        if src["last_polled_at"]:
            try:
                age = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(src["last_polled_at"])).total_seconds()
                due = age >= src["poll_interval_s"]
            except Exception:
                due = True
        if due:
            total += poll_source(con, client, src)
            time.sleep(1.0)
    return total


# --------------------------------------------------------------------------
# resolver -- adaptive pacing, this is also the rate experiment
# --------------------------------------------------------------------------

def record_attempt(con, item_id, kind, ok, http_status, err_type, err_text, ms):
    con.execute("""INSERT INTO attempts (item_id,kind,ts,ok,http_status,error_type,error_text,duration_ms)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (item_id, kind, now(), int(ok), http_status, err_type,
                 (err_text or "")[:500], ms))


def drain_resolver(con, budget_s, tick=None):
    from googlenewsdecoder import gnewsdecoder

    interval = float(rt_get(con, "resolve_interval", RESOLVE_INTERVAL_START))
    okrun = int(rt_get(con, "consecutive_ok", 0))
    deadline = time.time() + budget_s
    resolved_n = skipped_n = blocked_n = fail = 0
    blocked = blocked_hosts()

    pending = con.execute("SELECT count(*) c FROM items "
                          "WHERE resolve_status='PENDING'").fetchone()["c"]
    if not pending:
        return 0
    log.info(f"  {pending} pending, pacing {interval:.1f}s, budget {budget_s}s "
             f"(~{int(budget_s / (interval + 1.5))} this pass)")

    while time.time() < deadline and not STOP:
        if tick:
            tick()
        row = con.execute("""SELECT id,link,raw_payload FROM items
                             WHERE resolve_status='PENDING' AND resolve_attempts<?
                             ORDER BY id DESC LIMIT 1""", (RESOLVE_MAX_ATTEMPTS,)).fetchone()
        if not row:
            break

        item_id, link = row["id"], row["link"]

        if "news.google.com" not in link:
            con.execute("UPDATE items SET resolve_status='SKIPPED', resolved_url=? WHERE id=?",
                        (link, item_id))
            set_canon(con, item_id, link)
            con.commit()
            skipped_n += 1
            continue

        pub = publisher_host(row["raw_payload"])
        if pub and host_in(pub, blocked):
            # a blocklisted outlet is not worth a decode call against Google's throttle
            con.execute("""UPDATE items SET resolve_status='BLOCKED', extract_status='SKIPPED_BLOCKED',
                           extract_host=? WHERE id=?""", (pub, item_id))
            con.commit()
            blocked_n += 1
            continue

        t0 = time.time()
        err_type = err_text = None
        resolved = None
        try:
            res = gnewsdecoder(link, interval=None)   # we do our own pacing
            if res.get("status"):
                resolved = res.get("decoded_url")
            else:
                err_type, err_text = "DecodeFailed", str(res.get("message", ""))
        except Exception as ex:
            err_type, err_text = type(ex).__name__, str(ex)
        ms = int((time.time() - t0) * 1000)

        con.execute("UPDATE items SET resolve_attempts=resolve_attempts+1, resolve_ms=? WHERE id=?",
                    (ms, item_id))
        record_attempt(con, item_id, "resolve", bool(resolved), None, err_type, err_text, ms)

        if resolved:
            h = (urlparse(resolved).hostname or "?").replace("www.", "")
            if host_in(h, blocked):
                con.execute("""UPDATE items SET resolve_status='BLOCKED', resolved_url=?,
                               extract_status='SKIPPED_BLOCKED', extract_host=? WHERE id=?""",
                            (resolved, h, item_id))
                blocked_n += 1
            else:
                con.execute("UPDATE items SET resolve_status='RESOLVED', resolved_url=? WHERE id=?",
                            (resolved, item_id))
            set_canon(con, item_id, resolved)
            resolved_n += 1
            okrun += 1
            log.info(f"  [{resolved_n:>4}/{pending}] {ms/1000:4.1f}s  {h[:32]:<32}  pace {interval:.1f}s")
            if okrun >= RESOLVE_SPEEDUP_AFTER and interval > RESOLVE_INTERVAL_FLOOR:
                new_i = max(RESOLVE_INTERVAL_FLOOR, interval - RESOLVE_SPEEDUP_STEP)
                con.execute("""INSERT INTO rate_events (ts,event,old_interval,new_interval,consecutive_ok)
                               VALUES (?,'SPEEDUP',?,?,?)""", (now(), interval, new_i, okrun))
                log.info(f"  resolver speedup {interval:.1f}s -> {new_i:.1f}s after {okrun} clean")
                interval, okrun = new_i, 0
        else:
            fail += 1
            okrun = 0
            blob = f"{err_type} {err_text}"
            is_429 = "429" in blob or "Too Many" in blob or "rate" in blob.lower()
            new_i = min(RESOLVE_INTERVAL_CEIL, interval * (3.0 if is_429 else 1.5))
            con.execute("""INSERT INTO rate_events (ts,event,old_interval,new_interval,consecutive_ok,note)
                           VALUES (?,?,?,?,0,?)""",
                        (now(), "BACKOFF_429" if is_429 else "BACKOFF_ERR",
                         interval, new_i, blob[:200]))
            log.warning(f"  resolver {'429' if is_429 else 'err'}: {interval:.1f}s -> {new_i:.1f}s  {blob[:70]}")
            interval = new_i
            con.execute("""UPDATE items SET resolve_status=
                           CASE WHEN resolve_attempts>=? THEN 'FAILED' ELSE 'PENDING' END
                           WHERE id=?""", (RESOLVE_MAX_ATTEMPTS, item_id))

        rt_set(con, "resolve_interval", interval)
        rt_set(con, "consecutive_ok", okrun)
        con.commit()
        time.sleep(interval)

    log.info(f"  resolver: {resolved_n} decoded, {skipped_n} needed no decode, "
             f"{blocked_n} blocked, {fail} failed, pacing now {interval:.1f}s")
    return resolved_n


# --------------------------------------------------------------------------
# fetch + extract
# --------------------------------------------------------------------------

def drain_extractor(con, client, budget_s, tick=None):
    deadline = time.time() + budget_s
    ok = thin = failed = skipped = 0
    blocked = blocked_hosts()

    pending = con.execute("""SELECT count(*) c FROM items
                             WHERE resolve_status IN ('RESOLVED','SKIPPED')
                               AND extract_status='PENDING'""").fetchone()["c"]
    if not pending:
        return 0
    log.info(f"  {pending} ready to extract, budget {budget_s}s")
    keep_html = keep_failed_html()

    while time.time() < deadline and not STOP:
        if tick:
            tick()
        row = con.execute("""SELECT id,resolved_url FROM items
                             WHERE resolve_status IN ('RESOLVED','SKIPPED')
                               AND extract_status='PENDING'
                               AND extract_attempts<?
                             ORDER BY id DESC LIMIT 1""", (EXTRACT_MAX_ATTEMPTS,)).fetchone()
        if not row:
            break

        item_id, url = row["id"], row["resolved_url"]
        host = (urlparse(url).hostname or "").replace("www.", "")

        if host_in(host, VIDEO_HOSTS):
            con.execute("UPDATE items SET extract_status='SKIPPED_VIDEO', extract_host=? WHERE id=?",
                        (host, item_id))
            con.commit()
            skipped += 1
            continue

        if host_in(host, SOCIAL_HOSTS):
            con.execute("UPDATE items SET extract_status='SKIPPED_SOCIAL', extract_host=? WHERE id=?",
                        (host, item_id))
            con.commit()
            skipped += 1
            continue

        if host_in(host, blocked):
            con.execute("UPDATE items SET extract_status='SKIPPED_BLOCKED', extract_host=? WHERE id=?",
                        (host, item_id))
            con.commit()
            skipped += 1
            continue

        t0 = time.time()
        status = "FAILED"
        http_status = chars = None
        text = ""
        err_type = err_text = None
        html = ""
        try:
            r = client.get(url, headers={"User-Agent": UA})
            http_status = r.status_code
            html = r.text
            text = trafilatura.extract(html, include_comments=False,
                                       include_tables=False) or ""
            chars = len(text)
            status = "OK" if chars >= THIN_TEXT_CHARS else ("THIN" if chars else "FAILED")
        except Exception as ex:
            err_type, err_text = type(ex).__name__, str(ex)
        ms = int((time.time() - t0) * 1000)

        con.execute("""UPDATE items SET extract_status=?, extract_host=?, extract_http=?,
                       extract_chars=?, extract_text=?, extract_ms=?,
                       extract_attempts=extract_attempts+1 WHERE id=?""",
                    (status, host, http_status, chars, text or None, ms, item_id))
        record_attempt(con, item_id, "fetch", status == "OK", http_status, err_type, err_text, ms)

        # keep the page only when we could not read it (and not on a machine nobody inspects)
        if status in ("FAILED", "THIN") and html and keep_html:
            con.execute("INSERT OR REPLACE INTO failed_html (item_id,ts,html) VALUES (?,?,?)",
                        (item_id, now(), html[:400000]))

        if status == "OK":
            ok += 1
        elif status == "THIN":
            thin += 1
        else:
            failed += 1
        con.commit()
        log.info(f"  [{ok+thin+failed:>4}/{pending}] {status:<6} {str(chars or 0):>6}ch  "
                 f"{host[:32]:<32} {err_type or ''}")
        time.sleep(FETCH_DELAY_S)

    if ok or thin or failed or skipped:
        log.info(f"  extractor: {ok} ok, {thin} thin, {failed} failed, "
                 f"{skipped} skipped (video/social)")
    return ok


# --------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------

def cmd_health():
    """Small, pasteable summary of whether the collector has actually been up."""
    con = connect()
    p = print

    rows = [r["ts"] for r in con.execute(
        "SELECT ts FROM poll_runs WHERE ts IS NOT NULL ORDER BY ts")]
    if not rows:
        p("no polls recorded yet")
        return

    p(f"\nUPTIME   first poll {rows[0]}   last poll {rows[-1]}")
    gaps = []
    for a, b in zip(rows, rows[1:]):
        try:
            da, db = datetime.fromisoformat(a), datetime.fromisoformat(b)
        except ValueError:
            continue
        m = (db - da).total_seconds() / 60
        if m > 15:
            gaps.append((da, db, m))
    if gaps:
        p(f"  {len(gaps)} dark period(s) over 15 min -- machine asleep or offline:")
        for da, db, m in gaps[-8:]:
            h = f"{m/60:.1f}h" if m >= 60 else f"{m:.0f}m"
            p(f"     {da.astimezone().strftime('%d %b %H:%M')} -> "
              f"{db.astimezone().strftime('%H:%M')}   ({h})")
        p(f"  total dark: {sum(g[2] for g in gaps)/60:.1f}h")
    else:
        p("  no gaps over 15 min. Continuous coverage.")

    p("\nITEMS PER HOUR (local time)")
    hourly = con.execute("""SELECT substr(discovered_at,1,13) h, count(*) c
                            FROM items GROUP BY h ORDER BY h DESC LIMIT 18""").fetchall()
    mx = max((r["c"] for r in hourly), default=1)
    for r in reversed(hourly):
        try:
            lt = datetime.fromisoformat(r["h"] + ":00:00+00:00").astimezone()
            lbl = lt.strftime("%d %b %H:00")
        except ValueError:
            lbl = r["h"]
        p(f"  {lbl}  {r['c']:>4}  {'#' * int(28 * r['c'] / mx)}")

    p("\nSOURCE FRESHNESS")
    for r in con.execute("""SELECT source_id, enabled, last_polled_at, last_changed_at,
                                   failure_count FROM sources ORDER BY enabled DESC, source_id"""):
        if not r["enabled"]:
            p(f"  OFF  {r['source_id']}")
            continue
        age = "?"
        if r["last_changed_at"]:
            try:
                mins = (datetime.now(timezone.utc)
                        - datetime.fromisoformat(r["last_changed_at"])).total_seconds() / 60
                age = f"{mins/60:.1f}h ago" if mins >= 60 else f"{mins:.0f}m ago"
            except ValueError:
                pass
        warn = "   <-- STALE" if r["failure_count"] >= 3 else ""
        p(f"  on   {r['source_id']:<22} last new item {age:<10} "
          f"fails {r['failure_count']}{warn}")

    errs = con.execute("""SELECT source_id, substr(error,1,40) e, count(*) c
                          FROM poll_runs WHERE error IS NOT NULL
                          GROUP BY source_id, e ORDER BY c DESC LIMIT 8""").fetchall()
    if errs:
        p("\nPOLL ERRORS")
        for r in errs:
            p(f"  {r['c']:>4}x  {r['source_id']:<22} {r['e']}")

    rp, ep = queue_depth(con)
    p(f"\nqueues: {rp} to resolve, {ep} to extract\n")
    if alerts is None:
        p(f"ALERTS  not loaded: {PHASE1_ERROR}\n")
    else:
        try:
            p("\n".join(alerts.status_lines(con)) + "\n")
        except Exception as ex:
            p(f"ALERTS  status unavailable: {type(ex).__name__}: {ex}\n")
    con.close()


def cmd_stats():
    con = connect()
    p = print

    n = con.execute("SELECT count(*) c FROM items").fetchone()["c"]
    if not n:
        p("corpus is empty -- run 'python collect.py --once' first")
        return
    first = con.execute("SELECT min(discovered_at) m FROM items").fetchone()["m"]
    p(f"\n{n} items since {first}\n")

    p("by source")
    for r in con.execute("""SELECT source_id, count(*) c FROM items
                            GROUP BY source_id ORDER BY c DESC"""):
        p(f"   {r['source_id']:<24} {r['c']:>5}")

    p("\nresolution")
    for r in con.execute("SELECT resolve_status s, count(*) c FROM items GROUP BY s ORDER BY c DESC"):
        p(f"   {r['s']:<24} {r['c']:>5}")
    q = con.execute("""SELECT count(*) c, avg(duration_ms) a FROM attempts
                       WHERE kind='resolve' AND ok=1""").fetchone()
    if q["c"]:
        p(f"   avg successful decode    {q['a']/1000:>5.1f}s over {q['c']} attempts")
    iv = rt_get(con, "resolve_interval", "?")
    p(f"   current pacing interval  {iv}s")

    p("\nextraction")
    for r in con.execute("SELECT extract_status s, count(*) c FROM items GROUP BY s ORDER BY c DESC"):
        p(f"   {r['s']:<24} {r['c']:>5}")

    p("\ntop publishers (resolved host)")
    for r in con.execute("""SELECT extract_host h, count(*) c,
                                   sum(CASE WHEN extract_status='OK' THEN 1 ELSE 0 END) ok
                            FROM items
                            WHERE extract_host IS NOT NULL AND extract_host<>''
                              AND extract_status NOT IN ('SKIPPED_VIDEO','SKIPPED_SOCIAL')
                            GROUP BY h ORDER BY c DESC LIMIT 15"""):
        rate = 100.0 * r["ok"] / r["c"] if r["c"] else 0
        flag = "   <-- needs a custom selector" if r["c"] >= 5 and rate < 50 else ""
        p(f"   {r['h']:<30} {r['c']:>4} items  {rate:>5.1f}% ok{flag}")

    ev = con.execute("SELECT count(*) c FROM rate_events WHERE event='BACKOFF_429'").fetchone()["c"]
    p(f"\nresolver 429 backoffs: {ev}")
    if ev:
        r = con.execute("""SELECT ts, old_interval FROM rate_events
                           WHERE event='BACKOFF_429' ORDER BY id LIMIT 1""").fetchone()
        p(f"   first 429 while pacing at {r['old_interval']}s  ({r['ts']})")

    bad = con.execute("""SELECT count(*) c FROM items
                         WHERE published_at IS NULL AND published_raw<>''""").fetchone()["c"]
    p(f"unparseable published dates: {bad}")

    rp, ep = queue_depth(con)
    p(f"\nqueues: {rp} waiting to resolve, {ep} waiting to extract")
    if rp or ep:
        try:
            iv = float(iv)
        except (TypeError, ValueError):
            iv = 5.0
        eta = (rp * (iv + 1.5) + ep * 3.5) / 60
        p(f"   rough backlog: {eta:.0f} min of work -- 'python collect.py --drain' clears it")

    sz = DB.stat().st_size / 1e6 if DB.exists() else 0
    p(f"database size: {sz:.1f} MB\n")
    con.close()


# --------------------------------------------------------------------------

def cmd_prune(days):
    """Forget items older than `days` and shrink the file. Google News re-delivers
    items up to 14 days old (when:14d), so keep at least that much memory or old
    stories come back as new."""
    days = max(int(days), 15)
    con = connect()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    before = DB.stat().st_size / 1e6
    old = "SELECT id FROM items WHERE discovered_at < ?"
    n = con.execute(f"SELECT count(*) FROM ({old})", (cutoff,)).fetchone()[0]
    for table, col in (("attempts", "item_id"), ("failed_html", "item_id"), ("alert_queue", "item_id")):
        try:
            con.execute(f"DELETE FROM {table} WHERE {col} IN ({old})", (cutoff,))
        except sqlite3.OperationalError:
            pass                                    # table not created yet
    try:
        con.execute("DELETE FROM events WHERE event_id IN (SELECT event_id FROM items WHERE discovered_at < ?)"
                    " AND event_id NOT IN (SELECT event_id FROM items WHERE discovered_at >= ? AND event_id IS NOT NULL)",
                    (cutoff, cutoff))
    except sqlite3.OperationalError:
        pass
    con.execute(f"DELETE FROM items WHERE id IN ({old})", (cutoff,))
    con.execute("DELETE FROM poll_runs WHERE ts < ?", (cutoff,))
    con.execute("DELETE FROM rate_events WHERE ts < ?", (cutoff,))
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.execute("VACUUM")
    con.close()
    after = DB.stat().st_size / 1e6
    log.info(f"prune: forgot {n} items older than {days} days; db {before:.1f} MB -> {after:.1f} MB")


def queue_depth(con):
    r = con.execute("""SELECT
        (SELECT count(*) FROM items WHERE resolve_status='PENDING') rp,
        (SELECT count(*) FROM items WHERE resolve_status IN ('RESOLVED','SKIPPED')
                                      AND extract_status='PENDING') ep""").fetchone()
    return r["rp"], r["ep"]


def cycle(con, client, rb=RESOLVE_BUDGET_S, eb=EXTRACT_BUDGET_S, poll=True):
    # Alerts are checked between items, not once per cycle: a cycle with a backlog
    # runs ~11 minutes, and a flood alert should not wait for it to finish.
    tick = lambda: phase1_tick(con, client)                      # noqa: E731
    if poll:
        log.info("--- poll ---")
        poll_due(con, client)
        if STOP:
            return
    log.info("--- resolve ---")
    drain_resolver(con, rb, tick=tick)
    if STOP:
        return
    log.info("--- extract ---")
    drain_extractor(con, client, eb, tick=tick)
    tick()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--drain", action="store_true",
                    help="clear the backlog without polling, then exit")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--health", action="store_true",
                    help="uptime gaps, coverage by hour, source freshness")
    ap.add_argument("--minutes", type=float, default=0,
                    help="with --once: size the resolve/extract budgets to finish in about this long")
    ap.add_argument("--prune", type=int, metavar="DAYS",
                    help="forget items older than DAYS (minimum 15) and shrink the database")
    a = ap.parse_args()

    if a.init:
        return cmd_init()
    if a.stats:
        return cmd_stats()
    if a.health:
        return cmd_health()
    if a.prune:
        return cmd_prune(a.prune)

    if not DB.exists():
        log.error("no corpus.db -- run 'python collect.py --init' first")
        return

    signal.signal(signal.SIGINT, _sigint)
    con = connect()
    client = httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": UA})
    phase1_setup(con)
    rp, ep = queue_depth(con)
    log.info(f"collector up, node={rt_get(con,'node','local')}, "
             f"pacing={rt_get(con,'resolve_interval')}s, "
             f"queues: {rp} to resolve / {ep} to extract  (Ctrl+C to stop)")
    try:
        if a.drain:
            # backlog mode: no polling, no budget ceiling, run until both queues empty
            while not STOP:
                rp, ep = queue_depth(con)
                if not rp and not ep:
                    log.info("both queues empty")
                    break
                log.info(f"=== drain: {rp} to resolve, {ep} to extract ===")
                cycle(con, client, rb=1800, eb=1800, poll=False)
            return

        while not STOP:
            if a.once and a.minutes:
                # polling ~1.5 min for 50 sources; split the rest 35/65 as in the defaults
                rest = max(60.0, a.minutes * 60 - 90)
                cycle(con, client, rb=int(rest * 0.35), eb=int(rest * 0.65))
            else:
                cycle(con, client)
            if a.once:
                phase1_tick(con, client, force=True)
                break
            for _ in range(30):
                if STOP:
                    break
                phase1_tick(con, client)
                time.sleep(1)
    finally:
        client.close()
        con.close()
        log.info("stopped cleanly")


if __name__ == "__main__":
    main()
