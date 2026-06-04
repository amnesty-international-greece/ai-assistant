"""Glue between Phase 4b's email-anchor match and the Director-briefing flow.

Kept separate from :mod:`src.workflows.director_briefing` (pure functions
+ constants) so the intake side can be tested without dragging in
Graph / ArchiveWorkflow plumbing.

Wired by ``email_intake.process_inbox_message`` AFTER the anchor match
and BEFORE the Discord mirror — the briefing archive runs synchronously
so its result lands in the same Discord post the board sees.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from src.core.audit import (
    log_action,
    record_director_briefing,
    update_director_briefing_archive_result,
)
from src.core.event_bus import bus
from src.core.events import (
    EVENT_BOARD_EMAIL_SENT,
    BoardEmailSentPayload,
)
from src.workflows.director_briefing import (
    LOCAL_BRIEFING_DIR,
    briefing_title,
    find_briefing_attachment,
    is_director,
    local_copy_path,
    prefill_archive_context,
)

_BOARD_EMAIL = "board@amnesty.org.gr"
_KIND_DISPLAY = {
    "ΕΙΣΗΓΗΤΙΚΟ": "Εισηγητικό",
    "ΕΝΗΜΕΡΩΤΙΚΟ": "Ενημερωτικό",
}


def _build_announcement_body(
    *,
    kind: str,
    meeting_ref: str,
    protocol_number: str,
    sharepoint_url: str,
    extras_archived: list[str],
) -> str:
    """Compose the plain-text announcement the board sees in email AND Discord.

    Deliberately terse — it's a milestone note, not a real director update.
    The Director's own words stay private in the members@ inbox; the board
    only sees that the document has landed and where to find it.
    """
    display = _KIND_DISPLAY.get(kind, kind.title())
    lines = [
        f"Ο Διευθυντής έστειλε το {display} για την επόμενη συνεδρίαση "
        f"({meeting_ref}).",
        "",
    ]
    if protocol_number:
        lines.append(f"• Αρ. Πρωτ.: {protocol_number}")
    if sharepoint_url:
        lines.append(f"• SharePoint: {sharepoint_url}")
    if extras_archived:
        lines.append("")
        lines.append("Επιπλέον αρχεία που στάλθηκαν στην ίδια απάντηση "
                     "αρχειοθετήθηκαν χωριστά:")
        for x in extras_archived[:10]:
            lines.append(f"  • {x}")
    return "\n".join(lines).rstrip()


async def _send_announcement(
    *,
    meeting_id: str,
    meeting_ref: str,
    kind: str,
    body: str,
    email_anchor: str,
) -> None:
    """Send the announcement as a threaded reply to board@ AND publish on the
    bus so the Discord board thread mirrors the same content.

    Both surfaces show identical wording — there's a single source of truth
    for what the board sees.
    """
    display = _KIND_DISPLAY.get(kind, kind.title())
    subject = f"{display} Διευθυντή — Συνεδρίαση {meeting_ref}"

    # Send the email reply.  Non-fatal: if the email send fails, the Discord
    # mirror still happens via the bus event below.
    if email_anchor:
        try:
            from src.integrations.m365.mail import M365MailClient
            client = M365MailClient()
            await client.send_reply(
                parent_internet_message_id=email_anchor,
                body=body,
                html=False,
                to=_BOARD_EMAIL,
                workflow="director_briefing_announcement",
            )
        except Exception as exc:
            logger.warning(
                "Director-briefing announcement email send failed (non-fatal "
                "— Discord mirror still runs): %s", exc,
            )

    # Publish the bus event regardless — the existing _on_board_email_sent
    # handler renders this in the private Discord board thread.
    try:
        await bus.publish(
            EVENT_BOARD_EMAIL_SENT,
            BoardEmailSentPayload(
                meeting_id=meeting_id,
                meeting_ref=meeting_ref,
                kind="director_briefing_announcement",
                subject=subject,
                body_html=body,   # plain text; _html_to_plain is a no-op on it
                test_mode=False,
            ),
        )
    except Exception as exc:
        logger.warning(
            "Could not publish director-briefing announcement event: %s", exc,
        )

logger = logging.getLogger(__name__)


def _meeting_ref_from_meeting_id(meeting_id: str) -> str:
    """``board_meeting:ΔΣ05-2026`` → ``ΔΣ05-2026``; passthrough for raw refs."""
    return meeting_id.split(":", 1)[-1] if ":" in meeting_id else meeting_id


async def _resolve_email_anchor(meeting_id: str) -> str:
    """Look up the email thread anchor for a meeting from ``workflow_state``.

    The scheduling-email step persists ``context.email_thread_anchor`` (the
    Message-ID we thread on).  We use it to send the announcement as a real
    reply on the board email thread so it lands in-thread alongside the
    other meeting emails.

    Returns ``""`` if no workflow row matches — in which case the email
    side is silently skipped and only the Discord mirror runs.
    """
    import json as _json

    from src.core.audit import _get_connection

    raw_ref = _meeting_ref_from_meeting_id(meeting_id)
    conn = _get_connection()
    rows = conn.execute(
        """SELECT data FROM workflow_state
           WHERE workflow_name = 'board_meeting_invitation'
           ORDER BY updated_at DESC""",
    ).fetchall()
    for row in rows:
        try:
            data = _json.loads(row["data"] or "{}")
        except (ValueError, TypeError):
            continue
        ctx = data.get("context") or {}
        if ctx.get("raw_meeting_id") == raw_ref:
            anchor = (ctx.get("email_thread_anchor") or "").strip()
            if anchor:
                return anchor
    return ""


async def process_director_briefing_email(
    *,
    message: dict[str, Any],
    meeting_id: str,
    sender_email: str,
    subject: str,
    send_announcement: bool = True,
) -> dict[str, Any] | None:
    """Detect & archive the Director's briefing on a board-meeting reply.

    Returns ``None`` if the email isn't a Director briefing (caller continues
    normally).  Otherwise returns a small summary dict.

    Side effects on the happy path:
      - Local copy at :func:`local_copy_path`
      - Row in ``director_briefings`` table
      - ``ArchiveWorkflow`` run with pre-filled metadata
      - **Only when** ``send_announcement=True``: a bot-composed announcement
        is published as a threaded reply on the board email thread AND
        mirrored to the private board Discord thread (single message,
        single source of truth).

    The caller sets ``send_announcement=False`` when board@ is already a
    recipient of the Director's email — the regular ``_mirror_board_reply``
    flow handles Discord, and the email is already in-thread.
    """
    if not is_director(sender_email):
        return None
    if not message.get("hasAttachments"):
        return None

    meeting_ref = _meeting_ref_from_meeting_id(meeting_id)
    message_id = message.get("id") or ""
    imid = message.get("internetMessageId") or ""

    from src.integrations.m365.inbox import M365InboxClient
    inbox = M365InboxClient()
    attachments = await inbox.list_attachments(message_id)

    # The Director simply hits Reply — the subject stays whatever Outlook
    # prepends to "Συνεδρίαση ΔΣXX-YYYY".  Classification is therefore
    # **filename-driven**: the briefing is whichever attachment carries
    # Εισηγητικό / Ενημερωτικό in its filename.
    match = find_briefing_attachment(attachments)
    if match is None:
        logger.info(
            "Director email for %s has no attachment whose filename matches "
            "Εισηγητικό / Ενημερωτικό — no briefing classification.",
            meeting_ref,
        )
        return None
    main_briefing, kind = match

    # Slice the attachments into briefing vs. "rest" so the rest get sent
    # through the standard ArchiveWorkflow (which uses LLM extraction).
    other_attachments = [a for a in attachments if a is not main_briefing]

    logger.info(
        "Director briefing intake: meeting=%s kind=%s main=%r others=%d",
        meeting_ref, kind, main_briefing.get("name"), len(other_attachments),
    )

    # ── Archive the main briefing ─────────────────────────────────────────
    main_result = await _archive_main_briefing(
        inbox=inbox,
        message_id=message_id,
        attachment=main_briefing,
        meeting_ref=meeting_ref,
        kind=kind,
        source_message_id=imid,
    )

    # ── Archive the "rest" via the standard workflow (LLM extraction) ────
    extras_archived: list[str] = []
    for att in other_attachments:
        try:
            extra_proto = await _archive_extra_attachment(
                inbox=inbox,
                message_id=message_id,
                attachment=att,
                sender_email=sender_email,
                subject=f"{subject} — επιπλέον συνημμένο",
            )
            if extra_proto:
                extras_archived.append(f"{att.get('name')}: {extra_proto}")
        except Exception as exc:
            logger.warning(
                "Director extra attachment %r failed to archive (non-fatal): %s",
                att.get("name"), exc,
            )

    # ── Optional announcement (only when board@ wasn't on the Director's email) ──
    # When board@ is already a recipient, every board member sees the email
    # directly; the caller will run the normal Discord mirror and we skip the
    # announcement to avoid posting the same info twice.
    if send_announcement:
        announcement_body = _build_announcement_body(
            kind=kind,
            meeting_ref=meeting_ref,
            protocol_number=main_result.get("protocol_number", ""),
            sharepoint_url=main_result.get("sharepoint_url", ""),
            extras_archived=extras_archived,
        )
        email_anchor = await _resolve_email_anchor(meeting_id)
        await _send_announcement(
            meeting_id=meeting_id,
            meeting_ref=meeting_ref,
            kind=kind,
            body=announcement_body,
            email_anchor=email_anchor,
        )

    log_action(
        workflow="director_briefing",
        action="briefing_archived",
        actor="email",
        target=meeting_ref,
        details={
            "kind": kind,
            "protocol_number": main_result.get("protocol_number"),
            "extras": extras_archived,
            "source_message_id": imid,
        },
    )
    return {
        "outcome": "director_briefing_archived",
        "meeting_ref": meeting_ref,
        "kind": kind,
        **main_result,
        "extras_archived": extras_archived,
    }


async def _archive_main_briefing(
    *,
    inbox,
    message_id: str,
    attachment: dict[str, Any],
    meeting_ref: str,
    kind: str,
    source_message_id: str,
) -> dict[str, Any]:
    """Download the main briefing, save a persistent local copy, run
    ``ArchiveWorkflow`` with pre-filled metadata.

    Always saves the local copy at :func:`local_copy_path`.  The
    SharePoint archive happens via the standard workflow so the protocol
    number assignment + πρωτόκολλο append logic stays single-sourced.
    """
    from src.workflows.archive import ArchiveWorkflow

    filename = attachment.get("name") or "briefing.pdf"

    # Persistent local copy — used by Γενική Εγκύκλιος workflow later.
    local_dest = local_copy_path(meeting_ref, filename)
    local_dest.parent.mkdir(parents=True, exist_ok=True)
    await inbox.download_attachment(message_id, attachment["id"], local_dest)

    # DB row recorded before archive runs so we have traceability even if
    # the archive subsequently fails.
    workflow_actor = f"system:director_briefing:{meeting_ref}"
    briefing_id = record_director_briefing(
        meeting_ref=meeting_ref,
        kind=kind,
        local_path=str(local_dest),
        source_message_id=source_message_id,
        workflow_id="",
    )

    # ArchiveWorkflow expects a path it can read; we feed it the local
    # copy we just persisted.  test_mode is False — the Director writing
    # in production gets archived in production.
    wf = ArchiveWorkflow(actor=workflow_actor)
    initial_data = {
        "pdf_path": str(local_dest),
        "pdf_filename_orig": filename,
        **prefill_archive_context(meeting_ref=meeting_ref, kind=kind),
    }
    try:
        result = await wf.run(initial_data)
    except Exception as exc:
        logger.exception("Briefing ArchiveWorkflow failed: %s", exc)
        return {
            "title": briefing_title(meeting_ref, kind),
            "protocol_number": "",
            "sharepoint_url": "",
            "local_path": str(local_dest),
            "briefing_id": briefing_id,
            "error": str(exc),
        }

    ctx = wf.context or {}
    protocol_number = ctx.get("protocol_number") or ""
    sharepoint_url = ctx.get("archive_share_link") or ctx.get("archive_web_url") or ""

    update_director_briefing_archive_result(
        briefing_id,
        protocol_number=protocol_number or None,
        sharepoint_url=sharepoint_url or None,
    )

    return {
        "title": briefing_title(meeting_ref, kind),
        "protocol_number": protocol_number,
        "sharepoint_url": sharepoint_url,
        "local_path": str(local_dest),
        "briefing_id": briefing_id,
        "workflow_id": wf.workflow_id,
        "status": result.get("status", ""),
    }


async def _archive_extra_attachment(
    *,
    inbox,
    message_id: str,
    attachment: dict[str, Any],
    sender_email: str,
    subject: str,
) -> str | None:
    """Run the standard (LLM-driven) ArchiveWorkflow on a non-briefing
    attachment the Director happened to send along.

    Returns the assigned protocol number on success, ``None`` on failure.
    """
    from src.workflows.archive import ArchiveWorkflow

    filename = attachment.get("name") or "attachment.pdf"

    with tempfile.TemporaryDirectory(prefix="dirextra_") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        local_path = tmpdir / filename
        await inbox.download_attachment(message_id, attachment["id"], local_path)

        wf = ArchiveWorkflow(actor=f"system:director_extra:{filename}")
        try:
            await wf.run({
                "pdf_path": str(local_path),
                "pdf_filename_orig": filename,
                "sender_email": sender_email,
                "sender_name": "Διευθυντής",
                "email_subject": subject,
                "_source": "director_extra_attachment",
            })
        except Exception as exc:
            logger.warning("Extra attachment archive failed: %s", exc)
            return None
        return (wf.context or {}).get("protocol_number") or None
