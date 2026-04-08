/**
 * AI-in-AI Platform — Google Sheets Trigger
 *
 * Paste this entire script into:
 *   Google Sheet → Extensions → Apps Script → replace Code.gs → Save → Run setup()
 *
 * When the ΠΡΟΣΚΛΗΣΗ cell in column D is set to TRUE, this script reads the
 * meeting data and calls the AI-in-AI webhook to launch the invitation workflow.
 *
 * The ΠΡΟΣΚΛΗΣΗ row is detected dynamically by scanning column C, so the script
 * works with both the old template layout (D11) and the new layout (D15).
 */

// ── CONFIG ────────────────────────────────────────────────────────────────────
var WEBHOOK_URL = "https://127.0.0.1:8000/webhooks/invite";  // replace with ngrok URL
var DRY_RUN     = false;  // Set to true during testing
// ─────────────────────────────────────────────────────────────────────────────


/**
 * Run this once to register the onEdit trigger.
 * Go to Apps Script → Run → setup()
 */
function setup() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger("onSheetEdit")
    .forSpreadsheet(SpreadsheetApp.getActive())
    .onEdit()
    .create();
  Logger.log("Trigger installed successfully.");
}


/**
 * Find the row number of a label in column C of the given sheet.
 * Returns -1 if not found.
 */
function findLabelRow(sheet, label) {
  var colC = sheet.getRange("C1:C20").getValues();
  for (var i = 0; i < colC.length; i++) {
    if (String(colC[i][0]).trim().toUpperCase() === label.toUpperCase()) {
      return i + 1;  // 1-based row number
    }
  }
  return -1;
}


/**
 * Fires on any edit. Detects if the ΠΡΟΣΚΛΗΣΗ cell (column D, label in column C)
 * was changed to TRUE.
 */
function onSheetEdit(e) {
  var sheet = e.source.getActiveSheet();
  var range = e.range;

  // Only act on column D edits
  if (range.getColumn() !== 4) return;

  var editedRow = range.getRow();
  var val = range.getValue();
  if (val !== true && String(val).toUpperCase() !== "TRUE") return;

  // Confirm this is the ΠΡΟΣΚΛΗΣΗ row by checking column C
  var labelInC = String(sheet.getRange(editedRow, 3).getValue()).trim().toUpperCase();
  if (labelInC !== "ΠΡΟΣΚΛΗΣΗ") return;

  var triggerRow = editedRow;

  // Set status immediately
  sheet.getRange(triggerRow, 5).setValue("🔄 Επεξεργασία...");
  sheet.getRange(triggerRow, 4).setValue("🔄");

  try {
    var meetingNumber = sheet.getRange("D5").getValue();

    // Read date and time dynamically
    var dateRow = findLabelRow(sheet, "ΗΜΕΡΟΜΗΝΙΑ");
    var timeRow = findLabelRow(sheet, "ΩΡΑ ΕΝΑΡΞΗΣ");
    var typeRow = findLabelRow(sheet, "ΤΥΠΟΣ");
    var locRow  = findLabelRow(sheet, "ΤΟΠΟΘΕΣΙΑ");

    var meetingDate = dateRow > 0 ? sheet.getRange(dateRow, 4).getValue() : null;
    var meetingTime = timeRow > 0 ? sheet.getRange(timeRow, 4).getValue() : null;
    var meetingType = typeRow > 0 ? String(sheet.getRange(typeRow, 4).getValue()).trim() : "";
    var location    = locRow  > 0 ? String(sheet.getRange(locRow,  4).getValue()).trim() : "";

    // Format date as YYYY-MM-DD
    var dateStr = Utilities.formatDate(
      new Date(meetingDate), "Europe/Athens", "yyyy-MM-dd"
    );

    // Format time as HH:MM
    var timeStr = Utilities.formatDate(
      new Date(meetingTime), "Europe/Athens", "HH:mm"
    );

    // Collect agenda items from H7:H (skip empty)
    var agendaRange = sheet.getRange("H7:H30").getValues();
    var agendaItems = agendaRange
      .map(function(row) { return String(row[0]).trim(); })
      .filter(function(v) { return v && v !== "undefined" && v !== "null"; });

    var payload = {
      meeting_number: String(meetingNumber),
      meeting_date:   dateStr,
      meeting_time:   timeStr,
      meeting_type:   meetingType,
      location:       location,
      agenda_items:   agendaItems,
      trigger_row:    triggerRow,
      dry_run:        DRY_RUN
    };

    var options = {
      method:             "post",
      contentType:        "application/json",
      payload:            JSON.stringify(payload),
      muteHttpExceptions: true
    };

    var response = UrlFetchApp.fetch(WEBHOOK_URL, options);
    var code     = response.getResponseCode();

    if (code === 202) {
      sheet.getRange(triggerRow, 5).setValue("✅ Εστάλη — " + timeStr);
      // D[triggerRow] will be reset to FALSE by the platform when workflow completes
    } else {
      throw new Error("HTTP " + code + ": " + response.getContentText());
    }

  } catch (err) {
    Logger.log("Webhook error: " + err.message);
    sheet.getRange(triggerRow, 5).setValue("❌ Σφάλμα: " + err.message);
    sheet.getRange(triggerRow, 4).setValue(false);
  }
}


/**
 * Manual test — run from Apps Script editor to verify connectivity.
 */
function testWebhook() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  var payload = {
    meeting_number: String(sheet.getRange("D5").getValue()),
    meeting_date:   "2026-04-15",
    meeting_time:   "18:00",
    meeting_type:   "ΤΑΚΤΙΚΗ",
    location:       "ΔΙΑΔΙΚΤΥΑΚΑ",
    agenda_items:   ["Δοκιμαστικό θέμα"],
    trigger_row:    11,
    dry_run:        true
  };
  var options = {
    method:             "post",
    contentType:        "application/json",
    payload:            JSON.stringify(payload),
    muteHttpExceptions: true
  };
  var response = UrlFetchApp.fetch(WEBHOOK_URL, options);
  Logger.log("Response: " + response.getResponseCode() + " " + response.getContentText());
}
