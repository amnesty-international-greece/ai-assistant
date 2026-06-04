"""Email-route intake — process inbox messages into archive workflows.

Single entry point :func:`process_inbox_message` is shared by both:

  * ``/webhooks/m365/inbox`` (near-real-time, one message at a time)
  * The daily 12:00 Europe/Athens safety poll (catch-up; many messages)

Decision flow per message::

    1. has_seen_email?              → no-op (idempotent)
    2. sender in allow-list?        → mark seen as 'rejected_sender'
    3. subject matches patterns?    → mark seen as 'rejected_subject'
    4. exactly one PDF attachment?  → mark seen as 'no_pdf'
    5. download PDF to data/inbox/  → kick off ArchiveWorkflow
    6. send threaded reply with the πρωτόκολλο result
    7. mark email read + mark seen as 'archived' (with workflow_id)

The Greek confirmation template lives at
``assets/email_templates/archive_confirmation.html`` — read on every call
via :func:`src.core.email_templates.render_email`.
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from typing import Any

from src.config import settings
from src.core.audit import (
    _get_connection,
    has_seen_email,
    log_action,
    mark_email_seen,
)
from src.core.email_templates import render_email
from src.integrations.m365_inbox import (
    M365InboxClient,
    sender_allowed,
    subject_matches,
)
from src.integrations.m365_mail import M365MailClient
from src.workflows.archive import ArchiveWorkflow

logger = logging.getLogger(__name__)

_PDF_CT = "application/pdf"
_PDF_EXT_RE = re.compile(r"\.pdf$", re.IGNORECASE)
_INBOX_DIR = Path("data") / "inbox"

# The bot's own outbound identity — emails FROM this address that carry the
# Discord bridge marker are our own echoes coming back; skip them.
_BOT_IDENTITY = "members@amnesty.org.gr"
# Marker prepended by the Discord→email agent so we can detect our own echoes.
_DISCORD_BRIDGE_MARKER = " via Discord]"

# Max body length for a mirrored Discord post (Discord cap is 2000; leave room
# for the header lines).
_DISCORD_BODY_CAP = 1800

# Phase 5: extensions we can auto-convert to PDF at intake.  Mirrors the
# allow-list in src.utils.pdf_convert — kept in sync because the email route
# needs to pick the right attachment BEFORE convert_to_pdf gets a chance.
_CONVERTIBLE_EXT_RE = re.compile(
    r"\.(pdf|docx?|odt|rtf|xlsx?|ods|csv|pptx?|odp|jpe?g|png|bmp|tiff?|gif|heif|heic)$",
    re.IGNORECASE,
)


def _extract_sender(message: dict[str, Any]) -> tuple[str, str]:
    """Return ``(email, display_name)`` from a Graph message envelope."""
    sender = (message.get("from") or {}).get("emailAddress") or {}
    return (sender.get("address") or "").strip(), (sender.get("name") or "").strip()


def _is_test_sender(sender_email: str) -> bool:
    """True if the sender is the configured testing.test_email address.

    Used to route real inbound emails through the workflow's TEST MODE so
    we (the developer) can exercise the email route end-to-end against
    production SharePoint + Gemini without ever appending to the live
    πρωτόκολλο or uploading a real file.
    """
    test_addr = (settings.testing.test_email or "").strip().lower()
    if not test_addr:
        return False
    return (sender_email or "").strip().lower() == test_addr


def _find_pdf_attachment(attachments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the single archivable attachment.

    Accepts PDFs directly, plus any non-PDF type the converter can handle
    (DOCX, ODT, JPG, PNG, HEIC, ...).  Returns None if 0 or 2+ candidates
    are present — the workflow refuses to guess when multiple files came in
    the same email, since we can only archive one per πρωτόκολλο entry.
    """
    candidates = [
        a for a in attachments
        if _CONVERTIBLE_EXT_RE.search(a.get("name", "") or "")
        or a.get("contentType", "").lower() == _PDF_CT
    ]
    return candidates[0] if len(candidates) == 1 else None


