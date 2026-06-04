# Discord mirror template — board email thread
#
# This file is rendered for every outbound email on the board@amnesty.org.gr
# thread (scheduling email, final invitation, minutes-related emails).
# The handler in `platform_bridge._on_board_email_sent` reads this file,
# substitutes the placeholders, and posts it as a thread message.
#
# Available placeholders (single curly braces):
#   {kind_label}   — Greek label per email kind (Πρόσκληση / Τελική Πρόσκληση / etc.)
#   {meeting_ref}  — ΔΣXX-YYYY
#   {subject}      — the email's Subject line
#   {body_plain}   — the email body, with HTML stripped to plain Discord-flavoured text
#
# Editing: keep below the marker line.  Discord allows up to 2000 chars per
# message; if you go over, the bot truncates with a clear "…" suffix.
# ─── template starts below ──────────────────────────────────────────────────
📧 **{kind_label}** · `{meeting_ref}`
**Θέμα:** {subject}

{body_plain}
