# Architecture Review: From One Section's Tool to a Platform Other Sections Can Adopt

**Date:** 2026-09-15
**Scope:** the whole repository: code, data, assets, configuration, documentation, tests.
**Question asked:** is the way the code, data and assets are organised the right foundation for a modular platform that other Amnesty sections, using other tools and processes, can adopt and extend with their own workflows?
**Method:** direct reading of the core files, git history and the earlier reviews; four parallel audits (section-specific hardcoding, integration coupling, repository organisation, workflow engine); the 93 findings of the September data-layer review; and independent verification of every bug this document calls a bug.

Companion documents: `FOUNDATION.md` (the original plan, April 2026), `docs/code_structure_review.md` (the May 2026 organisation review, whose housekeeping items have landed), `framework/ETHICS_FRAMEWORK.md` (the principles the platform commits to).

---

## 1. The verdict in one page

The codebase is in better shape than most volunteer-built systems of its size: 33,400 lines, 94 modules, 756 passing tests with a database-isolation fixture that keeps tests off live data, a real layering of `core` / `workflows` / `integrations` / `cli` + `api`, typed configuration, and a deterministic minutes core that a model never touches. The May review's clean-ups were done. Nothing here needs to be thrown away.

But it was designed, in `FOUNDATION.md`, for **one** section, **one** language, **one** tool stack and **one** operator, and each of those "ones" has become load-bearing in a place where a second section would have to cut it out:

| Assumption baked in | Where it shows | Measured |
|---|---|---|
| One organisation | Emails, brand, org name, numbering formats and Greek copy live in Python | 53% of source files contain Greek literals; `board@`/`director@`/`secgen@` redefined in 5+ files; brand palette defined twice; meeting-ref format implemented 5 times, Greek month tables at least 5 times; all 8 prompts name Amnesty Greece |
| One tool stack | Workflows construct Zoom/Google/M365/Brevo clients in place; platform IDs are the workflow state | Only the LLM client is swappable through config; Zoom URLs, Google Doc IDs, SharePoint item IDs and Brevo list numbers travel through `ctx` and through the shared event payloads |
| One operator at a keyboard | Approval and resume are hand-rolled per workflow; the CLI hardcodes per-step dialogue | Four incompatible resume conventions; `input()` called inside four workflow steps; one resume path calls a method that does not exist |
| One process, small data | SQLite with no domain model; numbering by several code paths | No `meetings`, `people` or `votes` table; protocol numbers allocated by 4 paths with 2 gaps already in the live register (2026_017, 2026_029); decisions numbered by 3 counters; a verbatim transcript stored inside `workflow_state` |

**Would I arrive at the same design from scratch for a multi-section platform? No. Would I rewrite it? Also no.** The right move is to separate three things the code currently mixes, one seam at a time, while the system keeps running:

1. **A platform core** that knows what a meeting, a decision, a protocol register and an approval gate are, but not whose, in which language, or on which vendor.
2. **Adapters** that implement narrow capability interfaces ("ports") for Zoom, Google, Microsoft 365, Brevo, Discord, Whisper and so on, chosen by configuration.
3. **A section profile**: a code-free package of identity, roles and roster, numbering formats, statute rules, locale, prompts, templates, brand and taxonomy. Amnesty Greece becomes the first profile. A second section writes its own profile and, where its tools differ, an adapter.

Section 6 lays out that target; section 7 gives a phased migration in which every phase ships on its own and the ΔΣ minutes work continues on top. Section 2 lists what must be fixed regardless, because it is broken today.

---

## 2. Broken today, independent of any redesign

These were found during the audits and verified by hand. They are not architectural opinions.