async def _send_failure_reply(
    inbox: M365InboxClient,
    message: dict[str, Any],
    *,
    reason: str,
    sender_email: str,
) -> None:
    """Best-effort threaded reply explaining why we couldn't archive."""
    try:
        mail = M365MailClient()
        _sender_name = (message.get("from") or {}).get("emailAddress", {}).get("name", "")
        body = render_email(
            "archive_failure",
            kicker=(f"Καλησπέρα {_sender_name}" if _sender_name else "Καλησπέρα"),
            title="Δεν αρχειοθετήθηκε<br/>το έγγραφό σας.",
            header_ref="ΣΦΑΛΜΑ ΑΡΧΕΙΟΘΕΤΗΣΗΣ",
            footer_note=(
                "Διεθνής Αμνηστία — Ελληνικό Τμήμα · "
                "Αυτή είναι αυτόματη απάντηση από το AI Assistant."
            ),
            subject=message.get("subject", "(no subject)"),
            reason=reason,
        )
        await mail.send_reply(
            parent_internet_message_id=message["internetMessageId"],
            body=body,
            html=True,
            to=sender_email,
            workflow="email_intake",
        )
    except Exception as e:  # pragma: no cover — best-effort
        logger.warning("Failed to send failure reply: %s", e)


def _extract_internet_message_headers(message: dict[str, Any]) -> dict[str, str]:
    """Return a flat dict of RFC 5322 headers from a Graph message envelope.

    Graph exposes these as ``message["internetMessageHeaders"]`` — a list of
    ``{"name": "...", "value": "..."}`` objects — when the field is selected.
    If the field is absent (not selected, or message predates Graph change),
    fall back to an empty dict.
    """
    raw = message.get("internetMessageHeaders") or []
    return {h["name"].lower(): h["value"] for h in raw if "name" in h and "value" in h}


def match_board_meeting_anchor(
    headers: dict[str, str],
) -> str | None:
    """Match email threading headers against stored board-meeting anchors.

    Inspects ``in-reply-to`` and ``references`` headers (space-delimited
    Message-IDs) and queries ``workflow_state`` for a board_meeting_invitation
    row whose ``context.email_thread_anchor`` equals any candidate ID.

    Returns the canonical ``meeting_id`` (e.g. ``"board_meeting:ΔΣ05-2026"``)
    of the most recently updated matching workflow, or ``None`` when no match
    is found.
    """
    # Collect all candidate message-IDs to check: In-Reply-To first, then
    # every token in References (preserving order — In-Reply-To is the direct
    # parent and is most likely to match).
    candidates: list[str] = []

    in_reply_to = (headers.get("in-reply-to") or "").strip()
    if in_reply_to:
        candidates.append(in_reply_to)

    references_raw = (headers.get("references") or "").strip()
    for token in references_raw.split():
        token = token.strip()
        if token and token not in candidates:
            candidates.append(token)

    if not candidates:
        return None

    try:
        conn = _get_connection()
        # Query once per candidate; stop at first hit.  For the common case
        # (single In-Reply-To that matches) this is one query.
        for candidate in candidates:
            row = conn.execute(
                """
                SELECT workflow_id, data, updated_at
                  FROM workflow_state
                 WHERE workflow_name = 'board_meeting_invitation'
                   AND JSON_EXTRACT(data, '$.context.email_thread_anchor') = ?
                 ORDER BY updated_at DESC
                 LIMIT 1
                """,
                (candidate,),
            ).fetchone()
            if row:
                import json as _json
                data = _json.loads(row["data"] or "{}")
                ctx = data.get("context") or {}
                meeting_id = ctx.get("meeting_id") or ""
                if not meeting_id:
                    # Derive from workflow_id (format: board_meeting:ΔΣXX-YYYY)
                    meeting_id = row["workflow_id"]
                return meeting_id
    except Exception as exc:
        logger.warning("match_board_meeting_anchor: DB query failed: %s", exc)

    return None


def _strip_re_prefix(subject: str) -> str:
    """Remove leading Re:/RE:/Fwd: prefixes from a subject line."""
    return re.sub(r"^(Re|RE|Fwd|FWD|Fw|FW)\s*:\s*", "", subject, flags=re.IGNORECASE).strip()


