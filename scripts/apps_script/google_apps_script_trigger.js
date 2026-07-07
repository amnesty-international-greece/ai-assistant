/**
 * AI Assistant Platform - Google Sheets Auto-Trigger
 * =================================================
 *
 * Trigger model
 * -------------
 * Fires on EVERY cell edit (Sheets' built-in onEdit). The handler ignores
 * every edit except the three approval checkboxes in D16, D17, D18
 * (Πρόεδρος, Γενικός Γραμματέας, Διευθυντής). When ALL THREE become TRUE,
 * the webhook is POSTed to AI Assistant to start the invitation workflow.
 *
 * Payload
 * -------
 * Minimal: just the meeting_ref (D5) for idempotency, plus start_at_step
 * to tell the workflow to skip the manual scheduling-email + approval gate
 * and jump straight to read_agenda.  Everything else (date, time, type,
 * location, agenda items, durations) is read directly from this sheet by
 * the workflow's read_agenda step - no point duplicating that work here.
 *
 * Idempotency
 * -----------
 * Handled entirely by the Python side: webhooks.py looks up workflow_state
 * for any in-progress invitation with the same raw_meeting_id and returns
 * "already_in_progress" without starting a duplicate.  Nothing is written
 * back to the sheet for tracking purposes.
 *
 * Script-owned protection
 * -----------------------
 * When all 3 boxes flip to TRUE we add a sheet-wide protected range with
 * description "ai-assistant:cycle-locked". This sits ON TOP of the user's
 * manually-configured per-range protections (it does not modify or replace
 * them). The Python reset removes ONLY the protection whose description
 * matches "ai-assistant:cycle-locked" - the user's protections stay intact.
 *
 * Installation
 * ------------
 *   1. Google Sheet → Extensions → Apps Script → replace Code.gs with this file.
 *   2. Save (disk icon).
 *   3. Edit WEBHOOK_URL below to your public AI Assistant endpoint.
 *   4. Run setup() once from the editor - it installs the onEdit trigger.
 *
 * Debug helpers
 * -------------
 *   - testTrigger()    : reads D5 from this sheet and fires a test_mode
 *                        payload at the webhook (skips the requirement
 *                        of physically checking the boxes).
 *   - resetSheetState(): manual recovery - removes the script-owned protection.
 *                        Python normally does this via reset_agenda_sheet()
 *                        after minutes finalize.
 */

// ── CONFIG ────────────────────────────────────────────────────────────────────
var WEBHOOK_URL = "https://127.0.0.1:8000/webhooks/invite";   // replace with public URL
var LOCK_DESCRIPTION = "ai-assistant:cycle-locked";
var APPROVAL_RANGE   = "D16:D18";   // Πρόεδρος / Γενικός Γραμματέας / Διευθυντής
var MEETING_REF_CELL = "D5";
// Note: test_mode is intentionally NOT a setting here.  An auto-trigger from
// real board approval is by definition a live cycle.  If you want to dry-run
// the full workflow as a developer, use the CLI: `ai-assistant invite --test`.
// The testTrigger() helper below hardcodes test_mode=true for connection checks.
// ─────────────────────────────────────────────────────────────────────────────


/**
 * Run once from the editor to install the onEdit trigger.
 */
function setup() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === "onSheetEdit") ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger("onSheetEdit")
    .forSpreadsheet(SpreadsheetApp.getActive())
    .onEdit()
    .create();
  Logger.log("onSheetEdit trigger installed.");
}


/**
 * onEdit handler - fires on EVERY cell edit.
 *
 * Fast-path: if the edited cell is not in D16:D18 we exit immediately
 * (a few microseconds). Real work only happens when an approval checkbox
 * changes.
 */
