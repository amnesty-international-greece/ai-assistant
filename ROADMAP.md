# Roadmap - Next Phases & Pending Designs

What's planned but not yet in code. Each section captures the intent, the
design as discussed, and the open questions we need to answer before
implementation.

This is the live planning doc - replaces the deleted stub workflow files.
When a section is fully built and tested, delete it from here.

---

## 1. Explainer: `platform_bridge` and `scheduler` cogs

Asked about, worth pinning down so we share a mental model.

### `scheduler` cog (`src/integrations/discord/cogs/scheduler.py`)

Tiny cog. **Its only job is to run one background task** - the pending-actions
worker. The worker is in `src/integrations/discord/scheduler.py` (`run_pending_actions_worker`).

What the worker does, in plain English:
- Every 30 seconds, it queries the `discord_pending_actions` SQLite table:
  *"any row where status='pending' and due_at ≤ now?"*
- For each row found, it looks up a registered handler by `action_type`
- Calls the handler, marks the row `done` (or `failed` on error)
- Sleeps 30s, repeats

Other cogs register handlers when they load. Today there's exactly one:
`platform_bridge` registers a handler for `"board_meeting_reminder"`.

Mental model: this is a **persistent timer service**. If you say "remind me in 6
hours," a row goes into the DB; the bot can restart between now and then and
the reminder still fires.

### `platform_bridge` cog (`src/integrations/discord/cogs/platform_bridge.py`)

This is the **subscriber** to the platform event bus. It listens for 8 event
types (`board.meeting.scheduled`, `board.minutes.shared`, `ga.called`, etc.)
and reacts by doing Discord-side things (creating events, opening threads,
posting messages).

Today it fully implements 4 board-meeting events. The other 4 (GA, εγκύκλιοι,
member-approved) are stubs that log and exit - they'll get fleshed out when
those workflows ship.

The cog **never initiates** anything. It only reacts to events published from
elsewhere (`bus.publish(EVENT_BOARD_MEETING_SCHEDULED, payload)`).

### How they work together

When the board-invitation workflow finishes (`_step_confirm_newsletter` succeeds):

1. Workflow does `bus.publish(EVENT_BOARD_MEETING_SCHEDULED, payload)`
2. `platform_bridge._on_board_meeting_scheduled` fires:
   - Creates the Discord scheduled event
   - Opens the public agenda thread (and private board thread)
   - Inserts a `discord_pending_actions` row: `{action_type: "board_meeting_reminder", due_at: T-6h}`
3. Six hours before the meeting, the `scheduler` worker picks up that row, calls the registered handler, which republishes `EVENT_BOARD_MEETING_REMINDER_DUE`
4. `platform_bridge._on_board_meeting_reminder_due` fires, posts the countdown message in the public thread

Each system has one job:
- **`scheduler`** = persistent timers (survive restarts)
- **`platform_bridge`** = Discord-side reactions to platform events
- **`event_bus`** = the messaging layer that decouples them

If you ever wanted to add a "send a Discord DM when minutes are published," it'd
be a 5-line addition to `platform_bridge._on_board_minutes_shared` - no change
anywhere else.

---

## 2. Board-meeting workflow enhancement (refined design)

Refined after this decision pass. Three remaining questions are flagged 🔴 below.

### 2.1 Scheduling URL - paste, don't integrate

**Cabbagemeet dropped.** Self-hosting added too much infrastructure overhead
for a low-frequency use case. Instead: the SecGen creates a scheduling poll in
**any tool of choice** (When2Meet, Doodle, LettuceMeet, FindTime in Outlook,
Discord native poll, etc.) and pastes the URL when starting the workflow.

**Invocation:**
```
ai-in-ai board-meeting schedule --meeting-id ΔΣ05-2026 --poll-url <url>
```

- Workflow embeds the URL in the scheduling email
- **Poll stays open** until you manually advance the workflow (approval gate)
- You read the poll results in whatever tool you used, then advance - workflow
  doesn't need to query the poll programmatically