| # | Defect | Evidence | Consequence |
|---|---|---|---|
| B1 | **The circular workflow's approval-resume path calls a method that does not exist.** `wf.resume(workflow_id, approval_granted=True)` is called from the CLI and the Discord `/board` cog; no `resume` is defined on `BaseWorkflow` or any subclass. | `src/cli/commands.py:1166`, `src/integrations/discord/cogs/board.py:127`; `grep -rn "def resume" src/` returns nothing; no test exercises either call site | Approving a Γενική Εγκύκλιος from CLI or Discord raises `AttributeError`. The reference example of "how to resume a workflow" is dead code. |
| B2 | **Workflow steps block on the keyboard.** `input()` is called inside steps of the invitation workflow. | `src/workflows/board_meeting_invitation.py:654, 694, 761, 864` | A run started by the Sheets webhook or the scheduler hangs forever if it reaches one of these branches. `docs/DEBUG_CLI.md:204-210` already documents `schedule_zoom` creating a real Zoom meeting in debug runs because it has no `test_mode` check. |
| B3 | **Wrong recipient domain.** `board@amnesty.gr` (no `.org`) is used as a recipient in two code paths; twelve files use `board@amnesty.org.gr`. | `src/cli/commands.py:223`, `src/integrations/discord/cogs/board.py:336` | Board mail sent from those two paths goes to a domain the section does not own. |
| B4 | **The πρωτόκολλο register has gaps and the code cannot notice.** Four code paths allocate protocol numbers; only the archive workflow uses the reservation table; the other three read a local snapshot of the xlsx that only the archive workflow refreshes; two readers use different algorithms (last row vs numeric max). Live register: 31 entries, max sequence 33, numbers 017 and 029 missing. An uncommitted reservation from 2026-05-31 permanently inflates `MAX(seq)`. | `src/core/audit.py:445-530`, `src/integrations/m365/onedrive.py:447-1155`, `src/workflows/board_meeting_invitation.py:857, 1094`; data-layer review findings | A legally sequential register is already provably wrong. The invitation workflow stamps a number into a PDF and then treats the register write as non-fatal. |
| B5 | **Decisions are numbered by three independent counters.** `decision_drafter.compute_decision_ref` (used by the Zoom sidebar), a regex re-implementation in `board_meeting_minutes.py:648-678` (Google Sheet writer), and the Sheet itself. The live decision events in `meeting_events` never reach the Βιβλίο Αποφάσεων by any code path; the sheet receives the model's paraphrase. | `src/workflows/decision_drafter.py:43-68`, `src/workflows/board_meeting_minutes.py:648-678, 684` | The number shown to the board in the meeting can differ from the one in the minutes; the register of decisions holds a paraphrase, not the wording the board approved. |
| B6 | **Personal data retention is documented but not implemented.** `FOUNDATION.md §3.1` says transcripts are deleted once minutes are finalised. No code deletes `data/recordings/`, `data/transcripts/`, or the ~170 KB verbatim transcript stored inside a `workflow_state` row since 2026-04-09. | data-layer review; `src/core/workflow.py:228-235` serialises the entire `ctx` on every transition | Under GDPR Art. 5(2) a documented-but-unmet policy is worse than none. An erasure request cannot be served: the data sits in an opaque JSON blob. |
| B7 | **Two "minutes" products, neither finished.** `BoardMeetingMinutesWorkflow` (`minutes run`) and `minutes_pipeline.assemble_minutes` (`minutes build`) share no code; the richer pipeline (ASR, organiser, verbatim decisions) never reaches SharePoint or the decisions sheet; the older workflow's `finalize` entry builds a context of three keys and never reloads the persisted one. Both recorded runs sit at `awaiting_approval` since April. | `src/workflows/board_meeting_minutes.py` vs `src/workflows/minutes_*.py`; `workflow_state` rows | The system's own record says no minutes have ever been finalised, while minutes exist. |
| B8 | **Config and packaging drift.** `config.yaml.example` has Brevo keys the model silently drops and lacks six real fields; `.env.example` documents two Zoom variables nothing reads; `requirements.txt` and `pyproject.toml` disagree in both directions; `newsletter_list_ids: [1]` is a segment id, so campaign creation 404s and falls back to the full member list. | `src/config.py` vs `config.yaml.example`; `requirements.txt` vs `pyproject.toml`; `src/integrations/brevo.py` fallback | A new operator following the example config gets silently different behaviour from the one described. |
| B9 | **Single copy of the governance record.** `data/amnesty.db` had no backup routine until 2026-09-15; the one backup taken since sits on the same disk. | `data/backups/` | A disk failure loses every live-captured decision, including the Global Assembly positions. |

Items B1 to B3 are hours of work each. B4 to B7 are the first real design work and are folded into the migration below rather than patched in isolation, because patching them where they are would cement the structure this review recommends changing.

---

## 3. What is right and must be kept

An honest review names what not to touch. These decisions are earning their keep and the target architecture preserves each of them.

- **The layered `src/` layout** (`core` → `workflows` → `cli`/`api`, with `integrations` alongside). The May review defended it; it still holds as the skeleton. The target adds layers, it does not flatten these.
- **The LLM client as a port.** `ClaudeClient` routes to Gemini or Anthropic purely through `config.yaml: llm.provider` (`src/core/claude.py:50-67`). It is the one place the codebase already does what the whole integration layer should do, and it is the pattern to copy. The Crab Fit client is similarly clean.
- **The event bus and `platform_bridge`.** One publisher side, one reactive subscriber, ten declared events (`src/core/events.py`). It is only half used (section 4.2), but it is the right seam and it works.
- **The deterministic minutes core.** `minutes_skeleton.py`, the organiser's "flag, never delete" rule, `[ΝΑ ΕΠΙΒΕΒΑΙΩΘΕΙ]` markers. This is the ethics framework made concrete and is exactly what a movement-wide tool should be built around.
- **Tests and their isolation.** 756 tests; `tests/conftest.py`'s autouse `_isolate_db` copies a pre-built schema database per test. `tests/` mirrors `src/`. The `debug_fixture()` convention plus `test_debug_fixtures.py` is a genuinely good idea (it just does not yet cover the second kind of workflow, section 4.3).
- **Configuration split.** Secrets in `.env`, structure in `config.yaml`, validated by Pydantic. The target keeps this and adds a second document (the section profile) rather than replacing it.
- **Discord embeds as pure builders** with a README, and colours centralised in `discord/brand.py`. The PDF side should copy this, not the other way round.
- **`docs/DEBUG_CLI.md`, `docs/MINUTES_PIPELINE.md`, `docs/api_setup/*`.** Current, specific, the model for the documentation an adopting section will need.
- **Prompts versioned with code in `src/prompts/`.** The May review argued this against a reviewer and was right for one section. For many sections the prompts become profile assets (section 6.3), but the principle, versioned text with a config override, survives.
- **`.gitignore` discipline.** No personal data and no secrets are committed. The working tree is clean.

---

## 4. Diagnosis: three layers entangled

### 4.1 Section identity is in the code

What a section *is* (name, language, roles, people, statute rules, house formats) should be data. Today it is spread across code, config, prompts and assets with no single owner.

