# Archive Workflow - Complete Overview

**Last updated:** 2026-05-27 (post pre-existence-check rewrite)

This document describes the **archive workflow** end-to-end: what it does,
how it's triggered, the seven steps it runs, where each step's data lives,
how rollback works, and how the LLM classification fits in.

---

## 1. What the workflow does, in one sentence

> Takes a document (PDF, DOCX, image, etc.), files it in the SharePoint
> archive under the right πρωτόκολλο number, with the right title and tags,
> and appends/updates the row in the live `[Πρωτόκολλο] Αρχείο ΔΣ.xlsx`.

That's it. Everything below is the safety net around that one operation.

---

## 2. Entry points (5 ways to trigger it)

| Trigger | File | Who |
|---|---|---|
| **`/archive submit` slash command** | `src/integrations/discord/cogs/archive.py::cmd_submit` | Board members in Discord |
| **Right-click message → "Αρχειοθέτηση συνημμένου"** | `src/integrations/discord/cogs/context_menus.py` | Board members, on any message with a PDF |
| **Email to `members@amnesty.org.gr`** with "αρχείο" in subject + PDF attached | `src/workflows/email_intake.py::process_inbox_message` | Anyone in board allow-list (delivered via Graph webhook + daily safety poll) |
| **CLI `ai-assistant archive submit <path>`** | `src/cli/commands.py::cmd_archive_submit` | SecGen at the keyboard |
| **CLI `ai-assistant archive resolve <id> approve`** (resume from reservation-confirm pause) | same | SecGen - only after the bot DMs them about a low-confidence reservation match |

All five entry points end up running the same `ArchiveWorkflow.run(initial_data)`
defined in `src/workflows/archive.py`. The differences are purely in what
ends up in `initial_data` (sender info, test_mode flag, override fields).

---

## 3. The seven steps (in order)

`ArchiveWorkflow.define_steps()` returns this list. The orchestrator
(`src/core/workflow.py::BaseWorkflow.run`) calls each step in order; any
step returning `success=False` aborts the workflow and fires
`rollback(ctx)` (which see, §6).

```
1. intake               Load + validate the file, extract PDF text
2. extract_metadata     Run the LLM, get {title, labels, key_points, …}
3. resolve_protocol     Decide which πρωτόκολλο number to use
4. collision_check      [renamed-but-not-renamed] Check πρωτόκολλο xlsx + SharePoint for an existing entry
5. upload_and_register  Upload PDF to SharePoint + append/update the row
6. notify               Print CLI summary (Discord embed handled by caller)
7. revision_window      Record the 72h amendment-window deadline
```

### Step 1 - `intake`
**Job:** load the file, validate it, extract text.

- If the file isn't a PDF, auto-convert via LibreOffice headless
  (`src/utils/pdf_convert.py`) - DOCX/ODT/RTF/images all supported. Original
  filename is preserved in `ctx["pdf_filename_orig"]` because the LLM uses
  it as a strong signal.
- Extract the first 5000 chars of PDF text (`src/utils/pdf_text.py`). If
  the PDF is encrypted, fail loudly. If it's a scan with no extractable
  text, set `pdf_metadata["is_scan"]=True` so the LLM knows to lean on
  filename/sender/subject instead.
- Set `sender_email`, `sender_name`, `email_subject`, `email_body` in
  context - these flow into the LLM prompt. //Why do we share `sender_email` and `sender_name` to the LLM, I think they can only confuse it for no reason.

### Step 2 - `extract_metadata`
**Job:** run the LLM, capture title/labels/key_points/etc.

- Calls `src/workflows/archive_llm.py::classify_document`. The prompt
  template `_PROMPT_CLASSIFY` reads the live **Ετικέτες** and **Κατηγορίες**
  tabs from the SharePoint πρωτόκολλο xlsx on every invocation (one
  download, both tabs - `read_taxonomy_and_categories()`). So editing
  those tabs in SharePoint immediately changes the bot's behaviour on
  the very next archive run.
- LLM model: configured at `settings.llm.model` (currently
  `gemini-3.1-flash-lite`, with Claude as the production-tier fallback).
- If confidence < 0.7 OR `category_matched == "ad-hoc"`: a second LLM
  call (`refine_against_recent`) anchors the choice against the last 30
  recent entries to nudge toward the archive's existing style. //Make it check against the last 100 recent entries.
- **Filename title override** (added 2026-05-27): if the file is named
  `[YYYY_NNN] <Title>.<ext>` we ignore the LLM's title and use the
  filename's title verbatim. The LLM was hallucinating titles (e.g.
  substituting the sender's name for a candidate's name) - strict
  filename-wins prevents that.

