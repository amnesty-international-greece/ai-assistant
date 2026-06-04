# Templates & Message Formats — Reference

Every user-facing string the platform produces, organized by workflow.
Source paths shown so you can edit in place. Where the text lives in
`config.yaml`, it's editable without code changes — flagged ⚙️.
Where it's hardcoded in `.py` files, editing requires a code change — flagged 💻.
LLM prompts in `data/prompts/*.md` are 📝.

---

## 1. Board meeting — invitation workflow

`src/workflows/board_meeting_invitation.py`

### 1.1 LLM draft prompt 📝
**File:** `data/prompts/board_invitation.md`
System prompt that drafts the invitation as structured JSON. Output schema:
```json
{
  "title": "Πρόσκληση σε Συνεδρίαση Διοικητικού Συμβουλίου",
  "subtitle": "Αρ. Συνεδρίασης: [number] — [date]",
  "sections": [{"heading": "...", "body": "..."}],
  "footer": "Ο Γενικός Γραμματέας\\n[name]"
}
```

### 1.2 Document title 💻
**Where:** workflow line ~580 / ~746
**Text:**
```
Πρόσκληση - Συνεδρίαση {meeting_ref}
```
`meeting_ref` = `ΔΣ{NN}-{YYYY}` (e.g. `ΔΣ04-2026`). Used as PDF filename and email subject.

### 1.3 Location phrases 💻
**Where:** workflow lines 493-506. Hardcoded office address.
```
δια ζώσης στο Γραφείο του Τμήματος, στη διεύθυνση Σίνα 30, 2ος όροφος
υβριδικά, στο Γραφείο του Τμήματος στη διεύθυνση Σίνα 30, 2ος όροφος, και διαδικτυακά μέσω της πλατφόρμας Zoom
διαδικτυακά μέσω της πλατφόρμας Zoom
```
**🟡 Worth extracting:** the office address (`Σίνα 30, 2ος όροφος`) is hardcoded; if it changes you'd edit the workflow file. Could move to `config.yaml → app.office_address`.

### 1.4 Google Doc invitation template 📄
**Where:** Google Drive (ID `1n_2cpUazlYSYKNSSgEqGSiOSUGdCRc9uAJ8suEpRDDU` in `config.yaml → google.invitation_template_id`).
The Doc has placeholders like `[ΑΡΙΘΜΟΣ ΠΡΩΤΟΚΟΛΛΟΥ]`, `[ZOOM_PLACEHOLDER]` that the workflow substitutes. Edit the template directly in Google Docs.

### 1.5 Zoom meeting topic 💻
```
Συνεδρίαση {meeting_ref}
```
**Where:** workflow line ~393. Sent to Zoom's `topic` field.

### 1.6 Brevo email subject + campaign name 💻
**Where:** workflow lines 746-747.
```
Subject:       Πρόσκληση - Συνεδρίαση {meeting_ref}
Campaign name: Πρόσκληση {meeting_ref}
```

### 1.7 Brevo HTML template 🎨
**Where:** Brevo template ID **234** (`config.yaml → brevo.newsletter_template_id`).
Edit the HTML directly in Brevo's editor. The bot supplies substitutions for the Zoom link, date, agenda, etc.

### 1.8 Brevo sender ⚙️
```yaml
brevo.sender_email: "members@amnesty.org.gr"
brevo.sender_name:  "Διεθνής Αμνηστία - Ελληνικό Τμήμα"
```

### 1.9 Brevo recipient list ⚙️
```yaml
brevo.newsletter_list_ids: [74]   # Τακτικά Μέλη
```

---

## 2. Board meeting — minutes workflow

`src/workflows/board_meeting_minutes.py`

### 2.1 LLM minutes prompt 📝
**File:** `data/prompts/board_minutes.md` (large — 6.3 KB). Strict structure:
`Title → Metadata → Παρόντες → Απόντες → Διαπίστωση Απαρτίας → Ημερήσια Διάταξη → Συζήτηση → Αποφάσεις → Λήξη Συνεδρίασης`.
Plus a structured `decisions[]` array with vote tallies.

