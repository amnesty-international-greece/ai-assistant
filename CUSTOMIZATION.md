# Customisation Guide

This is the **how-to manual** for changing anything the platform shows to a
human outside the admin/ops circle: emails, Discord posts, PDFs, the
membership newsletter, and the small hardcoded phrases sprinkled through the
workflows.

It is written for two readers:

- **The user:** most copy, branding, recipients, and links
  are editable without touching code. Those are marked ⚙️ (config) or 🎨
  (asset file you can swap).
- **The developer:** anything needing a code edit is marked 💻, with the exact
  file and the editing contract.

> **Companion doc:** [`TEMPLATES.md`](TEMPLATES.md) is the exhaustive
> *string-by-string catalogue* (every sentence, where it lives). This guide is
> the *systems-level how-to* (the shells, the foundations, the editing
> workflow). Use this to understand **how** to change a surface; use
> `TEMPLATES.md` to locate a **specific** sentence.
>
> ⚠️ `TEMPLATES.md` currently points LLM prompts at `data/prompts/` - they have
> since moved to **`src/prompts/`**. Trust this guide for paths.

---

## 0. The output surfaces at a glance

The platform speaks on four channels. Each has its own design system:

| Channel | Audience | Design source | Edit difficulty |
|---------|----------|---------------|-----------------|
| **Email** (M365) | Board, Director | `assets/email_templates/` + `_shell.html` | 🎨 HTML, no code |
| **Newsletter** (Brevo) | All members | [Brevo](https://app.brevo.com/templates/listing) | 🎨 Brevo editor |
| **Discord posts** | Board + members | `src/integrations/discord/embeds/` | 💻 Python (pure builders) |
| **Documents** (PDF/Doc) | Board, members, archive | `assets/`, `brand/`, Google Doc, `src/documents/` | mixed |

Plus two cross-cutting layers:

- **Branding foundations** (§1) - palette, fonts, logo, candle, signatures.
  One change here propagates everywhere.
- **Generated prose** (§7) - the LLM prompts in `src/prompts/` that shape the
  *words* inside invitations, minutes, and circulars before they're poured
  into a template.

---

## 1. Branding foundations (change once, propagates everywhere)

### 1.1 The palette 🎨💻

The Amnesty 3-colour system (yellow `#FFFF00`, black `#0A0A0A`, white) is
defined in **two** places that must stay in sync:

| Surface | File | Constant |
|---------|------|----------|
| Discord | `src/integrations/discord/brand.py` | `AMNESTY_YELLOW`, `AMNESTY_BLACK`, `AMNESTY_WHITE`, `AMNESTY_FLAME` |
| Email | `assets/email_templates/_shell.html` | CSS hex values (`#FFFF00`, `#0a0a0a`, `#f5f3ee`) |
| PDF | `src/documents/pdf_generator.py` | `AMNESTY_YELLOW`, `AMNESTY_BLACK` |

`AMNESTY_FLAME` #E63B11 is reserved - it's only for triage cards and
chart gradients, never routine chrome.

### 1.2 Fonts 🎨

- **Email:** Roboto (Greek body) + Roboto Condensed (headlines), loaded via
  Google Fonts `@import` in `_shell.html`. Falls back to Arial in corporate
  gateways. Font files live in `brand/Fonts/`.
- **PDF:** Helvetica family (ReportLab built-in) in `src/documents/`.

### 1.3 Logo & candle 🎨

| Asset | Path | Used by |
|-------|------|---------|
| Yellow candle (avatar, emoji) | `brand/Logo/Amnesty Candle/Amnesty_candle_RGB_Yellow/…png` | Discord bot avatar + `:amnesty:` app emoji |
| Black logo | `brand/Logo/Amnesty Logo/ENG_Amnesty_logo_RGB/…_black.png` | Egkyklios PDF header/footer |
| Icons library | `brand/Icons/<theme>/` | available for future use |

**Email logo/candle are intentionally text-only by default.** The shell renders
a typographic "ΔΙΕΘΝΗΣ ΑΜΝΗΣΤΙΑ" fallback because the guessed amnesty.gr image
URLs 404'd. To show real images, upload the PNGs to a public HTTPS host (or
Brevo's image library) and pass `logo_url=` / `candle_url=` to `render_email()`
- see §3.

### 1.4 Signatures 🎨

`brand/Signatures/president.png`, `secgen.png`, `treasurer.png`. Overlaid on
the **minutes PDF** by `src/workflows/board_meeting_minutes.py:450` (president +
secgen). Swap the PNGs to change signatures; edit the workflow to change *who*
signs.

---

## 2. Email templates

**Location:** `assets/email_templates/`
**Renderer:** `src/core/email_templates.py` → `render_email()`

### 2.1 The shell 🎨

`_shell.html` is the master frame every shelled email is poured into: black
header (logo + ref), yellow titlebar (kicker + headline), body slot, candle
footer. All the reusable components live here as CSS classes:

- `.cta` / `.cta.alt` - buttons (black-on-yellow / yellow-on-black)
- `.info-row` - the labelled `<dl>` grid (Zoom credentials, archive details)
- `.stamp` - the protocol-number stamp
- `.timer` - the yellow-on-black 72h archive countdown
- `.deadline-strip`, `ol.task-list` - scheduling-email components
- `.test-banner`, `.reason-card` - test-mode + failure cards

Edit `_shell.html` to restyle **all** emails at once. `{...}` are Python
`str.format` slots - **double every literal brace** `{{ }}` in the CSS (already
done) or the renderer throws.

### 2.2 The body templates 🎨

| File | Email |
|------|-------|
| `scheduling_with_poll.html` / `scheduling_no_poll.html` | Board scheduling call |
| `invitation_board.html` | Final board invitation |
| `minutes_share.html` | Draft minutes for comment |
| `archive_confirmation.html` | Archive receipt (protocol stamp) |
| `archive_failure.html` | Archive failed (flame reason card) |
| `egkyklios_cover.html` | Circular cover email |

Each is just the inner `.e-body` content. Placeholders like `{meeting_ref}`,
`{poll_url}`, `{deadline}` are filled by the workflow. **The set of available
placeholders is fixed by the calling workflow** - adding a new `{foo}` to a
template requires the workflow to pass `foo=` (a 💻 change).

### 2.3 Default copy (sender, footer) ⚙️💻

- Sender name/email: `config.yaml → brevo.sender_*` (newsletter) and M365
  identity for board mail.
- Footer legal line + header ref: defaults in `render_email()` signature
  (`src/core/email_templates.py:110`). Override per-call or change the default 💻.

---

## 3. The membership newsletter (Brevo) 🎨

The public board-meeting invitation that goes to **all members** is **not** in
this repo - it's **Brevo template #234** (`config.yaml → brevo.newsletter_template_id`),
edited in the Brevo web editor. The workflow injects values via Brevo template
params. To restyle it, go to [Brevo](https://app.brevo.com/templates/listing).

A prior redesign prototype (`data/template_234_redesigned.html`) was removed; if
you want a repo-tracked source of truth for the Brevo design, that's a
future-ideas item (§9).

---

## 4. Discord posts (embeds)

**Location:** `src/integrations/discord/embeds/` - see its
[`README.md`](src/integrations/discord/embeds/README.md).

This is the single home for every **governance** Discord post. Each function is
a **pure builder**: data in, `discord.Embed` (+ optional button `View`) out. No
network, no logic - just look & copy. 💻 but trivially safe to edit.

| File | Posts |
|------|-------|
| `embeds/board_meeting.py` | Full ΔΣ lifecycle: thread-open, scheduling, public invitation, milestone, invitation mirror, reminder, minutes, cancellation |
| `embeds/egkyklios.py` | Member-facing circular announcement |

Conventions: builders that may carry buttons return `(embed, view|None)`; plain
notices return just `embed`. Dates use `<t:UNIX:STYLE>` tokens (via
`brand.fmt_ts`) so every viewer sees their own timezone + live countdown.
`test_mode=True` prepends `[TEST] `.

**Hand this folder to a designer** - editing it restyles the live posts without
touching any workflow.

### 4.1 Plain-text mirror template 🎨

`assets/discord_templates/board_email_mirror.md` - used only for
*conversational* mirrors (member replies, Director announcements), not the
structured posts above. Placeholders documented in the file's own header;
keep edits below the marker line.

### 4.2 Not yet consolidated 💻

Operational slash-command replies (`/admin`, `/forum`, `/stats`, `/board`,
`/team`) still build embeds inline in their cogs under
`src/integrations/discord/cogs/`. They're ops UI, not member-facing posts, so
they were left out of `embeds/`. The reusable registers `status_embed`,
`event_live_embed`, `stats_embed` live in `brand.py`. Folding these in is a
future-ideas item (§9).

### 4.3 The welcome DM 💻⚙️

`src/integrations/discord/cogs/welcome.py` builds the new-member DM inline.
Copy is hardcoded; the three useful links only appear if set in
`config.yaml → urls` (`katastatiko`, `esoterikoi_kanonismoi`, `website`).

---

## 5. PDF documents

**Location:** `src/documents/`

| File | Produces |
|------|----------|
| `egkyklios_pdf.py` | The Γενική Εγκύκλιος (Markdown → ReportLab PDF: title block, TOC, running header/footer with logo) |
| `pdf_generator.py` | Invitation/minutes PDFs + `embed_signatures()` overlay |
| `docx_generator.py` | Word output |
| `templates.py` | Shared document scaffolding |

Brand constants (`_LOGO_PATH`, `_ORG_HEADER`, `_ORG_FOOTER`, colours, styles)
are at the top of `egkyklios_pdf.py` and `pdf_generator.py`. The egkyklios's two
mandatory intro paragraphs are copied **verbatim** by the LLM from the prompt
(see §7) - they are not in the PDF code.

### 5.1 The Google Doc invitation template 📄

The board invitation body is a **Google Doc** (ID in
`config.yaml → google.invitation_template_id`) with `[PLACEHOLDER]` tokens the
workflow substitutes. Edit it directly in Google Docs - no code, no deploy.

---

## 6. Config-driven copy & recipients ⚙️

`config.yaml` is the no-code control panel. Highlights for output customisation:

- `brevo.sender_name` / `sender_email` - newsletter "From"
- `brevo.newsletter_list_ids` / `master_list_id` - **who receives** the newsletter
- `workflows.board_meeting.board_members[]` - names + emails on every invitation
- `urls.*` - links in the welcome DM and embeds
- `discord.platform_bridge.board_meeting.*` - which channels posts land in
  (incl. `*_test` sandbox channels)
- `discord.platform_bridge.board_meeting.agenda_forum_tag_name` - the forum tag

---

## 7. Generated prose - the LLM prompts 📝

**Location:** `src/prompts/` (config: `storage.prompts_dir`)

These shape the **words** the AI writes before they're placed into a template.
Editing a prompt changes tone, structure, and content rules - high-leverage.

| File | Controls |
|------|----------|
| `board_invitation.md` | Invitation draft (outputs structured JSON) |
| `board_minutes.md` | Minutes drafting |
| `egkyklios_general.md` | Γενική Εγκύκλιος - incl. the verbatim intro paragraphs, Α/Β structure, strict sourcing rules |
| `circular.md` | Circular drafting |
| `general_support.md` | General assistant replies |

These are plain Markdown with `{placeholder}` slots - edit freely, but keep the
placeholders intact and respect any "output JSON in this schema" instructions
(the workflow parses the result).

---

## 8. Hardcoded micro-outputs 💻

Small phrases living in `.py` files. To find them: search the workflow for the
Greek string, or consult `TEMPLATES.md`'s 💻 entries. Known ones worth knowing:

| Output | File:line | Note |
|--------|-----------|------|
| Office address `Σίνα 30, 2ος όροφος` | `board_meeting_invitation.py:790` (`_OFFICE_ADDRESS`) | 🟡 candidate for `config.yaml` if it ever changes |
| Location phrases (δια ζώσης / υβριδικά / διαδικτυακά) | `board_meeting_invitation.py:~794` | |
| Zoom meeting topic `Συνεδρίαση {meeting_ref}` | `board_meeting_invitation.py` | |
| Email kind labels (Discord mirror) | `platform_bridge.py` `_EMAIL_KIND_LABEL` | |
| Taxonomy categories | `assets/protokollo_taxonomy_template.xlsx` (regen via `scripts/build_protokollo_taxonomy_template.py`) | 27 categories |

---

## 9. Safe editing workflow

1. **Emails/PDF/embeds:** run the relevant workflow with `--test` first. Emails
   redirect to `testing.test_email`; Discord posts go to the `*_test` sandbox
   channels (see test-mode notes). Nothing reaches the board/members.
2. **Templates with `{slots}`:** never remove a placeholder the workflow passes,
   and never add one the workflow doesn't - `str.format` raises `KeyError`.
   In `_shell.html` CSS, literal braces must be doubled `{{ }}`.
3. **After any edit:** `python -m pytest -q` (546 tests). Template-render and
   embed-builder tests will catch a broken placeholder or import.
4. **Brevo / Google Doc:** edit in their web editors; no deploy needed, but do a
   `--test` send to preview.

---

---

# 🚧 TEMPORARY - Future ideas, proposals, alternatives & TODO

> Living scratchpad. Move items into `ROADMAP.md` once committed, or delete when
> done. Nothing here is a promise.

### Branding cohesion (end-of-project goal)
- Unify **all** templates (email, Discord, PDF, Brevo #234) under one consistent
  Amnesty visual language. This guide + `embeds/` + `_shell.html` are the
  groundwork; the finish line is a single design pass across every surface.
- **Real logo/candle images in email:** host the PNGs publicly and wire
  `logo_url`/`candle_url` so emails stop using the typographic fallback.

### Consolidation still pending
- **Fold ops-embeds into `embeds/`:** `/admin`, `/forum`, `/stats`, `/board`,
  `/team` still build embeds inline in their cogs. Extract to
  `embeds/ops.py` (or per-cog files) so *every* Discord post is in one place.
- **Repo-track the Brevo template designs:** keep an HTML source-of-truth in
  `assets/` and a sync script, instead of editing only in the Brevo web UI.
  (a prior `data/template_234_redesigned.html` prototype was removed).
- **Extract `_OFFICE_ADDRESS` to `config.yaml → app.office_address`** so a venue
  change is a no-code edit.

### Workflows imagined / discussed
- **Ειδική Εγκύκλιος** workflow (sibling to the Γενική) - special-purpose
  circular.
- **Γενική Συνέλευση (GA)** workflow - `_on_ga_called` / `_on_ga_proxy_window_opening`
  handlers exist as stubs in `platform_bridge.py`.
- **Brevo monthly newsletter template** (proposal #06).
- **Discord welcome *card*** (proposal #10) - richer than the current DM; was
  blocked on `config.yaml → urls` being populated.
- **Instagram → Discord** via RSSHub (RSS pipeline already exists).
- **`member.approved`** handler - stub in `platform_bridge.py`.

### Test-mode design (decided)
- Chosen approach for previewing the full public choreography before going live:
  **(1)** confirm-gate fires `board.meeting.scheduled` with `test_mode=True`, and
  **(2)** sandbox channels (`agenda_channel_id_test` / `board_channel_id_test`).
  Both implemented. A discussed-but-not-built alternative was a standalone
  `cli debug publish-scheduled` command that synthesises the payload directly.

### Docs hygiene
- **Refresh `TEMPLATES.md`:** its LLM-prompt paths still say `data/prompts/`
  (now `src/prompts/`). Reconcile it with this guide, or merge the two.
