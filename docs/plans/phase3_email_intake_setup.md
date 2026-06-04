# Phase 3 — Email-route Archive Setup & Operations

End-to-end runbook for getting the Graph webhook + safety-poll archive
pipeline live, plus the day-to-day commands you'll actually use.

---

## 0. Prerequisites checklist

Tick each before running the steps below:

- [ ] Cloudflare Tunnel is up and pointing at `http://localhost:8000`
      (whichever port `uvicorn` runs on) — same tunnel that already
      serves `/webhooks/invite` from the Google Apps Script integration.
- [ ] You know the **public hostname** of that tunnel (e.g.
      `https://tunnel.amnesty.example.com`).  Phase 3 will register
      `https://<hostname>/webhooks/m365/inbox` as the Graph notification URL.
- [ ] You can `ai-assistant auth microsoft` successfully — the
      Mail.ReadWrite + Files.ReadWrite.All scopes are both consented on
      the same delegated token.
- [ ] LibreOffice is installed on the host that runs the server (only
      matters if you want Phase 5 auto-conversion from DOCX/images).
      Check: `soffice --version`.

---

## 1. Configure `config.yaml`

Set the public webhook URL once:

```yaml
m365_inbox:
  webhook_url: "https://tunnel.amnesty.example.com"
  # ... other defaults are fine ...
```

Restart the server after editing `config.yaml` (settings are loaded at
import time).

---

## 2. Start the server

```powershell
uvicorn src.main:app --host 0.0.0.0 --port 8000
```

Watch the startup log — you should see:

```
... | src.core.scheduler | INFO | Scheduler started: safety_poll @ 12:00 Europe/Athens, renew_subs hourly
```

The scheduler is running in-process.  Three recurring jobs:

| Job id                          | Cadence              | Purpose |
|---------------------------------|----------------------|---------|
| `email_intake.safety_poll`      | 12:00 Europe/Athens  | Catches anything the webhook missed |
| `m365_inbox.renew_subs`         | hourly               | Renews expiring Graph subscriptions |
| `archive.collision_timeout`     | hourly               | Auto-fails archive workflows stuck on Phase 4 collision > 48h |

---

## 3. Create the Graph webhook subscription

In a second terminal:

```powershell
ai-assistant m365 subscribe
```

You should see:

```
============================================================
  Create Graph Webhook Subscription
============================================================
  Subscription ID:    abc123...-...-...
  Resource:           /me/mailFolders('Inbox')/messages
  Expiration (UTC):   2026-05-29T03:00:00Z
  Notification URL:   https://tunnel.amnesty.example.com/webhooks/m365/inbox
```

What just happened:
1. The CLI POSTed to Graph `/subscriptions` with a 70.5h lifetime + a
   random `clientState` token.
2. Graph immediately POSTed to `/webhooks/m365/inbox?validationToken=...`
   — our FastAPI route echoed it back as plain text within ~50ms.
3. Subscription metadata (id, clientState, expiry) is now in the local
   SQLite under `graph_subscriptions`.

Sanity check from Graph's own perspective:

```powershell
ai-assistant m365 subscriptions
```

The local row and the remote row should match.

---

## 4. Test end-to-end

### 4a. With your test address (auto TEST MODE)

Send an email from `georgeathanasias@gmail.com` (i.e. whatever you set
as `testing.test_email`) to `members@amnesty.org.gr`:

- **Subject**:  must contain `αρχείο` (or any case/accent variant) or
  `archive`
- **Attachment**:  one PDF (or DOCX / ODT / RTF / JPG / PNG / HEIC —
  Phase 5 auto-converts)

Expected behaviour:
1. Graph posts a notification within seconds.
2. The workflow runs in **TEST MODE** automatically (because the sender
   matches `testing.test_email`):
   - LLM classification runs against the real πρωτόκολλο xlsx (read-only).
   - **No** SharePoint upload.
   - **No** xlsx write.
   - The protocol reservation is released on rollback.
3. You receive a threaded reply in Gmail with a **[TEST MODE]** banner
   and the would-be archive metadata.
4. The local πρωτόκολλο backup at `data/backups/protokollo_latest.xlsx`
   gets refreshed (verify with `ai-assistant onedrive backup-status`).

### 4b. From a real board member

Same flow, but **without** the TEST MODE banner, and the file IS
actually filed in SharePoint + πρωτόκολλο.

---

## 5. Day-to-day operations

### Pause the watcher