### 2.2 Agenda input - direct Google Sheet edit

Cabbagemeet handles dates; agenda topics go straight into the existing agenda
Google Sheet (`config.yaml → google.agenda_sheet_id`).

The scheduling email (§2.3) contains **two links**:
1. Cabbagemeet poll URL - board picks date
2. Direct Google Sheet link - board adds proposed agenda items

When you advance past the approval gate, the workflow's existing `read_agenda`
step pulls the (now-updated-by-board) sheet content. No new code, just a new
link in the email.

🔴 **Open Q1:** Are board members already comfortable editing the agenda sheet
directly, or do they need a friendlier UI? If the latter, we'd add a Google Form
that writes back to the sheet - small extra step, no code in our repo.

### 2.3 Email threading per meeting - two identities, two flows

`amnesty.org.gr` is on Microsoft 365. Rather than fight M365's IMAP+OAuth
requirements, we **split email identities by direction**:

| Account | Role | Protocol | Notes |
|---|---|---|---|
| `membersforum.amnesty.gr@gmail.com` (existing) | Forum bridge (read + send to/from `forum-ai@googlegroups.com`) | IMAP + SMTP via Gmail App Password | Works today, no change |
| `members@amnesty.org.gr` (M365) | **Send-only** for board emails (to `board@amnesty.gr`) | Microsoft Graph API `/me/sendMail` | Reuses the MSAL stack already configured for OneDrive |

**Two distinct board-only email sends** per meeting cycle, both threaded under
subject `Συνεδρίαση ΔΣ{N}-{YYYY}`, both sent from `members@amnesty.org.gr` via
Graph:

| Email | When | Content |
|---|---|---|
| **Scheduling** | At workflow start | Poll URL + agenda-sheet link + call for input |
| **Final invitation** | After approval gate | Full agenda, Zoom link, meeting details |

Plus the existing Brevo campaign goes out **to all members** (not board-only)
with the public-facing invitation. The Brevo campaign is not threaded
(different audience, different infrastructure).

**Email threading mechanics:**
- Anchor `Message-ID` is generated at workflow start: `<{meeting_id}@amnesty.org.gr>`
  (e.g. `<board_meeting:2026-05-21@amnesty.org.gr>`)
- Stored in `workflow_state.data` so it survives workflow restarts
- Every subsequent `GmailClient.send_email` for this meeting passes
  `in_reply_to=<anchor>` and `references=[<anchor>]`
- **Minutes workflow** (separate workflow) re-derives the same anchor from the
  meeting_id and continues threading: draft-share email + final-minutes-link
  email both land in the same thread