def _build_discord_mirror_post(
    *,
    sender_email: str,
    sender_name: str,
    subject: str,
    body_html: str | None,
    body_plain: str,
) -> str:
    """Compose the Discord post text for a mirrored board-reply email.

    Header:  💬 **{display}** ({email})
    Subject: **Re:** {subject_stripped}
    Body:    plain text, truncated at _DISCORD_BODY_CAP chars.
    """
    display = sender_name if sender_name else sender_email

    # Use plain text if available, else strip HTML.
    if body_html:
        from src.integrations.discord.cogs.platform_bridge import PlatformBridgeCog
        plain = PlatformBridgeCog._html_to_plain(body_html)
    else:
        plain = body_plain or ""

    if len(plain) > _DISCORD_BODY_CAP:
        plain = plain[:_DISCORD_BODY_CAP].rstrip() + "..."

    subject_clean = _strip_re_prefix(subject)

    lines = [
        f"💬 **{display}** ({sender_email})",
        "",
        f"**Re:** {subject_clean}",
        "",
        plain,
    ]
    return "\n".join(lines)


async def _mirror_board_reply(
    *,
    message: dict[str, Any],
    meeting_id: str,
    imid: str,
    sender_email: str,
    sender_name: str,
    subject: str,
) -> dict[str, Any]:
    """Publish a BoardEmailSentPayload(kind='board_reply') so the existing
    _on_board_email_sent cog handler posts it to the Discord thread.

    The body_html field carries the pre-rendered Discord post content
    (plain text, not actual HTML) — the cog's _html_to_plain call will
    be a no-op on already-plain content.
    """
    from src.core.event_bus import bus
    from src.core.events import EVENT_BOARD_EMAIL_SENT, BoardEmailSentPayload

    # Extract body for rendering
    body_obj = message.get("body") or {}
    body_html_raw: str | None = None
    body_plain_raw: str = message.get("bodyPreview") or ""
    if (body_obj.get("contentType") or "").lower() == "html":
        body_html_raw = body_obj.get("content") or None
    else:
        body_plain_raw = body_obj.get("content") or body_plain_raw

    content = _build_discord_mirror_post(
        sender_email=sender_email,
        sender_name=sender_name,
        subject=subject,
        body_html=body_html_raw,
        body_plain=body_plain_raw,
    )

    # Derive meeting_ref from meeting_id (board_meeting:ΔΣ05-2026 → ΔΣ05-2026)
    meeting_ref = meeting_id.split(":", 1)[-1] if ":" in meeting_id else meeting_id

    payload = BoardEmailSentPayload(
        meeting_id=meeting_id,
        meeting_ref=meeting_ref,
        kind="board_reply",
        subject=subject,
        body_html=content,   # pre-rendered; cog's _html_to_plain is a no-op
        test_mode=False,
    )

    await bus.publish(EVENT_BOARD_EMAIL_SENT, payload)

    mark_email_seen(imid, outcome="board_reply_mirrored", notes=meeting_id)
    log_action(
        workflow="email_intake",
        action="board_reply_mirrored",
        actor="email",
        target=sender_email,
        details={
            "meeting_id": meeting_id,
            "internet_message_id": imid,
            "subject": subject,
        },
    )
    logger.info(
        "Board reply from %s mirrored to Discord thread for %s",
        sender_email, meeting_id,
    )
    return {"outcome": "board_reply_mirrored", "meeting_id": meeting_id}


