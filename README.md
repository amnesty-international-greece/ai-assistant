# AI in AI

Governance automation for the Greek Section of Amnesty International
(Διεθνής Αμνηστία - Ελληνικό Τμήμα).

The Board meets about eleven times a year. Each meeting has to be called with
statutory notice, minuted, its decisions numbered and entered in the Βιβλίο
Αποφάσεων, and every document filed in the πρωτόκολλο under a number that is
unique and never reused. That work used to be done by hand, by volunteers, in
the evenings. This platform does the mechanical part of it and leaves the
judgement to the Board.

It is one section's tool today. It is being reshaped into a platform other
sections can adopt, with their own rules and their own tools - see
[docs/ARCHITECTURE_REVIEW.md](docs/ARCHITECTURE_REVIEW.md).

## What it does

| Workflow | What it handles |
|---|---|
| Board meeting invitation | Availability poll, agenda from the Sheet, Zoom meeting with the Board pre-registered, invitation PDF, filing, board email, member newsletter |
| Board meeting minutes | Transcription, drafting in house style, decisions to the Βιβλίο Αποφάσεων, filing and circulation |
| Archive | A PDF mailed or dropped in Discord is filed in SharePoint under the next protocol number |
| Γενική Εγκύκλιος | The quarterly circular: drafted, approved by the General Secretary, filed and sent |
| Discord | The members' forum, the board channels, the email bridge and the in-meeting Zoom sidebar |

## Getting started

```bash
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env          # secrets: API keys and OAuth credentials
copy config.yaml.example config.yaml
```

Your section lives in `sections/<slug>/`: `profile.yaml` (identity, role
mailboxes, how the board is named), `rules.yaml` (quorum, notice periods, each
citing its article), and the section's own prompts, email templates and
governance corpus. `config.yaml` is what is local to this installation -
channel ids, sheet ids, retention, which section to load. `.env` holds only
secrets.

Local transcription is optional and heavy; install it when you need it:

```bash
pip install -e ".[transcription]"
```

## Everyday commands

```bash
python -m src.cli status                 # what is configured and reachable
python -m src.cli invite check           # Brevo: key, sender, template, audience
python -m src.cli invite --test          # full invitation run, nothing sent to the Board
python -m src.cli minutes run            # draft minutes from a recording
python -m src.cli archive submit <pdf>   # file a document under the next number
python -m src.cli register audit         # protocol numbers: gaps and stale claims
python -m src.cli retention report       # what the retention rules would delete
python -m src.cli backup now             # copy the database and rotate old copies
```

Every workflow has a `--test` mode that creates real artefacts and then rolls
them back, with emails redirected to `testing.test_email`. Use it before
anything reaches the Board or the members.

## Running the server

```bash
uvicorn src.main:app --reload
```

The server receives the Google Sheet and Microsoft Graph webhooks, runs the
Discord bot, serves the in-meeting Zoom sidebar, and runs the scheduled jobs
(inbox poll, subscription renewal, the quarterly circular, the nightly backup).

## Tests

```bash
python -m pytest -q
```

They never touch the live database; a temporary copy is used instead.

## Data and privacy

The database holds the record of what the Board did: workflow state, the audit
log, protocol reservations and captured decisions. It is backed up nightly and
rotated; moving copies off this machine is the section's decision.

Recordings and transcripts are the most sensitive data here and are deleted
once the minutes are finalised, under the rules in `config.yaml` and
`FOUNDATION.md` §3.1. `retention report` shows what is kept and why.

## Documentation

| File | What is in it |
|---|---|
| [FOUNDATION.md](FOUNDATION.md) | The statutory and GDPR ground the platform stands on |
| [docs/ARCHITECTURE_REVIEW.md](docs/ARCHITECTURE_REVIEW.md) | Where the code is going, and the migration |
| [CUSTOMIZATION.md](CUSTOMIZATION.md) | What can be changed without touching code |
| [TEMPLATES.md](TEMPLATES.md) | Documents, emails and where their wording lives |
| [docs/MINUTES_PIPELINE.md](docs/MINUTES_PIPELINE.md) | How a recording becomes minutes |
| [docs/DEBUG_CLI.md](docs/DEBUG_CLI.md) | Running a single workflow step in isolation |
| [CLAUDE.md](CLAUDE.md) | Conventions for anyone (or anything) writing code here |
