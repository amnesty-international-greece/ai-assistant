# Debug CLI - testing one workflow step in isolation

The `debug` command group lets you run a **single workflow step** (or a short
chain of steps) against a canonical fake context, instead of running a whole
workflow end-to-end. It is the fastest way to answer "does this one step still
work after my change?" without sitting through approval gates, real emails, or
a full Zoom + PDF + newsletter run.

Key properties:

- **`test_mode` is always forced on.** There is no live escape hatch in debug.
  Steps that honour `test_mode` (archive, the invitation/minutes/egkyklios
  archive steps, etc.) take their skip path automatically.
- **Nothing is persisted.** The runner never writes `workflow_state`, never
  advances a step index, and **never rolls back** - so whatever a step *does*
  do (see [Side effects](#side-effects)) stays done.
- **Only the steps you ask for run**, in the order you list them. Output of one
  step is threaded into the context of the next.

## Command summary

| Command | What it does |
|---|---|
| `debug list [workflow]` | List all workflows, or the steps + fixture keys of one |
| `debug fixture <workflow> [--json]` | Print a workflow's canonical fake context |
| `debug run <workflow> <step[,step2,...]> [--set k=v ...] [--from-state ID] [--show-ctx] [--json]` | Run one step (or a chain) in `test_mode` |

The four debuggable workflows are `archive`, `board_meeting_invitation`,
`board_meeting_minutes`, and `egkyklios_general`.

Invoke via the installed entry point (`ai-assistant ...`) or the module form
(`python -m src.cli ...`); they are equivalent. Examples below use the module
form.

## debug list

List every workflow and its step count:

```
$ python -m src.cli debug list
============================================================
  Debuggable workflows
============================================================

  archive  (7 steps)
  board_meeting_invitation  (12 steps)
  board_meeting_minutes  (6 steps)
  egkyklios_general  (10 steps)

  Inspect one with:  ai-assistant debug list <workflow>
```

Pass a workflow name to see its ordered steps (approval gates are flagged) and
the full set of fixture keys:

```
$ python -m src.cli debug list board_meeting_invitation
============================================================
  board_meeting_invitation - 12 steps
============================================================

  0. send_scheduling_email - Send scheduling email to board via M365
  1. await_approval - Wait for SecGen approval of final agenda + date  [approval gate]
  2. read_agenda - Read agenda from Google Sheets (single tab)
  3. init_meeting_thread - Initialise board email thread anchor
  4. schedule_zoom - Schedule Zoom meeting
  5. draft_invitation - Draft invitation
  6. generate_pdf - Generate PDF document
  7. approval - Review and approve draft  [approval gate]
  8. archive - Archive PDF to OneDrive
  9. send_board_email - Send final invitation reply to board via M365
  10. send_newsletter - Create campaign + (test or live) send
  11. confirm_newsletter - Confirm live send  [approval gate]

  fixture keys: _skip_approval_guard, _skip_read_agenda, agenda_items, agenda_sheet_id, archive_file_id, archive_share_link, brevo_list_ids, brevo_template_id, bus_event_published, email_thread_anchor, invitation_replacements, invitation_zoom_url, location, meeting_date, meeting_duration_minutes, meeting_location, meeting_number, meeting_ref_override, meeting_time, meeting_type, newsletter_campaign_id, newsletter_sent, newsletter_skipped, pdf_filename, pdf_path, poll_url, protocol_number, raw_meeting_id, response_deadline, zoom_join_url, zoom_meeting_id, zoom_passcode
```

## debug fixture

Print the canonical fake context a workflow's steps run against. Every key any
step reads from `ctx` is present with a safe placeholder value, so a step can
run without a `KeyError`. Add `--json` for machine-readable output.

```
$ python -m src.cli debug fixture archive
============================================================
  archive - debug_fixture()
============================================================

{ 'pdf_path': 'data/debug/sample.pdf',
  'sender_email': 'debug@amnesty.org.gr',
  'sender_name': 'Debug Sender',
  'email_subject': '[Debug] Δοκιμαστικό έγγραφο',
  'email_body': 'Δοκιμαστικό σώμα email.',
  '_skip_workbook_refresh': True,
  '_skip_llm': True,
  'pdf_filename_orig': '[2099_999] Δοκιμαστικός τίτλος.pdf',
  'pdf_text': 'Δοκιμαστικό περιεχόμενο PDF.',
  'pdf_metadata': {'page_count': 1, 'char_count': 30, 'is_scan': False},
  'llm_result': { 'title': 'Δοκιμαστικός τίτλος',
                  'labels': ['Διοικητικά', 'Δοκιμή'],
                  'key_points': 'Δοκιμαστικά κύρια σημεία.',
                  'confidence': 0.95,
                  'category_matched': 'Διοικητικά',
                  'existing_protocol': ''},
  'override_title': '',
  'override_labels': [],
  'override_protocol': '2099_999',
  'protocol_number': '2099_999',
  'protocol_source': 'cli_override',
  'is_filling_reservation': False,
  'reserved_row': {},
  'remote_filename': '[2099_999] Δοκιμαστικός τίτλος.pdf',
  'remote_folder': 'Αρχείο/Αρχείο ανά έτος/2099',
  'upload_file_id': 'debug-file-id',
  'share_link': 'https://example.invalid/share/debug',
  'local_copy_path': ''}
```

Note that several keys are escape hatches the fixtures bake in to keep debug
runs deterministic and side-effect-free - e.g. `_skip_llm` (archive bypasses
the live LLM call), `_skip_read_agenda` (invitation uses the supplied
`agenda_items` instead of reading Google Sheets), and `override_protocol` /
a valid `protocol_number` (so `resolve_protocol` echoes a number instead of
reserving a fresh one from the protocol DB).

## debug run

Run one step, or a comma-separated chain. The example below runs two archive
steps; `--show-ctx` additionally prints the ctx keys each step produced.

```
$ python -m src.cli debug run archive resolve_protocol,upload_and_register --show-ctx
2026-05-31 03:23:18 | src.core.audit | INFO | Database initialized at data\amnesty.db

============================================================
  debug run archive  [TEST MODE]
============================================================

  Steps: resolve_protocol, upload_and_register

  -- resolve_protocol --
     success: True
     message: Protocol number set from CLI: 2099_999
     data (keys produced):
       {'protocol_number': '2099_999', 'protocol_source': 'cli_override'}

  -- upload_and_register --
     success: True
     message: [TEST] Would upload to Αρχείο ανά έτος/2099/[2099_999] Δοκιμαστικός τίτλος.pdf and append protocol row 2099_999
     data (keys produced):
       { 'remote_filename': '[2099_999] Δοκιμαστικός τίτλος.pdf',
         'remote_folder': 'Αρχείο ανά έτος/2099',
         'upload_file_id': '',
         'share_link': '',
         'register_skipped': True}
```

(The `Database initialized ...` line goes to the log; `debug run` calls
`init_db()` because some steps touch the audit DB. It is not part of the step
output.)

### How the context is assembled

For `debug run`, the context is built in this exact order, each layer
overlaying the previous:

1. **Fixture** - `dict(<Workflow>.debug_fixture())`, the canonical fake ctx.
2. **`--from-state ID` overlay** - if given, the persisted `context` of that
   `workflow_state` row is merged on top (see
   [Replaying a real run](#replaying-a-real-run)).
3. **`--set k=v` overlay** - each `--set` pair is merged on top of that.
4. **`test_mode=True`** - forced last, so it cannot be overridden.

### `--set` value parsing

`--set` splits on the **first** `=` only. The value is parsed as **JSON**, so
types are preserved; if JSON parsing fails the raw string is used verbatim.
`--set` is repeatable.

```
--set meeting_number=99                  # int 99
--set meeting_time=18:00                 # not valid JSON -> string "18:00"
--set agenda_items='["Θέμα Α","Θέμα Β"]' # JSON list of two strings
--set override_protocol=                 # empty string (exercises live-reserve path)
```

### Step chaining

`debug run <wf> a,b,c` runs steps `a`, `b`, `c` in order. After each step the
runner does `ctx.update(result.data)` so a later step sees what an earlier one
produced - exactly like the real workflow engine threads data between steps.
If a step **raises**, the chain stops there (the exception is printed; no later
step runs). A step that merely returns `success=False` does **not** stop the
chain.

## Side effects

`test_mode` is forced, but it does **not** neutralise every step. Some steps
still perform real external actions, and because debug **never rolls back**,
those actions persist after the command exits. Know what you are triggering
before you run these.

The biggest trap is **`board_meeting_invitation schedule_zoom`**: it creates a
**real Zoom meeting** every time, and debug will not delete it.

| Workflow | Step | What it does under debug (test_mode, no rollback) |
|---|---|---|
| board_meeting_invitation | `send_scheduling_email` | Sends a real email to `testing.test_email` (skipped only if M365 creds unset). Publishes bus events; enqueues a Discord pending action. |
| board_meeting_invitation | `schedule_zoom` | **Creates a REAL Zoom meeting** and registers the test inbox as a participant (Zoom emails it a join link). **Not rolled back** - cancel it manually. |
| board_meeting_invitation | `generate_pdf` | Copies the Google Doc template, renders a PDF to `data/output/`, then trashes the working Google Doc. Live Google Drive/Docs calls. |
| board_meeting_invitation | `send_board_email` | Sends a real threaded reply to `testing.test_email` (needs an `email_thread_anchor`). Publishes a bus mirror event. |
| board_meeting_invitation | `send_newsletter` | Creates a Brevo **draft** campaign and sends one **test** email to `testing.test_email`. The draft is left in Brevo (not deleted in debug). |
| board_meeting_invitation | `confirm_newsletter` | In test_mode, publishes `board.meeting.scheduled` to the bus, spinning up the Discord choreography against sandbox channels. Not torn down. |
| egkyklios_general | `gather_sources` | Live **read** of the SQLite audit DB (idempotency check + source listing). May return `success=False` if a non-cancelled draft overlaps the fixture period - move it with `--set period_start=...`. |
| egkyklios_general | `draft_circular` | Calls the **LLM** (Claude), writes a Markdown draft to `data/egkyklios/drafts/`, and **creates a DB draft row**. |
| egkyklios_general | `render_pdf` | Renders a branded PDF to `data/egkyklios/drafts/` (and updates the DB row if `egkyklios_draft_id` is set). |
| egkyklios_general | `notify_board_for_review` | Sends a real email to `testing.test_email` with the PDF attached (skipped if M365 unset); publishes a bus event; sets the draft status. |
| egkyklios_general | `send_brevo_campaign` | Creates a Brevo campaign + test send (test_mode). |
| egkyklios_general | `publish_event` | Publishes `EVENT_EGKYKLIOS_PUBLISHED` to the event bus. |
| board_meeting_minutes | `select_sources` | Live **read** of the Google Doc (`source_doc_id`) and, unless skipped, Zoom recordings. |
| board_meeting_minutes | `draft_minutes` | Reads the system prompt from disk and calls the **LLM** (Claude). |
| board_meeting_minutes | `write_draft_to_doc` | **Writes** the draft back to the Google Doc and renames it. |
| board_meeting_minutes | `approval_and_share` | Sends a real Gmail to `testing.test_email` in test_mode (skipped if no test email). |
| board_meeting_minutes | `finalize` | Exports the Google Doc as PDF (live Google) and signs it locally. Persistent SharePoint upload, Πρωτόκολλο write, Doc rename, and the agenda-sheet reset are **skipped under test_mode**. |
| board_meeting_minutes | `extract_decisions` | Live **read** of the Βιβλίο Αποφάσεων Google Sheet to compute the next decision number; the **write is skipped under test_mode**. |

Steps **fully neutralised by test_mode** (safe - no external write):

| Workflow | Step | Behaviour |
|---|---|---|
| archive | `upload_and_register` | Returns "[TEST] Would upload ..." - no SharePoint upload, no xlsx write. |
| archive | `collision_check` | Skipped entirely under test_mode. |
| board_meeting_invitation | `archive` | Returns "[TEST] Archive skipped". |
| egkyklios_general | `archive_to_sharepoint` | Returns "[TEST] ... skipped" - no SharePoint upload, no protocol write. |

The remaining steps (`archive intake / extract_metadata / resolve_protocol /
notify / revision_window`, the various `await_approval` / `approval` gates,
`read_agenda` with `_skip_read_agenda`, `init_meeting_thread`,
`draft_invitation`, and the egkyklios `extract_briefing_texts` /
`extract_meeting_summaries`) are pure under the fixture - they read/format ctx
only and perform no external I/O. These are the steps on the allow-list in
`tests/workflows/test_debug_fixtures.py`.

## Replaying a real run

`--from-state <workflow_id>` overlays the persisted context of a real
`workflow_state` row onto the fixture, so you can re-run a single step with the
*actual* data a production run produced - handy for reproducing a failure.

```
python -m src.cli debug run board_meeting_invitation draft_invitation --from-state 3f9a1c2b --show-ctx
```

The `workflow_id` is the 8-character id shown when a workflow runs and in the
audit log; it is the primary key of the `workflow_state` table. If the id is
unknown or its blob is unparseable, the overlay is skipped with a warning and
the bare fixture is used.

## Adding a new step

> When you add a `WorkflowStep` to any workflow, extend that workflow's
> `debug_fixture()` with every new ctx key the step reads, so
> `python -m src.cli debug run <workflow> <step>` works without a KeyError. If
> the step is pure (no external I/O), add it to the allow-list in
> `tests/workflows/test_debug_fixtures.py`.

The regression test `tests/workflows/test_debug_fixtures.py` enforces the first
half of this rule: it actually runs every allow-listed pure step against the
fixture and fails if a `KeyError` (or `AttributeError` / `TypeError`) surfaces,
which is the signature of a fixture that forgot a key.
