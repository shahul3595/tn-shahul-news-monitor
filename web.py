#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web.py -- the web edition: data files plus one page that renders them.

    docs/index.html          the page (the latest edition is embedded, so it renders offline)
    docs/data/latest.json    rolling edition: everything kept in the last DIGEST_WEB_HOURS
    docs/data/YYYY-MM-DD.json  one edition per IST day, by publication date
    docs/data/index.json     the list of available editions, newest first

digest.py calls publish() after each brief (latest + today + yesterday are rebuilt) and
backfill() for `python digest.py --backfill 7`.

Each edition: candidates -> ranking (stored Gemini verdicts, Gemini for the rest when a key
is set, the rules otherwise) -> one clustering call across the whole edition (Tamil and
English reports of one event become one story) -> merge -> select (10 per category, 60
total) -> a 3-bullet executive summary (one call, cached until the top stories change).
"""

import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import alerts
import digest
from alerts import IST, esc, fmt_ist, iso

log = logging.getLogger("collect.digest.web")
DOCS = digest.HERE / "docs"
PICKER_DAYS = 7               # offered in the date picker; every edition ever published stays in docs/data
ARCHIVE_AT_DAYS = 2           # a day's kept items are archived once it is this old ...
ARCHIVE_FINAL_DAYS = 15       # ... and once more here, after Google News has stopped re-serving it
MATRIX_DAYS = 30              # the 7-day matrix can be widened to this
CATEGORIES, NICE, ICONS = digest.CATEGORIES, digest.NICE, digest.ICONS

SCHEMA = ["CREATE TABLE IF NOT EXISTS editions (key TEXT PRIMARY KEY, cand_hash TEXT, clustered_at TEXT, "
          "summary TEXT, summary_hash TEXT, made_at TEXT)"]

SUMMARY_PROMPT = """You write the morning briefing for the office of R. Kumar, MLA for Velachery and Tamil Nadu
Minister for AI, IT and Digital Services, and in-charge minister for Thiruvallur. Different
teams read different sections: the constituency team reads Velachery, the district team
reads Thiruvallur, the portfolio team reads AI / IT / Digital.

