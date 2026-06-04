# FOUNDATION PLAN — AI Automation Platform for Amnesty International Greece

> Working document. Author: Γεώργιος Αθανασίας (Γενικός Γραμματέας).
> Last updated: 2026-04-03.
> This document is a technical reference for platform development. It is not intended for Board or member distribution in its current form.

---

## 1. PROJECT OVERVIEW

### 1.1 Purpose

Build a modular automation platform that assists the Board of Directors (ΔΣ) of Amnesty International Greece in administrative governance tasks — document generation, meeting management, member communications, and archival — using AI (Claude API) and integration with existing tools (Microsoft 365, Google Workspace, Zoom, Brevo, Discord).

### 1.2 Guiding Principles

- **Human-in-the-loop**: AI drafts, humans approve. No automated action goes out without explicit user confirmation, especially for external communications and official documents.
- **Incremental deployment**: Ship one workflow end-to-end before starting the next. Validate with real use before expanding scope.
- **Institutional memory**: Every automated action is logged. The platform builds organizational knowledge, not just efficiency.
- **Minimal cost**: Leverage free tiers, open-source tools, and existing subscriptions. Budget ceiling: ~€5/month recurring after initial build.
- **Regulatory alignment**: Full compliance with GDPR, the Καταστατικό, and Εσωτερικοί Κανονισμοί. No shortcuts on data handling.

### 1.3 Scope Boundaries

**In scope (this plan):**
- Board meeting lifecycle (invitations, scheduling, minutes, decision tracking)
- Circulars (general and special)
- General Assembly lifecycle (invitations, preparation protocol, minutes)
- Member newsletter distribution
- Document archival and filing
- Member forum management (Discord)
- General AI-assisted governance support

**Out of scope (for now):**
- Fundraising/donor management
- Campaign coordination and activism tools
- Public-facing website automation
- Financial management systems
- Partnership management
- Election/voting platform (complex; needs dedicated planning)

---

## 2. ARCHITECTURE & TECH STACK