### 2.2 Email to board for review 💻 + ⚙️
**Where:** workflow line ~393. Subject hardcoded; body text editable in config.
```
Subject: Πρόχειρα Πρακτικά - Συνεδρίαση {meeting_ref}
Body:    <p>{minutes_share_message}</p>
         <p><a href="{draft_doc_url}">Άνοιγμα εγγράφου</a></p>
```
Where ⚙️:
```yaml
workflows.board_meeting.minutes_share_message:
  "Σας κοινοποιούνται τα πρόχειρα πρακτικά προς σχολιασμό. Παρακαλώ αφήστε τα σχόλιά σας απευθείας στο έγγραφο."
```

### 2.3 Finalized minutes filename 💻
```
Πρακτικά - Συνεδρίαση {meeting_ref}
```
**Where:** workflow line ~531.

### 2.4 Decision protocol pattern 💻
```
ΔΣ{seq:02d}-{MM}-{YYYY}     e.g. ΔΣ03-05-2026
```
**Where:** workflow line ~623.

---

## 3. Discord — board meeting integration (Phase D)

`src/integrations/discord/cogs/platform_bridge.py`

### 3.1 Discord scheduled event 💻
```
Name:        Συνεδρίαση ΔΣ — DD/MM/YYYY HH:MM
Description: {agenda_summary[:1000]}\n\nZoom: {zoom_url}
Location:    {zoom_url}  (or "Online" if no URL)
Duration:    2 hours (hardcoded — workflow line ~128)
```

### 3.2 Agenda thread (public + board, identical content) 💻
```
Thread name: Συνεδρίαση ΔΣ — DD/MM/YYYY
Thread body:
  **Ημερήσια Διάταξη**

  {agenda_summary or "(κατόπιν ανακοίνωσης)"}

  Zoom: {zoom_url}
```

### 3.3 Reminder (public thread only) 💻
```
⏰ Η συνεδρίαση ξεκινά σε {hours_before} ώρες.
```

### 3.4 Minutes posted (both threads) 💻
```
📄 Τα πρακτικά μοιράστηκαν: {drive_url}
```

### 3.5 Cancellation (both threads) 💻
```
❌ Η συνεδρίαση ακυρώθηκε. Λόγος: {reason or "δεν δόθηκε"}
```

---

## 4. Discord — Google Group ↔ Discord email bridge

`src/integrations/discord/cogs/email_sync.py`

### 4.1 Inbound email → forum thread 💻
**Where:** `_format_body`, line ~342.
```
**{sender}** via email:
> {body_line_1}
> {body_line_2}
...
📎 {N} attachment(s): {filenames}
```
The whole body is blockquoted line-by-line.

### 4.2 Unrouted email → admin channel 💻
**Where:** `_post_to_admin`, line ~321.
```
**Unrouted email** from **{sender}**
Subject: `{subject[:100]}`
Reason: {classifier_reason}
||EMAIL_ID:{message_id}||

> {body_plain[:400]}
```

### 4.3 Discord reply → outbound email 💻
**Where:** `on_message`, line ~406.
```
Subject: Re: {original_subject}   (auto-prefixed if missing)
Body:    {discord_author_display_name} (via Discord):

         {message_content}
```

### 4.4 Outbound email From header 💻
**Where:** `email_gateway.py` line ~516.
```
From: "Forum Assistant" <{gmail_user}>
```
The display name `EMAIL_SENDER_DISPLAY_NAME = "Forum Assistant"` (constants.py line 24).
**🟡 Worth renaming** to match brand cohesion goal (e.g. `"Διεθνής Αμνηστία — Forum"`).

---

## 5. Discord — events cog (Discord scheduled events → forum)

`src/integrations/discord/cogs/events.py`