Either:
- Stop `uvicorn` — the scheduler stops with it (no renewals; subscription
  expires in ~70h; safety poll doesn't run).
- Or `ai-assistant m365 unsubscribe <subscription_id>` if you want to
  permanently turn off email intake while keeping the rest of the
  platform running.

### Resume

`ai-assistant m365 subscribe` recreates the subscription with a fresh
`clientState`.

### Subscription renewal manual sanity check

```powershell
ai-assistant m365 renew-now --threshold-hours 0
```

This forces renewal regardless of remaining lifetime — useful to
confirm renewal-side bugs without waiting for the natural threshold.

### Run the safety poll on demand

```powershell
ai-assistant m365 poll-now
```

Goes through every unread Inbox message and processes the archive-shaped
ones.  Marks emails read + dedupes via `email_intake_seen` so the
webhook + poll never double-archive.

### Backup status

```powershell
ai-assistant onedrive backup-status
```

Shows the local copy of `[Πρωτόκολλο] Αρχείο ΔΣ.xlsx`, its size, age,
and whether openpyxl can parse it.  Refreshed automatically on every
SharePoint download.

### Backup restore (offline recovery)

If the live file is deleted in SharePoint, **don't panic**:

```powershell
ai-assistant onedrive backup-restore "C:\path\to\recovered.xlsx"
```

Copies the latest local snapshot to your chosen destination.  Open it,
verify the contents look right, then drag-and-drop it back into
SharePoint to restore.

### Phase 4 — protocol collision

If the workflow detects that a sender's αρ.πρωτ. already maps to a
different document, it parks the workflow and replies to the sender
explaining the situation.  Resolve from CLI:

```powershell
ai-assistant archive list                          # find the parked workflow id
ai-assistant archive resolve <wf_id> approve       # let the new doc overwrite
ai-assistant archive resolve <wf_id> reject        # roll back; sender re-submits
```

Stuck-workflow auto-fail kicks in at 48h (see `COLLISION_STUCK_HOURS`
in `src/core/scheduler.py`).

---

## 6. Troubleshooting

### "Webhook validation handshake failed" on subscribe

- Tunnel down?  Hit `https://<hostname>/webhooks/health` from a browser —
  should return JSON `{status: "ok", ...}`.
- Wrong tunnel hostname in `config.yaml`?  Edit and re-run.
- Server not running?  `uvicorn` must be live during `m365 subscribe`.

### Webhook fires but no reply lands

- Check `data/amnesty.db` audit log:
  `ai-assistant audit -w email_intake -l 30`
- Look for `rejected_sender` / `rejected_subject` rows — likely the
  sender's address isn't in `workflows.board_meeting.board_members`
  (or the configured `m365_inbox.sender_allow_list`).
- For `rejected_subject`: confirm the subject contains one of the
  `m365_inbox.subject_patterns` (default: `αρχειο`, `archive`).

### Subscription disappeared on Graph's side

Graph may delete a subscription if it can't reach the notification URL
for several days running (tunnel went down for a long weekend, etc.).
Just re-run `ai-assistant m365 subscribe`.  The safety poll fills the
gap until then.

### Workflow stuck "in_progress" forever

Either uvicorn died mid-flight, or a step is stuck on a network call.

```powershell
ai-assistant archive list                          # find it
ai-assistant archive cancel <wf_id>                # forcibly clean up
```

### LibreOffice not found (Phase 5)

The intake step reports `Conversion to PDF failed: LibreOffice (soffice) not found`.
Install from https://www.libreoffice.org/download/download/, or set
`$SOFFICE_BIN` to the binary path explicitly.

---

## 7. What lives where (cheat-sheet)

| Concept                         | Code                                    | Storage |
|---------------------------------|----------------------------------------|---------|
| Webhook subscription registry   | `src/integrations/graph_subscriptions.py` | `graph_subscriptions` table |
| Email-intake dedup              | `src/workflows/email_intake.py`           | `email_intake_seen` table   |
| Protocol-number reservations    | `src/core/audit.py`                       | `protocol_reservations` table |
| Workflow context (per run)      | `src/core/workflow.py`                    | `workflow_state` table |
| Audit trail (every action)      | `src/core/audit.py`                       | `audit_log` table |
| Local πρωτόκολλο backup         | `src/integrations/onedrive.py`            | `data/backups/protokollo_latest.xlsx` |
| Email templates                 | —                                         | `data/email_templates/*.html` |
| Tags + canonical patterns       | live read on each archive run             | SharePoint `[Πρωτόκολλο] Αρχείο ΔΣ.xlsx` (tabs Ετικέτες, Κατηγορίες) |

---

## 8. The 90-second smoke test

After any redeploy:

```powershell
# 1. Health
curl https://tunnel.amnesty.example.com/webhooks/health

# 2. Subscription healthy
ai-assistant m365 subscriptions

# 3. Backup healthy
ai-assistant onedrive backup-status

# 4. Database healthy
ai-assistant status

# 5. Run an idempotent end-to-end via your test inbox
#    (send the test email; wait for the threaded reply)
```

If all five pass, you're good.
