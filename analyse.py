#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 0 corpus analysis.

Reads corpus.db and answers the four questions we deferred:
  1. is token dedup working, and how much cross-outlet duplication is there?
  2. which queries are pulling noise, and from where?
  3. which publishers need a custom selector, and which are just short?
  4. is title similarity enough for dedup, or do we need embeddings?

    python analyse.py            full report
    python analyse.py --label    also write label_me.csv for hand labelling

Read-only. Safe to run while the collector is going.
"""

import re
import csv
import sys
import sqlite3
import argparse
import unicodedata
from pathlib import Path
from collections import defaultdict, Counter

HERE = Path(__file__).resolve().parent
DB = HERE / "corpus.db"

# Similarity thresholds to probe. We are looking for the value where true
# duplicates are caught and distinct stories are not.
THRESHOLDS = (0.45, 0.55, 0.65, 0.75)
NGRAM = 4                      # character n-grams: works for Tamil AND English
DUP_WINDOW_HOURS = 36


def con():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def norm(s):
    """Normalise for comparison only. Never write this back to the db."""
    s = unicodedata.normalize("NFC", s or "")
    s = s.replace("\u200c", "").replace("\u200d", "")      # ZWNJ / ZWJ
    s = re.sub(r"[^\w\u0B80-\u0BFF]+", " ", s.lower())      # keep Tamil block
    return re.sub(r"\s+", " ", s).strip()


def grams(s, n=NGRAM):
    s = norm(s)
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


def jaccard(a, b):
    if not a or not b:
        return 0.0
    i = len(a & b)
    return i / (len(a) + len(b) - i)


def hr(t):
    print(f"\n{'=' * 76}\n  {t}\n{'=' * 76}")


# --------------------------------------------------------------------------

def dedup_evidence(c):
    hr("1. TOKEN DEDUP -- is the UNIQUE constraint doing its job?")

    # Total rejections are dominated by re-polling: every poll re-delivers the
    # whole feed. The only meaningful number is the FIRST poll of each source,
    # where a rejection means two different queries found the same story.
    firsts = c.execute("""SELECT source_id, min(id) fid FROM poll_runs
                          WHERE entries > 0 GROUP BY source_id""").fetchall()
    ids = [r["fid"] for r in firsts]
    if ids:
        q = ",".join("?" * len(ids))
        r = c.execute(f"SELECT sum(entries) f, sum(new_items) s "
                      f"FROM poll_runs WHERE id IN ({q})", ids).fetchone()
        f, s = r["f"] or 0, r["s"] or 0
        print(f"  FIRST poll of each source: {f} delivered, {s} stored, "
              f"{f - s} rejected ({100*(f-s)/f if f else 0:.1f}%)")
        print("  That rejection rate is genuine cross-query overlap. Low means your")
        print("  queries are pulling different stories and each is earning its slot.\n")

    r = c.execute("SELECT sum(entries) f, sum(new_items) s FROM poll_runs "
                  "WHERE entries IS NOT NULL").fetchone()
    f, s = r["f"] or 0, r["s"] or 0
    print(f"  All polls combined: {f} delivered, {s} stored")
    print("  Ignore this ratio -- it just counts how often you re-read the same feed.")

    n304 = c.execute("SELECT count(*) c FROM poll_runs WHERE not_modified=1").fetchone()["c"]
    npoll = c.execute("SELECT count(*) c FROM poll_runs WHERE http_status IS NOT NULL").fetchone()["c"]
    print(f"\n  conditional GET: {n304}/{npoll} polls answered 304")
    if npoll and not n304:
        print("  -> these servers send no ETag/Last-Modified. The machinery is inert;")
        print("     you re-download every feed in full each poll. Wasteful, not harmful.")


def query_landscape(c):
    hr("2. WHERE EACH QUERY ACTUALLY LANDS")
    print("  If a query's top hosts are trade press or exam sites, the query is the")
    print("  problem -- not the filter you were planning to write.\n")

    by_src = defaultdict(Counter)
    for r in c.execute("""SELECT source_id, extract_host h FROM items
                          WHERE extract_host IS NOT NULL AND extract_host<>''"""):
        by_src[r["source_id"]][r["h"]] += 1

    host_srcs = defaultdict(set)
    for src, hosts in by_src.items():
        for h in hosts:
            host_srcs[h].add(src)

    for src in sorted(by_src):
        tot = sum(by_src[src].values())
        print(f"  {src}  ({tot} items)")
        for h, n in by_src[src].most_common(8):
            excl = " [only this query]" if len(host_srcs[h]) == 1 else ""
            print(f"      {n:>4}  {h}{excl}")
        print()


def extraction_health(c):
    hr("3. EXTRACTION -- selector work vs genuinely short articles")
    print("  A host whose THIN results are all the SAME length is returning a fixed")
    print("  stub (paywall or JS shell). A host with varied short lengths just writes")
    print("  short pieces. Only the first kind is worth fixing.\n")

    rows = c.execute("""SELECT extract_host h, extract_status s, extract_chars ch
                        FROM items
                        WHERE extract_host IS NOT NULL AND extract_host<>''
                          AND extract_status NOT IN ('SKIPPED_VIDEO','SKIPPED_SOCIAL')
                     """).fetchall()
    agg = defaultdict(lambda: {"n": 0, "ok": 0, "bad": [], "fail": 0})
    for r in rows:
        a = agg[r["h"]]
        a["n"] += 1
        if r["s"] == "OK":
            a["ok"] += 1
        elif r["s"] == "THIN":
            a["bad"].append(r["ch"] or 0)
        else:
            a["fail"] += 1

    worst = sorted((v["ok"] / v["n"], h, v) for h, v in agg.items() if v["n"] >= 2)
    shown = 0
    for rate, h, v in worst:
        if rate >= 0.95:
            continue
        shown += 1
        lens = sorted(set(v["bad"]))
        if rate < 0.5:
            # Low success rate is broken, full stop. Varying stub lengths still
            # mean a JS shell -- the variance just comes from differing boilerplate.
            diag = "BROKEN -> custom selector, or drop the source"
        elif v["fail"] and not v["bad"]:
            diag = "fetch fails outright -> check if it blocks non-browser clients"
        elif len(lens) == 1 and v["bad"]:
            diag = f"fixed stub at {lens[0]}ch -> paywall or JS shell"
        elif lens:
            diag = f"mostly fine; short ones at {lens[:4]} are probably real briefs"
        else:
            diag = ""
        print(f"  {h:<32} {v['n']:>3} items  {100*rate:>5.1f}% ok   {diag}")
    if not shown:
        print("  nothing below 95%. No selector work needed yet.")


def duplication(c):
    hr("4. CROSS-OUTLET DUPLICATION -- do we need embeddings, or is title enough?")
    rows = c.execute("""SELECT id, title, extract_host h, source_id,
                               coalesce(published_at, discovered_at) t
                        FROM items WHERE title<>'' ORDER BY t""").fetchall()
    n = len(rows)
    print(f"  comparing {n} titles, char-{NGRAM}gram Jaccard, {DUP_WINDOW_HOURS}h window")
    print(f"  ({n*(n-1)//2:,} pairs)\n")

    g = [grams(r["title"]) for r in rows]
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            s = jaccard(g[i], g[j])
            if s >= min(THRESHOLDS):
                pairs.append((s, i, j))
    pairs.sort(reverse=True)

    for th in THRESHOLDS:
        hit = [p for p in pairs if p[0] >= th]
        seen, sup = set(), 0
        for _, i, j in hit:
            if j not in seen:
                seen.add(j)
                sup += 1
        cross = sum(1 for _, i, j in hit if rows[i]["h"] != rows[j]["h"])
        print(f"  threshold {th:.2f}: {len(hit):>4} pairs, {cross:>4} cross-outlet, "
              f"{sup:>3} items would be suppressed ({100*sup/n:.1f}% of corpus)")

    print("\n  Closest pairs -- eyeball these. Same story, or different stories?\n")
    for s, i, j in pairs[:12]:
        a, b = rows[i], rows[j]
        tag = "CROSS" if a["h"] != b["h"] else "same "
        print(f"  {s:.2f} {tag}  [{a['h']}] {a['title'][:58]}")
        print(f"              [{b['h']}] {b['title'][:58]}\n")

    if not pairs:
        print("  no pairs above the lowest threshold -- little duplication in this sample")


def canon_url(url):
    """Collapse the cosmetic variants Google hands out for one article."""
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


def same_article_dupes(c):
    hr("5. SAME-ARTICLE DUPLICATES -- the hole in token dedup")
    print("  item_key is the Google News token. Google issues DIFFERENT tokens for")
    print("  the same article (different query, different time, amp vs canonical),")
    print("  so one story can land twice. Collapsing on the resolved URL finds them.\n")

    rows = c.execute("""SELECT id, resolved_url, title, source_id, extract_host h
                        FROM items WHERE resolve_status='RESOLVED'
                          AND resolved_url IS NOT NULL""").fetchall()
    groups = defaultdict(list)
    for r in rows:
        k = canon_url(r["resolved_url"])
        if k:
            groups[k].append(r)

    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    extra = sum(len(v) - 1 for v in dupes.values())
    print(f"  {len(rows)} resolved items -> {len(groups)} distinct articles")
    print(f"  {len(dupes)} articles arrived more than once, {extra} redundant rows "
          f"({100*extra/len(rows) if rows else 0:.1f}%)\n")

    if dupes:
        cross_q = sum(1 for v in dupes.values() if len({x['source_id'] for x in v}) > 1)
        print(f"  of those, {cross_q} were found by more than one query "
              f"(the rest are the same query re-tokenising)\n")
        print("  Worst offenders:")
        for h, n in Counter(x["h"] for v in dupes.values() for x in v[1:]).most_common(8):
            print(f"     {n:>3}  {h}")
        print("\n  Examples:")
        for k, v in sorted(dupes.items(), key=lambda kv: -len(kv[1]))[:4]:
            print(f"     x{len(v)}  {k[:70]}")
            print(f"          {v[0]['title'][:66]}")
    print("\n  -> This is cheap to fix and catches real duplicates. Add a canonical_key")
    print("     column, populate it at resolve time, and dedup on it in Phase 1.")


def body_duplication(c):
    hr("6. SAME EVENT, DIFFERENT OUTLET -- what titles cannot see")
    print("  Tamil outlets rewrite headlines heavily, so title similarity is blind to")
    print("  'eight papers report one TVK event'. Article BODIES share the names,")
    print("  places, numbers and quotes, so they stay similar even when headlines do not.")
    print("  This compares bodies across DIFFERENT hosts only.\n")

    rows = c.execute("""SELECT id, title, extract_host h, source_id, extract_text t,
                               coalesce(published_at, discovered_at) ts
                        FROM items
                        WHERE extract_status='OK' AND extract_text IS NOT NULL
                          AND length(extract_text) >= 400
                        ORDER BY ts""").fetchall()
    n = len(rows)
    if n < 2:
        print("  not enough extracted bodies yet")
        return

    BODY_CAP = 1200          # first N chars: the lede carries the who/what/where
    g = [grams(r["t"][:BODY_CAP]) for r in rows]
    print(f"  {n} extracted bodies, first {BODY_CAP} chars each\n")

    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            if rows[i]["h"] == rows[j]["h"]:
                continue                       # same outlet is a different problem
            s = jaccard(g[i], g[j])
            if s >= 0.25:
                pairs.append((s, i, j))
    pairs.sort(reverse=True)

    for th in (0.25, 0.35, 0.45, 0.55):
        hit = [p for p in pairs if p[0] >= th]
        seen = set()
        for _, i, j in hit:
            seen.add(j)
        print(f"  body threshold {th:.2f}: {len(hit):>4} cross-outlet pairs, "
              f"{len(seen):>3} items would be suppressed ({100*len(seen)/n:.1f}%)")

    print("\n  Top cross-outlet body matches. Read the TITLE pairs: same event or not?\n")
    for s, i, j in pairs[:10]:
        a, b = rows[i], rows[j]
        ts = jaccard(grams(a["title"]), grams(b["title"]))
        print(f"  body {s:.2f} / title {ts:.2f}")
        print(f"     [{a['h']:<22}] {a['title'][:56]}")
        print(f"     [{b['h']:<22}] {b['title'][:56]}")
        if ts < 0.35 <= s:
            print("     ^^ titles would NOT have matched. This is the case titles miss.")
        print()

    if not pairs:
        print("  no cross-outlet body matches above 0.25.")
        print("  -> genuine same-event duplication is rare in this corpus. Alert-level")
        print("     dedup on canonical URL alone would be sufficient for now.")


def _band_pairs(c, lo=0.35, hi=0.85):
    """Cross-outlet body pairs in a similarity band. Same-host pairs are never included."""
    rows = c.execute("""SELECT id, title, extract_host h, source_id, extract_text t,
                               resolved_url u, coalesce(published_at, discovered_at) ts
                        FROM items
                        WHERE extract_status='OK' AND extract_text IS NOT NULL
                          AND length(extract_text) >= 400
                        ORDER BY ts""").fetchall()
    g = [grams(r["t"][:1200]) for r in rows]
    pairs = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if rows[i]["h"] == rows[j]["h"]:
                continue                       # same outlet is never suppressed
            s = jaccard(g[i], g[j])
            if lo <= s <= hi:
                pairs.append((s, i, j))
    pairs.sort(reverse=True)
    return rows, pairs


def _clusters(rows, pairs, th):
    """Connected components at a threshold -- events, not pairs."""
    parent = list(range(len(rows)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for s, i, j in pairs:
        if s >= th:
            a, b = find(i), find(j)
            if a != b:
                parent[a] = b
    groups = defaultdict(list)
    for i in range(len(rows)):
        groups[find(i)].append(i)
    return [v for v in groups.values() if len(v) > 1]


def _hours_apart(a, b):
    try:
        from datetime import datetime as d
        return abs((d.fromisoformat(a["ts"]) - d.fromisoformat(b["ts"])).total_seconds()) / 3600
    except Exception:
        return -1


def export_dupe_band(c, floor=0.35):
    rows, pairs = _band_pairs(c, lo=floor)
    cl = {}
    for n, comp in enumerate(_clusters(rows, pairs, 0.45), 1):
        for i in comp:
            cl[i] = n

    out = HERE / "dupe_pairs.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["cluster", "body_sim", "title_sim", "hours_apart",
                    "id_a", "host_a", "title_a", "url_a",
                    "id_b", "host_b", "title_b", "url_b", "SAME_EVENT_y_n"])
        for s, i, j in pairs:
            a, b = rows[i], rows[j]
            w.writerow([cl.get(i, ""), f"{s:.3f}",
                        f"{jaccard(grams(a['title']), grams(b['title'])):.3f}",
                        f"{_hours_apart(a, b):.1f}",
                        a["id"], a["h"], a["title"], a["u"],
                        b["id"], b["h"], b["title"], b["u"], ""])
    print(f"\n  wrote {out}  ({len(pairs)} pairs, {floor}-0.85, cross-outlet only)")
    print("  Sorted high to low. Each unordered pair appears once, but one event")
    print("  spanning 3 outlets yields 3 rows -- the 'cluster' column ties them together.")
    print("  Open both urls, mark SAME_EVENT_y_n, then run:  python analyse.py --score")


def cmd_score(c):
    """Turn labelled pairs into precision/recall per threshold. No guessing."""
    path = HERE / "dupe_pairs.csv"
    if not path.exists():
        print("run --dupes first")
        return
    lab = []
    for r in csv.DictReader(open(path, encoding="utf-8-sig")):
        v = (r.get("SAME_EVENT_y_n") or "").strip().lower()
        if v in ("y", "n"):
            lab.append((float(r["body_sim"]), v == "y", float(r.get("hours_apart") or -1)))
    if not lab:
        print("no SAME_EVENT_y_n values filled in yet")
        return

    hr("THRESHOLD SCORING from your labels")
    print(f"  {len(lab)} pairs labelled: {sum(1 for _,y,_ in lab if y)} same-event, "
          f"{sum(1 for _,y,_ in lab if not y)} different\n")

    print("  by similarity band (covers every labelled pair):")
    lowest = min(s for s, _, _ in lab)
    bands = [(0.80, 1.01), (0.70, 0.80), (0.60, 0.70), (0.50, 0.60), (0.35, 0.50)]
    edge = 0.35
    while edge - 0.05 >= lowest - 1e-9:          # extend down to the actual floor
        bands.append((round(edge - 0.05, 2), edge))
        edge = round(edge - 0.05, 2)
    for lo, hi in bands:
        b = [x for x in lab if lo <= x[0] < hi]
        if b:
            y = sum(1 for _, t, _ in b if t)
            pct = 100 * y / len(b)
            note = ""
            if pct < 60:
                note = "   <-- coin flip: score carries no signal here"
            elif pct < 85:
                note = "   <-- mixed"
            print(f"    {lo:.2f}-{hi:.2f}  {y:>4} same / {len(b)-y:>4} different "
                  f"  ({pct:>3.0f}% true){note}")

    print("\n  if threshold set here:")
    print(f"    {'thresh':<8}{'kept(TP)':>9}{'wrong(FP)':>11}{'missed(FN)':>12}{'precision':>11}")
    for th in (0.45, 0.50, 0.55, 0.60, 0.65, 0.70):
        tp = sum(1 for s, y, _ in lab if s >= th and y)
        fp = sum(1 for s, y, _ in lab if s >= th and not y)
        fn = sum(1 for s, y, _ in lab if s < th and y)
        p = 100 * tp / (tp + fp) if tp + fp else 0
        flag = "   <-- no false positives" if fp == 0 and tp else ""
        print(f"    {th:<8.2f}{tp:>9}{fp:>11}{fn:>12}{p:>10.0f}%{flag}")

    fps = [(s, h) for s, y, h in lab if not y and s >= 0.55]
    if fps:
        print(f"\n  {len(fps)} false positives at 0.55. Time gaps: "
              f"{', '.join(f'{h:.0f}h' for _, h in sorted(fps, reverse=True)[:6])}")
        print("  If FPs are far apart in time and true pairs are close, add a time window.")

    print("\n  suppression with CLUSTERING (not pairwise deletion):")
    rows, pairs = _band_pairs(c)
    for th in (0.45, 0.55, 0.65):
        comps = _clusters(rows, pairs, th)
        sup = sum(len(x) - 1 for x in comps)
        print(f"    {th:.2f}: {len(comps)} events, {sup} items folded in "
              f"({100*sup/len(rows):.1f}% of {len(rows)} extracted)")


def cmd_compare(c, ida, idb):
    """Print two article bodies side by side so a pair can actually be judged."""
    for i in (ida, idb):
        r = c.execute("""SELECT id,title,extract_host h,resolved_url u,published_at p,
                                extract_text t FROM items WHERE id=?""", (i,)).fetchone()
        if not r:
            print(f"no item {i}")
            continue
        print(f"\n{'=' * 76}\n  #{r['id']}  [{r['h']}]  {r['p']}\n  {r['u']}\n{'=' * 76}")
        print(f"  {r['title']}\n")
        print("  " + (r["t"] or "")[:1400].replace("\n", "\n  "))


def export_labels(c):
    out = HERE / "label_me.csv"
    rows = c.execute("""SELECT id, source_id, coalesce(published_at,discovered_at) t,
                               extract_host h, extract_status s, extract_chars ch, title
                        FROM items
                        WHERE extract_status NOT IN ('SKIPPED_VIDEO')
                        ORDER BY source_id, t DESC""").fetchall()
    # utf-8-sig so Excel opens Tamil correctly instead of showing mojibake
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "source_id", "published", "host", "status", "chars", "title",
                    "RELEVANT_y_n", "TARGET_tvk_velachery_none", "NOTE"])
        for r in rows:
            w.writerow([r["id"], r["source_id"], r["t"], r["h"], r["s"], r["ch"],
                        r["title"], "", "", ""])
    print(f"\n  wrote {out}  ({len(rows)} rows)")
    print("  Fill RELEVANT_y_n for ~100 rows. That is the ground truth everything else")
    print("  gets measured against. Do the Velachery rows first.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", action="store_true")
    ap.add_argument("--dupes", action="store_true",
                    help="export cross-outlet body pairs in the threshold decision band")
    ap.add_argument("--floor", type=float, default=0.35,
                    help="lower edge of the exported band (drop to 0.20 to find "
                         "where the labeller starts saying 'n')")
    ap.add_argument("--score", action="store_true",
                    help="precision/recall per threshold from your labelled dupe_pairs.csv")
    ap.add_argument("--compare", nargs=2, type=int, metavar=("ID_A", "ID_B"),
                    help="print two article bodies side by side")
    a = ap.parse_args()

    if not DB.exists():
        print("no corpus.db here")
        return
    c = con()
    if a.compare:
        cmd_compare(c, *a.compare); c.close(); return
    if a.score:
        cmd_score(c); c.close(); return
    dedup_evidence(c)
    query_landscape(c)
    extraction_health(c)
    duplication(c)
    same_article_dupes(c)
    body_duplication(c)
    if a.dupes:
        export_dupe_band(c, a.floor)
    if a.label:
        export_labels(c)
    c.close()


if __name__ == "__main__":
    main()