### Step 3 - `resolve_protocol`
**Job:** pick the protocol number to use.

Priority order:
1. CLI override (`--proto 2026_017`)
2. Number embedded in the document filename or extracted by the LLM
   (`existing_protocol` field of the LLM output)
3. **Reserve the next available number** for the current year via the
   SQLite `protocol_reservations` table - this is race-safe across
   concurrent workflows because the reservation considers BOTH the xlsx
   max AND the in-flight reservations.

Stores `ctx["protocol_number"]` + `ctx["protocol_source"]` (`"cli_override"`
/ `"document"` / `"reserved"`).

### Step 4 - `collision_check` - pre-existence check (rewritten 2026-05-27)
**Job:** make sure we don't overwrite anything.

This is the single most-rewritten step in the codebase. As of 2026-05-27
the logic is:

```
if test_mode:                                      → skip entirely
if protocol_source == "reserved":                  → no-op (it's a fresh claim)
else:
    row = find_protocol_row(protocol_number)
    if not row:                                    → proceed (claim is free)
    elif file_exists_for_protocol(protocol_number):
        return FAIL("ήδη αρχειοθετηθεί - manual SecGen task")
    else:
        # row + no file = SecGen pre-reservation
        if title_match_confidence(row.title, llm.title) >= 0.7:
            ctx["is_filling_reservation"] = True
            ctx["reserved_row"] = row              → proceed in fill mode
        else:
            ctx["pending_reservation_confirmation"] = {...}
            publish("archive.reservation_confirmation_needed")
            return FAIL("RESERVATION_CONFIRMATION_NEEDED")
```

- **`file_exists_for_protocol(proto)`** = lists `Αρχείο ανά έτος/{year}/`
  in SharePoint and checks if any file starts with `[{proto}] `.
- **`title_match_confidence(a, b)`** = 1.0 if normalised equal, 0.85 if one
  is a normalised substring of the other, 0.0 otherwise. //Maybe have the LLM check and produce a confidence rating, so that its more flexible (and wont cause any problems if board members forget to name the file like in the reserved protokollo row title).
- **The bot NEVER overwrites archived files.** SecGen handles that case
  manually outside the bot. This is the load-bearing safety rule.

### Step 5 - `upload_and_register`
**Job:** upload to SharePoint, write/update the row.

The behaviour forks on `ctx["is_filling_reservation"]`:

**Normal mode** (new entry):
- Filename = `[{proto}] {llm_title}.pdf`
- Upload to `Αρχείο/Αρχείο ανά έτος/{year}/{filename}`
- Append row to the `{year}` tab: `[proto, today, title, key_points, tags]`
- Commit the SQLite reservation

**Reservation-fill mode** (SecGen pre-existing row):
- Filename = `[{proto}] {SecGen_row_title}.pdf` (SecGen's title is
  definitive - per user spec 2026-05-27)
- Upload to the same path
- **Fill-blanks-only** update: if SecGen's row has blank `Κύρια Σημεία`,
  fill in the LLM's key_points. If blank `Ετικέτες`, fill in the LLM's
  labels. If both are pre-set, the bot doesn't touch them - just attaches
  the file.

**Test mode** (in either path):
- Skip all SharePoint writes
- Return a synthetic result describing what *would* have happened

### Step 6 - `notify`
**Job:** tell the caller what happened.

- CLI: prints a multi-line summary to stdout.
- Discord: the cog reads the workflow result and renders a Rich Embed.
- Email: `email_intake.process_inbox_message` reads the result and sends
  a threaded reply.

### Step 7 - `revision_window`
**Job:** record the 72h deadline for `archive review/cancel` operations.

Stores `ctx["revision_open_until"]` as an ISO8601 timestamp. The CLI's
`archive review` command and the Discord persistent View buttons check
this before allowing any amendments.

---

## 4. The two LLM-driven tabs (`assets/protokollo_taxonomy_template.xlsx`)

These are the **only** way the bot learns how to classify documents.
They live in SharePoint as part of `[Πρωτόκολλο] Αρχείο ΔΣ.xlsx`, and the
workflow reads them at runtime on every archive call. The repo ships a
versioned reference copy at `assets/protokollo_taxonomy_template.xlsx`
generated by `scripts/build_protokollo_taxonomy_template.py`.

### Tab 1 - `Ετικέτες` (the 16-tag taxonomy)

Two columns: **Ετικέτα** | **Περιγραφή / Κανόνας χρήσης**.

