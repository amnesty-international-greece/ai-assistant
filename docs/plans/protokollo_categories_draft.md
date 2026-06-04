# Πρωτόκολλο — Canonical Categories (Draft)

This draft updates the Κατηγορίες reference of `[Πρωτόκολλο] Αρχείο ΔΣ.xlsx`
with the patterns observed across 2022–2026 entries. **Edit freely.**
The `Comments` column is for your notes — anything you write there will
guide the next pass. The `Κύρια Σημεία` column is the suggested convention
for what to fill in that column when archiving a document of this kind.

## Taxonomy constraints

- The 16-tag taxonomy stays **unchanged**.
- `Παγκόσμια Συνέλευση` content uses the `Διεθνές` tag.
- `Επιχειρησιακά` is reserved for board-level operational scope; office
  operational documents go to the Office's own archive (not this one). So
  `Επιχειρησιακά` stays sparse here on purpose.
- `Πλάνα` is the general-purpose tag for plans (budget, strategic, organizational).
- `Εκπαίδευση` is kept for future use (training materials, HRE-related docs).
- Bilingual / received international documents keep their original-language title.

## Director's pre-meeting report — naming rule

The Director sends one report to the board before each meeting. Two title forms:

| Has proposals / recommendations? | Title prefix |
|---|---|
| Yes | `Εισηγητικό Διευθυντή - Συνεδρίαση {raw_meeting_ref}` |
| No (informational only) | `Ενημερωτικό Διευθυντή - {raw_meeting_ref}` |

The PDF filename already encodes which form it is — the archive workflow
takes the filename verbatim, no inference needed. (The differentiation
between the two forms is the Director's choice when authoring the report,
encoded into the filename before the archive request is sent.)

---

## Source of truth — the live πρωτόκολλο xlsx

The taxonomy and the canonical category patterns live in
`[Πρωτόκολλο] Αρχείο ΔΣ.xlsx` on SharePoint:

| Tab | What it holds |
|---|---|
| `Ετικέτες` | The 16 tags + a per-tag description (2 columns). The description is where the tag's specific usage rule lives — e.g. "Επιχειρησιακά: χρήση με φειδώ, τα operational του Γραφείου πάνε αλλού". |
| `Κατηγορίες` | The canonical title patterns + default tags + `Κύρια Σημεία` convention (3 columns). |

The Phase 1 archive workflow reads BOTH tabs at runtime — every archive
request triggers a fresh read, so any edit you make in the live xlsx flows
into the LLM prompt on the very next invocation. No code change required
when you add/remove patterns or rewrite a tag's description.

---

## LLM prompt (Phase 1)

The prompt template below has three sections that get populated from the
live πρωτόκολλο at runtime: `{tag_descriptions_block}`, `{categories_block}`,
and (only on the fallback pass) `{recent_entries_block}`.

```
You are the archival assistant for the Greek section of Amnesty International.
Decide how to file the attached document in the institutional archive of the
Board of Directors (Διοικητικό Συμβούλιο).

DOCUMENT
========
Filename:        {filename}
Sender:          {sender_name} <{sender_email}>
Email subject:   {subject}
Email body:      {body[:1000]}
PDF text (first 5000 chars):
{pdf_text[:5000]}

TAXONOMY (live from Ετικέτες tab of [Πρωτόκολλο] Αρχείο ΔΣ.xlsx)
================================================================
{tag_descriptions_block}

  // each row rendered at runtime as:
  //   - {tag}: {description}
  // Tag-specific usage rules live in the description — follow them.

CANONICAL PATTERNS (live from Κατηγορίες tab)
==============================================
{categories_block}

  // each row rendered at runtime as:
  //   - Pattern: {title_pattern}
  //     Tags:    {default_tags}
  //     Σημεία:  {kuria_simeia_convention}

GENERAL RULES
=============
- Pick 1 to 3 tags typically (4 only for truly cross-cutting documents).
- Aim for consistency with the rest of the archive — match the style of
  titles, tag combinations, and Κύρια Σημεία used in recent entries.
- For documents originally in English, keep their original-language title.
- Tag-specific usage rules are in each tag's description (TAXONOMY section
  above) — follow them.

PROTOCOL NUMBER DETECTION
=========================
Look for an αρ.πρωτ. in the filename (e.g. "[2026_017] ..."), the PDF text
("Αρ. Πρωτ.: 2026_017"), or the email body. Report it as `existing_protocol`.
If you only see a year or a fragment, leave it null.

OUTPUT
======
Strict JSON, no preamble, no markdown fences:

{
  "title": "...",                  # Greek (or original language if foreign)
  "labels": ["...", "..."],        # 1-3 from the live taxonomy
  "key_points": "...",             # following the Σημεία convention of the
                                   # matched category; "" if none warranted
  "existing_protocol": "YYYY_NNN" | null,
  "category_matched": "...",       # name of the canonical pattern matched,
                                   # or "ad-hoc" if none fit
  "confidence": 0.0..1.0,
  "reasoning_brief": "one sentence why these choices"
}
```

---

## Two-step fallback for unmatched documents

When the first LLM pass returns `category_matched == "ad-hoc"` OR
`confidence < 0.7`, the workflow runs a second LLM call that anchors the
choices to the archive's existing conventions:

