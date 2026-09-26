#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trace_web.py -- the same pipeline trace, in your browser, live.

READ-ONLY. Never opens corpus.db, never writes, never sends anything.
Put it beside collect.py / rules.py and run:

    python trace_web.py

Your browser opens at http://127.0.0.1:8777 . Type a query, press Run,
and watch each stage appear as it happens.

Press Ctrl+C in this window to stop the server.
"""

import html as _html
import json
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse, parse_qs

try:
    import feedparser
    import httpx
    import trafilatura
except ImportError as ex:
    sys.exit(f"missing dependency: {ex}\nrun:  pip install -r requirements.txt")

try:
    import rules
except ImportError:
    sys.exit("run this from the same folder as rules.py")

PORT = 8777
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ============================================================ the trace itself

def build_url(query, lang, window):
    q = quote(f"{query} when:{window}") if window else quote(query)
    if lang == "ta":
        return f"https://news.google.com/rss/search?q={q}&hl=ta&gl=IN&ceid=IN%3Ata"
    return f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN%3Aen"


def entry_publisher(e):
    src = getattr(e, "source", None)
    if src is not None and getattr(src, "title", None):
        return src.title
    return (getattr(e, "author", "") or "").strip() or "?"


def entry_desc(e):
    raw = getattr(e, "summary", "") or ""
    return " ".join(_html.unescape(rules.strip_html(raw)).split())


def run_trace(emit, query, lang, window, resolve_n, pace):
    """emit(event_name, payload_dict) -- pushes one SSE event to the browser."""
    kw = rules.keywords_from_file()

    # ---- stage 1 -------------------------------------------------------
    url = build_url(query, lang, window)
    emit("stage", {"n": 1, "title": "The request",
                   "note": "Your keywords are NOT sent to Google. This URL is the "
                           "only thing that decides what arrives; the vocabulary "
                           "filters it afterwards."})
    emit("kv", {"rows": [["query", query], ["language", lang],
                         ["window", f"when:{window}"], ["url", url],
                         ["vocabulary", f"{len(kw.terms)} active terms, "
                                        f"{len(kw.excludes)} excludes "
                                        f"({kw.origin}, hash {kw.hash})"]]})

    # ---- stage 2 -------------------------------------------------------
    emit("stage", {"n": 2, "title": "What the RSS feed returned",
                   "note": "Everything Google sent back, before any filtering."})
    t0 = time.time()
    with httpx.Client(timeout=30, follow_redirects=True,
                      headers={"User-Agent": UA}) as c:
        r = c.get(url)
    ms = int((time.time() - t0) * 1000)
    emit("log", {"text": f"HTTP {r.status_code} · {len(r.content):,} bytes · {ms} ms"})
    if r.status_code != 200:
        emit("error", {"text": f"Google returned {r.status_code}. "
                               f"503 is transient -- try again."})
        emit("done", {"text": "stopped — nothing was fetched"})
        return

    entries = list(feedparser.parse(r.content).entries)
    if len(entries) >= 100:
        emit("warn", {"text": "100 items = Google's hard cap. More exists that you "
                              "did not receive. Narrow the window."})
    if not entries:
        emit("error", {"text": "Nothing returned. Try a wider window, or check "
                               "the query spelling."})
        emit("done", {"text": "stopped — the feed was empty"})
        return

    items = []
    for i, e in enumerate(entries, 1):
        items.append(dict(i=i,
                          title=rules.clean_title(getattr(e, "title", ""),
                                                  entry_publisher(e)),
                          publisher=entry_publisher(e),
                          published=getattr(e, "published", "") or "?",
                          link=getattr(e, "link", ""),
                          desc=entry_desc(e)))
    emit("feed", {"count": len(items),
                  "items": [{k: it[k] for k in
                             ("i", "title", "publisher", "published")} for it in items]})
    one = items[0]
    emit("raw", {"title": one["title"], "publisher": one["publisher"],
                 "published": one["published"], "desc": one["desc"] or "(empty)",
                 "link": one["link"],
                 "is_token": "news.google.com" in one["link"]})

    # ---- stage 3 -------------------------------------------------------
    emit("stage", {"n": 3, "title": "Scoring on the headline alone",
                   "note": "This filter does not exist in the collector today — "
                           "every item below is currently decoded and downloaded "
                           "regardless of score."})
    counts = {}
    rows = []
    for it in items:
        sc = rules.score(kw, it["title"], it["desc"], "", has_ai=False)
        ur = rules.urgent(kw, it["title"], it["desc"], "")
        it["title_score"] = sc
        it["title_urgent"] = bool(ur)
        counts[sc["band"]] = counts.get(sc["band"], 0) + 1
        rows.append({"i": it["i"], "band": sc["band"], "score": sc["score"],
                     "urgent": bool(ur), "terms": sc["terms"][:6],
                     "veto": sc.get("veto") or "",
                     "title": it["title"], "publisher": it["publisher"]})
    keep = sum(v for k, v in counts.items() if k != "DROP")
    emit("scores", {"rows": rows, "counts": counts, "keep": keep,
                    "total": len(items),
                    "saved": f"{keep * pace / 60:.1f} min instead of "
                             f"{len(items) * pace / 60:.1f} min"})

    if resolve_n <= 0:
        emit("done", {"text": "Stopped after stage 3 — no tokens decoded, "
                              "nothing downloaded."})
        return

    # ---- stage 4 -------------------------------------------------------
    emit("stage", {"n": 4, "title": "Decoding tokens into real links",
                   "note": f"Pacing {pace}s between calls, same as the collector. "
                           f"Google throttles by latency, not 429s — watch the "
                           f"seconds."})
    try:
        from googlenewsdecoder import gnewsdecoder
    except ImportError:
        emit("error", {"text": "googlenewsdecoder not installed."})
        return

    picked = [it for it in items if it["title_score"]["band"] != "DROP"][:resolve_n]
    if not picked:
        picked = items[:resolve_n]
        emit("warn", {"text": "Nothing scored above DROP on the headline — "
                              "tracing the first few anyway. (This is normal for "
                              "place-name queries: the civic words live in the body.)"})

    for it in picked:
        if "news.google.com" not in it["link"]:
            it["resolved"] = it["link"]
            emit("resolve", {"i": it["i"], "ok": True, "secs": 0.0,
                             "url": it["link"], "canon": rules.canonical_key(it["link"]),
                             "title": it["title"], "note": "no decode needed"})
            continue
        t0 = time.time()
        try:
            res = gnewsdecoder(it["link"], interval=None)
            it["resolved"] = res.get("decoded_url") if res.get("status") else None
            err = None if it["resolved"] else str(res.get("message", "unknown"))
        except Exception as ex:
            it["resolved"], err = None, f"{type(ex).__name__}: {ex}"
        secs = round(time.time() - t0, 1)
        if it["resolved"]:
            emit("resolve", {"i": it["i"], "ok": True, "secs": secs,
                             "url": it["resolved"],
                             "canon": rules.canonical_key(it["resolved"]),
                             "title": it["title"], "note": ""})
        else:
            emit("resolve", {"i": it["i"], "ok": False, "secs": secs,
                             "url": "", "canon": "", "title": it["title"],
                             "note": err})
        time.sleep(pace)

    # ---- stage 5 -------------------------------------------------------
    emit("stage", {"n": 5, "title": "Fetching and extracting the article body",
                   "note": "The full text. This is what Apps Script never sees."})
    with httpx.Client(timeout=30, follow_redirects=True,
                      headers={"User-Agent": UA}) as c:
        for it in picked:
            if not it.get("resolved"):
                continue
            t0 = time.time()
            try:
                rr = c.get(it["resolved"])
                it["body"] = trafilatura.extract(rr.text, include_comments=False,
                                                 include_tables=False) or ""
                code = str(rr.status_code)
            except Exception as ex:
                it["body"], code = "", f"ERR {type(ex).__name__}"
            n = len(it["body"])
            emit("extract", {"i": it["i"], "http": code,
                             "secs": round(time.time() - t0, 1), "chars": n,
                             "verdict": "OK" if n >= 400 else ("THIN" if n else "FAILED"),
                             "title": it["title"],
                             "body": it["body"][:1200],
                             "thin_warn": bool(n and n < 400)})

    # ---- stage 6 -------------------------------------------------------
    emit("stage", {"n": 6, "title": "Re-scoring, now with the body",
                   "note": "Headline-only score on the left, headline+body on the "
                           "right. The difference is what reading the article buys "
                           "you."})
    for it in picked:
        if not it.get("body"):
            continue
        before = it["title_score"]
        after = rules.score(kw, it["title"], it["desc"], it["body"], has_ai=False)
        ur = rules.urgent(kw, it["title"], it["desc"], it["body"])
        it["final"] = after
        emit("rescore", {"i": it["i"], "title": it["title"],
                         "b_band": before["band"], "b_score": before["score"],
                         "a_band": after["band"], "a_score": after["score"],
                         "delta": after["score"] - before["score"],
                         "new_terms": [t for t in after["terms"]
                                       if t not in before["terms"]],
                         "urgent_now": bool(ur) and not it["title_urgent"],
                         "urgent": (f"{ur.get('target')} / "
                                    f"{','.join(ur.get('groups', []))}") if ur else ""})

    # ---- stage 7 -------------------------------------------------------
    emit("stage", {"n": 7, "title": "Deduplication",
                   "note": f"Threshold {rules.DEDUP_THRESHOLD} · 4-gram Jaccard on "
                           f"the first {rules.BODY_CAP} chars · cross-host only."})
    have = [it for it in picked if it.get("body")]
    if len(have) < 2:
        emit("log", {"text": "Need at least 2 extracted bodies to compare — "
                             "raise the resolve count."})
    else:
        grams = {it["i"]: rules.sim_grams(it["body"][:rules.BODY_CAP]) for it in have}
        hosts = {it["i"]: rules.host_of(it.get("resolved") or "") for it in have}
        pairs = []
        for x in range(len(have)):
            for y in range(x + 1, len(have)):
                p, q = have[x]["i"], have[y]["i"]
                if hosts[p] == hosts[q]:
                    pairs.append({"a": p, "b": q, "sim": None,
                                  "verdict": f"skipped — same host ({hosts[p]})"})
                    continue
                s = rules.jaccard(grams[p], grams[q])
                near = abs(s - rules.DEDUP_THRESHOLD) < 0.03
                pairs.append({"a": p, "b": q, "sim": round(s, 3),
                              "verdict": ("MERGE" if s >= rules.DEDUP_THRESHOLD
                                          else "grey band" if s >= 0.20 else ""),
                              "near": near})
        emit("dedup", {"pairs": pairs, "threshold": rules.DEDUP_THRESHOLD})

    emit("done", {"text": "Finished. corpus.db was never opened — no database, "
                          "no Telegram, no state changed."})


# ============================================================ the web server

PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pipeline trace</title>
<style>
:root{--bg:#f6f7f8;--card:#fff;--ink:#14181a;--dim:#6b7580;--line:#dfe3e6;
--keep:#0e6e63;--ask:#93680a;--drop:#98a2ab;--urgent:#a62b1b;--accent:#1d4e89;
--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
@media(prefers-color-scheme:dark){:root{--bg:#14181a;--card:#1c2124;--ink:#e8ecee;
--dim:#8b959e;--line:#2c3338}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:20px 16px 80px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--dim);font-size:13px;margin:0 0 18px}
form{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px;display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;
position:sticky;top:0;z-index:5}
label{display:flex;flex-direction:column;gap:4px;font-size:11px;
text-transform:uppercase;letter-spacing:.08em;color:var(--dim)}
input,select{font:14px var(--mono);padding:7px 9px;border:1px solid var(--line);
border-radius:6px;background:var(--bg);color:var(--ink)}
input[type=text]{min-width:190px}
button{font:600 14px system-ui;padding:8px 18px;border:0;border-radius:6px;
background:var(--accent);color:#fff;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
.presets{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 0}
.presets button{background:transparent;color:var(--dim);border:1px solid var(--line);
font-weight:400;font-size:12px;padding:5px 10px}
.stage{background:var(--card);border:1px solid var(--line);border-radius:10px;
margin:14px 0;overflow:hidden}
.stage>h2{margin:0;padding:12px 16px;font-size:14px;letter-spacing:.02em;
border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:baseline}
.stage>h2 .num{font:600 11px var(--mono);color:#fff;background:var(--accent);
border-radius:4px;padding:2px 7px}
.note{padding:10px 16px;color:var(--dim);font-size:13px;
border-bottom:1px solid var(--line);background:rgba(127,127,127,.04)}
.body{padding:12px 16px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font:600 10px system-ui;text-transform:uppercase;
letter-spacing:.09em;color:var(--dim);padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
.num-col{font:13px var(--mono);color:var(--dim);width:34px}
.badge{font:600 10px var(--mono);padding:2px 7px;border-radius:4px;
letter-spacing:.06em;white-space:nowrap;color:#fff}
.b-AUTO_KEEP,.b-KEYWORD_KEEP{background:var(--keep)}
.b-AI{background:var(--ask)}.b-DROP{background:var(--drop)}
.b-URGENT{background:var(--urgent)}
.terms{font:12px var(--mono);color:var(--dim);word-break:break-word}
.kv{font:13px var(--mono);word-break:break-all}
.kv b{display:inline-block;min-width:96px;font-weight:600;color:var(--dim)}
.log{font:13px var(--mono);color:var(--dim);padding:4px 0}
.warn{color:var(--urgent);font-weight:600}
.excerpt{font-size:13px;color:var(--dim);border-left:3px solid var(--line);
padding:6px 0 6px 12px;margin:8px 0 0;white-space:pre-wrap;max-height:150px;
overflow:auto}
.row{display:flex;gap:10px;align-items:baseline;padding:7px 0;
border-bottom:1px solid var(--line);flex-wrap:wrap}
.row:last-child{border-bottom:0}
.arrow{color:var(--dim)}
.delta{font:600 13px var(--mono);color:var(--keep)}
.ok{color:var(--keep)}.bad{color:var(--urgent)}
.hairline{background:rgba(166,43,27,.10);border-radius:5px;padding:5px 8px}
.summary{margin-top:10px;padding:9px 12px;background:rgba(127,127,127,.07);
border-radius:6px;font-size:13px}
.title{font-size:13px}
a{color:var(--accent);word-break:break-all}
@media(max-width:620px){.num-col{width:26px}th:nth-child(4),td:nth-child(4){display:none}}
</style></head><body><div class="wrap">
<h1>Pipeline trace</h1>
<p class="sub">Read-only. corpus.db is never opened, nothing is written or sent.</p>

<form id="f">
  <label>Query<input type="text" id="q" value="Velachery"></label>
  <label>Language<select id="lang"><option value="en">English</option>
    <option value="ta">Tamil</option></select></label>
  <label>Window<select id="win">
    <option>6h</option><option>1d</option><option selected>2d</option>
    <option>7d</option><option>14d</option></select></label>
  <label>Decode<select id="res">
    <option value="0">0 — headlines only</option><option value="3" selected>3</option>
    <option value="5">5</option><option value="10">10</option></select></label>
  <label>Pace<select id="pace"><option value="5">5s (safe)</option>
    <option value="2">2s</option><option value="1">1s</option></select></label>
  <button id="go" type="submit">Run</button>
</form>
<div class="presets">
  <button data-q="Velachery" data-l="en" data-w="2d">Velachery</button>
  <button data-q="வேளச்சேரி" data-l="ta" data-w="2d">வேளச்சேரி</button>
  <button data-q="திருவள்ளூர்" data-l="ta" data-w="1d">திருவள்ளூர்</button>
  <button data-q="Thiruvallur district" data-l="en" data-w="1d">Thiruvallur</button>
  <button data-q="Pallikaranai" data-l="en" data-w="7d">Pallikaranai</button>
  <button data-q="Tamil Nadu AI minister" data-l="en" data-w="7d">AI minister</button>
  <button data-q="தவெக" data-l="ta" data-w="6h" data-r="0">தவெக (headlines only)</button>
</div>
<div id="out"></div></div>
<script>
const out=document.getElementById('out'),go=document.getElementById('go');
let stage=null;
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function newStage(d){const s=document.createElement('section');s.className='stage';
 s.innerHTML='<h2><span class="num">'+d.n+'</span>'+esc(d.title)+'</h2>'+
 (d.note?'<div class="note">'+esc(d.note)+'</div>':'')+'<div class="body"></div>';
 out.appendChild(s);stage=s.querySelector('.body');s.scrollIntoView({behavior:'smooth',block:'nearest'});}
function add(h){if(!stage){newStage({n:'·',title:'Output'});}const d=document.createElement('div');d.innerHTML=h;stage.appendChild(d);}
const bandBadge=b=>'<span class="badge b-'+b+'">'+b.replace('_',' ')+'</span>';

document.querySelectorAll('.presets button').forEach(b=>b.onclick=()=>{
 q.value=b.dataset.q;lang.value=b.dataset.l;win.value=b.dataset.w;
 if(b.dataset.r!==undefined)res.value=b.dataset.r;f.requestSubmit();});

f.onsubmit=e=>{e.preventDefault();out.innerHTML='';stage=null;go.disabled=true;go.textContent='Running…';
 const u='/run?query='+encodeURIComponent(q.value)+'&lang='+lang.value+
   '&window='+win.value+'&resolve='+res.value+'&pace='+pace.value;
 const src=new EventSource(u);
 src.onmessage=ev=>{const m=JSON.parse(ev.data),d=m.d;
  switch(m.e){
   case 'stage': newStage(d); break;
   case 'kv': add('<div class="kv">'+d.rows.map(r=>'<b>'+esc(r[0])+'</b>'+
     (r[0]==='url'?'<a href="'+esc(r[1])+'" target="_blank">'+esc(r[1])+'</a>':esc(r[1]))).join('<br>')+'</div>'); break;
   case 'log': add('<div class="log">'+esc(d.text)+'</div>'); break;
   case 'warn': add('<div class="log warn">⚠ '+esc(d.text)+'</div>'); break;
   case 'error': add('<div class="log warn">✕ '+esc(d.text)+'</div>'); break;
   case 'feed': add('<div class="log">'+d.count+' entries returned</div>'+
     '<table><tr><th>#</th><th>Published</th><th>Headline</th><th>Publisher</th></tr>'+
     d.items.map(i=>'<tr><td class="num-col">'+i.i+'</td><td class="terms">'+
       esc(i.published.slice(0,22))+'</td><td class="title">'+esc(i.title)+
       '</td><td class="terms">'+esc(i.publisher)+'</td></tr>').join('')+'</table>'); break;
   case 'raw': add('<div class="summary"><b>One entry in full — this is everything '+
     'you get before decoding:</b><div class="kv" style="margin-top:8px">'+
     '<b>title</b>'+esc(d.title)+'<br><b>publisher</b>'+esc(d.publisher)+
     '<br><b>published</b>'+esc(d.published)+'<br><b>description</b>'+esc(d.desc)+
     '<br><b>link</b>'+esc(d.link.slice(0,110))+'…'+
     (d.is_token?'<br><b></b><span class="warn">a Google token — useless until stage 4</span>':'')+
     '</div></div>'); break;
   case 'scores': add('<table><tr><th>#</th><th>Band</th><th>Score</th>'+
     '<th>Headline</th><th>Terms matched</th></tr>'+
     d.rows.map(r=>'<tr><td class="num-col">'+r.i+'</td><td>'+bandBadge(r.band)+
       (r.urgent?' '+bandBadge('URGENT'):'')+'</td><td class="terms">'+r.score+
       '</td><td class="title">'+esc(r.title)+'</td><td class="terms">'+
       esc(r.terms.join(', ')||'—')+(r.veto?'<br><span class="warn">VETO: '+
       esc(r.veto)+'</span>':'')+'</td></tr>').join('')+'</table>'+
     '<div class="summary">'+Object.entries(d.counts).map(c=>c[0]+' '+c[1]).join(' · ')+
     '<br>A headline pre-filter would decode <b>'+d.keep+' of '+d.total+'</b> — '+
     esc(d.saved)+'.</div>'); break;
   case 'resolve': add('<div class="row"><span class="num-col">'+d.i+'</span>'+
     (d.ok?'<span class="ok">✓ '+d.secs+'s</span><div style="flex:1;min-width:200px">'+
       '<a href="'+esc(d.url)+'" target="_blank">'+esc(d.url)+'</a>'+
       '<div class="terms">key: '+esc(d.canon)+'</div></div>'
      :'<span class="bad">✕ '+d.secs+'s</span><div style="flex:1" class="terms">'+
       esc(d.note)+'</div>')+'</div>'); break;
   case 'extract': add('<div class="row"><span class="num-col">'+d.i+'</span>'+
     '<span class="'+(d.verdict==='OK'?'ok':'bad')+'">'+d.verdict+'</span>'+
     '<span class="terms">HTTP '+esc(d.http)+' · '+d.secs+'s · '+d.chars+' chars</span>'+
     '<div style="flex:1;min-width:240px"><div class="title">'+esc(d.title)+'</div>'+
     (d.thin_warn?'<div class="log warn">under 400 chars — dedup will never compare this</div>':'')+
     (d.body?'<div class="excerpt">'+esc(d.body)+'</div>':'')+'</div></div>'); break;
   case 'rescore': add('<div class="row"><span class="num-col">'+d.i+'</span>'+
     bandBadge(d.b_band)+' '+d.b_score+' <span class="arrow">→</span> '+
     bandBadge(d.a_band)+' '+d.a_score+' <span class="delta">'+
     (d.delta>=0?'+':'')+d.delta+'</span>'+
     (d.urgent_now?' '+bandBadge('URGENT')+' <span class="warn">only after reading the body</span>':'')+
     '<div style="flex:1;min-width:220px"><div class="title">'+esc(d.title)+'</div>'+
     (d.new_terms.length?'<div class="terms">found only in the body: '+
       esc(d.new_terms.join(', '))+'</div>':'')+
     (d.urgent?'<div class="terms">urgent: '+esc(d.urgent)+'</div>':'')+
     '</div></div>'); break;
   case 'dedup': add(d.pairs.map(p=>'<div class="row'+(p.near?' hairline':'')+'">'+
     '<span class="num-col">'+p.a+'+'+p.b+'</span>'+
     (p.sim===null?'<span class="terms">'+esc(p.verdict)+'</span>'
      :'<b style="font-family:var(--mono)">'+p.sim.toFixed(3)+'</b>'+
       '<span class="terms">'+esc(p.verdict)+(p.near?' — within 0.03 of the '+
       d.threshold+' threshold':'')+'</span>')+'</div>').join('')); break;
   case 'done': add('<div class="summary">'+esc(d.text)+'</div>');
     src.close();go.disabled=false;go.textContent='Run'; break;
  }};
 src.onerror=()=>{src.close();go.disabled=false;go.textContent='Run';};};
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = urlparse(self.path)
        if path.path in ("/", "/index.html"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.path != "/run":
            self.send_error(404)
            return

        qs = parse_qs(path.query)
        def g(k, d=""):
            return (qs.get(k) or [d])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(event, data):
            chunk = "data: " + json.dumps({"e": event, "d": data},
                                          ensure_ascii=False) + "\n\n"
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.flush()

        try:
            run_trace(emit,
                      query=g("query", "Velachery"),
                      lang=g("lang", "en"),
                      window=g("window", "2d"),
                      resolve_n=int(g("resolve", "3") or 0),
                      pace=float(g("pace", "5") or 5))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as ex:
            try:
                emit("error", {"text": f"{type(ex).__name__}: {ex}"})
                emit("done", {"text": "stopped after an error"})
            except Exception:
                pass


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"\n  Pipeline trace running at  {url}")
    print("  Read-only: corpus.db is never opened.")
    print("  Press Ctrl+C here to stop.\n")
    threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("  stopped")
        srv.shutdown()


if __name__ == "__main__":
    main()