### 2.1 High-Level Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    ORCHESTRATION LAYER                   │
│                  (Python — FastAPI app)                  │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐             │
│  │ Workflow  │  │  Claude   │  │  Audit   │             │
│  │  Engine   │  │  Client   │  │  Logger  │             │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘             │
│       │              │              │                    │
│  ┌────┴──────────────┴──────────────┴────┐              │
│  │           INTEGRATION LAYER           │              │
│  │  ┌─────┐ ┌─────┐ ┌─────┐ ┌────────┐  │              │
│  │  │MS365│ │Zoom │ │Brevo│ │Discord │  │              │
│  │  │(API)│ │(API)│ │(API)│ │(Bot)   │  │              │
│  │  └─────┘ └─────┘ └─────┘ └────────┘  │              │
│  │  ┌─────────────┐  ┌───────────────┐   │              │
│  │  │Google Drive/ │  │  Google       │   │              │
│  │  │Sheets (API)  │  │  Gmail (API)  │   │              │
│  │  └─────────────┘  └───────────────┘   │              │
│  └───────────────────────────────────────┘              │
│                                                         │
│  ┌───────────────────────────────────────┐              │
│  │           STORAGE LAYER               │              │
│  │  ┌──────────┐  ┌──────────────────┐   │              │
│  │  │ SQLite   │  │ OneDrive         │   │              │
│  │  │ (local   │  │ (document        │   │              │
│  │  │  state)  │  │  archive)        │   │              │
│  │  └──────────┘  └──────────────────┘   │              │
│  └───────────────────────────────────────┘              │
│                                                         │
│  ┌───────────────────────────────────────┐              │
│  │           INTERFACE LAYER             │              │
│  │  ┌──────────────┐  ┌──────────────┐   │              │
│  │  │ CLI / REPL   │  │ Webhook      │   │              │
│  │  │ (primary)    │  │ Listener     │   │              │
│  │  └──────────────┘  └──────────────┘   │              │
│  └───────────────────────────────────────┘              │
└─────────────────────────────────────────────────────────┘
```

### 2.2 Component Breakdown

#### Orchestration Layer

| Component | Technology | Purpose |
|---|---|---|
| **Core framework** | Python 3.11+ / FastAPI | Lightweight async web framework. Handles webhook endpoints, serves as the backbone for all workflow logic. |
| **Workflow engine** | Custom Python module | Stateful workflow runner. Each workflow (e.g., "board meeting invitation") is a sequence of steps with checkpoints for human approval. |
| **Claude client** | `anthropic` Python SDK | Handles all LLM calls — document drafting, summarization, analysis. Uses Claude Sonnet for cost efficiency. |
| **Audit logger** | Python `logging` + SQLite | Every action (API call, document generated, email sent, approval given) is logged with timestamp, actor, and outcome. |

#### Integration Layer

| Service | API / Method | Authentication | Key Operations |
|---|---|---|---|
| **Microsoft 365 / OneDrive** | Microsoft Graph API | OAuth 2.0 (app registration in Azure AD) | Upload/download files, create folders, share documents, manage permissions |
| **Google Drive** | Google Drive API v3 | OAuth 2.0 (Google Cloud Console) | Read shared documents (templates, Google Sheets agenda data) |
| **Google Sheets** | Google Sheets API v4 | Same OAuth as Drive | Read agenda data, write to Βιβλίο Αποφάσεων |
| **Gmail** | Gmail API | OAuth 2.0 (same Google project) | Send board emails to board@amnesty.org.gr, director@amnesty.org.gr |
| **Zoom** | Zoom Server-to-Server OAuth | OAuth 2.0 (Zoom Marketplace app) | Schedule meetings, retrieve recordings/transcripts |
| **Brevo** | Brevo REST API v3 | API key | Send newsletters from templates, manage contact lists |
| **Discord** | Discord Bot (discord.py) | Bot token | Post announcements, manage forum channels, verify member applications |

#### Storage Layer

| Store | Technology | Contents |
|---|---|---|
| **Local state DB** | SQLite (single file) | Workflow states, audit log, scheduled tasks, configuration |
| **Document archive** | OneDrive (via Graph API) | All finalized documents (PDFs, approved minutes, circulars) |
| **Templates** | Google Drive (read-only) | Document templates (invitations, minutes, circulars, reports) |
| **Configuration** | `.env` file + `config.yaml` | API keys, OAuth tokens, workflow parameters |

#### Interface Layer

| Interface | Purpose | Notes |
|---|---|---|
| **CLI / REPL** | Primary interaction mode during Phase 1 | Operator runs commands, reviews drafts, gives approvals. Simple and auditable. |
| **Webhook listener** | Automated triggers | FastAPI endpoints that receive webhooks from Zoom (recording ready), Brevo (email events), etc. |
| **Future: Web dashboard** | Optional Phase 3+ | Simple web UI for non-technical board members to trigger workflows and view status. Not in initial scope. |

### 2.3 Project Structure

```
ai-in-ai/
├── README.md
├── FOUNDATION.md              # This document
├── .env.example               # Template for secrets (never commit .env)
├── config.yaml                # Non-secret configuration
├── requirements.txt
├── pyproject.toml
│
├── src/
│   ├── __init__.py
│   ├── main.py                # FastAPI app entry point
│   ├── config.py              # Config loader
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   ├── workflow.py        # Base workflow engine (state machine)
│   │   ├── claude.py          # Claude API client wrapper
│   │   ├── audit.py           # Audit logging system
│   │   └── scheduler.py       # Task scheduling (APScheduler)
│   │
│   ├── integrations/
│   │   ├── __init__.py
│   │   ├── onedrive.py        # Microsoft Graph API client
│   │   ├── google_drive.py    # Google Drive/Sheets client
│   │   ├── gmail.py           # Gmail API client
│   │   ├── zoom.py            # Zoom API client
│   │   ├── brevo.py           # Brevo API client
│   │   └── discord_bot.py     # Discord bot
│   │
│   ├── workflows/
│   │   ├── __init__.py
│   │   ├── board_meeting_invitation.py
│   │   ├── board_meeting_minutes.py
│   │   ├── general_circular.py
│   │   ├── special_circular.py
│   │   ├── general_assembly.py
│   │   └── forum_management.py
│   │
│   ├── documents/
│   │   ├── __init__.py
│   │   ├── pdf_generator.py   # PDF creation from templates
│   │   ├── docx_generator.py  # DOCX creation from templates
│   │   └── templates.py       # Template fetching and management
│   │
│   ├── translations/          # Placeholder — future translation API integration
│   │   └── __init__.py        # (EN→EL for international movement texts)
│   │
│   └── cli/
│       ├── __init__.py
│       └── commands.py        # CLI commands for manual workflow triggers
│
├── data/
│   ├── amnesty.db             # SQLite database (gitignored)
│   └── prompts/               # Claude system prompts per workflow
│       ├── board_invitation.md
│       ├── board_minutes.md
│       ├── circular.md
│       └── general_support.md
│
├── tests/
│   ├── __init__.py
│   ├── test_workflows.py
│   ├── test_integrations.py
│   └── fixtures/              # Sample data for tests
│
└── docs/
    ├── api_setup/             # Step-by-step API registration guides
    │   ├── microsoft_graph.md
    │   ├── google_cloud.md
    │   ├── zoom.md
    │   ├── brevo.md
    │   └── discord.md
    └── workflows/             # Per-workflow documentation