function onSheetEdit(e) {
  if (!e || !e.range) return;

  var sheet = e.range.getSheet();
  var col   = e.range.getColumn();
  var row   = e.range.getRow();

  // Only react to edits in D16, D17, D18
  if (col !== 4) return;
  if (row < 16 || row > 18) return;

  // Read all three approval cells - proceed only if all are TRUE
  var values = sheet.getRange(APPROVAL_RANGE).getValues();
  var allChecked = values.every(function(r) { return r[0] === true; });
  if (!allChecked) return;

  // Read the meeting_ref from D5 - used only for idempotency on the Python side
  var meetingRef = String(sheet.getRange(MEETING_REF_CELL).getValue()).trim();
  if (!meetingRef) {
    Logger.log("Approval boxes checked but " + MEETING_REF_CELL + " is empty - aborting.");
    return;
  }

  // Fire the webhook with a MINIMAL payload.  The workflow's read_agenda
  // step will pull date/time/type/location/agenda items from THIS sheet -
  // no point sending them via the payload only to be overwritten.
  try {
    var payload = {
      raw_meeting_id: meetingRef,
      start_at_step:  "read_agenda"
    };
    var response = _postPayload(payload);
    var code = response.getResponseCode();

    if (code >= 200 && code < 300) {
      _addCycleLock(sheet);
      Logger.log("Webhook OK (HTTP " + code + ") for " + meetingRef);
    } else {
      Logger.log("Webhook failed (HTTP " + code + "): " + response.getContentText());
    }
  } catch (err) {
    Logger.log("onSheetEdit error: " + err.message);
  }
}


/**
 * POST the payload to the AI-in-AI webhook.
 */
function _postPayload(payload) {
  var options = {
    method:             "post",
    contentType:        "application/json",
    payload:            JSON.stringify(payload),
    muteHttpExceptions: true
  };
  return UrlFetchApp.fetch(WEBHOOK_URL, options);
}


/**
 * Add the script-owned "cycle locked" protection.
 *
 * IMPORTANT: this is identified by its DESCRIPTION (not name, not range -
 * the user has their own per-range protections we must not touch). We only
 * add a new protection if one with our description does not already exist,
 * and we only ever remove a protection whose description matches.
 */
function _addCycleLock(sheet) {
  var existing = sheet.getProtections(SpreadsheetApp.ProtectionType.RANGE);
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].getDescription() === LOCK_DESCRIPTION) return;  // already locked
  }
  // Cover A1:K30 - comfortably wider than any interactive cell
  // (D5..D18 metadata + H7:K agenda items).
  var range = sheet.getRange("A1:K30");
  var protection = range.protect().setDescription(LOCK_DESCRIPTION);

  // Empty editor list - everyone is excluded for the locked window.
  // (We must keep the script owner as an editor; Apps Script enforces
  // at least one editor per protection.)
  var me = Session.getEffectiveUser();
  protection.addEditor(me);
  protection.removeEditors(protection.getEditors().filter(function(u) {
    return u.getEmail() !== me.getEmail();
  }));
  if (protection.canDomainEdit()) protection.setDomainEdit(false);
}


/**
 * Manual debug - mirrors onSheetEdit's payload but reads from the CURRENT
 * sheet state, so the test exercises the same data path as a real cycle.
 * test_mode is hardcoded TRUE so any side effects (Zoom, emails) stay
 * redirected to settings.testing.test_email on the Python side.
 */
function testTrigger() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  var meetingRef = String(sheet.getRange(MEETING_REF_CELL).getValue()).trim();
  if (!meetingRef) {
    Logger.log("ERROR: " + MEETING_REF_CELL + " is empty - cannot build test payload.");
    return;
  }
  var payload = {
    raw_meeting_id: meetingRef,
    start_at_step:  "read_agenda",
    test_mode:      true
  };
  var response = _postPayload(payload);
  Logger.log("testTrigger payload: " + JSON.stringify(payload));
  Logger.log("Response: " + response.getResponseCode() + " " + response.getContentText());
}


/**
 * Manual recovery - remove our script-owned protection.
 * Python normally does this via reset_agenda_sheet() after minutes finalize;
 * this function is only for manual recovery from a stuck state.
 */
function resetSheetState() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  var protections = sheet.getProtections(SpreadsheetApp.ProtectionType.RANGE);
  var removed = 0;
  for (var i = 0; i < protections.length; i++) {
    if (protections[i].getDescription() === LOCK_DESCRIPTION) {
      protections[i].remove();
      removed += 1;
    }
  }
  Logger.log("Sheet state reset: removed " + removed + " script-owned protection(s).");
}
