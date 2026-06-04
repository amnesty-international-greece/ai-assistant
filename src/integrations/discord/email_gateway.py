"""
Email gateway — async IMAP poller + SMTP sender for the Google Group ↔ Discord bridge.

Bridges a Gmail account (acting as relay for a Google Group) to Discord forum threads.
Uses ``aioimaplib`` and ``aiosmtplib`` so the gateway runs on the bot's asyncio event
loop without blocking it.

Typical usage::

    gateway = EmailGateway()
    gateway.on_inbound(my_async_callback)
    await gateway.start()   # begins background polling
    ...
    await gateway.stop()    # clean shutdown
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header as _std_decode_header
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Awaitable, Callable
from uuid import uuid4

import aioimaplib
import aiosmtplib

from src.config import settings
from src.core import audit
from src.integrations.discord.constants import (
    ATTACHMENT_DOWNLOAD_BACKOFF_BASE,
    EMAIL_ATTACHMENT_MAX_BYTES,
    EMAIL_DECODE_CHARSETS,
    EMAIL_IMAP_FOLDER,
    EMAIL_IMAP_SEARCH_CRITERION,
    EMAIL_MESSAGE_ID_DOMAIN,
    EMAIL_POLL_INTERVAL_SECONDS,
    EMAIL_SENDER_DISPLAY_NAME,
    EMAIL_TEST_POLL_INTERVAL_SECONDS,
    RESTART_INITIAL_DELAY_SECONDS,
    RESTART_MAX_DELAY_SECONDS,
    STATE_TEST_MODE_ACTIVE,
    WORKFLOW_NAME,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EmailAttachment:
    """A single file attachment carried by an email message."""

    filename: str
    """Original filename as decoded from the MIME part."""

    content_type: str
    """MIME content-type string, e.g. ``"application/pdf"``."""

    data: bytes
    """Raw attachment bytes. Size is enforced by :data:`EMAIL_ATTACHMENT_MAX_BYTES`."""

    is_inline: bool = False
    """``True`` when the ``Content-Disposition`` header is ``inline``."""


@dataclass(slots=True)
class InboundEmail:
    """Parsed representation of a single inbound email message."""

    message_id: str
    """RFC 822 ``Message-ID`` value — angle brackets stripped."""

    request_id: str
    """Short random hex token (8 chars) shared across all audit rows for this email."""

    in_reply_to: str | None
    """RFC 822 ``In-Reply-To`` value — angle brackets stripped, or ``None``."""

    references: list[str]
    """Ordered list of ``References`` header tokens, angle brackets stripped."""

    from_addr: str
    """Sender email address, e.g. ``"alice@example.com"``."""

    from_name: str
    """Sender display name, e.g. ``"Alice Smith"`` (may be empty string)."""

    to: list[str]
    """Primary recipient addresses."""

    cc: list[str]
    """CC recipient addresses."""

    subject: str
    """Decoded subject line."""

    body_plain: str
    """Plain-text body (preferred), or text extracted from HTML."""

    body_html: str | None
    """Raw HTML body if present, otherwise ``None``."""

    attachments: list[EmailAttachment]
    """Attachments that passed the size check."""

    received_at: datetime
    """Parse timestamp in UTC."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ANGLE_BRACKET_RE = re.compile(r"<([^>]+)>")


def _strip_angle_brackets(value: str | None) -> str | None:
    """Return the token inside ``<...>``, or the original string if no brackets."""
    if not value:
        return None
    m = _ANGLE_BRACKET_RE.search(value)
    return m.group(1) if m else value.strip()


def _split_references(raw: str | None) -> list[str]:
    """Split a ``References`` header into individual message-id tokens."""
    if not raw:
        return []
    tokens: list[str] = []
    for part in re.split(r"\s+", raw.strip()):
        stripped = _strip_angle_brackets(part)
        if stripped:
            tokens.append(stripped)
    return tokens


def _decode_header_value(raw: str | None) -> str:
    """Decode a potentially RFC 2047-encoded header value to a plain string."""
    if not raw:
        return ""
    parts: list[str] = []
    for fragment, charset in _std_decode_header(raw):
        if isinstance(fragment, bytes):
            if charset:
                try:
                    parts.append(fragment.decode(charset))
                    continue
                except (LookupError, UnicodeDecodeError):
                    pass
            for cs in EMAIL_DECODE_CHARSETS:
                try:
                    parts.append(fragment.decode(cs))
                    break
                except UnicodeDecodeError:
                    continue
            else:
                parts.append(fragment.decode("utf-8", errors="replace"))
        else:
            parts.append(fragment)
    return "".join(parts)