```

### 2.4 Key Technical Decisions

**Why Python?** It's the best-supported language for the Anthropic SDK, has mature libraries for every API we need, and matches your existing skill level. FastAPI gives us async support and auto-generated API docs for free.

**Why SQLite (not Postgres)?** This platform serves 3 users maximum. SQLite is zero-config, file-based, easy to back up, and more than sufficient. If you ever need to migrate, the schema will be simple enough to port in an hour.

**Why CLI-first (not web UI)?** Fastest to build, easiest to debug, most auditable. You can always add a web layer later — FastAPI makes this trivial since the backend is already an HTTP server.

**Why store templates on Google Drive?** The Board already uses Google Docs for templates. Pulling them via API avoids maintaining duplicate copies and ensures templates are always current. Google Drive API can export Docs directly as PDF, so we can download pixel-accurate PDFs without any local rendering pipeline.

**Why OneDrive for archival?** The organization already uses OneDrive as its official archive. We follow the existing institutional pattern.

### 2.5 Hosting Strategy

**Phase 1 (Pilot):** Run locally on your machine or a free-tier cloud instance. Options:

| Option | Free Tier | Limitations | Verdict |
|---|---|---|---|
| **Oracle Cloud Free Tier** | 2 AMD VMs (1 GB RAM each), always free | ARM instances have waitlist; AMD is reliable | **Best option** — genuinely always-free, sufficient specs |
| **Google Cloud Free Tier** | f1-micro (0.6 GB RAM), always free | Very limited RAM; tight for Python + FastAPI | Viable but tight |
| **Fly.io** | 3 shared VMs (256 MB each) | Apps sleep after inactivity; limited storage | Good for webhooks, not persistent workers |
| **Local machine** | Free | Must be running; no static IP for webhooks | Fine for development/testing only |

**Recommendation:** Oracle Cloud Free Tier for production pilot. Use local machine for development. Use a tunneling service (e.g., ngrok free tier or Cloudflare Tunnel) for webhook testing during development.

**Phase 2+:** Re-evaluate based on actual usage. If the platform proves valuable, a €4-5/month VPS (Hetzner, Contabo) gives you full control and reliability.

---

## 3. GOVERNANCE, COMPLIANCE & DATA HANDLING

### 3.1 GDPR Compliance Framework

The platform processes personal data of Board members and Section members. As Amnesty International Greece is the data controller, the platform must comply with the GDPR and the Greek implementation law (Ν. 4624/2019).

#### Data Inventory

| Data Category | Source | Processing Purpose | Legal Basis | Retention |
|---|---|---|---|---|
| Board member names & emails | Existing records | Meeting management, document distribution | Legitimate interest (Art. 6(1)(f)) | Duration of Board term + 5 years |
| Member registry (names, emails, join dates) | Μητρώο Μελών | Forum verification, newsletter distribution | Consent (Art. 6(1)(a)) + Legitimate interest | Active membership + 2 years |
| Meeting transcripts (Zoom) | Zoom API | Minutes drafting | Legitimate interest | Until minutes are finalized, then deleted |
| Board meeting minutes (content) | AI-generated drafts | Governance record-keeping | Legal obligation (Art. 6(1)(c)) | Permanent (statutory archive) |
| Newsletter interaction data | Brevo | Communication effectiveness | Legitimate interest | 12 months |

#### Data Processing Principles Applied

1. **Purpose limitation**: Data is only used for the specific workflow it was collected for. Meeting transcripts are not used for anything other than minutes generation.
2. **Data minimization**: Claude API calls include only the data needed for the specific task. Full member lists are never sent to Claude unless necessary (e.g., forum verification).
3. **Storage limitation**: Zoom transcripts are deleted after minutes are approved. Draft documents are deleted after final versions are archived.
4. **Integrity & confidentiality**: API keys stored in `.env` (never committed to git). OAuth tokens refreshed automatically. SQLite DB access restricted to platform process.
5. **Accountability**: Full audit log of all data processing actions.

#### Required Documentation (to create before launch)

- [ ] Data Processing Record (Αρχείο Δραστηριοτήτων Επεξεργασίας) — Art. 30 GDPR
- [ ] Privacy notice update for members regarding AI-assisted processing
- [ ] Data Processing Agreement (DPA) review for: Anthropic (Claude API), Zoom, Brevo, Microsoft, Google
- [ ] Data Protection Impact Assessment (DPIA) — recommended given AI processing of governance data

### 3.2 AI-Specific Governance

#### What Claude sees and doesn't see

| Claude receives | Claude does NOT receive |
|---|---|
| Meeting transcript text (for minutes drafting) | Raw audio/video files |
| Agenda items and dates (for invitation drafting) | Member personal contact details (unless needed) |
| Template structure and formatting instructions | API keys, OAuth tokens, internal credentials |
| Previous minutes/circulars (for consistency) | Financial records or donor data |
| Relevant Καταστατικό/Κανονισμοί excerpts (for advice) | Full member registry |

#### Prompt management

All Claude system prompts are stored in `data/prompts/` as versioned markdown files. This ensures:
- Reproducibility: any output can be re-generated with the same prompt + input
- Auditability: prompt changes are tracked in git
- Consistency: the AI's behavior is predictable and documented

#### Human approval gates

Every workflow has at least one mandatory approval checkpoint before any external action (email sent, document published, meeting scheduled). The platform NEVER:
- Sends an email without explicit user confirmation
- Publishes a document without review
- Modifies the Βιβλίο Αποφάσεων without approval
- Posts to Discord without approval (except pre-approved automated messages like forum verification)

### 3.3 Audit System

Every action is logged to the `audit_log` table in SQLite:

```sql
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    workflow TEXT NOT NULL,          -- e.g., 'board_meeting_invitation'
    action TEXT NOT NULL,            -- e.g., 'draft_generated', 'email_sent', 'approval_given'
    actor TEXT NOT NULL,             -- e.g., 'system', 'secgen', 'claude'
    target TEXT,                     -- e.g., 'board@amnesty.org.gr', 'ΔΣ04-2026.pdf'
    details TEXT,                    -- JSON blob with action-specific metadata
    status TEXT NOT NULL DEFAULT 'success'  -- 'success', 'failure', 'pending'
);
```

The audit log serves three purposes:
1. **Accountability**: Any Board member can see what the platform did, when, and who approved it.
2. **Debugging**: When something goes wrong, the log shows exactly where the workflow failed.
3. **Institutional memory**: Over time, the log becomes a record of organizational activity patterns.

### 3.4 Access Control

| Role | Access Level | Can Do | Cannot Do |
|---|---|---|---|
| **Γενικός Γραμματέας** | Full | Trigger all workflows, approve all actions, manage config | N/A |
| **Πρόεδρος** | Elevated | Trigger meeting workflows, approve documents, view audit log | Modify platform config, manage API keys |
| **Ειδική Γραμματέα** | Elevated | Same as Πρόεδρος | Same as Πρόεδρος |
| **System (automated)** | Restricted | Send pre-approved reminders, receive webhooks, log events | Any action requiring human approval |

Implementation in Phase 1 is simple: the CLI authenticates the operator and logs their identity. Multi-user access control becomes relevant only if/when a web dashboard is built.

### 3.5 Alignment with Καταστατικό & Εσωτερικοί Κανονισμοί

Key regulatory requirements the platform must respect:

| Requirement | Source | Platform Compliance |
|---|---|---|
| ΔΣ meets at least monthly | Καταστατικό | Platform tracks meeting cadence and can alert if overdue |
| ΓΣ notice requires 30 days (15 electronic) | Καταστατικό | Workflow enforces minimum notice periods |
| Documents must be available in Greek (and English where required) | Εσωτερικοί Κανονισμοί | Claude drafts in Greek; bilingual support added per workflow need |
| Minutes must record attendees, decisions, and voting results | Καταστατικό | Minutes template enforces these fields |
| Βιβλίο Αποφάσεων must be maintained | Εσωτερικοί Κανονισμοί | Platform writes decisions to the Google Sheet after approval |
| Member data must be kept secure | Εσωτερικοί Κανονισμοί + GDPR | Encryption at rest, access logging, data minimization |

---

## 4. PHASED ROLLOUT STRATEGY

### 4.1 Phase Overview

```
Phase 0: Infrastructure Setup              [~2 weeks]
Phase 1: Board Meeting Invitation Flow     [~2 weeks]
Phase 2: Board Meeting Minutes Flow        [~2 weeks]
Phase 3: Circulars (General + Special)     [~2 weeks]
Phase 4: General Assembly Lifecycle        [~2 weeks]
Phase 5: Discord Forum Management          [~1 week]
Phase 6: General Support & Refinement      [ongoing]
                                           ─────────
                                    Total: ~11 weeks