🔴 **Open Q2:** Can `members@amnesty.org.gr` (the bot's Gmail relay) currently
post to `board@amnesty.gr`? Two sub-questions:
   - Is `board@amnesty.gr` a Microsoft 365 / Google Workspace distribution
     list, or something else?
   - If you add `members@amnesty.org.gr` as a member of that list, will the
     list accept its posts (some lists are members-only-can-post, some are
     locked down further)?
   - **Quick test:** from `members@amnesty.org.gr`, send a plain test email to
     `board@amnesty.gr`. If it arrives at the board members' inboxes, we're
     good. If it bounces, we need a different recipient address.

### 2.4 Public Discord thread uses Brevo content

Per your direction: the **public** Discord forum thread (currently in `#ενημερώσεις`)
should contain the same body text as the Brevo campaign, not a separate
truncated agenda summary.

Implementation:
- `BoardMeetingScheduledPayload` already carries `agenda_summary` - we'll extend
  it with a new field `discord_thread_body: str` carrying the markdown-flavored
  Brevo content
- The workflow generates this when it generates the Brevo campaign (it's
  effectively the same content with HTML stripped)
- `platform_bridge._on_board_meeting_scheduled` uses `discord_thread_body` if
  present, else falls back to `agenda_summary`

This is a small, additive change. Existing tests stay green; the Brevo-body
generation lives in the workflow.

### 2.5 Private Discord thread - DEFERRED

Per your direction: skip for now. The Phase D dual-channel code stays in place
(`board_channel_id` config field stays available). When you later want it on,
set the channel ID and the bot picks it up - no code change needed.

### 2.6 Proposed step sequence (refined)

| # | Step | Status |
|---|---|---|
| 0 | **`init_meeting_thread`** | **NEW** - derive `meeting_id`, generate email anchor `Message-ID`, persist |
| 1 | **`send_scheduling_email`** | **NEW** - send threaded scheduling email to board via M365 (poll URL + agenda sheet URL) |
| 2 | **`await_responses`** | **NEW** - approval gate; you manually advance once responses are in and you've entered the chosen date into the agenda sheet |
| 3 | `read_agenda` | unchanged - reads from Google Sheet (now includes board additions) |
| 4 | `schedule_zoom` | unchanged |
| 5 | `draft_invitation` | unchanged |
| 6 | `generate_pdf` | unchanged |
| 7 | `approval` | unchanged |
| 8 | `archive` | unchanged |
| 9 | **`send_board_email`** | **NEW** - threaded final invitation to board via M365 |
| 10 | `send_newsletter_test` | unchanged (Brevo, not threaded) |
| 11 | `confirm_newsletter` | unchanged; publishes bus event with `discord_thread_body` |
| 12 | `schedule_reminder` | unchanged |

Minutes workflow grows: step 4 (`approval_and_share`) reuses the same thread
anchor when emailing the draft via M365; new final step appends the OneDrive
link reply.

---

## 3. Auto-pin replacement for `/pin-until` (you killed moderation cog ✅)

`/pin-until` and the moderation cog are gone. The replacement design:

The bot auto-pins a thread or message when certain events imply it should be
sticky. The most obvious trigger: a Γενική Συνέλευση announcement should be
pinned in `#γενικό` until the GA date.

**Design:**
- When `platform_bridge._on_ga_called` fires (Phase E), it pins the
  announcement thread automatically
- When `_on_board_meeting_cancelled` or analogous "the GA happened" event
  fires, it auto-unpins

No slash command, no admin involvement - the bot does it as part of its
existing event-driven flow. Admins can still pin/unpin manually via Discord's
native UI for anything else (board decisions to highlight, urgent εγκύκλιοι,
etc.).

The persistent `discord_pending_actions` queue we built for `/pin-until` is
**still useful** - it'll be used for the auto-unpin timers ("unpin GA thread
on the GA date"). So the infrastructure stays; only the user-facing command
goes away.

**Status:** nothing to implement now. Will land naturally when we build the GA
workflow (section 4).

---

## 4. Stub workflows - what they were and what they should become

We deleted four stub `.py` files. Here's what each was scaffolded to be, kept
here so we don't lose the intent.

### 4.1 General Circular (Γενική Εγκύκλιος)

**File deleted:** `src/workflows/general_circular.py`
**LLM prompt:** still exists at `data/prompts/circular.md`
**Trigger:** CLI command, or scheduled quarterly

**Intended steps:**
1. `aggregate_data` - pull recent board meeting minutes, director's reports, key decisions from `[Βιβλίο Αποφάσεων]`
2. `draft_circular` - LLM drafts the εγκύκλιος (general type - informative summary)
3. `generate_pdf` - render via Google Doc template (need to create one - like the invitation template)
4. `approval` - approval gate
5. `archive` - OneDrive: `/Amnesty/Archive/Εγκύκλιοι/{YYYY}/Γενική-{N}.pdf`
6. `distribute` - Brevo campaign to members + `bus.publish(EVENT_EGKYKLIOS_PUBLISHED)`

**What's missing before we can build:**
- Google Doc template for εγκύκλιος (like the invitation template)
- Brevo HTML template for the εγκύκλιος email
- A board sign-off mechanism beyond the SecGen single approval (γενική εγκύκλιοι are typically board-approved before distribution)

### 4.2 Special Circular (Ειδική Εγκύκλιος)

**File deleted:** `src/workflows/special_circular.py`
**LLM prompt:** same `data/prompts/circular.md` (handles both types)
**Trigger:** CLI command (ad-hoc)

**Intended steps:** same as general circular except step 1 is
`gather_context` - the SecGen supplies a topic and any source documents,
and the LLM works from that instead of aggregating broadly.

**Distinguishing notes:**
- Often more urgent than γενική
- May request specific action from members (sign petition, attend event, vote)
- Audience may be filtered (only Τακτικά Μέλη, only Νέοι, etc.)

### 4.3 General Assembly (Γενική Συνέλευση)

**File deleted:** `src/workflows/general_assembly.py`
**Trigger:** CLI command (annual or extraordinary GA)

**Intended steps:**
1. `draft_invitation` - LLM drafts the GA call (references Καταστατικό άρθρα for notice period)
2. `check_notice_period` - validates against `workflows.general_assembly.min_notice_days` (30) and `min_electronic_notice_days` (15)
3. `approval` - approval gate
4. `distribute_invitation` - Brevo + Discord announcement + **auto-pin in `#γενικό` until GA date**
5. `generate_activity_report` - LLM-aided report of section activities (often by director)
6. `compile_documents` - bundle: agenda, candidate statements, financial reports, audit reports
7. `schedule_reminders` - reminders at T-30d, T-15d, T-1d (uses `discord_pending_actions`)
8. `draft_minutes` (post-GA) - like board minutes but for GA
9. `finalize` - archive + bus event

**Notable: this is where `/pin-until`'s replacement lives.** The auto-pin
happens in step 4; the auto-unpin is the T-0 reminder.

### 4.4 Forum Management (deprecated - superseded by Discord work)

**File deleted:** `src/workflows/forum_management.py`
**Status:** the Phase C team-management cog + Phase B event bus replaced this
entirely. The original stub had `post_announcement` and `verify_member` steps;
the member-verification step is **explicitly out of scope** per your decision
(no auto-verification). Announcements happen via `platform_bridge` reacting to
bus events. No separate workflow needed.

---

## 5. Open questions

### Blocking implementation of §2 (board workflow)

Everything is resolved. Remaining work is implementation, in two phases:

**Phase 1 - wiring:**
- Extend the MSAL stack to handle Microsoft Graph `Mail.Send` (depends on Azure AD app registration getting the new scope + your consent)
- Add `M365MailClient` with one method: `send_email(to, subject, body, in_reply_to=None, references=None)`
- Update workflow with the new steps in §2.6
- Plumb `discord_thread_body` through `BoardMeetingScheduledPayload`

**Phase 2 - Azure AD consent (you do, once):**
- Open the existing app registration in Azure portal
- Add `Mail.Send` scope (delegated permissions) to API permissions
- Click "Grant admin consent"
- Next time the bot needs a token, you'll consent in the browser flow once,
  then it caches

### Resolved (kept here for reference)

- Scheduling: paste poll URL when starting the workflow (no Cabbagemeet, no API integration) - ✅
- Agenda input: direct Google Sheet edit via link in scheduling email - ✅
- Email identities: Gmail for forum bridge, M365 (Graph) for board sends - ✅
- Poll stays open until manual advance (approval gate) - ✅
- Brevo for member blast, separate threaded board emails - ✅
- Anchor `Message-ID` is `<{meeting_id}@amnesty.org.gr>` - ✅
- Private Discord board channel deferred - ✅
- Public Discord thread content = Brevo body - ✅

### For future workflows (not blocking now)

- Εγκύκλιοι: board approval beyond SecGen single approval?
- GA reminder cadence: T-30d, T-15d, T-1d, T-1h?
- Auto-pin reuses `discord_pending_actions` (default yes unless you object).

---

## 6. Greek minutes transcription pipeline (post-meeting per-participant audio + faster-whisper)

**Priority: HIGH for the validation spike (§6.5); MEDIUM for the full build,
gated on the spike outcome.** Fixes a real, shipped-today quality hole: the
`board_meeting_minutes` workflow's `draft_minutes` step ingests Zoom's
post-meeting **transcript**, which for Greek is corrupted garbage (see §6.1).

> **Design decision (2026-05-31): RTMS dropped in favour of post-meeting
> per-participant cloud recording.** See §6.7 for why. The two approaches share
> the *same* downstream (our own Whisper + skeleton + SLM tiers); the only
> difference is how we obtain per-participant audio. Cloud recording gives it
> with zero live infrastructure, zero Developer-Pack cost, and no Zoom-side
> enablement wait.

### 6.1 The problem

Zoom's Live AI Companion transcribes Greek accurately in real time, but Zoom's
**post-meeting** transcription supports only ~19 core languages, excluding
Greek. So Zoom's emitted transcript is broken - and that is what `draft_minutes`
is fed today. **The fix is to ignore Zoom's transcript entirely and transcribe
the audio ourselves.** Audio is never corrupted by cloud recording; only the
transcript is. Once we do our own ASR, we never needed live interception at all.

### 6.2 The mechanism - fetch post-meeting per-participant audio

- Enable **"Record a separate audio file of each participant"** in the account's
  cloud-recording settings (supported for up to 200 participants; must be ON
  *before* the meeting starts).
- A **`recording.completed`** webhook fires after the meeting → our backend
  (reuse the Cloudflare-tunnel webhook infra already used by `m365_inbox`).
- Fetch the per-participant audio files via the **cloud-recording REST API** -
  scopes already held: `cloud_recording:read:list_recording_files:admin`,
  `cloud_recording:read:recording:admin`. Save to
  `data/transcripts/{meeting_uuid}/`.
- Resolve attendance from the past-participants API
  (`meeting:read:list_past_participants:admin`, already held).

No always-on process, no WebSocket, no HMAC handshake, no streaming-minute
billing. Pure batch, post-meeting.

### 6.3 Why per-participant audio is the key win

Each participant's audio arrives as its **own file**, so **speaker attribution
comes free from Zoom** - we run faster-whisper per file and never touch speaker
**diarization** (the error-prone part of every other transcription tool):

```
per-participant audio files  →  faster-whisper per file  →  merge by timestamp
   (speaker = file owner)        (Greek text + times)        → speaker-labelled transcript
```

- **faster-whisper** (CTranslate2 reimpl of Whisper large-v3): MIT-licensed,
  free, self-hosted. int8 on CPU is fine - it's a post-meeting batch job, no GPU,
  no latency pressure.
- Caveat: if several board members share one room/device (one connection = one
  file) they collapse into a single speaker; only that room would need light
  diarization. Confirm how the board actually joins.

### 6.4 Transcript → proper minutes (the `draft_minutes` upgrade)

This upgrades `draft_minutes`'s input from garbage to a clean, speaker-labelled
Greek transcript. Drafting stays an LLM "structured extraction + formal rewrite"
task (the existing `src/prompts/board_minutes.md` already defines the πρακτικά
JSON structure and `[ΝΑ ΕΠΙΒΕΒΑΙΩΘΕΙ]` uncertainty markers). Quality levers:

- **Error reduction at source:** seed faster-whisper's `initial_prompt` with a
  glossary - board-member names (from `config.yaml → board_members`), the org
  name, recurring acronyms/campaign names - to cut proper-noun ASR errors.
- **Speaker → roster mapping:** map each audio file's participant to
  `board_members` so it renders as "Γρ. Μουζακίτης - Ταμίας".
- **Agenda anchoring + mid-meeting events:** the `meeting_events` store (built)
  supplies agenda-advance / vote / presence markers; `build_minutes_skeleton`
  (built) time-binds segments to agenda items deterministically.
- **Grounding context:** pass the Director's εισηγητικά/ενημερωτικά (archived by
  the director-briefing workflow) and a past finalized minutes doc as a style
  exemplar. SecGen notes remain **authoritative** (existing rule).
