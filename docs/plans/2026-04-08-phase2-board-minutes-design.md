# Phase 2 Design: Board Meeting Minutes Workflow

**Date:** 2026-04-08
**Status:** Approved
**Author:** Claude (Opus) + Georgios Athanasias

---

## Overview

Automate the board meeting minutes lifecycle: from raw sources (SecGen's
Google Doc notes + Zoom transcript) through Claude-assisted drafting, board
review, finalization with digital signatures, archiving, and decision
extraction to the Βιβλίο Αποφάσεων.

## Workflow Steps

### Step 1 - Select sources

**Inputs:**
- A Google Drive **folder** (configured once as `minutes_drafts_folder_id`
  in `config.yaml`) containing the SecGen's draft notes as Google Docs.
- The system lists all Docs in the folder, the user picks one via numbered
  CLI menu (same UX as the agenda sheet selector in Phase 1).
- The system queries the **Zoom recordings list** to find the meeting
  recording whose title matches `Συνεδρίαση ΔΣ{nn}-{yyyy}`. The user can
  override if the auto-match is wrong (same numbered-menu pattern).
- Zoom transcript is downloaded via `ZoomClient.get_transcript()`.

**Outputs in context:**
- `secgen_notes`: full text of the selected Google Doc
- `zoom_transcript`: Zoom VTT transcript text
- `meeting_ref`: e.g. `ΔΣ03-2026`
- `meeting_number`: e.g. `3`
- `meeting_year`: e.g. `2026`
- `source_doc_id`: Google Doc ID of the SecGen's notes

### Step 2 - Merge & draft with Claude

- System prompt from `data/prompts/board_minutes.md` (already exists).
- Claude receives **both sources** with clear role instructions:
  - SecGen notes are **authoritative** for decisions, protocol references,
    and formal wording.
  - Zoom transcript fills in discussion flow, attendance, speakers.
- Claude returns structured JSON: `{title, metadata, sections[], decisions[]}`.
- The system validates the JSON schema before proceeding.

**Output:** `draft_json` - the structured minutes content.

### Step 3 - Write draft back to Google Doc

Instead of creating a new file, the system **replaces the content of the
original source Google Doc** with the formatted draft minutes. This keeps
the file count minimal and lets the board comment directly on the same
document they'll eventually approve.

- Use Google Docs API to clear and write the new content.
- Rename the Doc title to: `[Πρόχειρο] Πρακτικά - Συνεδρίαση ΔΣ03-2026`
- The draft remains a live Google Doc in Drive until finalization.

**Output:** `draft_doc_id` (same as `source_doc_id`), `draft_doc_url`.

### Step 4 - [APPROVAL GATE] + Auto-share with board

- Workflow pauses for SecGen review (standard `requires_approval=True`).
- On `approve_and_resume()`, the system sends an email to the board via
  **Gmail** (not Brevo - this is internal board communication) containing:
  - Link to the Google Doc draft
  - Brief message: configurable via `workflows.board_meeting.minutes_share_message`
- Recipients: `board_members` list from `config.yaml`.

**Output:** `shared_at` timestamp.

### Step 5 - Finalize (PDF + signatures + archive)

Triggered in one of two ways:
1. **Explicit:** `python -m src.cli minutes finalize --meeting ΔΣ03-2026`
2. **Automatic detection:** When processing a later meeting's minutes and
   Claude extracts a decision like "Επικύρωση των πρακτικών υπ' αριθμόν
   ΔΣ03-2026", the system triggers finalization of that referenced minutes.

Finalization sequence:
1. Read the current Google Doc content (may have been edited after sharing).
2. Generate PDF with digital signatures (President + SecGen) embedded at
   defined coordinates. Signature images stored in `brand/Signatures/`.
3. Assign next protocol number: read the Πρωτόκολλο sheet for the current
   year, find the last `{year}_{nnn}` entry, increment.
4. Rename file: `[2026_015] Πρακτικά - Συνεδρίαση ΔΣ03-2026.pdf`
5. Upload PDF to OneDrive archive folder (`onedrive.archive_root/{year}/`).
6. Register in Πρωτόκολλο spreadsheet: append row with protocol number,
   date, document title, key points summary, tags `Διοικητικά, Πρακτικά`.
