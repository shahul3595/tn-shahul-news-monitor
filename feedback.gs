/**
 * Reader feedback for the web page -> a "Feedback" tab in the keyword Google Sheet.
 * Also: on-page moves and hides (-> the "Add links" tab) and the visit log (-> "Visits").
 *
 * ONE-TIME SETUP (about five minutes)
 *   1. Open the keyword Sheet. Extensions -> Apps Script. Delete whatever is in the editor,
 *      paste this whole file, press the save icon.
 *   2. Deploy -> New deployment. Click the gear next to "Select type" -> Web app.
 *      Description: feedback. Execute as: Me. Who has access: Anyone. Click Deploy.
 *      Authorise when asked (it only touches this spreadsheet).
 *   3. Copy the Web app URL (ends in /exec). In GitHub: Settings -> Secrets and variables ->
 *      Actions -> Variables -> New repository variable: FEEDBACK_URL = that URL.
 *      (A variable, not a secret: the page has to carry it. It only allows appending rows.)
 *   4. The next brief's page shows the 👍/👎 buttons and the "Submit missing news" button.
 *
 * If you later change this code: Deploy -> Manage deployments -> edit (pencil) -> Version:
 * New version -> Deploy. The URL stays the same.
 *
 * Columns written: when, type (up | down | missing), item id, title, url, category, outlet,
 * reason, notes, page, browser. Nothing identifies the reader.
 *
 * ON-PAGE MOVES AND HIDES (optional): the brief page can re-file a card, or take it off the
 * page, from the page itself. That writes a row into the "Add links" tab, which the
 * collector reads back. It is allowed only with the move token: open admin.html, unlock
 * it, copy the token shown there, then here: Project Settings (gear) -> Script properties
 * -> Add property: MOVE_TOKEN = that token. Re-deploy after pasting a new version of this
 * file.
 *
 * VISIT LOG (optional): a small function in front of the site posts one row per page view
 * here (type "visit"); admin.html reads the totals back (GET ?type=visits). Both need the
 * visit token from admin.html: Script properties -> VISIT_TOKEN = that token. The same
 * value goes into the site host's variables (see README). Rows older than 90 days are
 * trimmed; never publish the Visits tab to the web.
 */

var TAB = "Feedback";
var LINKS_TAB = "Add links";
var VISITS_TAB = "Visits";
var LINKS_HEADER = ["url", "category", "sentiment", "note", "added"];
var HEADER = ["when", "type", "item_id", "title", "url", "category", "outlet", "reason", "notes", "page", "browser"];
var VISITS_HEADER = ["when", "ip", "country", "region", "city", "device", "browser", "os", "page", "referrer", "bot", "ua"];
var RETENTION_DAYS = 90;
var MAX_ROWS = 20000;

function tab_(name, header) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    sh.appendRow(header);
    sh.setFrozenRows(1);
  }
  return sh;
}

function sheet_() { return tab_(TAB, HEADER); }
function links_() { return tab_(LINKS_TAB, LINKS_HEADER); }
function visits_() { return tab_(VISITS_TAB, VISITS_HEADER); }

function prop_(k) {
  return PropertiesService.getScriptProperties().getProperty(k) || "";
}

function text_(s) {
  return ContentService.createTextOutput(s);
}

function json_(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}

function doPost(e) {
  try {
    var d = JSON.parse((e && e.postData && e.postData.contents) || "{}");
    var type = String(d.type || "").slice(0, 10);
    if (type === "move") {
      var want = prop_("MOVE_TOKEN");
      if (!want || String(d.token || "") !== want) {
        return text_("denied");
      }
      var cat = String(d.category || "").slice(0, 20), sent = String(d.sentiment || "").slice(0, 10);
      var id = String(d.id || "").replace(/\D/g, "").slice(0, 12);
      var u = String(d.url || "").trim().slice(0, 500);
      if (!u && id) u = "item:" + id;            // the collector finds the story by its number
      if (!u || (!cat && !sent)) {
        return text_("ignored");
      }
      var verb = cat === "hidden" ? "hide" : cat === "restore" ? "restore" : "move";
      var row = [u, cat, sent, "on-page " + verb + (id ? " #" + id : "") + ": " + String(d.title || "").slice(0, 120), new Date()];
      if (!String(d.url || "").trim()) row.push("page sent no link: " + JSON.stringify(d).slice(0, 300));
      var lk = LockService.getScriptLock();
      lk.waitLock(5000);
      links_().appendRow(row);
      lk.releaseLock();
      return text_("ok");
    }
    if (type === "visit") {
      var vt = prop_("VISIT_TOKEN");
      if (!vt || String(d.token || "") !== vt) {
        return text_("denied");
      }
      var vl = LockService.getScriptLock();
      vl.waitLock(5000);
      var vs = visits_();
      vs.appendRow([
        new Date(), String(d.ip || "").slice(0, 45), String(d.country || "").slice(0, 4), String(d.region || "").slice(0, 60),
        String(d.city || "").slice(0, 60), String(d.device || "").slice(0, 10), String(d.browser || "").slice(0, 30),
        String(d.os || "").slice(0, 20), String(d.page || "/").slice(0, 120), String(d.ref || "").slice(0, 120),
        d.bot ? true : false, String(d.ua || "").slice(0, 160)
      ]);
      if (vs.getLastRow() > 6000 && Math.random() < 0.03) {
        trimVisits_(vs);
      }
      vl.releaseLock();
      return text_("ok");
    }
    if (["up", "down", "missing"].indexOf(type) < 0) {
      return text_("ignored");
    }
    var lock = LockService.getScriptLock();
    lock.waitLock(5000);
    sheet_().appendRow([
      new Date(), type, String(d.id || "").slice(0, 20), String(d.title || "").slice(0, 300),
      String(d.url || "").slice(0, 500), String(d.category || "").slice(0, 30),
      String(d.outlet || "").slice(0, 80), String(d.reason || "").slice(0, 30),
      String(d.notes || "").slice(0, 500), String(d.page || "").slice(0, 120),
      String(d.ua || "").slice(0, 120)
    ]);
    lock.releaseLock();
    return text_("ok");
  } catch (err) {
    return text_("error");
  }
}