def _decode_payload(part, declared_charset: str | None) -> str:
    """Decode a MIME part payload to a string, trying multiple charsets."""
    raw: bytes | None = part.get_payload(decode=True)
    if not raw:
        return ""
    charsets = []
    if declared_charset:
        charsets.append(declared_charset)
    charsets.extend(cs for cs in EMAIL_DECODE_CHARSETS if cs not in charsets)
    for cs in charsets:
        try:
            return raw.decode(cs)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_address_list(header_value: str | None) -> list[str]:
    """Extract individual email addresses from a comma-separated address header."""
    if not header_value:
        return []
    # Simple split — handles "Name <addr>, addr2" forms
    parts = [p.strip() for p in header_value.split(",") if p.strip()]
    result: list[str] = []
    for p in parts:
        m = re.search(r"<([^>]+)>", p)
        if m:
            result.append(m.group(1).strip())
        else:
            result.append(p)
    return result


def _parse_message(raw_bytes: bytes) -> InboundEmail:
    """Parse a raw RFC 822 message into an :class:`InboundEmail`."""
    msg = message_from_bytes(raw_bytes)

    # --- Headers ---
    raw_from = msg.get("From", "")
    from_name_raw, from_addr_raw = "", raw_from
    m = re.match(r"^(.*?)\s*<([^>]+)>\s*$", raw_from)
    if m:
        from_name_raw, from_addr_raw = m.group(1).strip(), m.group(2).strip()
    from_name = _decode_header_value(from_name_raw) if from_name_raw else ""
    from_addr = from_addr_raw.strip().strip('"')

    subject = _decode_header_value(msg.get("Subject"))
    message_id_raw = _strip_angle_brackets(msg.get("Message-ID"))
    in_reply_to = _strip_angle_brackets(msg.get("In-Reply-To"))
    references = _split_references(msg.get("References"))

    to = _parse_address_list(msg.get("To"))
    cc = _parse_address_list(msg.get("Cc"))

    # --- Body + attachments ---
    body_plain = ""
    body_html: str | None = None
    attachments: list[EmailAttachment] = []

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disposition = part.get("Content-Disposition", "")
            is_inline = disposition.lower().startswith("inline")

            if ct == "text/plain" and not body_plain and not part.get_filename():
                body_plain = _decode_payload(part, part.get_content_charset())

            elif ct == "text/html" and body_html is None and not part.get_filename():
                body_html = _decode_payload(part, part.get_content_charset())

            elif part.get_filename():
                filename_raw = part.get_filename() or "attachment"
                filename = _decode_header_value(filename_raw)
                raw_data: bytes = part.get_payload(decode=True) or b""
                if len(raw_data) > EMAIL_ATTACHMENT_MAX_BYTES:
                    logger.warning(
                        "Dropping attachment %r — size %d bytes exceeds limit %d",
                        filename,
                        len(raw_data),
                        EMAIL_ATTACHMENT_MAX_BYTES,
                    )
                    continue
                attachments.append(
                    EmailAttachment(
                        filename=filename,
                        content_type=ct,
                        data=raw_data,
                        is_inline=is_inline,
                    )
                )
    else:
        body_plain = _decode_payload(msg, msg.get_content_charset())

    # Fall back: extract plain text from HTML if no plain part
    if not body_plain and body_html:
        body_plain = re.sub(r"<[^>]+>", " ", body_html)
        body_plain = re.sub(r"\s+", " ", body_plain).strip()
        body_plain = html.unescape(body_plain)

    return InboundEmail(
        message_id=message_id_raw or f"unknown-{uuid4()}",
        request_id="",  # assigned by poll_once after construction
        in_reply_to=in_reply_to,
        references=references,
        from_addr=from_addr,
        from_name=from_name,
        to=to,
        cc=cc,
        subject=subject,
        body_plain=body_plain,
        body_html=body_html,
        attachments=attachments,
        received_at=datetime.now(timezone.utc),
    )