7. Rename Google Doc from `[Πρόχειρο]` to `[Τελικό]`.

**Outputs:** `pdf_path`, `protocol_number`, `archive_url`.

### Step 6 - Extract decisions → Βιβλίο Αποφάσεων

- Claude parses the finalized minutes for all formal decisions.
- For each decision, generates the next `ΔΣ{nn}-{mm}-{yyyy}` number by:
  - Reading the current year sheet of the Βιβλίο Αποφάσεων
  - Finding the last row for this meeting number
  - If no entries yet for this meeting, starting at `ΔΣ01-{mm}-{yyyy}`
- Appends rows to Google Sheets: column A = decision number, column B = text.
- All writes logged to audit trail.

**Output:** `decisions_written` count, `decision_numbers` list.

---

## Numbering Systems

| System | Format | Example | Scope |
|--------|--------|---------|-------|
| Board meetings | `ΔΣ{seq}-{year}` | ΔΣ03-2026 | Sequential per year |
| Board decisions | `ΔΣ{decision}-{meeting}-{year}` | ΔΣ02-03-2026 | Decision seq resets per meeting |
| Protocol (archive) | `{year}_{seq:03d}` | 2026_015 | Running counter per year |

## CLI Commands

```
python -m src.cli minutes                     # Full workflow (steps 1-4)
python -m src.cli minutes finalize --meeting ΔΣ03-2026   # Steps 5-6
python -m src.cli minutes list-drafts         # Show pending drafts in Drive
```

Flags (same as Phase 1):
- `--test` - redirects emails to `testing.dry_run_email`, skips archive
- `--manual` - skip Zoom transcript (use SecGen notes only)

## Config Additions

```yaml
google:
  minutes_drafts_folder_id: ""   # Google Drive folder with SecGen's draft notes
  protokollo_sheet_id: ""        # [Πρωτόκολλο] Αρχείο ΔΣ spreadsheet

workflows:
  board_meeting:
    minutes_share_message: >-
      Σας κοινοποιούνται τα πρόχειρα πρακτικά προς σχολιασμό.
      Παρακαλώ αφήστε τα σχόλιά σας απευθείας στο έγγραφο.
```

## New Integration Methods Needed

### Google Drive / Docs
- `list_docs_in_folder(folder_id)` - list Google Docs in a folder
- `read_doc_content(doc_id)` - get full text of a Google Doc
- `replace_doc_content(doc_id, content)` - clear and rewrite a Google Doc
- `rename_doc(doc_id, new_title)` - rename a Google Doc

### Google Sheets (write)
- `append_rows(sheet_id, sheet_name, rows)` - append rows to a sheet
- `read_last_row(sheet_id, sheet_name)` - read last non-empty row

### Zoom
- `list_recordings(from_date, to_date)` - list recent recordings
- (existing: `get_transcript(meeting_id)`)

### PDF
- `embed_signatures(pdf_path, signatures_config)` - overlay signature images

### Gmail
- `send_email(to, subject, body, html_body)` - send via Gmail API

## Files to Create/Modify

**New files:**
- `src/workflows/board_meeting_minutes.py` - full implementation (replace skeleton)
- `tests/test_minutes_workflow.py` - comprehensive tests

**Modified files:**
- `src/integrations/google_drive.py` - add Docs API methods
- `src/integrations/google_sheets.py` or extend `google_drive.py` - Sheets write
- `src/integrations/zoom.py` - add `list_recordings()`
- `src/integrations/gmail.py` - add `send_email()` if not present
- `src/documents/pdf_generator.py` - add signature embedding
- `src/cli/commands.py` - add `minutes` subcommand with sub-subcommands
- `config.yaml` - add new config fields
- `data/prompts/board_minutes.md` - enhance with merge instructions

## Testing Strategy

- Mock all API calls (Google, Zoom, Gmail, OneDrive)
- Test each step independently (same pattern as Phase 1)
- Test decision number generation with edge cases
- Test protocol number auto-increment
- Test auto-detection of minutes approval in later meetings
- Test `--test` mode email redirection

## Future Enhancement (Deferred)

- **Comment incorporation:** CLI command to read Google Doc comments, feed
  to Claude for revision, update the Doc in-place. Deferred per user request.
