# Design Brief - Amnesty International Greece - AI Assistant

You are helping redesign the **visual surfaces** of an internal governance
automation platform for the Board of Directors (Διοικητικό Συμβούλιο) of
**Amnesty International - Greek Section**. Everything is in **Greek**.

There are two kinds of surface in this bundle:

1. **HTML emails** - sent to the board and to members (invitations, scheduling,
   minutes, archive receipts).
2. **Discord embeds** - posted by a bot into the board's private Discord server
   (meeting mirrors, status cards, the new date-picker).

Your job: help make these **beautiful, elegant, consistent, and on-brand**,
while respecting the hard technical constraints of each medium (below).

---

## Brand

Amnesty International's identity is **stark and iconic**: three colours, heavy
weight, lots of contrast. Do not introduce gradients, pastels, or decorative
flourishes - the power is in the restraint.

| Token | Hex | Use |
|---|---|---|
| Amnesty Yellow | `#FFFF00` | Primary accent - titlebars, CTAs, the candle |
| Black | `#0a0a0a` (near-black) / `#000000` | Text, headers, CTA backgrounds |
| Paper | `#f5f3ee` | Email background (warm off-white) |
| White | `#ffffff` | Card backgrounds |
| Flame | `#E63B11` | RARE accent - only "needs attention" / errors. Never routine chrome. |

**Typography (emails):** Roboto for Greek body; Roboto Condensed (900 weight)
for big headlines; Roboto Mono for protocol numbers / IDs. The English "chrome"
word-mark uses a condensed grotesque.

**ALL-CAPS Greek titles must strip τόνους** (accent marks) - e.g.
"ΣΥΝΕΔΡΙΑΣΗ" not "ΣΥΝΕΔΡΊΑΣΗ". This is a Greek typographic convention.

The logo is the **Amnesty candle** (yellow candle wrapped in barbed wire).

---

## Surface 1 - HTML emails

**Architecture:** every email = an **inner template** (the body content) injected
into a **shared shell** (`_shell.html`), via `render_email()` in
`src/core/email_templates.py`.

- `_shell.html` owns the chrome: black header w/ logo, yellow titlebar
  (`kicker` + big `title`), white body, paper footer. ALL styling lives here in
  a `<style>` block. **This is the main file to redesign.**
- Inner templates (`invitation_board.html`, `scheduling_with_poll.html`, etc.)
  are tiny - just the body, using `{placeholder}` slots and CSS classes defined
  in the shell (`.cta`, `.cta.alt`, `.info-row`, `.deadline-strip`, etc.).
- `render_email(name, *, kicker, title, header_ref, footer_note, stamp, **kwargs)`
  wraps an inner template in the shell. `kwargs` fill the `{placeholders}`.

**Hard constraints (email HTML is NOT web HTML):**
- Must render in **Gmail (web + mobile), Apple Mail, Outlook 2019+, Outlook.com**.
- No external CSS files, no JS. A single `<style>` block + inline styles only.
- Flexbox/grid work in modern clients but **degrade in Outlook** - keep a sane
  block fallback. Tables are the bulletproof option for critical layout.
- Web fonts load via `@import` but **fall back to Arial** in locked-down clients
  - never rely on the web font for legibility.
- Keep it **single-column, ≤640px**.

**To preview locally:** `python scripts/preview_email.py` renders every template
with sample data into `data/preview/*.html`, or
`python scripts/preview_email.py invitation_board` opens one in the browser.
Iterate: edit `_shell.html` → re-run → refresh.

---

## Surface 2 - Discord embeds

**Architecture:** Python (`discord.py`). `brand.py` is the single source of
truth for colour + the `brand_embed()` helper. Embed builders live in
`src/integrations/discord/embeds/`. Interactive components (buttons) live in
`src/integrations/discord/views/` (see `calendar_picker.py`).

**Hard constraints (Discord embeds are NOT HTML at all):**
- You get a **fixed embed schema**: title, description (markdown), up to 25
  `fields` (name + value, inline or not), a single accent `color` (left bar),
  `author`, `footer`, `thumbnail`, `image`, `timestamp`. That's it.
- **No custom fonts, no CSS, no HTML.** Styling = emoji, markdown (`**bold**`,
  `` `code` ``), field layout (inline columns), and the colour bar.
- Buttons/selects come from **Views** - max 5 rows × 5 buttons. Button styles
  are fixed: primary (blurple), secondary (grey), success (green), danger (red),
  link (grey w/ URL). You cannot recolour them to yellow.
- `<t:UNIX:F>` timestamps render in each viewer's local timezone.
- Elegance here = **information hierarchy, whitespace via fields, tasteful emoji,
  and consistency** - not visual styling. Think: what's the cleanest field
  layout, what's the one emoji that communicates state, what belongs in the
  description vs a field.

---

## Files in this bundle

```
DESIGN_BRIEF.md                         ← this file
brand_palette.py                        ← the colour constants (excerpt of brand.py)
core/email_templates.py                 ← render_email() - how templates wrap
scripts/preview_email.py                ← local preview tool
email_templates/
  _shell.html                           ← THE email shell (main redesign target)
  invitation_board.html                 ← final board invitation (Zoom + agenda)
  scheduling_with_poll.html             ← "fill availability + propose agenda"
  scheduling_no_poll.html               ← agenda-only variant
  minutes_share.html                    ← draft minutes for comment
  archive_confirmation.html             ← archive receipt (72h revision window)
  archive_failure.html                  ← archive failed, how to fix
  egkyklios_cover.html                  ← quarterly general circular cover
discord/
  brand.py                              ← palette + brand_embed() + fmt_ts()
  embeds/board_meeting.py               ← meeting lifecycle embeds (the main set)
  embeds/egkyklios.py                   ← circular embeds
  embeds/__init__.py
  views/calendar_picker.py              ← interactive date-picker (buttons)
```

---

## What I want from you

1. **Audit** the current emails + embeds for consistency and elegance - call out
   what's clunky, inconsistent, or off-brand.
2. **Redesign `_shell.html`** to be as elegant as the Amnesty identity deserves,
   within the email-client constraints above. Keep all existing CSS class names
   the inner templates rely on (`.cta`, `.cta.alt`, `.info-row`, `.deadline-strip`,
   `.task-list`, `.timer`, `.reason-card`, `.stamp`, `.test-banner`) OR tell me
   exactly which to rename so I can update the inner templates.
3. **Propose Discord embed improvements** as concrete field-layout / emoji /
   copy changes (give me the `brand_embed(...)` + `add_field(...)` calls).
4. Always **show me a preview** (rendered HTML for emails; a mock or screenshot
   description for embeds) before I commit.

Keep everything **Greek**, keep it **restrained**, keep it **Amnesty**.