### 5.1 New scheduled-event announcement 💻
```
**{event.name}**
{event.description}
Ημερομηνία: {start_time:%A %d %B %Y, %H:%M UTC}
Λήξη: {end_time:%H:%M UTC}
Τόπος: {location}
{event.url}
```
🟡 The date format uses `%A %d %B %Y` which in C locale gives English month names. If your bot's locale is unset, you'll get `"Wednesday 21 May 2026"` not `"Τετάρτη 21 Μαΐου 2026"`. Worth setting a Greek locale or switching to a custom Greek-month dictionary.

### 5.2 Event went live 💻
```
**{event.name}** ξεκινά τώρα!
{event.url}
```

### 5.3 [LIVE] thread name (forum channel) 💻
```
[LIVE] {event.name}
```

---

## 6. Discord — team management (Phase C)

`src/integrations/discord/cogs/teams.py`

All ephemeral (visible only to the invoker) Greek-language confirmations/errors. Examples:

| Trigger | Message |
|---|---|
| `/team add` success | `✅ Προστέθηκε ο/η {user} στην ομάδα **{team_name}**.` |
| `/team remove` success | `✅ Αφαιρέθηκε ο/η {user} από την ομάδα **{team_name}**.` |
| `/team transfer` success | `✅ Ο/η {user} μετακινήθηκε από **{from}** σε **{to}**.` |
| Not a coordinator | `Δεν έχεις τα απαραίτητα δικαιώματα (απαιτείται ο ρόλος Συντονιστής + ένας ρόλος ομάδας).` |
| Team not in your scope | `Η ομάδα '{team}' δεν βρέθηκε στις ομάδες σου.` |
| Multiple teams, no `team:` | `Είσαι μέλος πολλών ομάδων ({names}) — προσδιόρισε με 'team:'.` |
| Role hierarchy fails | `Σφάλμα: ο ρόλος μου είναι κάτω από '{role}' στην ιεραρχία ρόλων — δεν μπορώ να τον αναθέσω. Μετακίνησε τον ρόλο του bot ψηλότερα.` |
| User already in team | `Ο/η {user} είναι ήδη στην ομάδα {team_name}.` |
| Empty team | `Η ομάδα **{team}** δεν έχει μέλη.` |

Slash-command parameter descriptions also in Greek (`Όνομα ή ID ομάδας`, `Μέλος προς προσθήκη`, etc.).

---

## 7. Discord — admin commands

`src/integrations/discord/cogs/admin.py`

Slash commands for `/discord-admin`:

| Command | Description |
|---|---|
| `status` | Show current bot state |
| `test-mode` | Toggle test mode on/off |
| `classify-toggle` | Toggle auto-classification on/off |
| `add-channel` | Add a channel to the routing table |
| `remove-channel` | Remove a channel from the routing table |
| `notify-me` | Register for stats digest DMs |
| `add-team` | Καταχώρηση ομάδας στο σύστημα |
| `remove-team` | Διαγραφή ομάδας από το σύστημα |
| `teams` | Λίστα καταχωρημένων ομάδων |

Confirmation messages all in Greek. The four older commands (`status`, `test-mode`, etc.) have **English** descriptions and Greek-mixed responses — inconsistent. **🟡 Worth normalising** to all-Greek for member-facing consistency.

---

## 8. Discord — `/pin-until` (moderation cog)

`src/integrations/discord/cogs/moderation.py`

| Trigger | Message |
|---|---|
| Success | `📌 Καρφιτσώθηκε. Θα ξεκαρφιτσωθεί στις {date}.` |
| Invalid link | `Μη έγκυρος σύνδεσμος μηνύματος.` |
| Invalid date | `Μη έγκυρη ημερομηνία. Χρησιμοποίησε YYYY-MM-DD ή YYYY-MM-DDTHH:MM.` |
| Past date | `Η ημερομηνία ξεκαρφιτσώματος πρέπει να είναι στο μέλλον.` |
| Message not found | `Το μήνυμα δεν βρέθηκε.` |
| Missing permission | `Δεν έχω δικαιώματα να καρφιτσώσω σε αυτό το κανάλι.` |