The description is the **prompt-engineering surface**. When you edit a
tag's description in SharePoint, the bot reads it on the next archive
run and applies that rule going forward. Example: the `Επιχειρησιακά`
description explicitly tells the LLM "χρήση με φειδώ, τα operational του
Γραφείου πάνε αλλού" - that's why the bot tags `Επιχειρησιακά` rarely
even though it's a defined tag.

The 16 tags in functional groups:
- **Core ΔΣ**: Διοικητικά, Πρακτικά, Προσκλήσεις, Εισηγήσεις, Αναφορές, Υποψηφιότητες
- **Subject matter**: Οικονομικά, Πλάνα, Επιχειρησιακά, Κανονισμοί, Πολιτικές
- **Audience / origin**: Γραφείο, Μέλη, Διεθνές, Εξωτερικά
- **Reserved for future use**: Εκπαίδευση

### Tab 2 - `Κατηγορίες` (canonical title patterns)

Three columns: **Πρότυπο τίτλου** | **Προεπιλεγμένες Ετικέτες** | **Σύμβαση Κύριων Σημείων**.

This is the **style guide**. The LLM consults this list when picking a
title for a new document: "is this similar to one of these patterns?
If yes, mimic the style." The `Σύμβαση Κύριων Σημείων` column documents
how to write the `Κύρια Σημεία` cell for each category - e.g. for
`Πρακτικά` it's intentionally terse (just date + meeting ref), while
for `Εισήγηση` you want 1-2 lines of the actual proposal.

When the LLM's first pass produces `category_matched: "ad-hoc"`, the
fallback pass nudges the title toward the closest existing pattern.

**Both tabs are user-editable, no code change required.** Drop them
into SharePoint, edit the cells, save - the next archive run picks up
the change.

---

## 5. Test-mode safety rules

Set via `STATE_TEST_MODE_ACTIVE` in `discord_bot_state` (toggled by
`/ai-assistant test-mode value:on`) OR per-call `test_mode=True` in the
initial workflow data:

| What's affected | Behaviour |
|---|---|
| `collision_check` step | Skipped entirely |
| `upload_and_register` step | Skipped entirely - no SharePoint upload, no xlsx write |
| **Rollback** | **Never calls `delete_protocol_row` or `delete_file`** (added 2026-05-27 after the rollback deleted 2 real rows from the live πρωτόκολλο) |
| Discord embed | Carries a `[TEST MODE]` banner |
| Email reply | Same banner |

The pinned regression test `test_rollback_in_test_mode_never_touches_sharepoint`
makes sure this guarantee can never be undone.

---

## 6. Rollback semantics

Triggered automatically when any step returns `success=False`, OR
manually via `archive cancel`/`archive resolve … reject`. In order:

```
1. delete_protocol_row(proto)   - UNLESS test_mode OR is_filling_reservation
2. delete_file(remote_path)     - UNLESS test_mode
3. release_protocol_reservation - always (releases the SQLite row)
4. unlink(local_copy_path)      - only the bot's own staging copy
```

**Two "never" rules:**

- **Test mode**: rollback never touches SharePoint. Period. (See §5.)
- **Reservation-fill mode**: rollback never deletes the row. SecGen made
  that row before we got involved; it's not ours to delete. (Added in
  the same 2026-05-27 rewrite.)

---

## 7. Amendments (the 72h revision window)

After a successful archive run, `ctx["revision_open_until"]` is set 72h
ahead. Within that window:

- **CLI**: `ai-assistant archive review <wf_id> "<text>"` - LLM parses
  the free-text into structured amendments (rename title, change tags,
  cancel entirely)
- **Discord**: amend/cancel buttons under the confirmation embed (persistent
  View - survives bot restarts because we re-register on `on_ready` from
  rows in `workflow_state`)

Amendments call `apply_amendments(workflow_id, ctx, amendments)` which:
1. Renames the SharePoint file (if title or proto_id changed)
2. Updates the row's title/key_points/labels in-place via `update_protocol_row`
3. Patches the in-memory context

After 72h the buttons still appear but click into a "revision window closed"
error. The workflow_state row stays as historical record.

---

## 8. Where everything lives (file/path index)

