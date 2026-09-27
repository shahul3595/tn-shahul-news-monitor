/**
 * Reader feedback for the web page -> a "Feedback" tab in the keyword Google Sheet.
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
 */

var TAB = "Feedback";
var HEADER = ["when", "type", "item_id", "title", "url", "category", "outlet", "reason", "notes", "page", "browser"];

function sheet_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(TAB);
  if (!sh) {
    sh = ss.insertSheet(TAB);
    sh.appendRow(HEADER);
    sh.setFrozenRows(1);
  }
  return sh;
}

function doPost(e) {
  try {
    var d = JSON.parse((e && e.postData && e.postData.contents) || "{}");
    var type = String(d.type || "").slice(0, 10);
    if (["up", "down", "missing"].indexOf(type) < 0) {
      return ContentService.createTextOutput("ignored");
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
    return ContentService.createTextOutput("ok");
  } catch (err) {
    return ContentService.createTextOutput("error");
  }
}

function doGet() {
  return ContentService.createTextOutput("feedback endpoint is up");
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