---

## 9. Stats digest (weekly DM + admin-channel post)

`src/integrations/discord/cogs/stats.py`

### 9.1 Embed structure 💻
```
Title:  "Weekly Stats Digest — Last 7 days"   ← English!
Fields: Total / Inbound emails / Outbound emails / Discord posts /
        Avg confidence / Top channels / By classification
```
**🟡 Whole embed is in English.** Should be Greek for member-facing alignment.

---

## 10. LLM system prompts (governance support)

📝 `data/prompts/general_support.md` — system prompt establishing the assistant's role (knows Καταστατικό, Εσωτερικοί Κανονισμοί, GDPR, Amnesty governance). Used for ad-hoc governance Q&A.

---

# What's MISSING (templates that don't exist yet but the platform will need)

These are the gaps. Some are blocked on the workflow not existing; some are pure copywriting.

### 🔴 Εγκύκλιοι workflow (blocked on workflow code)

Will need:
1. **Email subject + Brevo template** for the εγκύκλιος email blast
2. **Brevo template ID** in `config.yaml` (currently only the board-meeting template `234` is set)
3. **Discord forum thread opener** for `#εγκύκλιοι` — opening text + footer with link to PDF / Brevo campaign view
4. **OneDrive archive path pattern** under `/Amnesty/Archive/Εγκύκλιοι/{YYYY}/` (the archive_root config exists, the sub-path doesn't)
5. **Audience tagging** — should the Discord post `@everyone`, `@Μέλη {team}`, or be silent?

The LLM draft prompt for εγκύκλιοι **already exists** (`data/prompts/circular.md`), just no workflow to consume it.

### 🔴 Γενικές Συνελεύσεις workflow (blocked on workflow code)

Will need:
1. **Notice email subject + Brevo template** (formal, references Καταστατικό άρθρο for GA notice period)
2. **Reminder emails** at -30d, -15d, -1d (config: `general_assembly.min_notice_days`, etc.)
3. **Discord scheduled event name** (similar pattern: `Γενική Συνέλευση — DD/MM/YYYY`)
4. **Discord announcement thread** in `#γενικό` with full notice text — would be a great fit for `/pin-until: GA-date` so it stays visible
5. **Proxy nomination poll template** (Discord native poll question text)

### 🟡 Discord scheduled-event description for εκδηλώσεις

When a Discord event is created manually for an εκδήλωση (e.g. a webinar), the events cog announces it in `#εκδηλώσεις` using **5.1** above. But there's no LLM-drafted "describe this event nicely" step. If you wanted the bot to suggest a thread body when announcing a new εκδήλωση, that's a new prompt + workflow.

### 🟡 Member acknowledgement of κανονισμός

You said the κανονισμός lives in a dedicated channel and Discord's Onboarding flow handles member intake. No bot template needed unless you want a "please react ✅ once you've read it" reminder later.

### 🟡 Brand-consistent webhook identity (cross-cutting)

When the bot posts via webhook (the email-bridge), the webhook is named `"Event_Info"` (legacy from the old bot — `constants.py` line 73: `WEBHOOK_NAME = "Event_Info"`). For brand cohesion, should be renamed to something like `"Διεθνής Αμνηστία — Forum"` with the Amnesty logo as the webhook avatar. One constant change + uploading an avatar via Discord Developer Portal.

---

# Suggested next steps for templates

Two small, separable improvements worth considering:

1. **Centralize Discord bot strings** into `src/integrations/discord/messages.py` (one module of constants like `MSG_REMINDER = "⏰ Η συνεδρίαση ξεκινά σε {hours} ώρες."`). Makes copy editing a one-file change and makes localization trivial later. ~2 hours of refactor.

2. **Move the office address and bot display names into config.yaml** so non-code edits cover them. ~30 minutes.

3. **Stats digest → Greek** — small win, all in one file.

When εγκύκλιοι / GA workflows get built, the new templates should go through the same review you're doing now, so we can copy-edit them before they ship.