1. Fetch the last N (default: 30) archived entries from the current year's
   tab + the previous year's tab of the πρωτόκολλο
2. Send a second LLM call with:
   - The same `DOCUMENT` block as the first pass
   - The first pass's output (title / tags / Σημεία)
   - `RECENT EXAMPLES` block — the fetched entries as exemplars showing the
     archive's typical title shape, tag combos, and Σημεία verbosity
3. Ask the LLM: "given these recent entries, refine title / tags / Σημεία to
   match the archive's existing conventions"
4. Use the second call's output as the final answer

This second pass exists to anchor the LLM's choices to the archive's
style — capitalization, abbreviation conventions, typical tag combos,
Σημεία verbosity. It runs rarely (~10-20% of submissions based on the
long-tail distribution observed in the 2022-2026 data).

`confidence < 0.7` ALSO triggers a `[ΥΠΟ ΕΞΕΤΑΣΗ]` prefix on the final
`Κύρια Σημεία` value, so SecGen can grep the πρωτόκολλο for entries that
need a human spot-check.

---

## Email-route delivery — Graph webhook + safety poll

The archive workflow's email route watches `members@amnesty.org.gr` via
**two parallel mechanisms**:

| Mechanism | Trigger latency | Reliability |
|---|---|---|
| Microsoft Graph webhook subscription on `/users/members@.../mailFolders/inbox/messages` | Near-real-time (sub-second) | Subscriptions expire every 3 days — need renewal |
| Safety poll, once daily at **12:00 Europe/Athens** | Up to 24h | Catches anything the webhook missed (e.g. during subscription renewal gaps, downtime) |

### Webhook subscription lifecycle

| Action | When | What it does |
|---|---|---|
| `subscription_create` | First boot, or whenever the active subscription is missing | `POST /subscriptions` with `changeType: created`, `resource: /users/{members-id}/mailFolders/inbox/messages`, `notificationUrl: https://{public}/webhooks/m365/inbox`, `expirationDateTime: now+72h` |
| `subscription_renew` | Daily check; renew when remaining lifetime < 24h | `PATCH /subscriptions/{id}` with `expirationDateTime: now+72h` |
| Webhook receiver `/webhooks/m365/inbox` | When Graph posts a notification | Validate Graph's `validationToken` on subscription creation; on real notifications, fetch the message via `GET /messages/{id}`, check subject + sender, kick off archive workflow |

### Safety poll

A daily background task at 12:00 Europe/Athens:
1. `GET /users/members@.../mailFolders/inbox/messages?$filter=isRead eq false`
2. Filter by subject (contains `αρχειο` via the normalized matcher)
3. Filter by sender (must be in the board members' email allow-list)
4. For each match: check workflow_state for an existing archive run keyed on
   the email's `internetMessageId` — if no match, kick off a new archive
   workflow (defends against webhook gaps)

The safety poll uses the existing Graph delegated token (no extra
permission needed). The webhook subscription needs the SAME token plus a
publicly-reachable URL — both of which are already in place via
Cloudflare Tunnel (Phase 1) → permanent named tunnel + domain (Phase 2).

---

## Implementation roadmap

Five-phase build, ordered so each phase is testable on its own:

| Phase | Scope | Dependencies |
|---|---|---|
| **1.** CLI archive (happy path) | `ai-assistant archive <file>` + LLM metadata extraction + SharePoint upload + xlsx append (using the `protocol_reservations` table) + LLM reads tags & categories live from the πρωτόκολλο xlsx | New SQLite table `protocol_reservations` |
| **2.** CLI revision + cancel | `ai-assistant archive review/cancel/list` + LLM intent parsing. Revision window enforced via DB timestamps. | Phase 1 |
| **3.** Email intake via Graph webhook + safety poll | `subscription_create/renew`, `/webhooks/m365/inbox` route, daily 12:00 poll, sender allow-list, threaded reply with the Greek confirmation template | Phase 1+2, public URL (Cloudflare Tunnel) |
| **4.** Protocol-collision gate | LLM title-match + secgen-only `resolve` flow + time-bounded stuck-workflow auto-fail | Phase 1 |
| **5.** Auto-convert non-PDF (DOCX, images) | LibreOffice headless conversion | Phase 1-3 |

Phase 1 is shippable standalone — even without email, `ai-assistant archive
<file>` saves real time on the SecGen's CLI archive workflow.

---

## Implementation notes

- **Live xlsx reads**: the workflow code pulls Ετικέτες and Κατηγορίες from
  the SharePoint xlsx on every archive run. No caching, no code change
  when you edit those tabs.
- **Director's-report naming rule** above is consumed by the **board
  meeting workflow** (which generates these filenames); the **archive
  workflow** just takes the filename verbatim.
- `category_matched` tracking in the LLM output lets us audit behaviour
  over time — which canonical patterns get used, which never do (candidates
  for removal/merging on a future taxonomy pass).
- `confidence < 0.7` triggers a `[ΥΠΟ ΕΞΕΤΑΣΗ]` prefix on Κύρια Σημεία so
  SecGen can grep for entries needing review.
- The Graph webhook subscription auto-renewal runs daily; if it ever fails
  to renew, the safety poll guarantees no archive email goes unprocessed
  for more than 24 hours.