| Kind of identity | Where it lives now | Measured |
|---|---|---|
| Role addresses (`board@`, `director@`, `secgen@`) | Module-level constants, redefined per file | `board_meeting_invitation.py:40-42`, `director_briefing.py:52,91`, `egkyklios_general.py:42-43`, `director_briefing_intake.py:38`, `platform_bridge.py:243`, `archive.py:288`, `commands.py:1778,3038`; `secgen@` is in `board_members` but never looked up |
| Roster | `config.yaml: workflows.board_meeting.board_members` (name + email, **no role field**) plus `minutes_pipeline.speaker_aliases`, a second manually synced name list | Role → person is an email-prefix convention, not data |
| Org name | 13 hits in 10 `.py` files plus all 8 prompts | `config.py:70`, `discord/brand.py:224`, `documents/egkyklios_pdf.py:42-43` |
| Brand palette, logo, signatures | Hex values in `discord/brand.py:20-22` **and** `documents/pdf_generator.py:27-28` (independently); logo paths in two modules; signature PNGs with hard-coded pixel coordinates and role labels in `board_meeting_minutes.py:485-501` | Two palettes to keep in sync; adding a signatory is a code change |
| Numbering and naming formats | Meeting ref `ΔΣ{n}-{year}` implemented 5 times; protocol `{year}_{seq:03d}` 3 times; archive filename `[{protocol}] {title}.pdf` at 6 call sites in 4 files; Greek long-date month tables at least 5 times (two in the same file) | No `refs.py`, no locale module |
| Statute rules | GA notice periods configured (`config.py:188-189`) but read nowhere; quorum (Art. 16.2, five members) checked nowhere; `articles.json` path defined twice and the config value never reaches `load_articles()` | Rules are prose the prompts cite, not constraints the engine enforces |
| Prose | 50 of 94 modules contain Greek literals; two LLM prompts inline in Python (`decision_drafter.py:225-251`, `archive_llm.py:38-160`) | Email HTML is already externalised in `assets/email_templates/`; that is the pattern to extend |

None of this is bad engineering for one section. It becomes a wall the moment a second section wants to change its name, language or roles without forking the code.

### 4.2 Platform concepts are the workflow state

Workflows depend on vendors directly, and the vendors' identifiers have become the shape of the data.

| Coupling | Evidence |
|---|---|
| Workflows construct concrete clients | Eager in `__init__` (`board_meeting_invitation.py:49-50`), lazy properties (`archive.py:75-79`, `egkyklios_general.py:111-121`), per-call in Discord cogs. No injection point. Tests patch four different concrete import paths for the same client. |
| Platform IDs in `ctx` | `zoom_join_url`, `working_doc_id` (Google), SharePoint `file_id`, `brevo_template_id`/`list_ids` as raw values (`board_meeting_invitation.py:831-852, 1394-1395`; `archive.py:968-979, 1055`) |
| Platform fields in the shared event schema | `BoardMinutesSharedPayload.doc_id` (Google), `zoom_url` on two payloads (`src/core/events.py:61-73, 92-96`). The one neutral seam is not neutral. |
| Discord drives workflows directly | `cogs/board.py`, `cogs/archive.py`, `cogs/admin.py`, `cogs/context_menus.py` construct `XWorkflow(actor=...)`, call `.run()`/`.rollback()`, and branch on internal ctx keys (`cogs/archive.py:420-474`) |
| Discord → email bypasses the bus | `platform_bridge.py:940-1107` instantiates `M365MailClient` and hardcodes `board@amnesty.org.gr` (`:43, :243, :1053-1061`) |
| Core depends upward | `src/core/scheduler.py:75-77` imports concrete workflows **and** `GraphSubscriptionsClient` |
| Register conflated with document store | The πρωτόκολλο is openpyxl row-scanning inside `OneDriveClient` (`m365/onedrive.py:447-1155`) |
| Two live mail implementations | `GmailClient` (minutes only) and `M365MailClient` (everything else), no common interface |
| Half-finished module move | Legacy shims `integrations/onedrive.py`, `m365_mail.py`, `m365_inbox.py`, `graph_subscriptions.py` are still the import path used by production code (`board_meeting_invitation.py:34,36`, `archive.py:41`, `egkyklios_general.py:36`, `email_intake.py:39,44`, `webhooks.py:293`, `scheduler.py:75`) |

The capabilities the workflows actually need are few and generic. From the measured call surface:

| Capability | Operations the workflows use | Platform-neutral today? |
|---|---|---|
| Meeting scheduling | schedule, cancel, add registrants, join info | No: Zoom ids/URLs in ctx and events |
| Recording retrieval | list recordings, download assets, participants | Yes inside the client; the manifest is already an internal format |
| Document store / archive | upload, download, share link, delete | No: Google and SharePoint ids leak |
| Register (numbered rows) | next number, append row, find row, update row | No: Excel-specific, inside the wrong client |
| Mail | send, reply in thread | Two implementations, no interface |
| Newsletter | send campaign to audience | No: Brevo ids in ctx |
| Community / chat | post, thread, event, reaction | Entirely Discord-shaped |
| Scheduling poll | create availability grid | Yes (Crab Fit) |
| Language model | generate(system, user) | **Yes**, the reference implementation |
| Speech recognition | transcribe(audio) → segments | Yes (`Transcriber` protocol in `minutes_transcription.py`) |

### 4.3 The workflow engine has two kinds of workflow and four ways to resume