```

Timeline is working time, not calendar time. Assumes ~10-15 hours/week available. Calendar time will be longer.

---

### Phase 0 — Infrastructure Setup

**Goal:** Platform skeleton is running. All APIs are authenticated. One end-to-end test proves the pipeline works (Claude drafts something → saves to OneDrive → sends test email).

**Tasks:**

0.1. **Development environment**
   - Set up Python project with `pyproject.toml`, virtual environment, and dependency management
   - Initialize git repository with `.gitignore` (exclude `.env`, `data/amnesty.db`, `__pycache__/`)
   - Set up basic project structure (see §2.3)

0.2. **API registrations** (the tedious but critical part)
   - Microsoft Azure AD: Register app, configure Graph API permissions (Files.ReadWrite, Mail.Send, User.Read), set up OAuth flow
   - Google Cloud Console: Create project, enable Drive API + Sheets API + Gmail API, configure OAuth consent screen, generate credentials
   - Zoom Marketplace: Register Server-to-Server OAuth app, configure required scopes (meeting:write, recording:read, user:read)
   - Brevo: Generate API key, set up sender identity verification
   - Discord Developer Portal: Create bot application, configure intents (members, messages, guilds), generate bot token

   **Deliverable:** Step-by-step guide for each API in `docs/api_setup/` — so this process is documented and reproducible.

0.2b. **Brevo template design**
   - Design all newsletter templates needed for Phase 1+ (Board meeting invitation, meeting reminder, circular, GA invitation, etc.)
   - This is a manual task done in the Brevo UI by the operator, but must be completed before any workflow that sends newsletters

0.3. **Core modules**
   - `config.py`: Load `.env` and `config.yaml`, validate required values
   - `audit.py`: Create SQLite DB schema, implement logging functions
   - `claude.py`: Wrapper around Anthropic SDK with retry logic, token counting, and cost tracking
   - `workflow.py`: Base workflow class with state management (pending → in_progress → awaiting_approval → approved → executing → completed/failed)

0.4. **Integration smoke tests**
   - Each integration module (`onedrive.py`, `zoom.py`, etc.) gets a simple test: authenticate, perform one read operation, verify response
   - End-to-end test: Claude generates a test paragraph → saved as PDF to OneDrive → test email sent via Gmail

**Exit criteria:** All APIs authenticated and tested. Core modules working. Audit log recording actions. End-to-end smoke test passes.

---

### Phase 1 — Board Meeting Invitation Workflow

**Goal:** The complete invitation flow from Intro.md works end-to-end: read agenda from Google Sheets → Claude drafts PDF → user approves → archive to OneDrive + schedule Zoom + send Brevo newsletter + send board email + schedule reminder.

**Why first?** It touches almost every integration (Google Sheets, Claude, OneDrive, Zoom, Brevo, Gmail) but the document itself is formulaic — low risk, high learning value.

**Tasks:**

1.1. **Template analysis**
   - Fetch invitation template from Google Drive
   - Define the data schema: what variables does the template need (date, time, agenda items, Zoom link, etc.)
   - Create Claude prompt in `data/prompts/board_invitation.md`

1.2. **Workflow implementation** (`workflows/board_meeting_invitation.py`)
   - Step 1: Read agenda data from Google Sheets (date, time, agenda items)
   - Step 2: Schedule Zoom meeting → get meeting link
   - Step 3: Send data to Claude → receive formatted invitation text
   - Step 4: Generate PDF from Claude output using template styling
   - Step 5: **[APPROVAL GATE]** Display draft to user, await confirmation
   - Step 6: Archive PDF to OneDrive in correct folder structure
   - Step 7: Send newsletter via Brevo API using template
   - Step 8: Send Zoom link email to board@amnesty.org.gr and director@amnesty.org.gr
   - Step 9: Schedule reminder email 3 hours before meeting
   - All steps logged to audit trail

1.3. **PDF generation**
   - Implement `documents/pdf_generator.py` — convert Claude's structured output to formatted PDF matching the institutional template style

1.4. **Testing with real data**
   - Run the workflow for the next actual Board meeting
   - Compare output with manually created invitations
   - Iterate on Claude prompt and PDF formatting

**Exit criteria:** Invitation workflow runs end-to-end. PDF output matches institutional quality standards. All actions logged. User can trigger the workflow and approve the draft from CLI.

---

### Phase 2 — Board Meeting Minutes Workflow

**Goal:** After a Board meeting, process the Zoom transcript into draft minutes following the institutional template, share for review, and handle finalization.

**Tasks:**

2.1. **Transcript handling**
   - Primary path: Retrieve transcript via Zoom API (automatic when available)
   - Secondary path: Manual upload of transcript file (generated via Word transcribe from Zoom audio)
   - Workflow accepts a `transcript_source` parameter: `zoom_api` | `manual_upload`
   - Build transcript pre-processing: clean up speaker labels, timestamps, remove filler

2.2. **Minutes generation**
   - Fetch minutes template from Google Drive
   - Create Claude prompt (`data/prompts/board_minutes.md`) with specific instructions:
     - Extract decisions and record them distinctly
     - Identify attendees from transcript
     - Follow the institutional template structure exactly
     - Write in formal Greek (matching existing minutes style — use archived minutes as few-shot examples)
   - Implement DOCX generation (`documents/docx_generator.py`)

2.3. **Workflow implementation** (`workflows/board_meeting_minutes.py`)
   - Step 1: Retrieve or accept transcript
   - Step 2: Send to Claude with template and style examples
   - Step 3: Generate DOCX draft
   - Step 4: **[APPROVAL GATE]** User reviews draft
   - Step 5: Share DOCX via OneDrive, email link to board@amnesty.org.gr
   - Step 6: (Later, after Board approval) Convert to final PDF, archive
   - Step 7: Extract decisions → write to Βιβλίο Αποφάσεων (Google Sheets)
   - All steps logged

2.4. **Quality validation**
   - Compare AI-generated minutes against existing manually written ones (you have 10+ archived examples)
   - Iterate on prompt until output quality is consistently acceptable

**Exit criteria:** Minutes workflow produces draft DOCX that requires only minor edits. Decision extraction works reliably. Archival and decision logging automated.

---

### Phase 3 — Circulars (General + Special)

**Goal:** Automate quarterly general circulars and ad-hoc special circulars, from drafting through distribution.

**Tasks:**

3.1. **General circular workflow**
   - Aggregate data from recent Board minutes and director's reports
   - Claude drafts circular following template
   - Review → finalize → archive → distribute via Brevo

3.2. **Special circular workflow**
   - User provides topic and key documents
   - Claude pulls additional context from OneDrive/Google Drive
   - Claude drafts circular
   - Review → finalize → archive → distribute via Brevo

3.3. **Brevo template management**
   - Set up and test newsletter templates in Brevo
   - Implement dynamic content injection from Claude-generated text

**Exit criteria:** Both circular workflows functional. Brevo distribution tested with real member list.

---

### Phase 4 — General Assembly Lifecycle

**Goal:** Full General Assembly support: invitation → preparation protocol (activity report, supporting documents, reminders) → minutes → decision recording.

**This is the most complex phase** because it orchestrates multiple sub-workflows and has strict regulatory timelines (30-day notice, quorum requirements, etc.).

**Tasks:**

4.1. **GA invitation workflow** (adapt from Board invitation, with different template and regulatory checks)
4.2. **Preparation protocol** — automated checklist that tracks:
   - Activity report generation (reuse data from Board minutes, circulars)
   - Required document compilation and distribution
   - Reminder scheduling (at key milestones before the GA date)
   - Presidium election call (via Brevo)
4.3. **GA minutes workflow** (adapt from Board minutes, with different template)
4.4. **Timeline enforcement** — platform validates that notice periods and deadlines mandated by the Καταστατικό are respected

**Exit criteria:** Full GA lifecycle tested. Timeline checks prevent regulatory violations. Activity report generation produces usable first draft.

---

### Phase 5 — Discord Forum Management

**Goal:** Automated forum management: post announcements, verify member join requests, basic moderation support.

**Tasks:**

5.1. **Discord bot setup** — configure bot with appropriate permissions
5.2. **Announcement posting** — when a newsletter is sent via Brevo, cross-post a summary to the Discord forum
5.3. **Member verification** — when a join request comes in, check against Μητρώο Μελών. If match: approve. If no match: flag for manual review (never auto-reject)
5.4. **Forum analytics** — basic metrics on engagement, active threads, member activity

**Exit criteria:** Bot runs reliably. Announcements posted automatically after approval. Member verification works with escalation path.

---

### Phase 6 — General Support & Refinement

**Goal:** Claude as an ongoing governance assistant, continuously improving.

**Tasks:**

6.1. **Knowledge base** — ingest Καταστατικό, Εσωτερικοί Κανονισμοί, and key strategic documents into a structured reference that Claude can query
6.2. **Proactive reminders** — platform alerts when:
   - Monthly Board meeting hasn't been scheduled
   - Quarterly circular is due
   - Document deadlines are approaching
6.3. **Prompt refinement** — analyze all Claude outputs to date, identify patterns of needed corrections, improve prompts
6.4. **Documentation** — comprehensive docs for platform maintenance and handover

**This phase has no end date.** It's the ongoing operation and improvement of the platform.

---

## 5. API SETUP PRIORITY & DEPENDENCIES

The order in which APIs should be set up, based on which workflows need them:

```
Priority 1 (needed for Phase 0 smoke test):
├── Anthropic (Claude API)     — needed for everything
├── Google Drive/Sheets API    — needed for template + agenda reading
└── Gmail API                  — needed for board email

