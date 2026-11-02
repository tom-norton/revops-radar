// Adds a row to the Applications tab of Job Search Tracker (formerly Networking Tracker)
// when "Mark applied" is tapped on the dashboard. Lives in the sheet's own Apps Script
// project (Extensions → Apps Script), not in the repo's pipeline; this file is the source
// of truth to paste from. Setup is in the README under "Application tracker".
//
// The web app has to be deployed with access "Anyone", because the dashboard is a static
// page with no Google login. The URL is in the public repo, so the TOKEN script property is
// what stops a stranger writing to the sheet. The dashboard keeps its copy in localStorage.
//
// The newest application goes at the top (row 2), as Tom has always entered them by hand.
// A link already in column G is not added twice, so un-applying and re-applying a role, or
// marking it on two devices, leaves one row.
//
// A GET with ?action=contacts&token=... answers with the Contacts tab's people who have a
// Chat Date: the dashboard flags roles at their companies as referral candidates. That
// list goes only to a browser holding the token, never into the public repo.
// ?action=applications&token=... answers with the Applications tab as CSV, for the weekly
// review. Both reads are GETs rather than POST actions on purpose: a deployment still
// running an older version of this file would take a POST it did not understand for an
// application and add a blank row, where a GET it has no handler for just fails.

const SHEET_NAME = "Applications";
const LINK_COL = 7; // G
const CONTACTS_SHEET = "Contacts";

function doPost(e) {
  let body;
  try { body = JSON.parse(e.postData.contents); }
  catch (err) { return reply({ ok: false, error: "bad json" }); }

  const token = PropertiesService.getScriptProperties().getProperty("TOKEN");
  if (!token || body.token !== token) return reply({ ok: false, error: "bad token" });

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) return reply({ ok: false, error: "no tab named " + SHEET_NAME });

  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    const link = String(body.link || "").trim();
    const last = sheet.getLastRow();
    if (link && last >= 2) {
      const links = sheet.getRange(2, LINK_COL, last - 1, 1).getDisplayValues();
      if (links.some(r => String(r[0]).trim() === link)) return reply({ ok: true, dup: true });
    }

    sheet.insertRowBefore(2);
    const score = body.score === "" || body.score == null ? "" : Number(body.score);
    // Today in the SHEET's timezone, at noon. Midnight in the script's own timezone showed
    // as the evening before once the sheet displayed it in a zone further west.
    const [y, m, d] = Utilities.formatDate(new Date(), ss.getSpreadsheetTimeZone(), "yyyy-MM-dd")
                               .split("-").map(Number);
    sheet.getRange(2, 1, 1, 9).setValues([[
      body.company || "", body.title || "", body.location || "", score,
      new Date(y, m - 1, d, 12), "Applied", link, "", "",
    ]]);
    // Commit the row before touching formats, so nothing below can take it with it.
    SpreadsheetApp.flush();
    // Cosmetic only, and after the data is in. The converted sheet is a Sheets table, whose
    // typed columns refuse format changes -- the reason a run failed AFTER the row was
    // written, and the dashboard reported a failure for a row that had landed. The table's
    // own column formats cover it; outside a table, take the old top row's look.
    //
    // The flush() INSIDE the try is what makes the try work. Sheets queues these calls and
    // only sends them when the script ends, so the refusal used to surface after doPost had
    // returned, outside any catch: Google then answered with an HTML error page and the
    // dashboard said "didn't confirm" for every row, though each one had gone in.
    try {
      if (last >= 2) {
        sheet.getRange(3, 1, 1, sheet.getLastColumn())
             .copyTo(sheet.getRange(2, 1, 1, sheet.getLastColumn()),
                     SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
      }
      sheet.getRange(2, 5).setNumberFormat("M/d");
      SpreadsheetApp.flush();
    } catch (err) { /* left to the table's formatting */ }
    return reply({ ok: true });
  } finally {
    lock.releaseLock();
  }
}

function doGet(e) {
  const p = (e && e.parameter) || {};
  const token = PropertiesService.getScriptProperties().getProperty("TOKEN");
  if (!token || p.token !== token) return reply({ ok: false, error: "bad token" });
  if (p.action === "contacts") return contacts(SpreadsheetApp.getActiveSpreadsheet());
  if (p.action === "applications") return applications(SpreadsheetApp.getActiveSpreadsheet());
  return reply({ ok: false, error: "unknown action" });
}

// The whole Applications tab as CSV, for the weekly review (tools/review_data.py). The
// review used to export it through the Drive connector, which handed it back as base64
// it had no way to save. Display values, so dates arrive as the sheet shows them ("9/25"),
// which is what review_data.py's date parsing expects.
function applications(ss) {
  const sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) return reply({ ok: false, error: "no tab named " + SHEET_NAME });
  const csv = sheet.getDataRange().getDisplayValues()
    .map(r => r.map(cell).join(","))
    .join("\r\n");
  return ContentService.createTextOutput(csv).setMimeType(ContentService.MimeType.CSV);
}

function cell(v) {
  const s = String(v);
  return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

// Columns are found by their header text, not position, so inserting a column in the tab
// does not quietly turn this into a list of follow-up dates.
function contacts(ss) {
  const sheet = ss.getSheetByName(CONTACTS_SHEET);
  if (!sheet) return reply({ ok: false, error: "no tab named " + CONTACTS_SHEET });
  const rows = sheet.getDataRange().getDisplayValues();
  const head = (rows.shift() || []).map(h => String(h).trim().toLowerCase());
  const col = name => head.indexOf(name);
  const [n, c, d] = [col("name"), col("company"), col("chat date")];
  if (c < 0 || d < 0) return reply({ ok: false, error: "Contacts needs Company and Chat Date headers" });
  const out = rows
    .filter(r => String(r[c]).trim() && String(r[d]).trim())
    .map(r => ({ name: n < 0 ? "" : String(r[n]).trim(),
                 company: String(r[c]).trim(), chat: String(r[d]).trim() }));
  return reply({ ok: true, contacts: out });
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
                       .setMimeType(ContentService.MimeType.JSON);
}