| Finding | Evidence |
|---|---|
| **Two kinds.** 4 modules subclass `BaseWorkflow` (state machine, gates, persistence, rollback). 11 modules are plain function pipelines with none of that, including the whole minutes stack. `registry.py` lists the 4. | `src/workflows/registry.py:10-15`; `test_debug_fixtures.py` hardcodes the same 4 |
| **Four resume conventions.** In-process loop (`commands.py:299-357`); manual `wf.workflow_id = <persisted>` then `run(ctx)` with `_start_at_step` (`commands.py:1872-2044`, `scheduler.py:138`, three Discord sites); the non-existent `.resume()` (B1); none at all for minutes. `BaseWorkflow.__init__` regenerates `workflow_id` on every construction (`workflow.py:60`). | A second section has no canonical pattern to copy |
| **`ctx` has no schema.** `dict[str, Any]`; the 35-50-key `debug_fixture()` per workflow is the only enumeration. | `workflow.py:63` |
| **`test_mode` is a per-step convention**, not an engine guarantee. | `schedule_zoom` has no check (`board_meeting_invitation.py:747`) |
| **Copy-pasted engine pieces.** The `getattr(self, f"_step_{name}")` dispatcher appears identically in all 4 subclasses; JSON-fence stripping has 5 implementations; upload+register is hand-rolled twice; `bus.publish` try/except is inlined across 3 workflows. | `archive.py:150`, `board_meeting_invitation.py:152`, `board_meeting_minutes.py:197`, `egkyklios_general.py:193`; `commands.py:43`, `board_meeting_minutes.py:24`, `archive_llm.py:191,201`, `minutes_pipeline.py:430`, `minutes_organizer.py:66,72` |
| **Five entry paths, no shared start.** CLI, webhooks, Discord, scheduler, Zoom sidebar each build `initial_data` by hand. | section 4 of the engine audit |
| **The CLI is not generated from metadata** except the `debug` subtree, which is, and proves the rest could be. | `commands.py:849-1037` vs `:303-357` |

The extensibility test makes it concrete. To add a "Committee meeting minutes" workflow that reuses the minutes pipeline but posts to Slack and archives to Nextcloud, a developer at another section touches today: a new workflow module (choosing, unguided, between wrapping a pipeline with no gates or writing a third minutes implementation), `registry.py`, the test allow-list, a new `argparse` block and approval loop in `commands.py`, two new integration modules from scratch, `events.py` if anything should react, and `DEBUG_CLI.md`. Seven files in the platform's core, for one workflow.

### 4.4 The data layer has no domain model

The September data-layer review is summarised here because its findings decide what the storage layer must become. The full findings are cached from that review; the ones that matter for structure:

- **No entity for a meeting, a person or a vote.** `meeting_ref` is free text duplicated across four tables and the ctx blobs; it has been populated with a spreadsheet header 21 times. Roles are an email convention. Votes exist only as sidebar events.
- **`workflow_state` is a document store by accident.** Twelve modules read JSON fields out of it; the quarterly circular reconstructs board activity from draft JSON inside it; the Zoom sidebar serves the agenda of the most recently *updated* invitation regardless of state, including cancelled runs.
- **Nothing is tamper-evident and nothing is attributable.** `actor` is unauthenticated free text defaulting to `--actor secgen`; 13.8% of audit rows carry any correlation key; approval rows name a step, not a document. Test-suite writes have contaminated the production audit log (64 identifiable rows).
- **Retention, encryption and deletion are documented, not implemented** (B6).
- **The register and decision numbering are structurally unsafe** (B4, B5).
- **SQLite itself is fine.** At 1.8 MB and ~11 meetings a year the engine has decades of headroom. The problems are the missing schema above it, the missing migration mechanism (`user_version` is 0; ~90 raw SQL statements live in 17 modules outside `audit.py`), and the single shared connection whose `commit()` commits everyone's work.

### 4.5 The repository is organised for its author