Priority 2 (needed for Phase 1):
├── Microsoft Graph (OneDrive) — needed for document archival
├── Zoom API                   — needed for meeting scheduling
└── Brevo API                  — needed for newsletter distribution

Priority 3 (needed for Phase 5):
└── Discord Bot                — needed for forum management
```

### OAuth Token Management

Critical architectural decision: OAuth tokens expire. The platform needs a reliable token refresh mechanism.

**Approach:**
- Store refresh tokens encrypted in SQLite
- On each API call, check token expiry. If expired, refresh automatically.
- If refresh fails (e.g., token revoked), log the failure and alert the operator.
- Never store tokens in plaintext files.

Library recommendation: use `msal` for Microsoft, `google-auth` for Google, and direct OAuth2 for Zoom. Each has built-in token refresh support.

---

## 6. COST PROJECTION

| Item | Phase | Cost | Frequency |
|---|---|---|---|
| Claude Pro subscription | Build phase (Phase 0-1) | €21.50/month | 1-2 months |
| Claude Sonnet API (production) | Phase 1 onward | ~€1-2/month | Monthly |
| Oracle Cloud Free Tier | Phase 0 onward | €0 | Always free |
| Domain/SSL (if needed) | Phase 2+ | €0 (Cloudflare free) | N/A |
| Brevo | All phases | €0 (free tier: 300 emails/day) | N/A |
| Zoom | All phases | €0 (existing org subscription) | N/A |
| Microsoft 365 | All phases | €0 (existing org subscription) | N/A |
| **Total (build phase)** | | **~€25-45 one-time** | |
| **Total (ongoing)** | | **~€2/month** | |

---

## 7. RISK REGISTER

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| API authentication complexity takes longer than expected | High | Medium | Detailed setup guides in `docs/api_setup/`. Allocate extra time in Phase 0. |
| Claude output quality inconsistent for formal Greek | Medium | High | Use few-shot examples from existing documents. Iterative prompt engineering. Always human review. |
| Board members resist AI-generated documents | Medium | High | Start with low-stakes documents (invitations). Build trust gradually. Always frame as "AI-drafted, human-approved." |
| API rate limits or downtime | Low | Medium | Implement retry logic with exponential backoff. Graceful degradation (manual fallback for each workflow). |
| GDPR complaint or data breach | Low | Very High | DPIA before launch. Data minimization. Audit logging. No member PII sent to Claude unless necessary. |
| Key person dependency (only you can operate it) | High | High | Comprehensive documentation. Phase 6 includes handover preparation. Aim for at least one other person who can maintain the system. |
| Free hosting becomes unreliable | Medium | Medium | Platform is portable (Python + SQLite). Can migrate to any VPS in < 1 hour. |

---

## 8. SUCCESS METRICS

After 3 months of operation, evaluate:

| Metric | Target | How to Measure |
|---|---|---|
| Time saved per Board meeting cycle | >60% reduction | Compare time spent on invitation + minutes before/after |
| Document quality (edits needed on AI drafts) | <20% of content needs manual editing | Track edit distance between draft and final version |
| Workflow completion rate | >90% of workflows complete without failure | Audit log analysis |
| Regulatory compliance | 0 missed deadlines or notice periods | Audit log + calendar tracking |
| Platform uptime | >95% | Monitoring logs |
| Adoption | All three authorized users have used the platform | Usage logs |

---

## 9. DECISIONS LOG

*Resolved questions from initial planning.*

1. **Template format** — RESOLVED: Templates stay as Google Docs. The Google Drive API supports exporting Docs directly as PDF (`export` endpoint with `application/pdf` mime type), so we get the best of both worlds: Board edits in a familiar interface, platform downloads pixel-accurate PDFs programmatically. No need for local LaTeX or markdown templates.

2. **Transcript source** — RESOLVED: Primary path is Zoom API transcripts. Secondary fallback: manual upload of a transcript file (generated via Word's transcribe feature from the Zoom audio recording). The Phase 2 workflow must support both paths — implement a `transcript_source` parameter that accepts either `zoom_api` or `manual_upload`.

3. **Brevo templates** — RESOLVED: All Brevo newsletter templates will be designed from scratch by the operator. This is a prerequisite task before Phase 1 goes live. Add to Phase 0 checklist.

4. **Board communication about AI** — RESOLVED: Board is already informed and supportive. No separate communication plan needed. Proceed with implementation.

5. **Git hosting** — RESOLVED: Private GitHub repository. Free tier, automatic backups, and collaboration-ready.

6. **Bilingual support** — RESOLVED: The Εσωτερικοί Κανονισμοί only requires translation of international movement texts (English → Greek), not bilingual generation of all documents. Translation is not a priority for initial implementation. The architecture should leave room for a translation API integration later (e.g., DeepL or Google Translate API) but no implementation now. Add a `translations/` module placeholder in the project structure.

### All Questions Resolved

No open questions remain.

---

## 10. KNOWN REFINEMENTS FOR LATER PHASES

Items that are intentionally under-specified in this foundation plan. They will be fleshed out during the implementation of each respective phase, not upfront.

1. **Reminder scheduling mechanism**: Phase 1 mentions scheduling a reminder email 3 hours before each Board meeting. The implementation choice (APScheduler task in the platform vs. Brevo's scheduled send feature) will be decided during Phase 1 based on what Brevo's API supports natively. If Brevo supports delayed sends, prefer that (simpler). Otherwise, use APScheduler with the `scheduler.py` module.

2. **Βιβλίο Αποφάσεων extraction**: Phase 2 includes writing decisions to the Google Sheet. The exact extraction logic (how Claude identifies and structures decisions from minutes) will be designed during Phase 2 prompt engineering. This is a prompt problem, not an architecture problem.

3. **Director's reports for circulars**: Phase 3 requires fetching "εισηγητικά του Διευθυντή" as input for general circulars. Their location (OneDrive, Google Drive, or email) will be determined when Phase 3 starts. The integration layer already supports all three sources.

4. **Transcript pre-processing**: The specifics of cleaning Zoom transcripts (speaker label normalization, filler removal, timestamp handling) will be refined during Phase 2 based on actual transcript samples. Start with minimal processing and iterate.

5. **Voting/quorum data from transcripts**: Not all voting details may be captured in transcripts (e.g., roll calls may happen off-mic). Phase 2 should allow manual input of voting results as a supplement to transcript-extracted data.

6. **Translation API**: Placeholder exists (`translations/` module) for future EN→EL translation of international movement texts. Not implemented until needed.

---

## 11. NEXT STEPS

Immediate actions to start Phase 0:

1. **Create the private GitHub repository** with the project structure from §2.3
2. **Register for API access** starting with Priority 1 (Anthropic, Google Cloud) — this often takes a few days for approval
3. **Write `config.py` and `audit.py`** — the two foundational modules everything else depends on
4. **Set up the development environment** — Python venv, dependencies, linting (ruff), formatting (black)
5. **Design Brevo newsletter templates** — prerequisite for any workflow that sends newsletters
6. **Begin API setup guides** — document each registration process in `docs/api_setup/` as you go

---

*This document will be updated as decisions are made and phases are completed.*