Below are today's stories, already ranked, grouped by section and numbered [1], [2], ...
For EVERY section that has stories, write 2 or 3 takeaways -- 3 when the section has 4 or
more stories, 2 otherwise. Each takeaway is one plain English sentence of at most 28 words
that names the place, the people and what happened, and says what the office should do or
watch when that is clear. With each takeaway give "story": the number of the ONE story it
is mainly drawn from (the page links the takeaway to that story's card).
Do not invent anything not in the stories. Do not write for sections with no stories. No
preamble, no bullet symbols, no headings inside the text.

Section names to use, exactly: {names}

STORIES BY SECTION:
{payload}"""

SUMMARY_SCHEMA = {"type": "OBJECT", "properties": {"groups": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "section": {"type": "STRING"},
    "takeaways": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "text": {"type": "STRING"}, "story": {"type": "INTEGER"}}, "required": ["text", "story"]}}},
    "required": ["section", "takeaways"]}}}, "required": ["groups"]}
SUMMARY_FORMAT = "v3"         # bumped when the shape changes, so cached summaries are rebuilt
SECTION_LABEL = dict([("urgent", "Urgent")] + [(c, NICE[c]) for c in CATEGORIES])


def _hash(ids):
    return hashlib.sha1(",".join(str(i) for i in sorted(ids)).encode()).hexdigest()[:16]


def migrate(con):
    digest.migrate(con)
    for ddl in SCHEMA:
        con.execute(ddl)
    con.commit()


# --------------------------------------------------------------------------
# building one edition
# --------------------------------------------------------------------------

def _load_window(con, since, until):
    """Kept items published in [since, until). The rules_at bound is generous so that a
    story scored late still belongs to the day it was published."""
    reps, ids = digest._load(con, f"coalesce(i.published_at, i.discovered_at) < '{iso(until)}'",
                             since - timedelta(days=2), since)
    return reps, ids


def build(con, env, now, key, since, until, gemini=None):
    """One edition. Returns dict(key, cands, ids, urgent, sections, chosen, stats, summary)."""
    cfg = digest.settings(env)
    cands, ids = _load_window(con, since, until)
    cands, st_reruns = digest.drop_reruns(con, cands, since)
    st = digest.rank_all(con, cands, gemini, now)
    if st_reruns:
        st["reruns"] = st_reruns
    row = con.execute("SELECT cand_hash, summary, summary_hash FROM editions WHERE key=?", (key,)).fetchone()
    ch = _hash([c["id"] for c in cands])
    # clustering: once per distinct candidate set, then the stored story keys carry it
    if gemini is not None and cands and (row is None or row["cand_hash"] != ch):
        groups = digest.cluster(gemini, con, cands, f"{key}@{ch}")
        st["clusters"] = groups
        if getattr(digest.cluster, "last_refused", None):
            st["refused"] = list(digest.cluster.last_refused)
            digest.cluster.last_refused = []
        con.execute("INSERT INTO editions (key, cand_hash, clustered_at, made_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET cand_hash=excluded.cand_hash, clustered_at=excluded.clustered_at",
                    (key, ch, iso(now), iso(now)))
        con.commit()
    before = len(cands)
    cands = digest.merge_stories(cands, con)
    st["merged"] = before - len(cands)
    urgent, sections, leftovers = digest.select(cands, cfg["web_per"], cfg["web_max"])
    chosen = urgent + [c for cat in CATEGORIES for c in sections[cat]]
    filled = digest.fill_takeaways(gemini, con, chosen, now)
    if filled:
        st["notes"] = filled
    summary = _summary(con, gemini, key, urgent, sections, row, now)
    return {"key": key, "since": since, "until": until, "cands": cands, "ids": ids, "urgent": urgent,
            "sections": sections, "chosen": chosen, "leftovers": leftovers, "stats": st, "summary": summary}


def _summary(con, gemini, key, urgent, sections, row, now):
    """Category-wise takeaways: [{"cat": "urgent"|category, "bullets": [{"text", "id"}, ..]}]
    in section order, each takeaway pointing at the story card it comes from (id None when
    Gemini's number did not resolve). Cached until the chosen stories change."""
    groups = [("urgent", urgent)] + [(cat, sections.get(cat) or []) for cat in CATEGORIES]
    groups = [(cat, items) for cat, items in groups if items]
    sh = _hash([c["id"] for _, items in groups for c in items]) + SUMMARY_FORMAT
    cached = json.loads(row["summary"]) if row is not None and row["summary"] else []
    if row is not None and row["summary_hash"] == sh and cached:
        return cached
    if gemini is None or not groups:
        return cached if isinstance(cached, list) and cached and isinstance(cached[0], dict) else []
    payload, numbered = [], []
    for cat, items in groups:
        payload.append(f"## {SECTION_LABEL[cat]} ({len(items)} stories)")
        for c in items[:12]:
            numbered.append(c)
            payload.append(f"[{len(numbered)}] {digest._title(c)} ({alerts.outlet_name(c)}) -- {digest._snippet(c)}")
    names = ", ".join(SECTION_LABEL[cat] for cat, _ in groups)
    data = gemini.call(SUMMARY_PROMPT.format(names=names, payload="\n".join(payload)), SUMMARY_SCHEMA, "summary")
    out = []
    if isinstance(data, dict):
        by_label = {SECTION_LABEL[cat].lower(): cat for cat, _ in groups}
        got = {}
        for g in data.get("groups") or []:
            cat = by_label.get(str(g.get("section", "")).strip().lower())
            bullets = []
            for b in (g.get("takeaways") or [])[:3]:
                text = str(b.get("text", "") if isinstance(b, dict) else b).strip()
                n = digest._int(b.get("story"), 1, len(numbered), 0) if isinstance(b, dict) else 0
                src = numbered[n - 1] if n else None
                if text and digest.names_in(text, (), strict=True) and (src is None or not digest.names_in(digest._text(src), src.get("tags") or ())):
                    continue                          # his name put into a story that does not carry it
                if text:
                    bullets.append({"text": text, "id": src["id"] if src else None})
            if cat and bullets and cat not in got:
                got[cat] = bullets
        out = [{"cat": cat, "bullets": got[cat]} for cat, _ in groups if cat in got]
    if out:
        con.execute("INSERT INTO editions (key, summary, summary_hash, made_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET summary=excluded.summary, summary_hash=excluded.summary_hash, "
                    "made_at=excluded.made_at", (key, json.dumps(out, ensure_ascii=False), sh, iso(now)))
        con.commit()
        return out
    return cached if isinstance(cached, list) and cached and isinstance(cached[0], dict) else []


# --------------------------------------------------------------------------
# data files
# --------------------------------------------------------------------------

def _story(c, urgent):
    return {"id": c["id"], "title": digest._title(c), "url": alerts.display_url(c),
            "outlet": alerts.outlet_name(c), "sources": len(c.get("sources") or []),
            "reports": [{"outlet": r["outlet"], "url": r["url"], "title": r["title"], "lang": r["lang"]}
                        for r in (c.get("reports") or []) if r["url"] != alerts.display_url(c)],
            "takeaways": c.get("takeaways") or [],
            "manual": bool(c.get("manual")),
            "merged": [i for i in (c.get("merged") or []) if i != c["id"]],
            "cat": c["category"], "urgent": bool(urgent), "sentiment": c.get("sentiment") or "neutral",
            "priority": c["priority"], "impact": c.get("impact") or 5,
            "snippet": digest._snippet(c), "image": digest._image_for(c) or "",
            "time": fmt_ist(c["published_at"] or c["discovered_at"]),
            "ts": c["published_at"] or c["discovered_at"] or ""}


def edition_json(ed, label, now):
    stories = [_story(c, True) for c in ed["urgent"]]
    for cat in CATEGORIES:
        stories += [_story(c, False) for c in ed["sections"][cat]]
    return {"key": ed["key"], "label": label, "generated": iso(now), "generated_ist": fmt_ist(iso(now)),
            "window": [iso(ed["since"]), iso(ed["until"])], "considered": len(ed["cands"]),
            "items": len(ed["ids"]), "summary": ed["summary"], "stories": stories}


def _day_bounds(day):
    """IST calendar day -> (since, until) as aware datetimes."""
    start = datetime(day.year, day.month, day.day, tzinfo=IST)
    return start, start + timedelta(days=1)


def _index(data_dir, now):
    days = []
    for p in data_dir.glob("????-??-??.json"):
        try:
            days.append(datetime.strptime(p.stem, "%Y-%m-%d").date())
        except ValueError:
            continue
    days.sort(reverse=True)               # nothing is deleted: every edition ever published stays
    entries = [{"key": "latest", "file": "data/latest.json", "label": "Latest"}]
    entries += [{"key": f"{d:%Y-%m-%d}", "file": f"data/{d:%Y-%m-%d}.json", "label": d.strftime("%a %d %b"),
                 "pick": k < PICKER_DAYS} for k, d in enumerate(days)]
    for e in entries:                     # category x sentiment counts, for the matrix
        try:
            payload = json.loads((data_dir / Path(e["file"]).name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        counts = {}
        for st in payload.get("stories", []):
            cat = "urgent" if st.get("urgent") else st.get("cat")
            counts.setdefault(cat, {"positive": 0, "neutral": 0, "critical": 0})
            counts[cat][st.get("sentiment") or "neutral"] += 1
        e["counts"] = counts
        e["total"] = len(payload.get("stories", []))
    (data_dir / "index.json").write_text(json.dumps({"generated": iso(now), "editions": entries}, ensure_ascii=False),
                                         encoding="utf-8")
    return entries


def write_edition(docs, ed, label, now):
    data_dir = Path(docs) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = edition_json(ed, label, now)
    (data_dir / f"{ed['key']}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def write_page(docs, latest_payload, entries, feedback_url=""):
    docs = Path(docs)
    docs.mkdir(parents=True, exist_ok=True)
    (docs / ".nojekyll").touch()
    html = PAGE.replace("__DATA__", json.dumps(latest_payload, ensure_ascii=False).replace("</", "<\\/")) \
               .replace("__EDITIONS__", json.dumps(entries, ensure_ascii=False).replace("</", "<\\/")) \
               .replace("__FEEDBACK__", esc(feedback_url)).replace("__CSS__", CSS).replace("__JS__", JS)
    (docs / "index.html").write_text(html, encoding="utf-8")
    return docs / "index.html"


# --------------------------------------------------------------------------
# the item archive -- one file per day, everything the rules kept, not only the page
# --------------------------------------------------------------------------

def _shown_ids(docs, day_key):
    try:
        ed = json.loads((Path(docs) / "data" / f"{day_key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    out = set()
    for st in ed.get("stories", []):
        out.add(st["id"])
        out.update(st.get("merged") or [])
    return out


def write_archive(con, docs, now, env=None, force_days=()):
    """docs/archive/YYYY-MM-DD.json for the day that turned ARCHIVE_AT_DAYS old (written
    once) and the day that turned ARCHIVE_FINAL_DAYS old (written once more, marked final:
    Google News re-serves a story for up to 14 days, so that pass catches late arrivals).
    No article bodies -- titles, links, outlets, dates, categories, sentiment, notes."""
    docs = Path(docs)
    adir = docs / "archive"
    adir.mkdir(parents=True, exist_ok=True)
    today = now.astimezone(IST).date()
    written = []
    jobs = [(today - timedelta(days=ARCHIVE_AT_DAYS), False), (today - timedelta(days=ARCHIVE_FINAL_DAYS), True)]
    jobs += [(d, True) for d in force_days]
    for day, final in jobs:
        path = adir / f"{day:%Y-%m-%d}.json"
        if path.exists() and not final:
            continue
        if path.exists() and final:
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("final"):
                    continue
            except (OSError, ValueError):
                pass
        since, until = _day_bounds(day)
        cands, ids = digest._load(con, f"coalesce(i.published_at, i.discovered_at) < '{iso(until)}'",
                                  since - timedelta(days=2), since)
        if not ids and not path.exists():
            continue
        digest.rank_all(con, cands, None, now)          # stored verdicts and the rules; no Gemini calls
        shown = _shown_ids(docs, f"{day:%Y-%m-%d}")
        items = []
        for c in sorted(cands, key=digest._order):
            items.append({"id": c["id"], "title": digest._title(c), "url": alerts.display_url(c),
                          "outlet": alerts.outlet_name(c), "lang": "ta" if digest._is_tamil(c) else "en",
                          "published": fmt_ist(c["published_at"] or c["discovered_at"]),
                          "cat": c["category"], "priority": c["priority"], "sentiment": c.get("sentiment") or "neutral",
                          "takeaways": c.get("takeaways") or [], "by": c.get("by", ""),
                          "outlets": c.get("sources") or [], "shown": c["id"] in shown or bool(set(c.get("merged") or []) & shown)})
        payload = {"day": f"{day:%Y-%m-%d}", "label": day.strftime("%A %d %B %Y"), "written": iso(now), "final": final,
                   "items_in_db": len(ids), "stories": len(items), "shown": sum(1 for i in items if i["shown"]), "items": items}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        written.append(f"{day:%Y-%m-%d}")
    # the archive index
    days = []
    for p in sorted(adir.glob("????-??-??.json"), reverse=True):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            days.append({"day": d["day"], "file": f"archive/{p.name}", "stories": d.get("stories", 0),
                         "shown": d.get("shown", 0), "final": bool(d.get("final"))})
        except (OSError, ValueError, KeyError):
            continue
    (adir / "index.json").write_text(json.dumps({"generated": iso(now), "days": days}, ensure_ascii=False), encoding="utf-8")
    return written


# --------------------------------------------------------------------------
# the status snapshot (docs/data/status.json) and the admin page
# --------------------------------------------------------------------------

def _feedback_counts(env, now):
    url = digest.settings(env)["feedback_csv"]
    if not url:
        return {"connected": False}
    try:
        import csv
        import io
        import httpx
        r = httpx.get(url, timeout=20.0, follow_redirects=True)
        if r.status_code != 200:
            return {"connected": True, "error": f"HTTP {r.status_code}"}
        rows = list(csv.reader(io.StringIO(r.text)))
    except Exception as ex:
        return {"connected": True, "error": type(ex).__name__}
    out = {"connected": True, "all": {"up": 0, "down": 0, "missing": 0}, "week": {"up": 0, "down": 0, "missing": 0}}
    week = now - timedelta(days=7)
    for row in rows[1:]:
        if len(row) < 2:
            continue
        kind = row[1].strip().lower()
        if kind not in out["all"]:
            continue
        out["all"][kind] += 1
        ts = None
        try:
            ts = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
        except ValueError:
            pass
        if ts is not None and ts >= week:
            out["week"][kind] += 1
    return out


def status_snapshot(con, env, now, editions, brief=None, gemini=None):
    """What the admin page shows. Counts and times only -- nothing that names the host."""
    cfg = digest.settings(env)
    day = alerts.utcnow().strftime("%Y-%m-%d")          # the budget table is kept by the real clock
    r = con.execute("SELECT calls FROM ai_budget WHERE day=?", (day,)).fetchone()
    calls = r[0] if r else 0
    dead = gemini.dead if gemini is not None else None
    if not (env.get("GEMINI_API_KEY") or "").strip():
        state, note = "standby", "no GEMINI_API_KEY"
    elif dead and ("HTTP 4" in dead or "check GEMINI" in dead):
        state, note = "standby", dead
    elif dead or calls >= cfg["ai_calls"]:
        state, note = "throttled", dead or "daily cap reached"
    else:
        state, note = "active", ""
    since24 = iso(now - timedelta(hours=24))
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0]
    last_run = q("SELECT max(ts) FROM poll_runs")
    items24 = q("SELECT count(*) FROM items WHERE discovered_at >= ?", since24)
    kept24 = q("SELECT count(*) FROM items WHERE discovered_at >= ? AND (band IN ('AUTO_KEEP','AI','KEYWORD_KEEP') OR urgent=1)", since24)
    unscored = q("SELECT count(*) FROM items WHERE rules_at IS NULL")
    pending_resolve = q("SELECT count(*) FROM items WHERE resolve_status='PENDING'")
    pending_extract = q("SELECT count(*) FROM items WHERE extract_status='PENDING' AND resolve_status='RESOLVED'")
    yt_failing = [row[0] for row in con.execute("""SELECT s.name FROM sources s WHERE s.kind='youtube' AND coalesce(s.enabled,1)=1
                     AND EXISTS (SELECT 1 FROM poll_runs p WHERE p.source_id=s.source_id AND p.ts >= ?)
                     AND NOT EXISTS (SELECT 1 FROM poll_runs p WHERE p.source_id=s.source_id AND p.ts >= ? AND coalesce(p.entries,0) > 0)
                     ORDER BY s.name""", (since24, since24))]
    grouping = {"clusters": 0, "refused": [], "reruns": 0, "guards": {}}
    for key, ed in (editions or {}).items():
        st = ed.get("stats") or {}
        grouping["clusters"] += st.get("clusters", 0)
        grouping["reruns"] += st.get("reruns", 0)
        for why, n in (st.get("guards") or {}).items():
            grouping["guards"][why] = grouping["guards"].get(why, 0) + n
        for rf in st.get("refused") or []:
            grouping["refused"].append(dict(rf, edition=key))
    grouping["refused"] = grouping["refused"][:12]
    last_brief = con.execute("SELECT slot, sent_at, chosen, status FROM digests ORDER BY id DESC LIMIT 1").fetchone()
    adir = Path(DOCS) / "archive"
    try:
        arch = json.loads((adir / "index.json").read_text(encoding="utf-8")).get("days", [])
    except (OSError, ValueError):
        arch = []
    return {"generated": iso(now), "generated_ist": fmt_ist(iso(now)),
            "gemini": {"calls_today": calls, "limit": cfg["ai_calls"], "state": state, "note": note,
                       "calls_this_run": gemini.calls if gemini is not None else 0, "model": cfg["model"]},
            "collector": {"last_run": fmt_ist(last_run) if last_run else "", "last_run_iso": last_run or "",
                          "items_24h": items24, "kept_24h": kept24, "dropped_24h": items24 - kept24, "unscored": unscored,
                          "pending_resolve": pending_resolve, "pending_extract": pending_extract, "youtube_failing": yt_failing},
            "brief": {"slot": last_brief["slot"], "sent": fmt_ist(last_brief["sent_at"] or ""), "chosen": last_brief["chosen"],
                      "status": last_brief["status"]} if last_brief else {},
            "grouping": grouping,
            "editions": {"stories_latest": len((editions or {}).get("latest", {}).get("chosen", [])),
                         "dated": len(list((Path(DOCS) / "data").glob("????-??-??.json"))), "archive_days": len(arch)},
            "feedback": _feedback_counts(env, now)}


# --- a secret sealed with the PIN: PBKDF2 -> HMAC-SHA256 keystream (CTR) + HMAC tag.
#     The browser does the same with WebCrypto, so nothing in admin.html says where the
#     runs live until the right PIN has been typed.

def _kdf(pin, salt, iters=120000):
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iters, dklen=64)


def seal(pin, text, iters=120000):
    salt, nonce = os.urandom(16), os.urandom(16)
    key = _kdf(pin, salt, iters)
    enc_key, mac_key = key[:32], key[32:]
    data = text.encode("utf-8")
    stream = b""
    for i in range((len(data) + 31) // 32):
        stream += hmac.new(enc_key, nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
    ct = bytes(a ^ b for a, b in zip(data, stream))
    tag = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode()
    return {"v": 1, "iters": iters, "salt": b64(salt), "nonce": b64(nonce), "ct": b64(ct), "tag": b64(tag)}


def unseal(pin, blob):
    """Python twin of the page's unlock, for the tests."""
    b = lambda k: base64.b64decode(blob[k])
    key = _kdf(pin, b("salt"), blob["iters"])
    enc_key, mac_key = key[:32], key[32:]
    nonce, ct = b("nonce"), b("ct")
    if not hmac.compare_digest(hmac.new(mac_key, nonce + ct, hashlib.sha256).digest(), b("tag")):
        return None
    stream = b""
    for i in range((len(ct) + 31) // 32):
        stream += hmac.new(enc_key, nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
    return bytes(a ^ b for a, b in zip(ct, stream)).decode("utf-8")


def write_admin(docs, env):
    """docs/admin.html: a PIN prompt and, behind it, the status page. The run-log host is
    sealed with the PIN; without ADMIN_PIN the page says it is not configured."""
    docs = Path(docs)
    pin = (env.get("ADMIN_PIN") or "").strip()
    repo = (env.get("GITHUB_REPOSITORY") or os.environ.get("GITHUB_REPOSITORY") or "").strip()
    sealed = "null"
    if pin:
        cfg = {"status": "data/status.json"}
        if repo:
            cfg["runs"] = f"https://api.github.com/repos/{repo}/actions/runs?per_page=12"
            cfg["web"] = f"https://github.com/{repo}/actions/runs/"
            cfg["accept"] = "application/vnd.github+json"
        sealed = json.dumps(seal(pin, json.dumps(cfg)))
    html = ADMIN_PAGE.replace("__SEALED__", sealed).replace("__CSS__", CSS)
    (docs / "admin.html").write_text(html, encoding="utf-8")
    return docs / "admin.html"


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------

def publish(con, env, now, gemini=None, docs=None, days_back=1, brief=None):
    """Rebuild the latest edition and the dated editions for today and `days_back` earlier
    days (late-arriving stories land on the right day), then the index, the archive, the
    status snapshot, the page and the admin page."""
    global DOCS
    docs = Path(docs or DOCS)
    DOCS = docs
    cfg = digest.settings(env)
    migrate(con)
    out = {}
    latest = build(con, env, now, "latest", now - timedelta(hours=cfg["web_hours"]), now + timedelta(hours=1), gemini)
    latest_payload = write_edition(docs, latest, f"Last {cfg['web_hours']} hours", now)
    out["latest"] = latest
    today = now.astimezone(IST).date()
    for k in range(days_back + 1):
        day = today - timedelta(days=k)
        since, until = _day_bounds(day)
        ed = build(con, env, now, f"{day:%Y-%m-%d}", since, until, gemini)
        write_edition(docs, ed, day.strftime("%A %d %B %Y"), now)
        out[f"{day:%Y-%m-%d}"] = ed
    entries = _index(docs / "data", now)
    write_page(docs, latest_payload, entries, cfg["feedback_url"])
    try:
        archived = write_archive(con, docs, now, env)
        if archived:
            log.info(f"archive: wrote {', '.join(archived)}")
    except Exception:
        log.exception("archive: could not write (the page itself is unaffected)")
    try:
        snap = status_snapshot(con, env, now, out, brief, gemini)
        (docs / "data" / "status.json").write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        write_admin(docs, env)
    except Exception:
        log.exception("status: could not write the snapshot or admin page")
    return out


def backfill(con, env, now, days, gemini=None, docs=None):
    """Dated editions for the past `days` days from what is already in the database."""
    docs = Path(docs or DOCS)
    migrate(con)
    today = now.astimezone(IST).date()
    out = {}
    for k in range(1, days + 1):
        day = today - timedelta(days=k)
        since, until = _day_bounds(day)
        ed = build(con, env, now, f"{day:%Y-%m-%d}", since, until, gemini)
        write_edition(docs, ed, day.strftime("%A %d %B %Y"), now)
        out[f"{day:%Y-%m-%d}"] = ed
        log.info(f"backfill: {day} -- {len(ed['chosen'])} stories from {len(ed['ids'])} items"
                 + (f", {ed['stats'].get('clusters', 0)} clusters" if ed["stats"].get("clusters") else ""))
    return out


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------

CSS = r"""
:root{--bg:#f4f5f7;--card:#fff;--ink:#17191c;--dim:#667085;--line:#e4e7ec;--accent:#1d4e89;--urgent:#b42318;
--chip:#eef2f7;--ok:#0e6e63;--pos:#0e6e63;--neu:#667085;--crit:#b42318;
font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans Tamil","Noto Sans",sans-serif}
@media(prefers-color-scheme:dark){:root{--bg:#111417;--card:#1a1f24;--ink:#e8ecef;--dim:#98a2b3;--line:#2a323b;--chip:#232a32}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);line-height:1.45;font-size:16px}
.wrap{max-width:1080px;margin:0 auto;padding:12px 16px 80px}
header{display:flex;gap:10px;align-items:center;justify-content:space-between;position:sticky;top:0;background:var(--bg);padding:8px 0;z-index:5}
header h1{font-size:19px;margin:0;line-height:1.2}header .sub{color:var(--dim);font-size:12px;margin:2px 0 0}
.icon{min-width:44px;min-height:44px;border:1px solid var(--line);border-radius:12px;background:var(--card);color:var(--ink);font-size:20px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center}
.btn{min-height:44px;padding:8px 14px;font:inherit;font-size:15px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink);cursor:pointer}
.btn.primary{background:var(--accent);color:#fff;border-color:var(--accent)}.btn[aria-pressed=true]{background:var(--accent);color:#fff;border-color:var(--accent)}
.tools{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0;align-items:center}
.tools input[type=search]{flex:1 1 200px;min-height:44px;padding:8px 12px;font:inherit;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink)}
.tools select{min-height:44px;padding:8px 10px;font:inherit;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink)}
.callout{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:12px;padding:12px 16px;margin:10px 0}
.callout h2{font-size:13px;letter-spacing:.05em;text-transform:uppercase;color:var(--dim);margin:0 0 6px}
.callout .sgs{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:8px 22px}
.sg h3{font-size:14px;margin:6px 0 2px}.sg h3 a{color:var(--ink);text-decoration:none;display:inline-flex;align-items:center;min-height:32px}
.sg h3 a span{color:var(--dim);margin-left:4px}.sg h3 a:hover{color:var(--accent)}.sg.urgent h3 a{color:var(--urgent)}
.sg ul{margin:0 0 6px;padding-left:18px;font-size:14px}.sg li{margin:3px 0}.sg li a{color:var(--ink);text-decoration:underline;text-decoration-color:var(--line);text-underline-offset:3px}.sg li a:hover{color:var(--accent);text-decoration-color:var(--accent)}@keyframes flash{0%,60%{box-shadow:0 0 0 4px var(--accent);border-color:var(--accent)}100%{box-shadow:0 0 0 0 transparent}}.card{scroll-margin-top:90px}.card.flash{animation:flash 2.4s ease-out}
.dash{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin:10px 0;overflow-x:auto}
.dash h2{font-size:13px;letter-spacing:.05em;text-transform:uppercase;color:var(--dim);margin:0 0 6px;display:flex;justify-content:space-between;align-items:center}
.dash table{border-collapse:collapse;width:100%;font-size:14px}.dash th,.dash td{padding:6px 8px;text-align:center;border-top:1px solid var(--line)}
.dash th:first-child,.dash td:first-child{text-align:left}.dash thead th{border-top:0;color:var(--dim);font-weight:500;font-size:12px}
.dash button{min-height:36px;min-width:44px;border:0;background:transparent;font:inherit;color:var(--ink);border-radius:8px;cursor:pointer;padding:4px 8px}
.dash button:hover,.dash button.on{background:var(--chip)}.dash td.zero button{color:var(--dim);opacity:.5}
.dash .cat{font-weight:600}.dash tfoot td{font-weight:600}
.dash .hint{margin:6px 0 0;font-size:12px;color:var(--dim)}.dash .seg{display:flex;gap:4px}.btn.sm{min-height:34px;padding:4px 10px;font-size:13px}
.mwrap{overflow-x:auto;-webkit-overflow-scrolling:touch}.matrix{min-width:560px}.matrix th{font-size:12px;color:var(--dim);font-weight:500;white-space:nowrap}
.matrix td{padding:4px 4px;vertical-align:middle}.matrix td.day{white-space:nowrap;text-align:left}.matrix td.day.on{color:var(--accent)}
.cell{display:flex;flex-direction:column;align-items:center;gap:2px;min-width:72px;padding:2px 0}.cell.zero{opacity:.35;min-height:44px;justify-content:center}.cell.zero .n{color:var(--dim)}
.matrix button.n{display:flex;flex-direction:column;align-items:center;gap:3px;min-height:40px;min-width:64px;padding:4px 6px;border:0;border-radius:8px;background:transparent;font:inherit;font-weight:600;font-size:14px;color:var(--ink);cursor:pointer}
.matrix button.n:hover{background:var(--chip)}.matrix button.n.day{font-weight:600;white-space:nowrap;min-width:0;align-items:flex-start;padding:4px 6px 4px 0}
.badges button.bd{min-width:36px;min-height:36px;padding:0;border:0;background:transparent;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;border-radius:8px}.badges button.bd:hover{background:var(--chip)}
.cell.all{border-left:1px solid var(--line)}.cell.all .bar{width:64px}.bar{display:flex;width:56px;height:6px;border-radius:3px;overflow:hidden;background:var(--chip)}.bar i{display:block;height:100%}.bar .p{background:var(--pos)}.bar .u{background:#98a2b3}.bar .c{background:var(--crit)}
.badges{display:flex;gap:0}.badges b{font-weight:500;font-size:11px;padding:2px 5px;border-radius:999px;background:var(--chip);min-width:22px;text-align:center;white-space:nowrap;line-height:1.3}
.badges b.p{color:var(--pos)}.badges b.c{color:var(--crit)}.badges b.u{color:var(--dim)}
.outlets{margin:0;padding:6px 14px 8px;list-style:none;border-top:1px dashed var(--line);font-size:14px}.outlets li{padding:4px 0}.outlets a{color:var(--accent);text-decoration:none;min-height:32px;display:inline-flex;align-items:center}
.outlets small{color:var(--dim);margin-left:4px}button.more{border:1px solid var(--line);background:var(--chip);border-radius:999px;padding:2px 8px;font:inherit;font-size:12px;color:var(--ink);cursor:pointer;min-height:28px}
.filterbar{display:none;gap:8px;align-items:center;margin:8px 0;font-size:14px;color:var(--dim)}.filterbar.on{display:flex}
nav.chips{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 14px}
nav.chips a{font-size:13px;min-height:36px;display:inline-flex;align-items:center;padding:4px 12px;border:1px solid var(--line);border-radius:999px;color:var(--ink);text-decoration:none;background:var(--card)}
nav.chips a b{color:var(--dim);font-weight:500;margin-left:5px}
section{margin:0 0 26px}section h2{font-size:14px;letter-spacing:.05em;text-transform:uppercase;margin:0 0 10px;color:var(--dim)}
section.urgent h2{color:var(--urgent)}section[hidden]{display:none}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;flex-direction:column;position:relative}
.card[hidden]{display:none}.card img{width:100%;aspect-ratio:16/9;object-fit:cover;display:block;background:var(--chip)}
.card .body{padding:12px 14px 8px;display:flex;flex-direction:column;gap:6px;flex:1}
.card h3{font-size:16px;margin:0;line-height:1.35;font-weight:600}.card h3 a{color:var(--ink);text-decoration:none}.card h3 a:hover{text-decoration:underline}
.card p{margin:0;color:var(--dim);font-size:14px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.card .ailbl{font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--dim)}.card .notes{margin:0;padding-left:18px;font-size:14px;color:var(--ink)}.card .notes li{margin:2px 0}
body.nonotes .card .ailbl{display:none}
body.nonotes .card .notes,body.nonotes .card p,body.nonotes .card img{display:none}body.nonotes .card .body{padding:10px 14px 6px}
.card .hrow{display:flex;gap:10px;align-items:flex-start}.card .hrow h3{flex:1;min-width:0}
.card .tick{display:none;flex:0 0 44px;height:44px;align-items:center;justify-content:center;background:var(--chip);border:1px solid var(--line);border-radius:10px;cursor:pointer;margin-top:-4px}
.card .tick input{width:22px;height:22px;margin:0;accent-color:var(--accent);cursor:pointer}body.selecting .card .tick{display:flex}body.selecting .card:has(.tick input:checked){outline:3px solid var(--accent)}
.selbar{position:fixed;left:50%;bottom:16px;transform:translate(-50%,120%);transition:transform .2s;background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:0 6px 24px rgba(0,0,0,.25);padding:8px 12px;display:flex;flex-direction:column;gap:4px;align-items:center;z-index:12;width:min(440px,calc(100% - 32px));font-size:14px}
.selbar.on{transform:translate(-50%,0)}.selbar label{display:inline-flex;align-items:center;gap:6px;min-height:44px;cursor:pointer}.selbar input{width:20px;height:20px;accent-color:var(--accent)}
#top.lift{bottom:140px}.grid.list .card .tick{flex-basis:36px;height:36px;margin-top:0}.grid.list .card .notes{display:none}
.selbar .row{display:flex;gap:8px;align-items:center;justify-content:center;width:100%}.selbar a.btn{text-decoration:none;display:inline-flex;align-items:center}
.meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:auto;padding-top:6px;font-size:12px;color:var(--dim)}
.chip{background:var(--chip);border-radius:999px;padding:2px 8px;color:var(--ink)}
.chip.u{background:var(--urgent);color:#fff}.chip.src{border:1px solid var(--line);background:transparent}
.sent{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid var(--line)}
.sent.positive{color:var(--pos)}.sent.neutral{color:var(--neu)}.sent.critical{color:var(--crit)}
.acts{display:flex;gap:4px;align-items:center;border-top:1px solid var(--line);padding:4px 8px}
.acts button,.acts a.wa{min-width:44px;min-height:44px;border:0;background:transparent;font-size:18px;border-radius:10px;cursor:pointer;color:var(--ink);display:inline-flex;align-items:center;justify-content:center;text-decoration:none}
.acts button:hover,.acts a.wa:hover{background:var(--chip)}.acts .sp{flex:1}
.acts .why{display:none;flex-wrap:wrap;gap:6px}.acts.open .why{display:flex}.acts.open>button,.acts.open>a{display:none}
.acts .why button{font-size:13px;border:1px solid var(--line);padding:6px 10px;min-height:44px}.acts .done{font-size:13px;color:var(--ok);padding:0 6px}
.grid.list{display:block}.grid.list .card{flex-direction:row;align-items:center;border-radius:0;border-width:0 0 1px;background:transparent}
.grid.list .card img,.grid.list .card p{display:none}.grid.list .card .body{padding:8px 4px;gap:2px}
.grid.list .card h3{font-size:15px;font-weight:500}.grid.list .meta{padding-top:0}.grid.list .acts{border:0;padding:0 0 0 6px}
.grid.list .acts .why{position:absolute;right:8px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px;z-index:2}
dialog{border:1px solid var(--line);border-radius:14px;background:var(--card);color:var(--ink);max-width:520px;width:calc(100% - 32px);padding:18px}
dialog::backdrop{background:rgba(0,0,0,.45)}dialog label{display:block;font-size:14px;color:var(--dim);margin:10px 0 4px}
dialog input,dialog textarea{width:100%;min-height:44px;padding:8px 10px;font:inherit;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink)}
dialog .row{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}
.drawer{position:fixed;top:0;left:0;bottom:0;width:min(320px,86vw);background:var(--card);border-right:1px solid var(--line);transform:translateX(-105%);transition:transform .2s;z-index:20;padding:14px 16px;overflow-y:auto}
.drawer.open{transform:none}.scrim{position:fixed;inset:0;background:rgba(0,0,0,.4);display:none;z-index:15}.scrim.on{display:block}
.drawer h2{font-size:13px;letter-spacing:.05em;text-transform:uppercase;color:var(--dim);margin:14px 0 6px}
.drawer a,.drawer button.link{display:flex;align-items:center;min-height:44px;padding:0 8px;border-radius:10px;color:var(--ink);text-decoration:none;font:inherit;background:transparent;border:0;width:100%;text-align:left;cursor:pointer;font-size:15px}
.drawer a:hover,.drawer button.link:hover{background:var(--chip)}.drawer .close{position:absolute;top:10px;right:10px}
.drawer select{width:100%;min-height:44px;font:inherit;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink);padding:8px}
.drawer .seg{display:flex;gap:6px}.drawer .seg .btn{flex:1}.tools label.opt.sm{display:inline-flex;align-items:center;gap:6px;min-height:44px;font-size:14px;color:var(--dim);cursor:pointer}.tools label.opt.sm input{width:18px;height:18px;accent-color:var(--accent)}
.hits{margin:0;padding-left:0;list-style:none;font-size:14px}.hits li{padding:6px 0;border-top:1px solid var(--line)}.hits li:first-child{border-top:0}.hits a{color:var(--ink);text-decoration:none}.hits a:hover{color:var(--accent)}.hits small{color:var(--dim);margin-left:6px}
.month{margin:10px 0 4px;font-size:13px;letter-spacing:.05em;text-transform:uppercase;color:var(--dim)}.arow{display:flex;align-items:center;gap:8px;min-height:44px;border-top:1px solid var(--line)}.arow b{flex:1;font-weight:500}.arow button{font:inherit;font-size:13px;min-height:36px;border:1px solid var(--line);border-radius:999px;background:var(--card);color:var(--ink);padding:4px 10px;cursor:pointer}
.architems{max-height:60vh;overflow:auto;font-size:14px}.architems div{padding:6px 0;border-top:1px solid var(--line)}.architems a{color:var(--ink);text-decoration:none}.architems small{color:var(--dim)}.architems .dim{opacity:.6}.drawer label.opt{display:flex;align-items:center;gap:8px;min-height:44px;padding:0 8px;font-size:15px;cursor:pointer}.drawer label.opt input{width:20px;height:20px;accent-color:var(--accent)}
#top{position:fixed;right:16px;bottom:16px;display:none;z-index:10;box-shadow:0 4px 14px rgba(0,0,0,.2)}#top.on{display:inline-flex}
.toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:10px 16px;border-radius:999px;font-size:14px;z-index:30}
.empty{color:var(--dim);padding:20px 0}footer{color:var(--dim);font-size:13px;border-top:1px solid var(--line);padding-top:12px}
@media(max-width:600px){.grid{grid-template-columns:1fr}header h1{font-size:17px}.dash td,.dash th{padding:5px 4px}.mlab{display:none}.matrix{min-width:520px}}
"""

JS = r"""
(function(){
var DATA=JSON.parse(document.getElementById('data').textContent),EDS=JSON.parse(document.getElementById('editions').textContent);
var FB=document.documentElement.getAttribute('data-feedback')||'';
var CATS=[['urgent','🚨','Urgent'],['mention','🗣','Mentions'],['constituency','📍','Velachery'],['district','🏛','Thiruvallur'],['portfolio','💻','AI / IT / Digital'],['political','🏳','Political'],['opportunity','🎯','Opportunities'],['added','📌','Added by the office']];
var NAME={};CATS.forEach(function(c){NAME[c[0]]=c[2]});var SENT={positive:'🟢 Positive',neutral:'⚪ Neutral',critical:'🔴 Critical'};
var REASONS=[['unrelated','Unrelated to constituency / portfolio'],['category','Wrong category'],['old','Duplicate / old'],['spam','Spam / noise']];
var $=function(s,r){return (r||document).querySelector(s)},$$=function(s,r){return [].slice.call((r||document).querySelectorAll(s))};
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
var state={view:'cards',q:'',cat:'',sent:'',voted:{},notes:true,withNotes:true,selecting:false,sel:{}};
try{state.view=localStorage.getItem('view')||'cards';state.voted=JSON.parse(localStorage.getItem('voted')||'{}');state.notes=localStorage.getItem('notes')!=='off';state.withNotes=localStorage.getItem('withnotes')!=='off'}catch(e){}
function waText(s,withNotes){var t='*'+(s.urgent?'🚨 ':'')+s.title+'*\n';if(withNotes&&s.takeaways&&s.takeaways.length)t+='_AI summary_\n'+s.takeaways.map(function(b){return '• '+b}).join('\n')+'\n';return t+'🔗 '+s.url}
function waLink(text){return 'https://wa.me/?text='+encodeURIComponent(text)}
function story(id){return DATA.stories.filter(function(x){return String(x.id)===String(id)})[0]}
function group(s){return s.urgent?'urgent':s.cat}
function card(s){var g=group(s),notes=s.takeaways&&s.takeaways.length?'<div class="ailbl">✨ AI summary</div><ul class="notes">'+s.takeaways.map(function(b){return '<li>'+esc(b)+'</li>'}).join('')+'</ul>':(s.snippet?'<p>'+esc(s.snippet)+'</p>':'');
 var why=REASONS.map(function(r){return '<button type="button" data-r="'+r[0]+'">'+esc(r[1])+'</button>'}).join('');
 var acts=state.voted[s.id]?'<span class="done">Thanks for the feedback</span>':'<button type="button" class="up" aria-label="Useful">👍</button><button type="button" class="down" aria-label="Not useful">👎</button><div class="why">'+why+'</div>';
 return '<article class="card" id="story-'+s.id+'" data-id="'+s.id+'" data-cat="'+g+'" data-sent="'+s.sentiment+'" data-text="'+esc((s.title+' '+(s.snippet||'')+' '+s.outlet+' '+(s.reports||[]).map(function(r){return r.outlet}).join(' ')+' '+NAME[s.cat]+' '+SENT[s.sentiment]+(s.urgent?' urgent':'')).toLowerCase())+'">'+
 (s.image?'<a href="'+esc(s.url)+'" target="_blank" rel="noopener"><img src="'+esc(s.image)+'" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.parentNode.remove()"></a>':'')+
 '<div class="body"><div class="hrow"><label class="tick" aria-label="Select this story"><input type="checkbox"'+(state.sel[s.id]?' checked':'')+'></label><h3><a href="'+esc(s.url)+'" target="_blank" rel="noopener">'+esc(s.title)+'</a></h3></div>'+notes+
 '<div class="meta">'+(s.urgent?'<span class="chip u">URGENT</span>':'<span class="chip">'+esc(NAME[s.cat])+'</span>')+(s.manual&&s.cat!=='added'?'<span class="chip" title="A link the office handed in">📌 Added by the office</span>':'')+'<span class="sent '+s.sentiment+'">'+SENT[s.sentiment]+'</span><span class="chip src">'+esc(s.outlet)+'</span><span>'+esc(s.time)+'</span>'+(s.reports&&s.reports.length?'<button type="button" class="more" aria-expanded="false">+'+s.reports.length+' more outlet'+(s.reports.length>1?'s':'')+' ▾</button>':'')+'</div>'+
 (s.reports&&s.reports.length?'<ul class="outlets" hidden>'+s.reports.map(function(r){return '<li><a href="'+esc(r.url)+'" target="_blank" rel="noopener" title="'+esc(r.title)+'">'+esc(r.outlet)+' <small>('+esc(r.lang)+')</small> ↗</a></li>'}).join('')+'</ul>':'')+'</div>'+
 '<div class="acts">'+(FB?acts:'')+'<span class="sp"></span><a class="wa" href="#" target="_blank" rel="noopener" aria-label="Share on WhatsApp" title="Share on WhatsApp">📲</a></div></article>'}
function render(){var d=DATA,by={};d.stories.forEach(function(s){(by[group(s)]=by[group(s)]||[]).push(s)});
 $('#title').textContent=d.label+(d.key==='latest'?' · '+d.stories.length+' stories':'');
 $('#sub').textContent=d.stories.length+' stories · '+d.considered+' considered · updated '+d.generated_ist;
 var sum=$('#summary'),ICO={};CATS.forEach(function(c){ICO[c[0]]=c[1]});
 if(d.summary&&d.summary.length&&typeof d.summary[0]==='object'){sum.hidden=false;$('.sgs',sum).innerHTML=d.summary.map(function(g){return '<div class="sg'+(g.cat==='urgent'?' urgent':'')+'"><h3><a href="#sec-'+g.cat+'" data-jump="'+g.cat+'">'+(ICO[g.cat]||'')+' '+esc(NAME[g.cat]||g.cat)+' <span>›</span></a></h3><ul>'+g.bullets.map(function(b){var t=typeof b==='object'?b.text:b,id=typeof b==='object'?b.id:null;return '<li>'+(id?'<a href="#story-'+id+'" data-story="'+id+'">'+esc(t)+'</a>':esc(t))+'</li>'}).join('')+'</ul></div>'}).join('')}
 else if(d.summary&&d.summary.length){sum.hidden=false;$('.sgs',sum).innerHTML='<div class="sg"><ul>'+d.summary.map(function(b){return '<li>'+esc(b)+'</li>'}).join('')+'</ul></div>'}else{sum.hidden=true}
 $$('[data-jump]',sum).forEach(function(a){a.onclick=function(ev){ev.preventDefault();state.cat='';state.sent='';apply();var t=$('#sec-'+a.getAttribute('data-jump'));if(t)t.scrollIntoView({behavior:'smooth',block:'start'})}});
 $$('[data-story]',sum).forEach(function(a){a.onclick=function(ev){ev.preventDefault();jump(a.getAttribute('data-story'))}});
 var rows=CATS.filter(function(c){return by[c[0]]}).map(function(c){var l=by[c[0]],n={positive:0,neutral:0,critical:0};l.forEach(function(s){n[s.sentiment]++});
  return '<tr><td class="cat"><button type="button" data-cat="'+c[0]+'" data-sent="">'+c[1]+' '+esc(c[2])+'</button></td>'+['positive','neutral','critical'].map(function(k){return '<td class="'+(n[k]?'':'zero')+'"><button type="button" data-cat="'+c[0]+'" data-sent="'+k+'">'+n[k]+'</button></td>'}).join('')+'<td><button type="button" data-cat="'+c[0]+'" data-sent="">'+l.length+'</button></td></tr>'}).join('');
 var tot={positive:0,neutral:0,critical:0};d.stories.forEach(function(s){tot[s.sentiment]++});
 $('#dashbody').innerHTML=rows;$('#dashfoot').innerHTML='<tr><td>All</td>'+['positive','neutral','critical'].map(function(k){return '<td><button type="button" data-cat="" data-sent="'+k+'">'+tot[k]+'</button></td>'}).join('')+'<td><button type="button" data-cat="" data-sent="">'+d.stories.length+'</button></td></tr>';
 $('#chips').innerHTML=CATS.filter(function(c){return by[c[0]]}).map(function(c){return '<a href="#sec-'+c[0]+'">'+c[1]+' '+esc(c[2])+'<b>'+by[c[0]].length+'</b></a>'}).join('');
 $('#drawer-cats').innerHTML=CATS.filter(function(c){return by[c[0]]}).map(function(c){return '<a href="#sec-'+c[0]+'">'+c[1]+' '+esc(c[2])+' ('+by[c[0]].length+')</a>'}).join('');
 $('#content').innerHTML=CATS.filter(function(c){return by[c[0]]}).map(function(c){return '<section id="sec-'+c[0]+'" data-cat="'+c[0]+'"'+(c[0]==='urgent'?' class="urgent"':'')+'><h2>'+c[1]+' '+esc(c[2])+'</h2><div class="grid'+(state.view==='list'?' list':'')+'">'+by[c[0]].map(card).join('')+'</div></section>'}).join('')||'<p class="empty">Nothing kept for this edition.</p>';
 $$('#dash-today button').forEach(function(b){b.onclick=function(){var c=b.getAttribute('data-cat'),s=b.getAttribute('data-sent');if(state.cat===c&&state.sent===s){state.cat='';state.sent=''}else{state.cat=c;state.sent=s}apply();
  var t=$(c?'#sec-'+c:'#content');if(t)t.scrollIntoView({behavior:'smooth',block:'start'})}});
 wire();apply()}
function apply(){var q=state.q.toLowerCase().normalize('NFC').trim(),n=0;
 $$('.card').forEach(function(c){var ok=(!q||c.getAttribute('data-text').indexOf(q)>-1)&&(!state.cat||c.getAttribute('data-cat')===state.cat)&&(!state.sent||c.getAttribute('data-sent')===state.sent);c.hidden=!ok;if(ok)n++});
 $$('section[data-cat]').forEach(function(s){s.hidden=!$$('.card',s).some(function(c){return !c.hidden})});
 $('#nohit').hidden=n>0;$$('#dash-today button').forEach(function(b){b.classList.toggle('on',state.cat===b.getAttribute('data-cat')&&state.sent===b.getAttribute('data-sent')&&(state.cat||state.sent))});
 var fb=$('#filterbar');fb.classList.toggle('on',!!(state.cat||state.sent));$('#filterlabel').textContent=(state.cat?NAME[state.cat]:'All')+(state.sent?' · '+SENT[state.sent]:'')+' · '+n+' shown'}
function jump(id){var c=$('#story-'+id);if(!c)return;if(c.hidden){state.cat='';state.sent='';state.q='';$('#q').value='';apply()}
 c.scrollIntoView({behavior:'smooth',block:'center'});c.classList.remove('flash');void c.offsetWidth;c.classList.add('flash');setTimeout(function(){c.classList.remove('flash')},2600);try{history.replaceState(null,'',location.search+'#story-'+id)}catch(e){}}
function send(p){if(!FB)return;p.page=DATA.key;p.ua=navigator.userAgent.slice(0,120);fetch(FB,{method:'POST',mode:'no-cors',headers:{'Content-Type':'text/plain'},body:JSON.stringify(p)}).catch(function(){})}
function wire(){$$('.card a.wa').forEach(function(a){a.onclick=function(){var s=story(a.closest('.card').getAttribute('data-id'));if(s)a.href=waLink(waText(s,state.withNotes))}});
 $$('.card .tick input').forEach(function(i){i.onchange=function(){var id=i.closest('.card').getAttribute('data-id');if(i.checked)state.sel[id]=1;else delete state.sel[id];selbar()}});
 $$('.more').forEach(function(b){b.onclick=function(){var u=b.closest('.card').querySelector('.outlets'),o=u.hidden;u.hidden=!o;b.setAttribute('aria-expanded',o);b.textContent=b.textContent.replace(o?'▾':'▴',o?'▴':'▾')}});
 $$('.acts').forEach(function(f){var c=f.closest('.card'),id=c.getAttribute('data-id');function item(){var s=DATA.stories.filter(function(x){return String(x.id)===id})[0]||{};return {id:id,title:s.title,url:s.url,category:s.cat,outlet:s.outlet}}
 function done(m){$$('button,.why',f).forEach(function(e){e.remove()});f.insertAdjacentHTML('afterbegin','<span class="done">'+m+'</span>');state.voted[id]=1;try{localStorage.setItem('voted',JSON.stringify(state.voted))}catch(e){}}
 var up=$('.up',f),dn=$('.down',f);if(up)up.onclick=function(){var p=item();p.type='up';send(p);done('Thanks 👍')};if(dn)dn.onclick=function(){f.classList.add('open')};
 $$('.why button',f).forEach(function(b){b.onclick=function(){var p=item();p.type='down';p.reason=b.getAttribute('data-r');send(p);done('Noted 👎')}})})}
function setNotes(on){state.notes=on;document.body.classList.toggle('nonotes',!on);var b=$('#notes');b.setAttribute('aria-pressed',on);b.textContent=on?'📝 Notes: ON':'📝 Notes: OFF';try{localStorage.setItem('notes',on?'on':'off')}catch(e){}}
$('#notes').onclick=function(){setNotes(!state.notes)};
function setSelecting(on){state.selecting=on;document.body.classList.toggle('selecting',on);$('#select').setAttribute('aria-pressed',on);$('#select').textContent=on?'✓ Done':'☑ Select';if(!on){state.sel={};$$('.card .tick input').forEach(function(i){i.checked=false})}selbar()}
$('#select').onclick=function(){setSelecting(!state.selecting)};
function setWithNotes(on){state.withNotes=on;$$('.withnotes').forEach(function(i){i.checked=on});try{localStorage.setItem('withnotes',on?'on':'off')}catch(e){}}
$$('.withnotes').forEach(function(i){i.onchange=function(){setWithNotes(i.checked)}});
function selbar(){var ids=Object.keys(state.sel),n=ids.length,bar=$('#selbar');bar.classList.toggle('on',n>0);$('#selcount').textContent=n+(n===1?' story':' stories')+' selected';$('#top').classList.toggle('lift',n>0)}
$('#selclear').onclick=function(){state.sel={};$$('.card .tick input').forEach(function(i){i.checked=false});selbar()};
var KEY=['1️⃣','2️⃣','3️⃣','4️⃣','5️⃣','6️⃣','7️⃣','8️⃣','9️⃣','🔟'];
$('#selshare').onclick=function(ev){ev.preventDefault();var ids=Object.keys(state.sel);if(!ids.length)return;var list=DATA.stories.filter(function(s){return state.sel[s.id]});
 var t='📢 *Selected News Updates — Office of Minister R. Kumar*\n_'+DATA.label+' · '+DATA.generated_ist+'_\n\n'+list.map(function(s,i){return (KEY[i]||(i+1)+'.')+' '+waText(s,state.withNotes).replace('🔗 ','🔗 Source: ')}).join('\n\n');
 if(list.length>25)toast('That is a long message — WhatsApp may cut it');window.open(waLink(t),'_blank','noopener')};
function setView(v){state.view=v;$$('.grid').forEach(function(g){g.classList.toggle('list',v==='list')});$$('[data-view]').forEach(function(b){b.setAttribute('aria-pressed',b.getAttribute('data-view')===v)});try{localStorage.setItem('view',v)}catch(e){}}
$$('[data-view]').forEach(function(b){b.onclick=function(){setView(b.getAttribute('data-view'))}});
$('#q').addEventListener('input',function(){state.q=this.value;apply();allSearch()});
var ALL={},ARCH=null,qt=null;$('#qall').onchange=function(){allSearch()};
function loadEdition(e){if(ALL[e.key])return Promise.resolve(ALL[e.key]);return fetch(e.file,{cache:'no-store'}).then(function(r){return r.json()}).then(function(d){ALL[e.key]=d;return d})}
function allSearch(){var box=$('#allhits');clearTimeout(qt);var q=state.q.toLowerCase().normalize('NFC').trim();if(!$('#qall').checked||q.length<3){box.hidden=true;return}
 qt=setTimeout(function(){var eds=EDS.filter(function(e){return e.key!=='latest'});Promise.all(eds.map(function(e){return loadEdition(e).catch(function(){return null})})).then(function(ds){var hits=[];ds.forEach(function(d,i){if(!d)return;d.stories.forEach(function(s){var txt=(s.title+' '+(s.snippet||'')+' '+(s.takeaways||[]).join(' ')+' '+s.outlet).toLowerCase();if(txt.indexOf(q)>-1)hits.push({e:eds[i],s:s})})});
  if(hits.length>60)hits=hits.slice(0,60);$('#allhits-n').textContent=hits.length+(hits.length===60?'+ matches':' matches');box.hidden=false;
  $('#allhits-list').innerHTML=hits.map(function(h){return '<li><a href="#" data-e="'+esc(h.e.file)+'" data-s="'+h.s.id+'">'+esc(h.s.title)+'</a><small>'+esc(h.e.label)+' · '+esc(h.s.outlet)+'</small></li>'}).join('')||'<li>nothing in '+ds.filter(Boolean).length+' editions</li>';
  $$('#allhits-list a').forEach(function(a){a.onclick=function(ev){ev.preventDefault();var id=a.getAttribute('data-s');$('#q').value='';state.q='';box.hidden=true;drill(a.getAttribute('data-e'),'','');setTimeout(function(){jump(id)},400)}})})},250)}
function archive(){var dlg=$('#archive'),days=EDS.filter(function(e){return e.key!=='latest'});var months={},order=[];days.forEach(function(e){var m=e.key.slice(0,7);if(!months[m]){months[m]=[];order.push(m)}months[m].push(e)});
 var mname=function(m){var d=new Date(m+'-01T00:00:00');return d.toLocaleString('en-IN',{month:'long',year:'numeric'})};
 var arch={};(ARCH||[]).forEach(function(a){arch[a.day]=a});
 $('#archlist').innerHTML=order.map(function(m){return '<div class="month">'+esc(mname(m))+'</div>'+months[m].map(function(e){var a=arch[e.key];return '<div class="arow"><b>'+esc(e.label)+'</b><span class="sub">'+(e.total||0)+' stories</span><button type="button" data-open="'+esc(e.file)+'">Open</button>'+(a?'<button type="button" data-arch="'+esc(a.file)+'" data-day="'+esc(e.label)+'">all kept ('+a.stories+')</button>':'')+'</div>'}).join('')}).join('')||'<p class="empty">No editions yet.</p>';
 $$('#archlist [data-open]').forEach(function(b){b.onclick=function(){dlg.close();load(b.getAttribute('data-open'))}});
 $$('#archlist [data-arch]').forEach(function(b){b.onclick=function(){fetch(b.getAttribute('data-arch'),{cache:'no-store'}).then(function(r){return r.json()}).then(function(d){$('#archday-title').textContent='Everything kept — '+(d.label||b.getAttribute('data-day'));
  $('#archday-list').innerHTML=d.items.map(function(i){return '<div'+(i.shown?'':' class="dim"')+'><a href="'+esc(i.url)+'" target="_blank" rel="noopener">'+esc(i.title)+'</a><br><small>'+esc(NAME[i.cat]||i.cat)+' · '+esc(SENT[i.sentiment]||'')+' · '+esc(i.outlet)+' · '+esc(i.published)+(i.shown?'':' · not on the page')+'</small></div>'}).join('');dlg.close();$('#archday').showModal()}).catch(function(){toast('Could not load that day')})}});
 dlg.showModal()}
$('#open-archive').onclick=function(){openD(false);if(ARCH===null){fetch('archive/index.json',{cache:'no-store'}).then(function(r){return r.json()}).then(function(j){ARCH=j.days||[];archive()}).catch(function(){ARCH=[];archive()})}else archive()};
$('#close-archive').onclick=function(){$('#archive').close()};$('#close-archday').onclick=function(){$('#archday').close()};
$('#clear').onclick=function(){state.cat='';state.sent='';apply()};
function fillPickers(){var o=EDS.filter(function(e){return e.key==='latest'||e.pick!==false||e.key===DATA.key}).map(function(e){return '<option value="'+esc(e.file)+'"'+(e.key===DATA.key?' selected':'')+'>'+esc(e.label)+'</option>'}).join('');$$('select.pick').forEach(function(s){s.innerHTML=o})}
function load(file,after){if(!file)return;var e=EDS.filter(function(x){return x.file===file})[0];if(e&&e.key===DATA.key){if(after)after();return}
 fetch(file,{cache:'no-store'}).then(function(r){if(!r.ok)throw 0;return r.json()}).then(function(d){DATA=d;state.cat='';state.sent='';render();fillPickers();matrix();try{history.replaceState(null,'','?e='+encodeURIComponent(d.key))}catch(x){};if(after)after();else window.scrollTo({top:0,behavior:'smooth'})}).catch(function(){toast('Could not load that edition')})}
function drill(file,cat,sent){load(file,function(){state.cat=cat||'';state.sent=sent||'';apply();var t=$(cat?'#sec-'+cat:'#content');if(t)t.scrollIntoView({behavior:'smooth',block:'start'})})}
function matrix(){var days=EDS.filter(function(e){return e.key!=='latest'&&e.counts}).slice(0,state.mdays||7);$$('[data-mdays]').forEach(function(b){b.setAttribute('aria-pressed',+b.getAttribute('data-mdays')===(state.mdays||7))});var cats=CATS.filter(function(c){return days.some(function(d){return d.counts[c[0]]})});
 $('#mhead').innerHTML='<tr><th>Day</th>'+cats.map(function(c){return '<th title="'+esc(c[2])+'">'+c[1]+'<span class="mlab"> '+esc(c[2].split(' ')[0])+'</span></th>'}).join('')+'<th>All</th></tr>';
 function cell(file,cat,n,cls){var t=n.positive+n.neutral+n.critical;if(!t)return '<td><span class="cell zero"><span class="n">·</span></span></td>';
  var w=function(k){return (100*n[k]/t)+'%'},badge=function(k,c,ic){return n[k]?'<button type="button" class="bd" data-file="'+esc(file)+'" data-cat="'+cat+'" data-sent="'+k+'" aria-label="'+n[k]+' '+k+'"><b class="'+c+'">'+ic+' '+n[k]+'</b></button>':''};
  return '<td><div class="cell'+(cls||'')+'"><button type="button" class="n" data-file="'+esc(file)+'" data-cat="'+cat+'" data-sent="" aria-label="'+t+' stories, all sentiments">'+t+'<span class="bar"><i class="p" style="width:'+w('positive')+'"></i><i class="u" style="width:'+w('neutral')+'"></i><i class="c" style="width:'+w('critical')+'"></i></span></button><span class="badges">'+badge('positive','p','🟢')+badge('neutral','u','⚪')+badge('critical','c','🔴')+'</span></div></td>'}
 $('#mbody').innerHTML=days.map(function(d){var a={positive:0,neutral:0,critical:0};cats.forEach(function(c){var n=d.counts[c[0]];if(n){a.positive+=n.positive||0;a.neutral+=n.neutral||0;a.critical+=n.critical||0}});
  return '<tr><td class="day'+(d.key===DATA.key?' on':'')+'"><button type="button" class="n day" data-file="'+esc(d.file)+'" data-cat="" data-sent="" aria-label="Open '+esc(d.label)+'">'+esc(d.label)+'</button></td>'+cats.map(function(c){return cell(d.file,c[0],d.counts[c[0]]||{positive:0,neutral:0,critical:0},'')}).join('')+cell(d.file,'',a,' all')+'</tr>'}).join('')||'<tr><td colspan="9" class="empty">No past editions yet — run the backfill.</td></tr>';
 $$('#mbody button[data-file]').forEach(function(b){b.onclick=function(){drill(b.getAttribute('data-file'),b.getAttribute('data-cat'),b.getAttribute('data-sent'))}})}
$$('[data-mdays]').forEach(function(b){b.onclick=function(){state.mdays=+b.getAttribute('data-mdays');matrix();try{localStorage.setItem('mdays',state.mdays)}catch(e){}}});
try{state.mdays=+localStorage.getItem('mdays')||7}catch(e){}
$$('[data-dash]').forEach(function(b){b.onclick=function(){var w=b.getAttribute('data-dash')==='week';$('#dash-today').hidden=w;$('#dash-week').hidden=!w;$('#dashtitle').textContent=w?'Last 7 days':'Today at a glance';$$('[data-dash]').forEach(function(x){x.setAttribute('aria-pressed',x===b)});if(w)matrix();try{localStorage.setItem('dash',w?'week':'today')}catch(e){}}});
try{if(localStorage.getItem('dash')==='week')$('[data-dash=week]').click()}catch(e){}
$$('select.pick').forEach(function(s){s.onchange=function(){load(this.value)}});
fetch('data/index.json',{cache:'no-store'}).then(function(r){return r.json()}).then(function(j){EDS=j.editions;fillPickers();matrix();var m=location.search.match(/[?&]e=([\w-]+)/);if(m&&m[1]!==DATA.key){var e=EDS.filter(function(x){return x.key===m[1]})[0];if(e)load(e.file)}}).catch(function(){});
var drawer=$('#drawer'),scrim=$('#scrim');function openD(o){drawer.classList.toggle('open',o);scrim.classList.toggle('on',o)}
$('#menu').onclick=function(){openD(true)};$('#closed').onclick=function(){openD(false)};scrim.onclick=function(){openD(false)};
drawer.addEventListener('click',function(ev){if(ev.target.closest('a'))openD(false)});
var top=$('#top');window.addEventListener('scroll',function(){top.classList.toggle('on',window.scrollY>600)},{passive:true});top.onclick=function(){window.scrollTo({top:0,behavior:'smooth'})};
var dlg=$('#missing');$('#open-missing').onclick=function(){openD(false);dlg.showModal()};$('#cancel-missing').onclick=function(){dlg.close()};
$('#send-missing').onclick=function(ev){ev.preventDefault();var u=$('#m-url').value.trim(),n=$('#m-notes').value.trim();if(!u&&!n)return;send({type:'missing',url:u,notes:n.slice(0,500)});dlg.close();$('#m-url').value='';$('#m-notes').value='';toast('Thank you — sent for review.')};
function toast(m){var t=document.createElement('div');t.className='toast';t.textContent=m;document.body.appendChild(t);setTimeout(function(){t.remove()},3500)}
if(!FB){$('#open-missing').hidden=true}
fillPickers();render();matrix();setView(state.view);setNotes(state.notes);setWithNotes(state.withNotes);
var h=location.hash.match(/^#story-(\d+)$/);if(h)setTimeout(function(){jump(h[1])},300);
})();
"""

ADMIN_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><meta name="robots" content="noindex, nofollow"><meta name="referrer" content="no-referrer">
<title>System status</title><style>__CSS__
.lock{max-width:420px;margin:60px auto;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px}
.lock input{width:100%;min-height:48px;font:inherit;font-size:18px;letter-spacing:.2em;padding:8px 12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink);margin:10px 0}
.lock .err{color:var(--crit);min-height:20px;font-size:14px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;margin:12px 0}
.kcard{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}.kcard h2{font-size:13px;letter-spacing:.05em;text-transform:uppercase;color:var(--dim);margin:0 0 8px}
.big{font-size:28px;font-weight:700;line-height:1.1}.kcard .row{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-top:1px solid var(--line);font-size:14px}.kcard .row:first-of-type{border-top:0}.kcard .row[hidden]{display:none}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-size:13px;font-weight:600}.pill.active{background:#dcf5ea;color:var(--pos)}.pill.throttled{background:#fdecc8;color:#8a5a00}.pill.standby{background:var(--chip);color:var(--dim)}
.meter{height:8px;background:var(--chip);border-radius:4px;overflow:hidden;margin:8px 0}.meter i{display:block;height:100%;background:var(--accent)}
.runs .row span:first-child{flex:1}.runs a{color:var(--accent);text-decoration:none}.ok{color:var(--pos)}.bad{color:var(--crit)}.run{color:var(--dim)}
.refused{font-size:13px;color:var(--dim)}.refused li{margin:4px 0}
</style></head><body><div class="wrap">
<header><div style="flex:1"><h1 id="title">System status</h1><div class="sub" id="sub">private page</div></div><a class="btn" href="index.html">← Brief</a></header>
<div class="lock" id="lock"><h2 style="margin:0 0 4px">Enter the PIN</h2><p class="sub" style="margin:0">Nothing loads until the PIN is right.</p>
<form id="f"><input id="pin" type="password" inputmode="text" autocomplete="current-password" aria-label="PIN" autofocus><div class="err" id="err"></div><button type="submit" class="btn primary" style="width:100%">Unlock</button></form></div>
<div id="panel" hidden>
<div class="cards">
<div class="kcard"><h2>Gemini</h2><div class="big"><span id="g-calls">–</span> <small style="font-size:14px;color:var(--dim)">of <span id="g-limit">80</span> calls today</small></div><div class="meter"><i id="g-bar" style="width:0"></i></div>
<div class="row"><span>Status</span><span class="pill" id="g-state">–</span></div><div class="row"><span>Model</span><span id="g-model">–</span></div><div class="row" id="g-note-row" hidden><span>Note</span><span id="g-note"></span></div></div>
<div class="kcard"><h2>Collector</h2><div class="row"><span>Last poll</span><span id="c-last">–</span></div><div class="row"><span>Items in, 24 h</span><span id="c-items">–</span></div><div class="row"><span>Kept / dropped</span><span id="c-kept">–</span></div>
<div class="row"><span>Waiting: resolve / extract / score</span><span id="c-pending">–</span></div><div class="row"><span>YouTube channels returning nothing</span><span id="c-yt">–</span></div></div>
<div class="kcard"><h2>Last brief</h2><div class="row"><span>Sent</span><span id="b-sent">–</span></div><div class="row"><span>Stories</span><span id="b-chosen">–</span></div><div class="row"><span>Status</span><span id="b-status">–</span></div><div class="row"><span>Latest edition</span><span id="e-latest">–</span></div><div class="row"><span>Dated editions / archive days</span><span id="e-days">–</span></div></div>
<div class="kcard"><h2>Grouping (this run)</h2><div class="row"><span>Story groups kept</span><span id="r-clusters">–</span></div><div class="row"><span>Re-runs dropped</span><span id="r-reruns">–</span></div><div id="r-guards"></div><ul class="refused" id="r-refused"></ul></div>
<div class="kcard"><h2>Reader feedback</h2><div id="fb"></div></div>
<div class="kcard runs" id="runs-card"><h2>Recent runs <small id="runs-note" style="font-weight:400;text-transform:none;letter-spacing:0"></small></h2><div id="runs">–</div></div>
</div><p class="hint" id="gen"></p></div>
<script id="sealed" type="application/json">__SEALED__</script>
<script>
(function(){
var S=JSON.parse(document.getElementById('sealed').textContent),$=function(s){return document.querySelector(s)};
function b64(s){var b=atob(s),a=new Uint8Array(b.length);for(var i=0;i<b.length;i++)a[i]=b.charCodeAt(i);return a}
function cat(a,b){var o=new Uint8Array(a.length+b.length);o.set(a,0);o.set(b,a.length);return o}
function be32(i){return new Uint8Array([(i>>>24)&255,(i>>>16)&255,(i>>>8)&255,i&255])}
function eq(a,b){if(a.length!==b.length)return false;var d=0;for(var i=0;i<a.length;i++)d|=a[i]^b[i];return d===0}
async function open(pin){
 if(!S)return null;var enc=new TextEncoder();
 var base=await crypto.subtle.importKey('raw',enc.encode(pin),'PBKDF2',false,['deriveBits']);
 var key=new Uint8Array(await crypto.subtle.deriveBits({name:'PBKDF2',hash:'SHA-256',salt:b64(S.salt),iterations:S.iters},base,512));
 var hk=function(k){return crypto.subtle.importKey('raw',k,{name:'HMAC',hash:'SHA-256'},false,['sign'])};
 var encKey=await hk(key.slice(0,32)),macKey=await hk(key.slice(32,64)),nonce=b64(S.nonce),ct=b64(S.ct);
 var mac=new Uint8Array(await crypto.subtle.sign('HMAC',macKey,cat(nonce,ct)));
 if(!eq(mac,b64(S.tag)))return null;
 var out=new Uint8Array(ct.length);
 for(var i=0;i*32<ct.length;i++){var blk=new Uint8Array(await crypto.subtle.sign('HMAC',encKey,cat(nonce,be32(i))));for(var j=0;j<32&&i*32+j<ct.length;j++)out[i*32+j]=ct[i*32+j]^blk[j]}
 return JSON.parse(new TextDecoder().decode(out))}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function show(st){var g=st.gemini||{},c=st.collector||{},b=st.brief||{},e=st.editions||{},r=st.grouping||{},fb=st.feedback||{};
 $('#g-calls').textContent=g.calls_today;$('#g-limit').textContent=g.limit;$('#g-bar').style.width=Math.min(100,100*g.calls_today/(g.limit||80))+'%';
 var p=$('#g-state');p.textContent=(g.state||'').replace(/^./,function(x){return x.toUpperCase()});p.className='pill '+g.state;$('#g-model').textContent=g.model||'';
 if(g.note){$('#g-note-row').hidden=false;$('#g-note').textContent=g.note}
 $('#c-last').textContent=c.last_run||'never';$('#c-items').textContent=c.items_24h;$('#c-kept').textContent=c.kept_24h+' / '+c.dropped_24h;
 $('#c-pending').textContent=c.pending_resolve+' / '+c.pending_extract+' / '+c.unscored;$('#c-yt').textContent=(c.youtube_failing||[]).length?(c.youtube_failing||[]).join(', '):'none';
 $('#b-sent').textContent=b.sent||'–';$('#b-chosen').textContent=b.chosen==null?'–':b.chosen;$('#b-status').textContent=b.status||'–';$('#e-latest').textContent=e.stories_latest+' stories';$('#e-days').textContent=e.dated+' / '+e.archive_days;
 $('#r-clusters').textContent=r.clusters;$('#r-reruns').textContent=r.reruns;var gs=r.guards||{},keys=Object.keys(gs);
 $('#r-guards').innerHTML=keys.length?keys.map(function(k){return '<div class="row"><span>Re-filed: '+esc(k)+'</span><span>'+gs[k]+'</span></div>'}).join(''):'<div class="row"><span>Verdicts re-filed by the guards</span><span>0</span></div>';
 $('#r-refused').innerHTML=(r.refused||[]).map(function(x){return '<li><b>'+esc(x.why)+'</b> ('+esc(x.edition)+') — shared: '+esc(x.shared||'–')+'<br>'+(x.titles||[]).map(esc).join(' · ')+'</li>'}).join('');
 if(fb.connected===false)$('#fb').innerHTML='<div class="row"><span>Feedback tab</span><span>not connected (FEEDBACK_CSV_URL)</span></div>';
 else if(fb.error)$('#fb').innerHTML='<div class="row"><span>Feedback tab</span><span class="bad">'+esc(fb.error)+'</span></div>';
 else $('#fb').innerHTML=['up','down','missing'].map(function(k){var l={up:'👍 Useful',down:'👎 Not useful',missing:'➕ Missing news'}[k];return '<div class="row"><span>'+l+'</span><span>'+fb.all[k]+' all time · '+fb.week[k]+' this week</span></div>'}).join('');
 $('#gen').textContent='Snapshot taken '+(st.generated_ist||'')+' at the last brief. Runs below are live.'}
async function runs(cfg){if(!cfg.runs){$('#runs-card').hidden=true;return}
 try{var r=await fetch(cfg.runs,{headers:{'Accept':cfg.accept||'application/json'}});if(!r.ok)throw new Error('HTTP '+r.status);var j=await r.json();
  $('#runs').innerHTML=(j.workflow_runs||[]).map(function(x){var ok=x.conclusion==='success',live=x.status!=='completed';var when=new Date(x.updated_at).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'});
   return '<div class="row"><span>'+(live?'<span class="run">⏳</span>':ok?'<span class="ok">✅</span>':'<span class="bad">❌</span>')+' '+esc(x.name)+'</span><span>'+esc(when)+' · <a href="'+esc(cfg.web+x.id)+'" target="_blank" rel="noopener">log ↗</a></span></div>'}).join('')||'no runs yet';
  $('#runs-note').textContent='· live'}catch(e){$('#runs').innerHTML='<span class="bad">could not reach the run list ('+esc(e.message)+')</span>'}}
async function unlocked(cfg){$('#lock').hidden=true;$('#panel').hidden=false;
 try{var st=await (await fetch(cfg.status,{cache:'no-store'})).json();show(st)}catch(e){$('#gen').textContent='status snapshot not available yet — it is written at each brief'}
 runs(cfg)}
if(!S){$('#f').hidden=true;$('#err').textContent='Not configured: set the ADMIN_PIN secret and run a brief.'}
try{var saved=sessionStorage.getItem('admin');if(saved&&S){unlocked(JSON.parse(saved))}}catch(e){}
$('#f').onsubmit=async function(ev){ev.preventDefault();$('#err').textContent='';var pin=$('#pin').value;if(!pin)return;
 try{var cfg=await open(pin);if(!cfg){$('#err').textContent='Wrong PIN';$('#pin').value='';return}
  try{sessionStorage.setItem('admin',JSON.stringify(cfg))}catch(e){}unlocked(cfg)}catch(e){$('#err').textContent='This browser cannot unlock the page ('+e.message+')'}};
})();
</script></body></html>"""

PAGE = """<!DOCTYPE html><html lang="en" data-feedback="__FEEDBACK__"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<meta name="robots" content="noindex"><meta name="referrer" content="no-referrer">
<title>TN news brief</title><style>__CSS__</style></head><body>
<div class="scrim" id="scrim"></div>
<aside class="drawer" id="drawer" aria-label="Menu"><button type="button" class="icon close" id="closed" aria-label="Close">✕</button>
<h2>Edition</h2><select class="pick" aria-label="Edition"></select>
<h2>Sections</h2><a href="#dashboard">📊 Dashboard</a><div id="drawer-cats"></div>
<h2>View</h2><div class="seg"><button type="button" class="btn" data-view="cards">Cards</button><button type="button" class="btn" data-view="list">List</button></div>
<h2>Sharing</h2><label class="opt"><input type="checkbox" class="withnotes" checked> Include takeaways in WhatsApp shares</label>
<h2>More</h2><button type="button" class="link" id="open-archive">🗄 Archive — every edition</button><button type="button" class="link" id="open-missing">＋ Submit missing news</button></aside>
<div class="wrap">
<header><button type="button" class="icon" id="menu" aria-label="Menu">☰</button><div style="flex:1;min-width:0"><h1 id="title"></h1><div class="sub" id="sub"></div></div></header>
<div class="tools"><input type="search" id="q" placeholder="Search headlines, summaries, outlets, categories" aria-label="Search"><label class="opt sm" title="Search every edition ever published, not only this one"><input type="checkbox" id="qall"> All editions</label>
<select class="pick" aria-label="Edition"></select><button type="button" class="btn" data-view="cards">Cards</button><button type="button" class="btn" data-view="list">List</button><button type="button" class="btn" id="notes" aria-pressed="true">📝 Notes: ON</button><button type="button" class="btn" id="select" aria-pressed="false">☑ Select</button></div>
<div class="callout" id="summary" hidden><h2>✨ 60-second AI briefing · tap a line to open its story</h2><div class="sgs"></div></div>
<div class="dash" id="dashboard"><h2><span id="dashtitle">Today at a glance</span><span class="seg"><button type="button" class="btn sm" data-dash="today" aria-pressed="true">Today</button><button type="button" class="btn sm" data-dash="week" aria-pressed="false">7 days</button></span></h2>
<div id="dash-today"><table><thead><tr><th>Category</th><th>🟢 Positive</th><th>⚪ Neutral</th><th>🔴 Critical</th><th>All</th></tr></thead><tbody id="dashbody"></tbody><tfoot id="dashfoot"></tfoot></table><p class="hint">Tap a number to filter the stories below.</p></div>
<div id="dash-week" hidden><div class="seg" style="margin:0 0 8px"><button type="button" class="btn sm" data-mdays="7" aria-pressed="true">7 days</button><button type="button" class="btn sm" data-mdays="30" aria-pressed="false">30 days</button></div><div class="mwrap"><table class="matrix"><thead id="mhead"></thead><tbody id="mbody"></tbody></table></div><p class="hint">Each cell: 🟢 positive · ⚪ neutral · 🔴 critical. Tap a count to open that day and section; tap 🟢 ⚪ 🔴 for one sentiment only; tap the day to open all of it.</p></div></div>
<div class="filterbar" id="filterbar"><span id="filterlabel"></span><button type="button" class="btn" id="clear">Show all</button></div>
<div class="callout" id="allhits" hidden><h2>Across all editions · <span id="allhits-n"></span></h2><ul class="hits" id="allhits-list"></ul></div>
<nav class="chips" id="chips"></nav>
<div id="content"></div><p class="empty" id="nohit" hidden>No stories match.</p>
<dialog id="archive"><h3 style="margin:0 0 6px">Archive</h3><p class="sub" style="margin:0 0 10px">Every edition, by month. Tap a day to open it; "all kept" lists everything the rules kept that day, shown or not.</p><div id="archlist"></div><div class="row"><button type="button" class="btn" id="close-archive">Close</button></div></dialog>
<dialog id="archday"><h3 id="archday-title" style="margin:0 0 6px"></h3><div id="archday-list" class="architems"></div><div class="row"><button type="button" class="btn" id="close-archday">Close</button></div></dialog>
<dialog id="missing"><form method="dialog"><h3 style="margin:0">Report a missing story</h3>
<label for="m-url">Link to the article or video</label><input id="m-url" type="url" placeholder="https://">
<label for="m-notes">What is it about, and why does it matter?</label><textarea id="m-notes" rows="3" maxlength="500"></textarea>
<div class="row"><button type="button" class="btn" id="cancel-missing">Cancel</button><button type="submit" class="btn primary" id="send-missing">Send</button></div></form></dialog>
<footer>Links open the original article or video. Feedback goes to the editor for review.</footer></div>
<div class="selbar" id="selbar" role="region" aria-label="Selected stories"><div class="row"><b id="selcount">0 selected</b><label><input type="checkbox" class="withnotes" checked> Include takeaways</label></div><div class="row"><a class="btn primary" id="selshare" href="#" target="_blank" rel="noopener">Share to WhatsApp ↗</a><button type="button" class="btn" id="selclear">Clear</button></div></div>
<button type="button" class="icon" id="top" aria-label="Back to top">↑</button>
<noscript><p style="padding:16px">This page needs JavaScript to show the stories.</p></noscript>
<script id="data" type="application/json">__DATA__</script>
<script id="editions" type="application/json">__EDITIONS__</script>
<script>__JS__</script></body></html>"""