| Concern | Location |
|---|---|
| Workflow definition + steps | `src/workflows/archive.py` |
| LLM prompt + classifier | `src/workflows/archive_llm.py` |
| Amendments helper | `src/workflows/archive.py::apply_amendments` |
| Email intake entry | `src/workflows/email_intake.py` |
| Discord `/archive` slash command | `src/integrations/discord/cogs/archive.py` |
| Right-click context menu | `src/integrations/discord/cogs/context_menus.py` |
| CLI commands | `src/cli/commands.py::cmd_archive_*` |
| OneDrive client (SharePoint I/O) | `src/integrations/m365/onedrive.py` |
| Protocol reservation table + helpers | `src/core/audit.py` (table `protocol_reservations`) |
| Workflow state persistence | `src/core/audit.py` (table `workflow_state`) |
| Audit log | `src/core/audit.py` (table `audit_log`) |
| Local πρωτόκολλο backup | `data/backups/protokollo_latest.xlsx` (auto-refreshed) |
| Taxonomy template (versioned reference) | `assets/protokollo_taxonomy_template.xlsx` |
| Email templates | `assets/email_templates/*.html` |
| Prompts | `src/prompts/*.md` |

---

## 9. State machine - the happy path vs the deferral path

### Happy path (no SecGen reservation, no collision)
```
intake → extract_metadata → resolve_protocol → collision_check (no-op)
       → upload_and_register → notify → revision_window
       → status="completed"
```

### Reservation-fill happy path (SecGen pre-reserved, titles match)
```
intake → extract_metadata → resolve_protocol → collision_check
       → ctx["is_filling_reservation"] = True
       → upload_and_register (uses SecGen's title, fills blanks)
       → notify → revision_window
       → status="completed"
```

### Deferral path (titles don't match)
```
intake → extract_metadata → resolve_protocol → collision_check
       → ctx["pending_reservation_confirmation"] = {...}
       → publish "archive.reservation_confirmation_needed"
       → return success=False
       → status="failed", but ctx preserved

[Discord cog DMs SecGen with buttons | CLI: archive resolve <id>]

         ┌─ approve ──→ ctx pops pending, sets is_filling_reservation
         │              + _start_at_step="upload_and_register"
         │              → wf.run(ctx) re-enters at upload step
         │              → status="completed"
         │
         └─ reject  ──→ wf.rollback(ctx)
                        → status="cancelled"
```

### Hard-fail path (row + file both exist)
```
intake → extract_metadata → resolve_protocol → collision_check
       → file_exists_for_protocol(proto) = True
       → return success=False with "ήδη αρχειοθετηθεί" message
       → status="failed"

No event, no DM, no buttons. SecGen handles manually.
```

### Timeout cleanup
Workflows stuck in `pending_reservation_confirmation` for more than 48h
get auto-rolled-back by the hourly scheduler job
`_reservation_confirm_timeout_job` and marked
`state="failed_reservation_timeout"`.

---

## 10. Quick reference - operator commands

```bash
# CLI
ai-assistant archive submit "/path/to/[2026_017] My File.pdf"
ai-assistant archive submit "/path/file.pdf" --proto 2026_017 --title "Override"
ai-assistant archive review <wf_id> "rename to New Title"
ai-assistant archive cancel <wf_id>
ai-assistant archive resolve <wf_id> approve   # only after a SecGen DM/email
ai-assistant archive resolve <wf_id> reject
ai-assistant archive list

# Discord (in any channel)
/archive submit file:<attachment> [title:?] [proto:?] [tags:?] [sender:?]
# Right-click any message → Αρχειοθέτηση συνημμένου
```

```bash
# Backup recovery (if SharePoint πρωτόκολλο gets corrupted/deleted)
ai-assistant onedrive backup-status
ai-assistant onedrive backup-restore "C:\path\to\restored.xlsx"
```

---

## 11. Things to know before you change anything

1. **The Ετικέτες/Κατηγορίες tabs are live config, not code.** Edit them in
   SharePoint and the bot picks up the change on the next archive run.
   No deployment needed.

2. **The two safety rules are non-negotiable**: (a) test-mode rollback
   never touches SharePoint, (b) reservation-fill rollback never deletes
   SecGen's row. Both have pinned regression tests. If you must change
   the contract, update both the code AND the test.

3. **The bot never overwrites archived files.** If a row + file both
   exist for a protocol number, the workflow refuses. There is no
   "force" flag. Add one only after thinking really hard about why.

4. **The local backup at `data/backups/protokollo_latest.xlsx` is the
   recovery story.** It refreshes on every SharePoint download. If you
   lose the live xlsx, this gets you most of the way back. (SharePoint
   version history is the authoritative recovery - see §6 of the runbook.)

5. **Don't reach into `integrations/m365/onedrive.py` from a workflow
   step.** All SharePoint I/O should go through the `OneDriveClient`
   methods. Workflows should not know about Graph URLs or auth - that's
   the boundary the code review marked as non-negotiable.