def _read_test_mode_active() -> bool:
    """Read the ``test_mode_active`` flag from ``discord_bot_state`` via the shared connection."""
    try:
        conn = audit._get_connection()
        row = conn.execute(
            "SELECT value FROM discord_bot_state WHERE key = ?",
            (STATE_TEST_MODE_ACTIVE,),
        ).fetchone()
        return bool(row and row[0].lower() in ("1", "true", "yes"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# EmailGateway
# ---------------------------------------------------------------------------


class EmailGateway:
    """
    Gmail IMAP poller + SMTP sender for the Google Group ↔ Discord bridge.

    All network operations are native-async via ``aioimaplib`` / ``aiosmtplib``;
    no blocking calls are made on the event loop.

    Configuration is read from ``src.config.settings.discord.email_gateway``
    and ``settings.gmail_app_password`` at construction time — no network
    activity happens until :meth:`start` or :meth:`poll_once` is called.
    """

    def __init__(self) -> None:
        cfg = settings.discord.email_gateway
        self._gmail_user: str = cfg.gmail_user
        self._gmail_password: str = settings.gmail_app_password
        self._google_group_email: str = cfg.google_group_email
        self._imap_host: str = cfg.imap_host
        self._imap_port: int = cfg.imap_port
        self._smtp_host: str = cfg.smtp_host
        self._smtp_port: int = cfg.smtp_port

        self._callbacks: list[Callable[[InboundEmail], Awaitable[None]]] = []
        self._poll_task: asyncio.Task[None] | None = None
        self._running: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_inbound(self, callback: Callable[[InboundEmail], Awaitable[None]]) -> None:
        """
        Register an async callback invoked for every successfully fetched email.

        Multiple callbacks can be registered; they are called sequentially.
        If a callback raises, the email is left unread for the next poll cycle
        and remaining callbacks for that message are skipped.
        """
        self._callbacks.append(callback)

    async def start(self) -> None:
        """
        Begin polling IMAP in the background.

        Idempotent — calling :meth:`start` while already running is a no-op.
        """
        if self._running:
            return
        self._running = True
        self._poll_task = asyncio.get_event_loop().create_task(
            self._poll_loop(), name="email_gateway_poll"
        )
        logger.info("EmailGateway started (IMAP host: %s)", self._imap_host)

    async def stop(self) -> None:
        """
        Stop polling cleanly.

        Idempotent — safe to call even if not started.  Waits for the current
        poll iteration to finish before returning.
        """
        if not self._running:
            return
        self._running = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        logger.info("EmailGateway stopped.")

    async def poll_once(self) -> list[InboundEmail]:
        """
        Fetch all UNSEEN messages from INBOX, mark them ``\\Seen``, and return
        the parsed :class:`InboundEmail` list.

        Each message is marked ``\\Seen`` **after** all registered callbacks
        return successfully.  If a callback raises, the message is left unread
        so the next poll cycle retries it (the exception is logged but not
        re-raised; remaining messages in the batch continue to be processed).
        """
        results: list[InboundEmail] = []
        try:
            client = aioimaplib.IMAP4_SSL(host=self._imap_host, port=self._imap_port)
            await client.wait_hello_from_server()
            await client.login(self._gmail_user, self._gmail_password)
            await client.select(EMAIL_IMAP_FOLDER)
        except Exception as exc:
            # ``%r`` preserves the exception type name when str(exc) is
            # empty (aioimaplib raises bare exceptions on some early
            # connection failures — observed in production 2026-05-27).
            logger.warning("IMAP connection/login failed: %r", exc)
            raise

        try:
            _, data = await client.search(EMAIL_IMAP_SEARCH_CRITERION)
            if not data or not data[0]:
                return results

            uid_list: list[str] = data[0].decode().split() if isinstance(data[0], bytes) else data[0].split()
            uid_list = [u for u in uid_list if u]

            for uid in uid_list:
                try:
                    _, fetch_data = await client.fetch(uid, "(RFC822)")
                except Exception as exc:
                    logger.warning("IMAP FETCH failed for UID %s: %s", uid, exc)
                    continue

                # aioimaplib returns a list of lines; find the raw message bytes
                raw: bytes | None = None
                for chunk in fetch_data:
                    if isinstance(chunk, bytes) and len(chunk) > 100:
                        raw = chunk
                        break

                if not raw:
                    logger.warning("Empty fetch response for UID %s", uid)
                    continue

                try:
                    email = _parse_message(raw)
                    email.request_id = uuid4().hex[:8]
                except Exception as exc:
                    logger.error("Failed to parse email UID %s: %s", uid, exc)
                    continue

                # Anti-loop: skip emails sent by the gateway itself
                if email.from_addr and self._gmail_user.lower() == email.from_addr.lower().strip():
                    logger.debug("Anti-loop: skipping email from self (%s)", email.from_addr)
                    try:
                        await client.store(uid, "+FLAGS", r"\Seen")
                    except Exception:
                        pass
                    continue

                # Invoke callbacks; leave unread on any failure
                callback_ok = True
                for cb in self._callbacks:
                    try:
                        await cb(email)
                    except Exception as exc:
                        logger.exception(
                            "Inbound callback %r raised for message %s: %s",
                            cb,
                            email.message_id,
                            exc,
                        )
                        callback_ok = False
                        break

                if callback_ok:
                    try:
                        await client.store(uid, "+FLAGS", r"\Seen")
                    except Exception as exc:
                        logger.warning(
                            "Could not mark UID %s as \\Seen: %s", uid, exc
                        )
                    results.append(email)

                    # Audit log
                    try:
                        audit.log_action(
                            workflow=WORKFLOW_NAME,
                            action="email_inbound",
                            target=email.message_id,
                            details={
                                "from": email.from_addr,
                                "subject": email.subject,
                                "attachments": len(email.attachments),
                                "request_id": email.request_id,
                            },
                        )
                    except Exception:
                        pass  # audit failure must never disrupt the gateway

        finally:
            try:
                await client.logout()
            except Exception:
                pass

        return results

    async def send_email(
        self,
        to: str | list[str],
        subject: str,
        body: str,
        in_reply_to_message_id: str | None = None,
        references: list[str] | None = None,
        attachments: list[EmailAttachment] | None = None,
    ) -> str:
        """
        Send an email via SMTP and return the outbound ``Message-ID``.

        ``Message-ID`` is always generated fresh as
        ``<{uuid4}@{EMAIL_MESSAGE_ID_DOMAIN}>`` — callers must persist it if
        they need to correlate future replies.

        ``In-Reply-To`` and ``References`` are set per RFC 5322 when provided.
        Attachment bytes are embedded directly — no temp-file I/O.
        """
        outbound_id = f"<{uuid4()}@{EMAIL_MESSAGE_ID_DOMAIN}>"
        to_str = ", ".join(to) if isinstance(to, list) else to

        msg = EmailMessage()
        msg["From"] = formataddr((EMAIL_SENDER_DISPLAY_NAME, self._gmail_user))
        msg["To"] = to_str
        msg["Subject"] = subject
        msg["Message-ID"] = outbound_id

        if in_reply_to_message_id:
            clean_irt = in_reply_to_message_id.strip("<>")
            msg["In-Reply-To"] = f"<{clean_irt}>"

            # Build References: existing chain + the message we are replying to
            ref_tokens = list(references or [])
            if clean_irt not in ref_tokens:
                ref_tokens.append(clean_irt)
            msg["References"] = " ".join(f"<{t.strip('<>')}>" for t in ref_tokens)
        elif references:
            msg["References"] = " ".join(f"<{t.strip('<>')}>" for t in references)

        # Body — plain text only; rich rendering is handled by Discord cogs
        msg.set_content(body, subtype="plain", charset="utf-8")

        for att in attachments or []:
            if len(att.data) > EMAIL_ATTACHMENT_MAX_BYTES:
                logger.warning(
                    "Dropping outbound attachment %r — %d bytes exceeds limit",
                    att.filename,
                    len(att.data),
                )
                continue
            main_type, sub_type = (att.content_type.split("/", 1) + ["octet-stream"])[:2]
            msg.add_attachment(att.data, maintype=main_type, subtype=sub_type, filename=att.filename)

        await aiosmtplib.send(
            msg,
            hostname=self._smtp_host,
            port=self._smtp_port,
            username=self._gmail_user,
            password=self._gmail_password,
            use_tls=False,
            start_tls=True,
        )

        bare_id = outbound_id.strip("<>")
        logger.info(
            "Sent email to %s subject=%r",
            to_str, subject,
            extra={"to": to_str, "subject": subject, "message_id": bare_id},
        )

        try:
            audit.log_action(
                workflow=WORKFLOW_NAME,
                action="email_outbound",
                target=bare_id,
                details={
                    "to": to_str,
                    "subject": subject,
                    "in_reply_to": in_reply_to_message_id,
                    "attachments": len(attachments) if attachments else 0,
                },
            )
        except Exception:
            pass

        return bare_id

    # ------------------------------------------------------------------
    # Internal polling loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """
        Background task: repeatedly call :meth:`poll_once`, then sleep for the
        configured interval.  Uses exponential back-off on IMAP errors.
        """
        backoff = RESTART_INITIAL_DELAY_SECONDS

        while self._running:
            test_mode = _read_test_mode_active()
            interval = (
                EMAIL_TEST_POLL_INTERVAL_SECONDS if test_mode else EMAIL_POLL_INTERVAL_SECONDS
            )

            try:
                await self.poll_once()
                backoff = RESTART_INITIAL_DELAY_SECONDS  # reset on success
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # ``%r`` preserves the exception type name when str(exc)
                # is empty (some aioimaplib failures do that — observed
                # producing 'IMAP error in poll loop:  — retrying ...' in
                # production 2026-05-27 logs).  ``exc_info`` adds the
                # traceback at DEBUG-equivalent verbosity for diagnosis.
                logger.warning(
                    "IMAP error in poll loop: %r — retrying in %ds", exc, backoff,
                    extra={"backoff_seconds": backoff},
                    exc_info=True,
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(backoff * ATTACHMENT_DOWNLOAD_BACKOFF_BASE, RESTART_MAX_DELAY_SECONDS)
                continue

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