- `README.md` is one blank line. There is no setup path.
- Root holds design and presentation material (`BRAND.md`, `DESIGN_BRIEF.md`, `PRESENTATION.md`, two zip bundles), a personal scratch file (`Notes.md`), an empty `start` file, packaging that disagrees with itself, and reference PDFs.
- `brand/` tracks 451 files; about 415 are an Amnesty icon library, ~190 with corrupted filenames (a stray control character), none referenced by code.
- `TEMPLATES.md` points at a path moved months ago; `docs/code_structure_review.md` is 3,300 lines of which the first 650 are the useful review and the rest pasted chat and third-party commentary.
- The two output templates that matter most (the Google Doc invitation, Brevo template #234) exist only in those services.
- `data/` mixes state, cache, artifacts and personal data with no documented retention; `data/logs/` has accumulated since May.

None of this blocks the current section. All of it blocks a second one.

---

## 5. Principles for the target

Derived from the ethics framework, the operating reality (one volunteer maintainer, ~€5/month, self-hosted on a laptop today) and the goal (adoptable by sections with different tools):

1. **Deterministic core, models at the edges.** Already true for minutes; make it true for numbering, rules and records too.
2. **A section is data, not code.** If changing the organisation requires a Python edit, the boundary is in the wrong place.
3. **Platforms are adapters behind small ports.** Ports name capabilities in the section's vocabulary (a register, an archive, a mail sender), never in the vendor's.
4. **One workflow engine, one way to start, one way to resume.** Every trigger (CLI, web, chat, scheduler, sidebar) goes through the same function.
5. **Governance records are append-only, attributable and reconstructible.** Nothing a member said or voted is ever overwritten; corrections are new records.
6. **Retention is executable policy.** Every personal-data category has a clock and a job.
7. **No infrastructure a volunteer cannot run.** No Postgres, no queues, no containers-of-containers. SQLite, files, one process per section, cron-like scheduling.
8. **Ship every phase.** The strangler pattern: new structure grows beside the old, one seam at a time, with the test suite green at every step. No long-lived rewrite branch.

---

## 6. Target architecture

### 6.1 Three repositories, or one with a hard boundary

The platform code should be **publishable**; the ethics framework's whole pitch is a working demonstration other sections can inspect. The section profile contains a roster with names and emails and must stay **private**. That tension resolves cleanly:

```
amnesty-governance/              public-capable: the platform
  governance/                    (the Python package; see 6.2)
  adapters/
  docs/
  tests/

amnesty-gr-section/              private: one repo per section
  profile.yaml                   identity, roles, roster, formats, locale
  rules.yaml                     quorum, notice periods, cadence, retention
  prompts/                       Greek prompts, parameterised by profile
  templates/                     email HTML, document templates, taxonomy
  brand/                         only the assets actually referenced
  config.yaml                    adapters chosen + their settings
  .env                           secrets
  requirements.txt               pins one platform version
```

Until a second section exists, both can live in this repository under `sections/amnesty-gr/`, with the boundary enforced by imports (platform code never imports from `sections/`) and by `.gitignore` for roster files. The split into two repositories is then a `git mv`.

### 6.2 Package layout

Rename the importable package from `src` (an accident of the setuptools configuration) to a real name. `governance` is used below; the choice is the owner's.

```
governance/
  domain/          Pure Python: Meeting, AgendaItem, Decision, Vote, Person, Role,
                   ProtocolEntry, Artifact, GoverningRule, refs and formatting driven
                   by the profile. No I/O, no vendor names, no Greek literals.
  ports/           typing.Protocol interfaces, one per capability (6.4).
  engine/          Workflow base, step metadata, typed context, start/resume/rollback,
                   dry-run enforcement, plugin discovery, approval as a port.
  runtime/         Storage (schema, migrations, allocator, governance ledger, retention),
                   event bus, scheduler, composition root that builds adapters from config.
  workflows/       Platform-agnostic, section-agnostic processes: meeting lifecycle,
                   minutes, archive, circular, general assembly.
  profile/         Loader and schema for a section profile; locale modules (el, en).
  channels/        Entry points that only translate intent: cli/, api/, discord/, zoom_app/.
adapters/
  zoom/  google/  m365/  brevo/  discord/  crabfit/  whisper/  llm_gemini/  llm_anthropic/
  local_files/     A files-and-folders implementation of DocumentStore and Register,
                   so the platform runs end to end with no vendor account (also the test double).
sections/
  amnesty-gr/      The first profile (6.3).
```

Two rules make the layout mean something: **`governance/` never imports `adapters/` or `sections/`** (enforced by an import-linter test), and **`channels/` never contains business logic** (they build a request and call `engine.start` / `engine.resume`).

### 6.3 The section profile

Everything the audit found baked into code, as data. Illustrative, not final:

```yaml
# sections/amnesty-gr/profile.yaml
section:
  slug: amnesty-gr
  name: { el: "Διεθνής Αμνηστία - Ελληνικό Τμήμα", en: "Amnesty International Greece" }
  locale: el
  timezone: Europe/Athens
roles:
  chair:      { title: { el: "Πρόεδρος" },           address: chair@amnesty.org.gr }
  vice_chair: { title: { el: "Αντιπρόεδρος" },       address: vicechair@amnesty.org.gr }
  treasurer:  { title: { el: "Ταμίας" },             address: treasurer@amnesty.org.gr }
  secgen:     { title: { el: "Γενικός Γραμματέας" }, address: secgen@amnesty.org.gr, signs_minutes: true }
  director:   { title: { el: "Διευθυντής Τμήματος" }, address: director@amnesty.org.gr, member: false }
board_address: board@amnesty.org.gr
bodies:
  board:            { name: { el: "Διοικητικό Συμβούλιο" }, abbrev: "ΔΣ", meeting_ref: "{abbrev}{seq:02d}-{year}" }
  general_assembly: { name: { el: "Γενική Συνέλευση" },    abbrev: "ΓΣ" }
formats:
  decision_ref:  "{body}{seq:02d}-{meeting_seq:02d}-{year}"
  protocol:      "{year}_{seq:03d}"
  archive_name:  "[{protocol}] {title}.pdf"
  long_date:     genitive            # locale module renders "9 Ιουνίου 2026"
roster: roster.yaml                  # gitignored in a shared repo; person -> role, display names, Zoom aliases
```

```yaml
# sections/amnesty-gr/rules.yaml      (replaces the decorative parts of articles.json)
board:
  quorum: { members: 5, source: "Καταστατικό άρθρο 16 §2" }
  cadence: { at_least_every_days: 60, source: "Καταστατικό άρθρο 16 §1" }
  notice_days: 7
general_assembly:
  notice_days: 30
  electronic_notice_days: 15
retention:
  recordings:  { delete_after: minutes_finalised }
  transcripts: { delete_after: minutes_finalised, keep_skeleton: true }
  audit_log:   { keep_years: 10 }
```

Prompts, templates, brand and taxonomy move under the same directory. Prompts gain profile variables (`{section.name.el}`, `{bodies.board.abbrev}`) so a second section's prompt is a translation, not a fork. The English profile written for tests and documentation is the proof that the code is language-neutral.

### 6.4 Ports and adapters

Ports are `typing.Protocol` classes in `governance/ports/`, kept small and phrased in governance terms. From the measured call surface, nine cover everything the current workflows do:

| Port | Methods (indicative) | First adapters |
|---|---|---|
| `MeetingScheduler` | `schedule(meeting) -> MeetingLink`, `cancel(ref)`, `register(ref, people)` | zoom; later teams |
| `RecordingSource` | `list(since)`, `download(ref) -> Manifest`, `participants(ref)` | zoom |
| `DocumentStore` | `put(bytes, name, folder) -> DocumentRef`, `get`, `share_link`, `delete` | m365 (SharePoint), google (Drive), local_files |
| `Register` | `next_number(year) -> Reservation`, `commit(reservation, row)`, `release`, `find`, `update` | m365 (xlsx), local_files (csv/sqlite); later a database-backed one |
| `MailSender` | `send(msg) -> MessageId`, `reply(in_thread, msg)` | m365, gmail |
| `Newsletter` | `send(campaign, audience)`; audiences are profile names, mapped to vendor ids in adapter config | brevo; later mailchimp |
| `Community` | `announce(channel, message)`, `open_thread`, `schedule_event`, `pin` | discord; later slack |
| `AvailabilityPoll` | `create(window) -> PollLink` | crabfit |
| `Transcriber`, `LanguageModel` | already exist as protocols | whisper; gemini, anthropic |

Cross-cutting rules:

- **Neutral references.** `MeetingLink`, `DocumentRef(adapter, id, url)`, `Reservation` are domain types; a Zoom URL and a Teams URL both become `meeting.link.join_url`. Event payloads carry these, never vendor ids.
- **Configuration chooses adapters.** `config.yaml` gains a block: `adapters: { document_store: m365, register: m365, mail: m365, newsletter: brevo, community: discord, meetings: zoom, transcriber: whisper, llm: gemini }`. The composition root instantiates them once and injects them into workflows. Module-level `from src.config import settings` inside workflows disappears over time.
- **A `local_files` adapter set** makes the platform runnable with zero vendor accounts and gives tests a real double instead of four inconsistent patch paths.
- **Approval is a port too** (`Approver`): the engine asks it for a decision; the CLI implements it with a prompt, Discord with a button, the web with a page. This removes `input()` from steps for good.

### 6.5 The workflow engine, second generation

One kind of workflow. Steps are declared, not discovered:

```python
class CommitteeMinutes(Workflow):
    name = "committee_minutes"
    Context = CommitteeMinutesContext        # pydantic model; replaces the untyped dict

    steps = [
        Step("fetch_recording",  uses=[RecordingSource]),
        Step("build_skeleton"),
        Step("draft",            uses=[LanguageModel]),
        Step("review",           approval=True),
        Step("archive",          uses=[DocumentStore, Register], compensate="unarchive"),
        Step("announce",         uses=[Community, MailSender]),
    ]
```

What the engine then owns, once, instead of each workflow re-implementing it:

- **Dispatch, persistence, resume.** `engine.start(name, context) -> run_id` and `engine.resume(run_id, decision)` are the only entry points; every channel calls them. `run_id` is persisted and never regenerated.
- **Dry run.** When `test_mode` is set, every side-effecting port declared in `uses` is wrapped in a recording no-op. Steps cannot forget.
- **Compensation.** Rollback calls declared `compensate` handlers in reverse; the twelve hand-wired rollback call sites collapse into one.
- **Idempotency.** Each run carries an idempotency key (for example `meeting_ref + workflow`); the webhook's ad hoc `_find_in_progress_invite` scan goes away.
- **Plugin discovery.** Workflows and adapters register through Python entry points (`[project.entry-points."governance.workflows"]`), so a section's private package can add both without editing the platform. In-repo modules register the same way.
- **Channels generated from metadata.** The `debug` CLI subtree already generates itself from `WORKFLOWS`, `steps` and fixtures; the same applies to `run`, `approve`, `status` and to the Discord command set.
- **Fixtures for every workflow.** `debug_fixture()` becomes a required class attribute checked by one generic test, so the eleven pipeline modules stop being invisible.

The minutes stack unifies onto the richer pipeline: `assemble_minutes` becomes the drafting steps of one `BoardMinutes` workflow whose tail (review, finalise, archive, register decisions, announce) is what `BoardMeetingMinutesWorkflow` does today. One transcript parser, one fence stripper, one decision numbering call.

### 6.6 Storage and the governance ledger

SQLite stays. What changes is what sits on it:

- **A real schema** with migrations (`user_version`-based, a few dozen lines; Alembic is optional later): `meetings`, `agenda_items`, `people`, `roles`, `attendance`, `decisions`, `votes`, `protocol_entries`, `artifacts` (path, hash, version, approved_by), `workflow_runs` (state, typed context as JSON **referencing** artifacts, never embedding them).
- **One number allocator** for protocol and decision numbers, transactional, used by every path, reconciled against the external register on every read, with a `register audit` command that reports gaps and duplicates instead of the code silently creating them.
- **An append-only governance ledger**: every agenda advance, presence change, vote, decision, approval and finalisation as a row that carries a content hash and the previous row's hash, with a daily digest written to the archive. This is what makes a member's contestation (ethics framework L3) answerable years later.
- **Authenticated actors.** Approvals record the identity the channel authenticated (the M365 or Google account, the Discord user), never a `--actor` flag.
- **Retention as code.** A nightly job applies `rules.yaml: retention` to recordings, transcripts, logs and run contexts, and logs what it deleted. Backups: nightly `Connection.backup()` to a rotating local set, plus one copy to the section's archive store through the `DocumentStore` port.
- **Test writes cannot reach production.** The isolation fixture already ensures this for the suite; the runtime adds a `mode` column so debug runs are distinguishable if they ever leak.

### 6.7 Repository organisation

| Now | Target |
|---|---|
| `README.md` (empty) | Setup from zero for a new section, in under a page, linking to the rest |
| `FOUNDATION.md`, `ROADMAP.md`, `CUSTOMIZATION.md`, `TEMPLATES.md`, `Notes.md`, design docs, zips at root | `docs/` for developer docs (`architecture.md`, `adopting-the-platform.md`, `adding-a-workflow.md`, `adding-an-adapter.md`, `data-and-gdpr.md`, `operations.md`), `docs/decisions/` for dated design records (the current `ROADMAP.md` and `docs/plans/` content), `design/` for brand briefs and generated bundles, `sections/amnesty-gr/` for section assets. `Notes.md` and `start` deleted. `TEMPLATES.md` merged into the profile documentation. |
| `docs/code_structure_review.md` (3,300 lines) | Trim to the dated review plus its results; move to `docs/decisions/2026-05-code-structure.md` |
| `brand/` (451 files, ~415 unreferenced) | Only referenced assets, under the section; the icon library out of the repo or in a release-asset bundle |
| `framework/*.pdf`, `past_minutes/*.pdf` | Outside the working tree (they are gitignored, but personal data should not sit next to code); the ethics framework stays in `docs/` |
| `data/` | `data/state/` (database, backups), `data/cache/` (register snapshot, ASR pieces), `data/artifacts/<meeting>/`, `data/personal/` (recordings, transcripts, under retention), `data/logs/` with rotation, documented in `docs/data-and-gdpr.md` |
| `pyproject.toml` + `requirements.txt` | One source of truth; `pip-compile` if a lock is wanted |
| Package `src` | `governance` (or the chosen name) |

---

## 7. Migration: six phases, each shippable

Effort assumes the current pace (part-time, one maintainer, meetings continuing). Total: roughly four to six months, interleaved with operations. Every phase ends with the suite green and the current section working.

### Phase 0: Stop the bleeding (this month; small commits)

Fix what is broken now, in place, without changing structure.

- B1: implement `resume()` properly or route the two callers through the existing `_start_at_step` mechanism; add a test for each caller.
- B2: remove the four `input()` calls; steps return a `StepResult` naming the missing value and the CLI collects it before starting.
- B3: fix the two `board@amnesty.gr` recipients and add a test that scans code for role addresses not present in config.
- B4 (containment): route all four number-allocation paths through `reserve_next_protocol_number`; make the register write fatal in the invitation workflow; release the orphaned 2026-05-31 reservation; add `register audit` that lists gaps.
- B5 (containment): make the Sheet writer call `compute_decision_ref`; write the verbatim `meeting_events` decision text to the Βιβλίο Αποφάσεων instead of the paraphrase.
- B6 (containment): a retention job for `data/recordings/` and `data/transcripts/` per `FOUNDATION.md`; purge the transcript inside `workflow_state`; stop `_persist` from serialising keys over a size threshold (store a path instead).
- B8, B9: reconcile config example and model; pick one dependency file; nightly backup with rotation; `README.md`.

### Phase 1: Extract the section profile (2 to 4 weeks)

- Create `sections/amnesty-gr/` with `profile.yaml`, `rules.yaml`, `roster.yaml` (gitignored), and move prompts, email templates, taxonomy and referenced brand assets under it.
- Split `Settings` into platform settings and a `Profile` model; add the `roles` map; replace every hard-coded role address and org-name literal with profile lookups.
- Add `governance/domain/refs.py` and `governance/profile/locale/el.py`; delete the five meeting-ref, three protocol, six filename and five month-table implementations.
- Move the two inline prompts to files; parameterise all prompts with profile variables.
- Wire `rules.yaml`: enforce quorum in the skeleton's presence step, notice periods in the invitation workflow, cadence as a reminder.
- Write the English test profile; make the suite run against both.

### Phase 2: Ports and adapters (4 to 6 weeks)

- Define the nine ports; wrap the existing clients as adapters with no behaviour change; add the composition root; inject into workflows.
- Introduce `MeetingLink`, `DocumentRef`, `Reservation`; migrate `ctx` and event payloads to them.
- Build `local_files` adapters; replace the inconsistent test patches with them.
- Extract the register logic out of `OneDriveClient` into a `Register` adapter; delete the legacy import shims; make `scheduler` register jobs declared by workflows instead of importing them.
- Route Discord → email through the `MailSender` port and the bus.

### Phase 3: Engine v2 and one minutes stack (4 to 6 weeks)

- Declared steps, typed contexts, `engine.start` / `engine.resume` used by every channel, dry-run enforcement, compensation, idempotency keys.
- Generate the CLI and Discord command surfaces from workflow metadata; delete the hand-written approval loops.
- Unify minutes: `assemble_minutes` as the drafting steps of one `BoardMinutes` workflow; retire `BoardMeetingMinutesWorkflow` and `utils/transcript_parser.py`; run ΔΣ05 and ΔΣ06 through it end to end as the acceptance test.
- Required `debug_fixture` for every workflow; one generic test.

### Phase 4: Domain model and governance ledger (4 to 8 weeks)

- Schema and migrations for meetings, people, roles, attendance, decisions, votes, protocol entries, artifacts, runs; backfill from `meeting_events`, `workflow_state` and the register.
- The transactional allocator; the append-only hash-chained ledger; authenticated actors; mandatory correlation keys; retention job driven by `rules.yaml`.
- The Zoom sidebar, the circular and the webhooks read from the model, not from `workflow_state` blobs.
- This phase is the prerequisite for the General Assembly workflow (motions, ballots, per-member votes), which should not be built on the current tables.

### Phase 5: Packaging for a second section (2 to 4 weeks)

- Rename the package; entry-point discovery; `governance init --section <slug>` scaffolder; the adoption documents in `docs/`.
- Dry run: a fictional second section with an English profile and `local_files` adapters, exercised end to end in CI. If that passes without editing platform code, the goal is met.
- Split `sections/amnesty-gr/` into its own private repository pinning a platform release.

### What not to do

- **No Postgres, no message queue, no container orchestration.** The volume is ~11 meetings a year; the constraint is maintainer time, not throughput.
- **No rewrite branch.** Every phase lands on `main` behind the existing tests.
- **No premature abstraction of things with one implementation and no second in sight.** Ports are defined for capabilities the workflows already use; a port for "video conferencing whiteboards" is not.
- **No multi-tenancy in one process.** One deployment per section, selecting one profile, is simpler, safer for personal data, and matches how sections actually operate.
- **No blurring of the boundary the May review named.** Adapters still never learn what a πρωτόκολλο is; they implement `Register`.

---

## 8. Decisions for the owner

1. **Package name** (replacing `src`). Needed before Phase 5, cheap any time before it.
2. **One repository with `sections/` now, two later?** Recommended yes; the split is mechanical once the boundary exists.
3. **Should the General Assembly workflow wait for Phase 4?** Recommended yes. Its data (motions × members × ballots, with legal weight) is exactly what the current tables cannot hold, and building it first would double the migration.
4. **Off-laptop backup destination** (the section's SharePoint archive is the obvious one). It moves board data off the laptop, so it is the owner's call.
5. **Retention clocks** in `rules.yaml`: the values in `FOUNDATION.md §3.1` are a starting point; the board should confirm them, since the recordings are the most sensitive data the platform holds.
6. **Which second section, if any, to design against.** A real conversation with one other section about their tools would turn the English test profile from a proof into a pilot.

---

## Appendix A: Evidence index

Sources for the measurements above, so any claim can be re-checked.

| Topic | Where the evidence is |
|---|---|
| Layer sizes | `core` 2,590 lines / `workflows` 10,267 / `integrations` 2,565 + `discord` 3,190 + `m365` 2,063 / `api` 1,139 / `cli` 3,269; largest file `cli/commands.py` 3,266 |
| Greek in code | 50 of 94 `.py` files; top: `board_meeting_invitation.py` 116 lines, `discord/cogs/board.py` 91, `cli/commands.py` 90, `egkyklios_general.py` 79 |
| Role addresses | `board_meeting_invitation.py:40-42`, `director_briefing.py:52,91`, `egkyklios_general.py:42-43`, `director_briefing_intake.py:38`, `platform_bridge.py:43,243,1053-1061`, `archive.py:288`, `commands.py:1778,3038` |
| Numbering duplication | meeting ref: `board_meeting_invitation.py:776,954,1064,1311,1567`, `commands.py:648,763`, `google_drive.py:566`, `board_meeting_minutes.py:34-46`; protocol: `audit.py:485`, `m365/onedrive.py:485`, `minutes_documents.py:68`; decision: `decision_drafter.py:43-68` vs `board_meeting_minutes.py:648-678` |
| Brand duplication | `discord/brand.py:20-22` vs `documents/pdf_generator.py:27-28`; logo paths `discord/brand.py:42-46`, `egkyklios_pdf.py:37-39`; signatures `board_meeting_minutes.py:485-501` |
| Statute rules unused | `config.py:188-189` (GA notice, never read); quorum absent; `articles.json` path `config.py:264` vs `decision_drafter.py:34` |
| Client construction | `board_meeting_invitation.py:49-65`, `archive.py:71-79`, `egkyklios_general.py:107-121`; Discord per-call `cogs/board.py:469`, `cogs/archive.py:406,666,713` |
| Platform ids in state | `board_meeting_invitation.py:831-852,1394-1395`, `archive.py:968-979,1055`, `core/events.py:61-73,92-96` |
| Reverse dependencies | `core/scheduler.py:75-77`; cogs `admin.py:57,111,318`, `board.py:112,414,469,545,618`, `archive.py:171,276,405,650,704`, `context_menus.py:119` |
| Legacy shims in use | `board_meeting_invitation.py:34,36`, `archive.py:41`, `egkyklios_general.py:36`, `email_intake.py:39,44`, `webhooks.py:293`, `scheduler.py:75` |
| Engine duplication | dispatcher `archive.py:150`, `board_meeting_invitation.py:152`, `board_meeting_minutes.py:197`, `egkyklios_general.py:193`; fence stripping `commands.py:43`, `board_meeting_minutes.py:24`, `archive_llm.py:191,201`, `minutes_pipeline.py:430`, `minutes_organizer.py:66,72` |
| Resume conventions | `commands.py:299-357` (loop), `:1872,1977,2013,2044` + `scheduler.py:138` (manual id), `:1166` + `cogs/board.py:127` (non-existent `resume`), `:1393` (minutes, in-process only) |
| Repository | `git ls-files`: 451 files under `brand/`; `README.md` 1 line; `config.yaml.example` vs `src/config.py` drift; `requirements.txt` vs `pyproject.toml` drift |
| Data layer | September 2026 data-layer review (five lenses, 93 findings); live register: 31 entries, max seq 33, missing 017 and 029; `protocol_reservations` orphan `a178da24` (2026-05-31) |

## Appendix B: Relationship to earlier reviews

- **`FOUNDATION.md` (April 2026)** set the single-section design this review generalises. Its principles (human in the loop, incremental delivery, minimal cost, regulatory alignment) all survive; its "why SQLite" reasoning is endorsed; its "why CLI-first" reasoning is honoured by keeping channels thin.
- **`docs/code_structure_review.md` (May 2026)** fixed the housekeeping and explicitly deferred two debts: splitting `audit.py` and splitting `commands.py`. Both are absorbed here: `audit.py` becomes `runtime/storage` with a schema and migrations (Phase 4); `commands.py` is largely generated from workflow metadata (Phase 3). Its one boundary rule, that integrations never learn governance concepts, is kept as a port rule.
- **The September data-layer review** supplied the storage findings summarised in 4.4 and the fixes in Phases 0 and 4.