function doGet(e) {
  var p = (e && e.parameter) || {};
  if (p.type === "visits") {
    var want = prop_("VISIT_TOKEN");
    if (!want || String(p.token || "") !== want) {
      return json_({ error: "denied" });
    }
    try {
      return json_(visitStats_(Math.min(500, parseInt(p.limit, 10) || 100)));
    } catch (err) {
      return json_({ error: String(err).slice(0, 200) });
    }
  }
  return text_("feedback endpoint is up");
}

/** Oldest rows out: past RETENTION_DAYS, and never more than MAX_ROWS. */
function trimVisits_(sh) {
  var last = sh.getLastRow();
  if (last < 3) return;
  var col = sh.getRange(2, 1, last - 1, 1).getValues();
  var cutoff = new Date(Date.now() - RETENTION_DAYS * 86400000);
  var old = 0;
  while (old < col.length && col[old][0] instanceof Date && col[old][0] < cutoff) old++;
  var del = Math.max(old, (last - 1) - MAX_ROWS);
  if (del >= 100) {
    sh.deleteRows(2, del);
  }
}

/** Totals for admin.html: today and the last 7 IST days, plus the most recent rows. */
function visitStats_(limit) {
  var sh = visits_();
  var last = sh.getLastRow();
  var n = Math.min(last - 1, 5000);
  var rows = n > 0 ? sh.getRange(last - n + 1, 1, n, VISITS_HEADER.length).getValues() : [];
  var now = new Date(), day = 86400000;
  var istDay = function (d) { return Utilities.formatDate(d, "Asia/Kolkata", "yyyy-MM-dd"); };
  var today = istDay(now), weekAgo = new Date(now.getTime() - 7 * day);
  var byDay = {}, days = [];
  for (var k = 6; k >= 0; k--) {
    var dk = istDay(new Date(now.getTime() - k * day));
    byDay[dk] = { day: dk, views: 0, visitors: {} };
    days.push(dk);
  }
  var t = { views: 0, visitors: {}, ips: {} }, w = { views: 0, visitors: {}, ips: {}, bots: 0, admin: 0 }, recent = [];
  for (var i = rows.length - 1; i >= 0; i--) {
    var r = rows[i];
    var when = r[0] instanceof Date ? r[0] : new Date(r[0]);
    if (isNaN(when.getTime())) continue;
    var bot = r[10] === true || String(r[10]) === "true";
    var page = String(r[8] || "/");
    var isAdmin = page.indexOf("admin") >= 0;
    var who = String(r[1]) + "|" + String(r[11]);
    if (when >= weekAgo) {
      if (bot) {
        w.bots++;
      } else if (isAdmin) {
        w.admin++;
      } else {
        w.views++; w.visitors[who] = 1; w.ips[String(r[1])] = 1;
        var dk2 = istDay(when);
        if (byDay[dk2]) { byDay[dk2].views++; byDay[dk2].visitors[who] = 1; }
        if (dk2 === today) { t.views++; t.visitors[who] = 1; t.ips[String(r[1])] = 1; }
      }
    }
    if (recent.length < limit) {
      recent.push({
        when: when.toISOString(), ip: String(r[1]), loc: [r[4], r[3], r[2]].filter(function (x) { return x; }).join(", "),
        device: String(r[5] || ""), browser: String(r[6] || ""), os: String(r[7] || ""), page: page,
        ref: String(r[9] || ""), bot: bot
      });
    }
  }
  var size = function (o) { return Object.keys(o).length; };
  return {
    generated: now.toISOString(), rows: Math.max(0, last - 1), retention_days: RETENTION_DAYS,
    today: { views: t.views, visitors: size(t.visitors), ips: size(t.ips) },
    week: { views: w.views, visitors: size(w.visitors), ips: size(w.ips), bots: w.bots, admin: w.admin },
    days: days.map(function (d) { return { day: d, views: byDay[d].views, visitors: size(byDay[d].visitors) }; }),
    recent: recent
  };
}

/**
 * Optional: run this from the editor to get the last 7 days of feedback as text you can
 * paste into an LLM. The same block is also produced automatically on each brief's run
 * page when FEEDBACK_CSV_URL is set (this tab published as CSV).
 */
function reviewText() {
  var rows = sheet_().getDataRange().getValues().slice(1);
  var since = new Date(Date.now() - 7 * 86400000);
  var out = ["Reader feedback, last 7 days", ""];
  rows.forEach(function (r) {
    if (!(r[0] instanceof Date) || r[0] < since) return;
    var when = Utilities.formatDate(r[0], "Asia/Kolkata", "dd MMM HH:mm");
    if (r[1] === "missing") {
      out.push("- MISSING " + when + ": " + r[4] + (r[8] ? " -- " + r[8] : ""));
    } else {
      out.push("- " + (r[1] === "up" ? "UP  " : "DOWN") + " " + when + " [" + r[5] + "/" + r[6] + "] " + r[3] +
               (r[7] ? " -- reason: " + r[7] : "") + " " + r[4]);
    }
  });
  Logger.log(out.join("\n"));
  return out.join("\n");
}