- **Map-reduce for long meetings:** pass 1 = per-agenda-item extraction to JSON;
  pass 2 = assemble formal Greek πρακτικά. (Tier-1/Tier-2 SLMs - see ROADMAP
  model-tier notes.)
- **Human gate stays:** `approval_and_share` routes the draft to a Google Doc for
  SecGen/board correction. Never auto-finalise.

### 6.5 The spike (do this FIRST, before any build) - now trivial

No Zoom enablement needed; we already have everything. Half-hour:
1. Turn on "Record a separate audio file of each participant" in cloud-recording
   settings.
2. Record one short test meeting with 2-3 people each on their own connection,
   speaking Greek.
3. Download the recording files via the API and inspect:
   - **Are there genuinely separate per-participant audio files?**
   - **What is their time origin** - does each file start at meeting-start
     (silence-padded) or at the participant's join time? This determines the
     merge-by-timestamp logic feeding `build_minutes_skeleton`.
4. Run faster-whisper on one file to sanity-check Greek quality with a glossary
   `initial_prompt`.

### 6.6 Open questions / risks

- 🔴 **Time origin of per-participant files** (common start vs join-time offset)
  - resolved by §6.5; drives timestamp alignment.
- 🔴 Host must enable the per-participant setting **before** each meeting - set
  it as the account default and add it to the pre-meeting checklist; fallback to
  mixed-audio + diarization if a recording lacks per-participant files.
