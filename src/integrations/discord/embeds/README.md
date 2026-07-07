# Discord post designs

This folder is the **single source of truth for every Discord message the
platform posts** in the governance workflows. Each function is a pure builder:
it takes data and returns a `discord.Embed` (plus an optional `discord.ui.View`
of link buttons). No network calls, no business logic - just the look and copy.

To restyle the live posts, edit the files here. Nothing else needs to change.

## Files

| File | Posts |
|------|-------|
| `board_meeting.py` | The full ΔΣ meeting lifecycle (see table below) |
| `egkyklios.py` | Member-facing circular (εγκύκλιος) announcements |

## Board-meeting posts (in lifecycle order)

| Builder | When it fires | Where it lands | Buttons |
|---------|---------------|----------------|---------|
| `board_thread_opened_embed` | Scheduling email goes out | Private board thread (opening post) | - |
| `scheduling_mirror_embed` | Scheduling email sent | Private board thread | 📆 Διαθεσιμότητα - 📋 Ημ. Διάταξη |
| `public_invitation_embed` | Meeting scheduled (live newsletter sent) | Public members forum thread | 🎥 Zoom |
| `milestone_published_embed` | Public invitation published | Private board thread | - |
| `invitation_mirror_embed` | Final invitation email sent | Private board thread | 🎥 Zoom - 📋 Ημ. Διάταξη |
| `reminder_embed` | N hours before the meeting | Both threads | - |
| `minutes_mirror_embed` | Minutes email sent (draft/final) | Private board thread | 📝 / 📄 Έγγραφο |
| `minutes_shared_embed` | `board.minutes.shared` event | Private board thread | 📄 Άνοιγμα |
| `cancellation_embed` | Meeting cancelled / rolled back | Both threads | - |

## Εγκύκλιος posts

| Builder | When it fires | Where it lands | Buttons |
|---------|---------------|----------------|---------|
| `egkyklios_published_embed` | Circular published to members | #ενημερώσεις forum | 📄 Κατέβασμα |

## Conventions

- Builders that may carry buttons return `(embed, view | None)`; plain notices
  return just `embed`.
- All colours come from `brand.AMNESTY_YELLOW` - change the palette once in
  `src/integrations/discord/brand.py`.
- Dates use Discord's `<t:UNIX:STYLE>` tokens (via `brand.fmt_ts`) so every
  viewer sees their own timezone and a live-updating relative countdown.
- `test_mode=True` prepends a `[TEST] ` marker to titles.

## Not (yet) in this folder

Operational slash-command responses - `/admin`, `/forum`, `/stats`, `/board`,
`/team` - still build their embeds inline in their cogs. They're ephemeral
ops UI rather than member-facing posts, so they were left out of this design
surface. The reusable register helpers (`status_embed`, `event_live_embed`,
`stats_embed`) live in `brand.py`.
