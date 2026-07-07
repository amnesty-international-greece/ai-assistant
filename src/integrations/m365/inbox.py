"""Microsoft Graph inbox reader - fetches messages + downloads attachments.

Used by both:
  * The ``/webhooks/m365/inbox`` route, which receives a notification and
    needs to fetch the full message that triggered it.
  * The daily 12:00 Europe/Athens safety poll, which lists unread Inbox
    messages and processes any that match the archive intake criteria.

Subject matching uses Unicode-NFD accent stripping + casefold so all of
the following count as a hit when the pattern is ``"αρχειο"``:

    "[Αρχείο] εισηγηση"
    "ΑΡΧΕΙΟ - Πρακτικά"
    "fwd: αρχείο - υποψηφιότητα"
    "Archive request"   ← matches the second default pattern "archive"
"""

from __future__ import annotations

import base64
import logging
import unicodedata
from pathlib import Path
from typing import Any

import httpx

from src.config import settings
from src.integrations.m365.auth import M365GraphAuthMixin

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def normalize_subject(s: str) -> str:
    """Strip accents and lowercase for case-/accent-insensitive matching."""
    if not s:
        return ""
    nfd = unicodedata.normalize("NFD", s)
    no_marks = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return no_marks.casefold()


def subject_matches(subject: str, patterns: list[str] | None = None) -> bool:
    """True if ``subject`` contains any of the configured patterns
    (case-/accent-insensitive)."""
    pats = patterns if patterns is not None else settings.m365_inbox.subject_patterns
    normalized = normalize_subject(subject)
    return any(normalize_subject(p) in normalized for p in pats)


def default_sender_allow_list() -> set[str]:
    """When the YAML allow-list is empty, default to all board_members.

    The configured ``testing.test_email`` (e.g. a developer's personal
    address) is ALWAYS allow-listed implicitly so that test-mode runs
    work end-to-end without having to fake the From header.  Real test
    emails routed through this address are forced into TEST MODE by
    :func:`src.workflows.email_intake._is_test_sender`, so no risk of
    accidentally archiving for real from a personal inbox.
    """
    cfg = settings.m365_inbox
    if cfg.sender_allow_list:
        base = {a.strip().lower() for a in cfg.sender_allow_list if a.strip()}
    else:
        base = {m.email.strip().lower() for m in settings.workflows.board_meeting.board_members}
    test_addr = (settings.testing.test_email or "").strip().lower()
    if test_addr:
        base.add(test_addr)
    return base


def sender_allowed(sender_email: str) -> bool:
    """Case-insensitive membership check against the allow-list."""
    if not sender_email:
        return False
    return sender_email.strip().lower() in default_sender_allow_list()


class M365InboxClient(M365GraphAuthMixin):
    """Read-only-style inbox queries against the signed-in user's mailbox.

    Shares the MSAL token cache with every other M365 client (OneDrive,
    Mail, Graph subscriptions) via :class:`M365GraphAuthMixin` - a single
    ``ai-assistant auth microsoft`` run covers them all.
    """

    _SCOPES = ["Mail.ReadWrite"]

    # ── Public API ───────────────────────────────────────────────────────────

    async def get_message(self, message_id: str) -> dict[str, Any]:
        """Fetch a single message by Graph id (returned in the webhook payload).

        Selects only the fields we need for intake - keeps responses small
        and avoids accidentally pulling the body's HTML payload twice.

        ``internetMessageHeaders`` is included so the board-reply bridge can
        inspect ``In-Reply-To`` / ``References`` without a second Graph call.
        ``body`` is included so the bridge can render the full body as Discord
        plain text (bodyPreview is capped at ~255 chars by Graph).
        """
        select = (
            "id,internetMessageId,subject,from,toRecipients,hasAttachments,"
            "receivedDateTime,bodyPreview,isRead,body,internetMessageHeaders"
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_GRAPH_BASE}/me/messages/{message_id}?$select={select}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()

    async def list_unread_inbox(self, *, max_results: int = 50) -> list[dict[str, Any]]:
        """List unread messages in Inbox, newest first.

        Used by the daily safety poll.  Returns the same field set as
        :meth:`get_message` - see ``$select`` above.
        """
        select = (
            "id,internetMessageId,subject,from,toRecipients,hasAttachments,"
            "receivedDateTime,bodyPreview,isRead,body,internetMessageHeaders"
        )
        url = (
            f"{_GRAPH_BASE}/me/mailFolders('Inbox')/messages"
            f"?$filter=isRead eq false"
            f"&$top={max_results}"
            f"&$orderby=receivedDateTime desc"
            f"&$select={select}"
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json().get("value", [])

    async def list_attachments(self, message_id: str) -> list[dict[str, Any]]:
        """List attachment metadata (no content bytes) for a message."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_GRAPH_BASE}/me/messages/{message_id}/attachments"
                "?$select=id,name,contentType,size",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json().get("value", [])

    async def download_attachment(
        self,
        message_id: str,
        attachment_id: str,
        dest_path: Path,
    ) -> Path:
        """Download one attachment to disk.

        Graph returns the file as a base64-encoded ``contentBytes`` blob
        on the attachment resource - we decode and write it to
        ``dest_path``, creating parent dirs as needed.
        """
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                f"{_GRAPH_BASE}/me/messages/{message_id}/attachments/{attachment_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            body = resp.json()
        content_b64 = body.get("contentBytes") or ""
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(base64.b64decode(content_b64))
        logger.info("Downloaded attachment %s → %s (%d bytes)",
                    body.get("name"), dest_path, dest_path.stat().st_size)
        return dest_path

    async def mark_read(self, message_id: str) -> None:
        """Mark a message as read (so it doesn't reappear in the safety poll)."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.patch(
                f"{_GRAPH_BASE}/me/messages/{message_id}",
                headers=self._headers(),
                json={"isRead": True},
            )
            resp.raise_for_status()
