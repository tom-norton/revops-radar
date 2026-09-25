// Adds a row to the Applications tab of Networking Tracker when "Mark applied" is tapped on
// the dashboard. Lives in the sheet's own Apps Script project (Extensions → Apps Script),
// not in the repo's pipeline; this file is the source of truth to paste from. Setup is in
// the README under "Application tracker".
//
// The web app has to be deployed with access "Anyone", because the dashboard is a static
// page with no Google login. The URL is in the public repo, so the TOKEN script property is
// what stops a stranger writing to the sheet. The dashboard keeps its copy in localStorage.
//
// The newest application goes at the top (row 2), as Tom has always entered them by hand.
// A link already in column G is not added twice, so un-applying and re-applying a role, or
// marking it on two devices, leaves one row.

const SHEET_NAME = "Applications";
const LINK_COL = 7; // G

function doPost(e) {
  let body;
  try { body = JSON.parse(e.postData.contents); }
  catch (err) { return reply({ ok: false, error: "bad json" }); }

  const token = PropertiesService.getScriptProperties().getProperty("TOKEN");
  if (!token || body.token !== token) return reply({ ok: false, error: "bad token" });

  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
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
    // A row inserted under the header picks up the header's look; take the old top row's.
    if (last >= 2) {
      sheet.getRange(3, 1, 1, sheet.getLastColumn())
           .copyTo(sheet.getRange(2, 1, 1, sheet.getLastColumn()),
                   SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
    }
    const score = body.score === "" || body.score == null ? "" : Number(body.score);
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    sheet.getRange(2, 1, 1, 9).setValues([[
      body.company || "", body.title || "", body.location || "", score,
      today, "Applied", link, "", "",
    ]]);
    sheet.getRange(2, 5).setNumberFormat("M/d");
    return reply({ ok: true });
  } finally {
    lock.releaseLock();
  }
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
                       .setMimeType(ContentService.MimeType.JSON);
}