async def process_inbox_message(
    message: dict[str, Any],
    *,
    source: str = "webhook",
) -> dict[str, Any]:
    """Run the full intake pipeline for one Graph message.

    Args:
        message: A Graph message envelope as returned by
            :meth:`M365InboxClient.get_message`.  Must include
            ``id``, ``internetMessageId``, ``from``, ``subject``,
            ``hasAttachments``.
        source: ``"webhook"`` or ``"safety_poll"`` — included in audit
            log entries for traceability.

    Returns:
        Dict with ``outcome`` (one of ``archived``,
        ``duplicate``, ``rejected_sender``, ``rejected_subject``,
        ``no_pdf``, ``failed``) and optional ``workflow_id``.
    """
    message_id = message.get("id", "")
    imid = message.get("internetMessageId", "")
    subject = message.get("subject", "") or ""
    sender_email, sender_name = _extract_sender(message)

    log_action(
        workflow="email_intake",
        action="message_received",
        actor=source,
        target=sender_email or "(unknown)",
        details={
            "subject": subject,
            "internet_message_id": imid,
            "graph_id": message_id,
        },
    )

    if not imid:
        # Defensive: every real Graph message has this field.
        return {"outcome": "failed", "reason": "missing internetMessageId"}

    if has_seen_email(imid):
        logger.debug("Skipping already-processed message %s", imid)
        return {"outcome": "duplicate"}

    # ── Board-meeting reply bridge (Phase 4b) ─────────────────────────────
    # Check BEFORE sender/subject guards: a board member replying to a board
    # thread email is a valid "board_reply" even if their address wouldn't
    # pass the archive allow-list or the subject-pattern check.
    headers = _extract_internet_message_headers(message)
    meeting_id = match_board_meeting_anchor(headers)
    if meeting_id is not None:
        # Loop prevention — two cases:
        # 1. The email came FROM the bot's own address AND carries the
        #    Discord-bridge marker in the body: this is our own outbound
        #    Discord→email echo bouncing back via the distribution list.
        # 2. kind='discord_bridge' events published by the other agent are
        #    already filtered at the cog level (_EMAIL_KIND_LABEL key guards).
        bot_from = sender_email.strip().lower() == _BOT_IDENTITY.lower()
        body_preview = message.get("bodyPreview") or ""
        body_content = (message.get("body") or {}).get("content") or ""
        has_bridge_marker = (
            _DISCORD_BRIDGE_MARKER in body_preview
            or _DISCORD_BRIDGE_MARKER in body_content
        )
        if bot_from and has_bridge_marker:
            # Our own echo — mark seen and drop silently.
            mark_email_seen(imid, outcome="loop_skipped",
                            notes=f"discord_bridge_echo|{meeting_id}")
            log_action(
                workflow="email_intake",
                action="loop_prevention_skipped",
                actor=source,
                target=sender_email,
                details={"meeting_id": meeting_id, "internet_message_id": imid},
            )
            logger.debug(
                "Loop prevention: skipping Discord-bridge echo from %s for %s",
                sender_email, meeting_id,
            )
            return {"outcome": "loop_skipped"}

        # Director branch.  Two sub-paths depending on whether board@ is on
        # the recipient list of THIS email:
        #
        # 1. board@ present (Reply-All, or any Director-initiated email that
        #    addresses board@) → every board member already sees the email
        #    in their inbox.  We treat it as a normal board reply: mirror to
        #    Discord verbatim, plus archive any briefing attachment in the
        #    background (no bot announcement — would be duplicate info).
        #
        # 2. board@ absent (Director replied just to members@) → the email
        #    is private to the bot.  We archive any briefing and post a
        #    single bot-composed announcement to BOTH the board email
        #    thread and the private board Discord thread.  The Director's
        #    raw email never reaches the board.
        from src.workflows.director_briefing import (
            board_in_recipients as _board_in_recipients,
            is_director as _is_director,
        )
        if _is_director(sender_email):
            board_visible = _board_in_recipients(message)
            try:
                from src.workflows.director_briefing_intake import (
                    process_director_briefing_email,
                )
                # send_announcement only when board@ is NOT on the email —
                # otherwise the announcement would duplicate what the board
                # is about to see via the regular mirror below.
                await process_director_briefing_email(
                    message=message,
                    meeting_id=meeting_id,
                    sender_email=sender_email,
                    subject=subject,
                    send_announcement=not board_visible,
                )
            except Exception as exc:
                logger.exception(
                    "Director briefing intake failed (non-fatal): %s", exc,
                )

            if board_visible:
                # Sub-path 1 — board sees the email directly; mirror as usual.
                return await _mirror_board_reply(
                    message=message,
                    meeting_id=meeting_id,
                    imid=imid,
                    sender_email=sender_email,
                    sender_name=sender_name,
                    subject=subject,
                )

            # Sub-path 2 — private to members@; no Discord mirror.  The
            # announcement (if any briefing was found) is already published.
            mark_email_seen(
                imid, outcome="director_private_handled", notes=meeting_id,
            )
            return {
                "outcome": "director_private_handled",
                "meeting_id": meeting_id,
            }

        # Non-director board-thread reply: mirror the body to Discord.
        return await _mirror_board_reply(
            message=message,
            meeting_id=meeting_id,
            imid=imid,
            sender_email=sender_email,
            sender_name=sender_name,
            subject=subject,
        )
    # ── End board-meeting reply bridge ────────────────────────────────────

    if not sender_allowed(sender_email):
        mark_email_seen(imid, outcome="rejected_sender",
                        notes=f"sender={sender_email}")
        log_action(
            workflow="email_intake",
            action="rejected_sender",
            actor=source,
            target=sender_email,
            status="rejected",
        )
        return {"outcome": "rejected_sender"}

    if not subject_matches(subject):
        mark_email_seen(imid, outcome="rejected_subject",
                        notes=f"subject={subject!r}")
        # Don't bother replying — sender allow-list passed, but they didn't
        # ask for archiving.  Common case: regular board correspondence.
        return {"outcome": "rejected_subject"}

    if not message.get("hasAttachments"):
        mark_email_seen(imid, outcome="no_pdf", notes="no attachments at all")
        inbox = M365InboxClient()
        await _send_failure_reply(
            inbox, message,
            reason="Δεν εντοπίστηκε συνημμένο στο email σας.",
            sender_email=sender_email,
        )
        return {"outcome": "no_pdf"}

    inbox = M365InboxClient()
    attachments = await inbox.list_attachments(message_id)
    pdf_meta = _find_pdf_attachment(attachments)
    if not pdf_meta:
        mark_email_seen(imid, outcome="no_pdf",
                        notes=f"attachments={[a.get('name') for a in attachments]}")
        await _send_failure_reply(
            inbox, message,
            reason=(
                "Δεν εντοπίστηκε αρχείο που μπορούμε να αρχειοθετήσουμε (ή στείλατε "
                "πολλά — στείλτε ένα κάθε φορά).  Δεκτά: PDF, DOCX, ODT, RTF, "
                "εικόνες (JPG/PNG/HEIC)."
            ),
            sender_email=sender_email,
        )
        return {"outcome": "no_pdf"}

    # Download the PDF to data/inbox/<safe-name>
    _INBOX_DIR.mkdir(parents=True, exist_ok=True)
    pdf_name = pdf_meta.get("name") or "attachment.pdf"
    # ``tempfile.TemporaryDirectory`` handles the create + cleanup contract
    # natively — exiting the ``with`` block (success, failure, or exception)
    # always rmtrees the directory.  Idiomatic Python; replaces the manual
    # try/finally + shutil.rmtree pattern used before.  Per-message tempdir
    # so two concurrent intakes with the same filename can't collide.
    with tempfile.TemporaryDirectory(
        prefix="m365_intake_", dir=str(_INBOX_DIR),
    ) as tmpdir_name:
        tmpdir = Path(tmpdir_name)
        pdf_path = tmpdir / pdf_name
        await inbox.download_attachment(message_id, pdf_meta["id"], pdf_path)
        return await _process_after_download(
            inbox=inbox,
            message=message,
            message_id=message_id,
            imid=imid,
            subject=subject,
            sender_email=sender_email,
            sender_name=sender_name,
            pdf_path=pdf_path,
            source=source,
        )