- ✅ **GDPR/consent** - board granted consent for transcription (2026-05-31).
  Cloud recording shows Zoom's native "recording in progress" notice, which
  helps the per-meeting consent requirement (ethics L5). Still document a
  retention/access policy for the audio + transcripts (data minimisation).

### 6.7 Rejected alternative: RTMS (Real-Time Media Streams)

Originally planned, then dropped (2026-05-31). RTMS would intercept live media
over a WebSocket. Its *only* advantage over cloud recording was capturing the
live Greek **transcript** before Zoom's pipeline mangles it - but since we run
our **own** Whisper on audio (cheaper, more private, better Greek; see the model
ethics framework), we never use Zoom's transcript, so that advantage is moot.
RTMS would have added: a Zoom account-enablement wait (long pole), a Developer
Pack subscription + per-streaming-minute charges, and an always-on WebSocket
consumer with HMAC handshake/keep-alive/reconnection. Cloud recording delivers
the same per-participant audio with none of that. (The exploratory
`ZOOM_RTMS_SECRET_TOKEN` field was renamed to `ZOOM_WEBHOOK_SECRET_TOKEN` - the
same Zoom per-app Secret Token is reused for `recording.completed` webhook CRC
validation.)

---

## 7. Status snapshot

✅ = done - 🟡 = designed, not implemented - ⬜ = not yet designed

| Item | Status |
|---|---|
| Phase A: tech-debt cleanup of Discord bot | ✅ |
| Phase B: event bus + persistent reminders + skeleton platform_bridge | ✅ |
| Phase C: team management cog (`/team` commands) | ✅ |
| Phase D: board-meeting Discord integration (4 event handlers) | ✅ |
| Phase D dual-channel: public + private board thread | ✅ |
| Phase E.1: Εγκύκλιοι workflow + Discord bridge | 🟡 |
| Phase E.2: General Assembly workflow + Discord + auto-pin | 🟡 |
| Board workflow §2.1: Cabbagemeet scheduling step | 🟡 |
| Board workflow §2.2: email threading per meeting | 🟡 |
| Board workflow §2.3: board emails → `#συνεδριάσεις` | 🟡 |
| Member welcome (Discord onboarding does it) | - n/a |
| Polls cog | ⬜ |
| Activism team migration | ⬜ |
| §6 Greek minutes (cloud-recording audio + Whisper) - spike | 🟡 HIGH (unblocked) |
| §6 Greek minutes - full pipeline | 🟡 MED (gated on spike) |
| §6.7 RTMS approach | ❌ rejected (superseded by cloud recording) |