async def _process_after_download(
    *,
    inbox: "M365InboxClient",
    message: dict[str, Any],
    message_id: str,
    imid: str,
    subject: str,
    sender_email: str,
    sender_name: str,
    pdf_path: Path,
    source: str,
) -> dict[str, Any]:
    """Continue the intake flow once the PDF is on disk.

    Extracted from ``process_inbox_message`` so the outer function can wrap
    this in a clean ``try / finally`` that guarantees tempdir cleanup.
    """
    # Test-sender check: emails from settings.testing.test_email are routed
    # through the archive workflow in TEST MODE — no SharePoint upload, no
    # πρωτόκολλο write, and the workflow rolls back on completion.  This
    # lets the developer drive a real end-to-end run from a personal inbox
    # without polluting production state.
    test_mode = _is_test_sender(sender_email)
    if test_mode:
        log_action(
            workflow="email_intake",
            action="forced_test_mode",
            actor=source,
            target=sender_email,
            details={"reason": "sender matches settings.testing.test_email"},
        )
        logger.info(
            "Email from %s matches testing.test_email — forcing TEST MODE",
            sender_email,
        )

    # Kick off the archive workflow
    wf = ArchiveWorkflow(actor=f"email:{sender_email}")
    body_preview = message.get("bodyPreview", "") or ""
    initial_data: dict[str, Any] = {
        "pdf_path": str(pdf_path.resolve()),
        "sender_email": sender_email,
        "sender_name": sender_name,
        "email_subject": subject,
        "email_body": body_preview,
        "test_mode": test_mode,
        # Used by the notify step in Phase 3 to know who to reply to
        "_reply_to_internet_message_id": imid,
    }

    try:
        result = await wf.run(initial_data)
    except Exception as e:
        logger.error("Archive workflow failed for message %s: %s", imid, e)
        mark_email_seen(imid, workflow_id=wf.workflow_id, outcome="failed",
                        notes=str(e))
        await _send_failure_reply(
            inbox, message,
            reason=f"Σφάλμα κατά την αρχειοθέτηση: {e}",
            sender_email=sender_email,
        )
        return {"outcome": "failed", "workflow_id": wf.workflow_id, "error": str(e)}

    if result.get("status") != "completed":
        # Special case (2026-05-27 logic rewrite): the workflow is parked
        # awaiting SecGen's confirmation that the submitted file matches a
        # pre-existing πρωτόκολλο reservation.  Reply with a clear "queued
        # for review" message rather than a generic failure.
        pending_confirm = (wf.context or {}).get("pending_reservation_confirmation")
        if pending_confirm:
            mark_email_seen(
                imid, workflow_id=wf.workflow_id,
                outcome="awaiting_reservation_confirmation",
                notes=pending_confirm.get("protocol_number", ""),
            )
            try:
                from src.integrations.m365_mail import M365MailClient as _Mail
                mail = _Mail()
                proto = pending_confirm.get("protocol_number", "?")
                existing = pending_confirm.get("existing_title", "?")
                body = (
                    f"Καλησπέρα {sender_name or sender_email},<br><br>"
                    f"Ο αριθμός πρωτοκόλλου <b>{proto}</b> είναι δεσμευμένος από τη "
                    f"Γραμματεία με τίτλο «{existing}» και ο τίτλος του αρχείου σας "
                    f"δεν ταιριάζει σίγουρα.<br><br>"
                    f"Το αίτημα έχει σταλεί στον/στη Γενικό/ή Γραμματέα για επιβεβαίωση. "
                    f"Θα ενημερωθείτε με την απόφαση.<br>"
                )
                await mail.send_reply(
                    parent_internet_message_id=imid,
                    body=body,
                    html=True,
                    to=sender_email,
                    workflow="email_intake",
                )
            except Exception as e:  # pragma: no cover — best-effort
                logger.warning("Failed to send reservation-pending reply: %s", e)
            log_action(
                workflow="email_intake",
                action="awaiting_reservation_confirmation",
                actor=source,
                target=sender_email,
                details={
                    "workflow_id": wf.workflow_id,
                    "protocol_number": pending_confirm.get("protocol_number"),
                    "existing_title": pending_confirm.get("existing_title"),
                },
            )
            return {
                "outcome": "awaiting_reservation_confirmation",
                "workflow_id": wf.workflow_id,
                "pending_reservation_confirmation": pending_confirm,
            }

        # Workflow failed cleanly — record + reply
        mark_email_seen(imid, workflow_id=wf.workflow_id, outcome="failed",
                        notes=result.get("error", "workflow did not complete"))
        await _send_failure_reply(
            inbox, message,
            reason=result.get("error", "Η αρχειοθέτηση δεν ολοκληρώθηκε."),
            sender_email=sender_email,
        )
        return {"outcome": "failed", "workflow_id": wf.workflow_id}

    # Success — send the confirmation reply
    ctx = wf.context
    llm_result = ctx.get("llm_result") or {}
    test_banner = (
        '<div class="test-banner">TEST MODE — δεν αρχειοθετήθηκε πραγματικά. '
        'Καμία εγγραφή δεν προστέθηκε στο πρωτόκολλο και κανένα αρχείο '
        'δεν ανέβηκε στο SharePoint.</div>'
        if test_mode else ""
    )
    proto = ctx.get("protocol_number", "?")
    try:
        mail = M365MailClient()
        body = render_email(
            "archive_confirmation",
            # Shelled-render: brand v2 wrapper.
            kicker=f"Καλησπέρα {sender_name or sender_email}",
            title="Το έγγραφό σας<br/>καταχωρήθηκε.",
            header_ref="ΑΡΧΕΙΟΘΕΤΗΣΗ ΕΓΓΡΑΦΟΥ",
            stamp=f"ΑΡ.ΠΡΩΤ. {proto}",
            footer_note=(
                "Αν αυτό το email σας έφτασε κατά λάθος, "
                "παρακαλούμε ενημερώστε τον Γενικό Γραμματέα."
            ),
            # Inner-template placeholders
            test_mode_banner=test_banner,
            proto=proto,
            doc_title=llm_result.get("title", "?"),
            labels=", ".join(llm_result.get("labels", [])) or "—",
            kuria_simeia=llm_result.get("key_points", "") or "—",
            folder=ctx.get("remote_folder", "—"),
            workflow_id=wf.workflow_id,
            revision_until=ctx.get("revision_open_until", "—"),
            share_link=ctx.get("share_link") or "#",
        )
        await mail.send_reply(
            parent_internet_message_id=imid,
            body=body,
            html=True,
            to=sender_email,
            workflow="email_intake",
        )
    except Exception as e:  # pragma: no cover — best-effort
        logger.warning("Failed to send confirmation reply for %s: %s", imid, e)

    try:
        await inbox.mark_read(message_id)
    except Exception as e:  # pragma: no cover
        logger.warning("Failed to mark message as read: %s", e)

    # Test-mode runs: roll back so the reserved protocol number is released and
    # the workflow_state row is marked cancelled — mirrors what the CLI
    # `archive submit --test` flow does when the user presses Enter at the
    # cleanup prompt.
    if test_mode:
        try:
            await wf.rollback(wf.context)
            log_action(
                workflow="email_intake",
                action="test_mode_rolled_back",
                actor=source,
                target=sender_email,
                details={"workflow_id": wf.workflow_id},
            )
        except Exception as e:  # pragma: no cover — best-effort
            logger.warning("Test-mode rollback failed for %s: %s", wf.workflow_id, e)

    mark_email_seen(
        imid,
        workflow_id=wf.workflow_id,
        outcome="archived_test" if test_mode else "archived",
        notes=ctx.get("protocol_number", ""),
    )
    log_action(
        workflow="email_intake",
        action="archived_test" if test_mode else "archived",
        actor=source,
        target=sender_email,
        details={
            "workflow_id": wf.workflow_id,
            "protocol_number": ctx.get("protocol_number"),
            "internet_message_id": imid,
            "test_mode": test_mode,
        },
    )
    return {
        "outcome": "archived",
        "test_mode": test_mode,
        "workflow_id": wf.workflow_id,
        "protocol_number": ctx.get("protocol_number"),
    }


async def run_safety_poll() -> dict[str, Any]:
    """Process every unread message currently sitting in Inbox.

    Called daily at 12:00 Europe/Athens by the scheduler.  Returns a
    summary dict for logging.
    """
    inbox = M365InboxClient()
    try:
        messages = await inbox.list_unread_inbox()
    except Exception as e:
        logger.error("Safety poll failed to list inbox: %s", e)
        return {"error": str(e), "processed": 0}

    counts: dict[str, int] = {}
    for msg in messages:
        try:
            result = await process_inbox_message(msg, source="safety_poll")
            counts[result["outcome"]] = counts.get(result["outcome"], 0) + 1
        except Exception as e:  # pragma: no cover — defensive
            logger.exception("Safety poll error processing message: %s", e)
            counts["error"] = counts.get("error", 0) + 1

    logger.info("Safety poll completed: %s", counts)
    log_action(
        workflow="email_intake",
        action="safety_poll_completed",
        actor="scheduler",
        details=counts,
    )
    return {"processed": sum(counts.values()), "by_outcome": counts}
