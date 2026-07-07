"""Microsoft 365 mail client via Microsoft Graph API.

Used for board-only emails sent from ``members@amnesty.org.gr`` to
``board@amnesty.gr`` and addressed individually.  Gmail handles the
forum bridge; this module handles M365-side outbound.

Threading model
---------------
Microsoft Graph does NOT allow setting RFC 5322 headers (``Message-ID``,
``In-Reply-To``, ``References``) directly via ``internetMessageHeaders`` -
only ``x-*`` custom headers are accepted.  To preserve threading we use
Graph's native reply chain.

There's one subtlety: a draft's Graph ``id`` **changes** when the draft is
moved from Drafts → Sent Items on send.  The RFC 5322 ``internetMessageId``
is stable, however, so we persist that and resolve back to a Graph id on
demand:

  1. ``send_email`` creates a draft, captures ``internetMessageId`` from
     the draft, sends it, and returns the ``internetMessageId``.  Caller
     persists this for the lifetime of the conversation.
  2. ``send_reply`` looks the parent up in Sent Items by ``internetMessageId``,
     calls ``/createReply`` on it, patches the body, and sends.  Exchange
     fills ``In-Reply-To``/``References`` so the reply threads in Outlook,
     Gmail, and Apple Mail.

  .. warning::
     ``/createReply`` defaults reply ``to=`` to the parent's ``from``.  When
     continuing a thread WE started (the common workflow case), pass
     ``to=`` explicitly to ``send_reply`` - otherwise the reply will be
     delivered to OUR own mailbox rather than the intended recipients.
     Verified end-to-end on 2026-05-23 via Gmail MCP.

Required scopes
---------------
``Mail.ReadWrite`` (covers draft creation, message lookup, reply chain).
Already consented in the existing Azure AD app - no extra admin step.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from src.core.audit import log_action
from src.integrations.m365.auth import M365GraphAuthMixin

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class M365MailClient(M365GraphAuthMixin):
    """Send fresh emails and threaded replies via Microsoft Graph.

    Reuses the MSAL token cache from ``data/tokens.json``.  The first call
    will hit the cache; the refresh token established by ``ai-in-ai auth
    microsoft`` covers all configured scopes.
    """

    # Mail.ReadWrite is a superset of Mail.Send; it also lets us create drafts
    # (which is how we capture the message ID for later replies).  offline_access
    # is added implicitly by MSAL when a token cache is configured.
    _SCOPES = ["Mail.ReadWrite"]

    # ── Internal helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _recipients(addresses: list[str] | str | None) -> list[dict[str, Any]]:
        """Convert ``"a@b"`` or ``["a@b", "c@d"]`` → Graph recipient list."""
        if not addresses:
            return []
        if isinstance(addresses, str):
            addresses = [addresses]
        return [{"emailAddress": {"address": addr}} for addr in addresses]

    @staticmethod
    def _body_block(body: str, html: bool) -> dict[str, str]:
        return {
            "contentType": "HTML" if html else "Text",
            "content": body,
        }

    @staticmethod
    def _attachment_blocks(
        attachments: list[Path] | list[str] | None,
    ) -> list[dict[str, Any]]:
        """Convert local file paths → Graph ``fileAttachment`` dicts.

        Each attachment is read into memory and base64-encoded.  Graph's
        message-create endpoint accepts up to ~3 MB inline; for larger
        files use ``/messages/{id}/attachments/createUploadSession``
        (not implemented here - board PDFs are well under the limit).

        Returns an empty list if ``attachments`` is None or empty.
        """
        if not attachments:
            return []
        import base64
        import mimetypes

        blocks: list[dict[str, Any]] = []
        for raw_path in attachments:
            path = Path(raw_path)
            content = path.read_bytes()
            mime, _ = mimetypes.guess_type(path.name)
            blocks.append({
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": path.name,
                "contentType": mime or "application/octet-stream",
                "contentBytes": base64.b64encode(content).decode("ascii"),
            })
        return blocks

    # ── Public API ───────────────────────────────────────────────────────────

    async def _find_message_by_internet_id(
        self,
        client: httpx.AsyncClient,
        internet_message_id: str,
    ) -> str:
        """Resolve a stable RFC 5322 ``internetMessageId`` → current Graph ``id``.

        After a draft is sent, its Graph ``id`` changes (Drafts → Sent Items).
        The RFC 5322 message-id assigned at draft creation is stable, so we
        filter Sent Items by it to find the live message.

        Raises:
            LookupError if no matching message is found.
        """
        # internetMessageId values include angle brackets; Graph filter expects
        # them quoted as a string literal (single-quote-escape inner quotes).
        escaped = internet_message_id.replace("'", "''")
        url = (
            f"{_GRAPH_BASE}/me/messages"
            f"?$filter=internetMessageId eq '{escaped}'"
            f"&$select=id&$top=1"
        )
        resp = await client.get(url, headers=self._headers())
        resp.raise_for_status()
        values = resp.json().get("value", [])
        if not values:
            raise LookupError(
                f"No message found with internetMessageId={internet_message_id!r}"
            )
        return values[0]["id"]

    async def send_email(
        self,
        to: list[str] | str,
        subject: str,
        body: str,
        *,
        html: bool = False,
        cc: list[str] | str | None = None,
        bcc: list[str] | str | None = None,
        attachments: list[Path] | list[str] | None = None,
        workflow: str = "m365_mail",
    ) -> str:
        """Send an email via the create-draft-then-send pattern.

        Two-step so we can capture the **internetMessageId** (RFC 5322 stable
        identifier) for later replies:
          1. ``POST /me/messages``          → returns the draft (with id +
             internetMessageId - Exchange generates the latter at creation time)
          2. ``POST /me/messages/{id}/send`` → dispatches it (202 Accepted)

        Args:
            to:          Recipient(s) - single address or list.
            subject:     Subject line.
            body:        Plain text or HTML body.
            html:        ``True`` → ``contentType: HTML``.
            cc, bcc:     Optional additional recipients.
            attachments: Optional list of local file paths to attach.  Each
                         file is read into memory and base64-encoded inline
                         in the draft (Graph limit ~3 MB total - fine for
                         board PDFs).
            workflow:    Workflow name for audit logging.

        Returns:
            The stable ``internetMessageId`` (e.g.
            ``"<abc@amnestygr.onmicrosoft.com>"``).  Persist this - it's
            the ``parent_internet_message_id`` for :meth:`send_reply`.
        """
        message: dict[str, Any] = {
            "subject": subject,
            "body": self._body_block(body, html),
            "toRecipients": self._recipients(to),
            "ccRecipients": self._recipients(cc),
            "bccRecipients": self._recipients(bcc),
        }
        attachment_blocks = self._attachment_blocks(attachments)
        if attachment_blocks:
            message["attachments"] = attachment_blocks

        async with httpx.AsyncClient() as client:
            # 1. Create draft
            draft_resp = await client.post(
                f"{_GRAPH_BASE}/me/messages",
                headers=self._headers(),
                json=message,
            )
            draft_resp.raise_for_status()
            draft = draft_resp.json()
            draft_id = draft["id"]
            internet_message_id = draft["internetMessageId"]  # stable

            # 2. Send the draft (returns 202 Accepted with no body).
            #    After this, draft_id is no longer valid - use internet_message_id.
            send_resp = await client.post(
                f"{_GRAPH_BASE}/me/messages/{draft_id}/send",
                headers=self._headers(),
            )
            send_resp.raise_for_status()

        log_action(
            workflow=workflow,
            action="email_sent",
            actor="system",
            target=", ".join(to) if isinstance(to, list) else to,
            details={"subject": subject, "internet_message_id": internet_message_id},
        )
        logger.info("M365 email sent: %r → %s", subject, to)
        return internet_message_id

    async def send_reply(
        self,
        parent_internet_message_id: str,
        body: str,
        *,
        html: bool = False,
        to: list[str] | str | None = None,
        cc: list[str] | str | None = None,
        attachments: list[Path] | list[str] | None = None,
        workflow: str = "m365_mail",
    ) -> str:
        """Send a threaded reply to a previously-sent message.

        Resolves the stable ``internetMessageId`` of the parent back to a
        live Graph ``id`` in Sent Items, then uses Graph's reply chain:
        ``/createReply`` gives an editable draft whose subject and
        recipients are pre-filled from the parent.  We replace the body
        (so the recipient sees a clean reply, not a quoted chain) and
        dispatch.  Exchange handles ``In-Reply-To`` / ``References`` so
        threading works in Outlook, Gmail, Apple Mail, etc.

        .. important::
            Graph's ``/createReply`` defaults the reply's ``to`` to the
            *sender* of the parent.  If the parent was sent by **us**
            (e.g. continuing a workflow-owned thread to the board), the
            reply will go back to our own mailbox.  **Always pass ``to=``
            explicitly when continuing your own outbound thread.**

        Args:
            parent_internet_message_id: RFC 5322 message-id returned by an
                earlier :meth:`send_email` or :meth:`send_reply` call.
            body: New reply body (replaces the quoted history).
            html: ``True`` → HTML body.
            to:   Override recipients.  REQUIRED when the parent was sent
                  by us (otherwise the reply lands in our own inbox).
                  Default ``None`` → Graph picks the parent's sender.
            cc:   Override CC (default: same as parent).
            attachments: Optional local file paths to attach to the reply.

        Returns:
            ``internetMessageId`` of the reply - chain further replies off
            this same id to keep one continuous thread.
        """
        async with httpx.AsyncClient() as client:
            # 1. Resolve parent's internetMessageId → current Graph id
            parent_graph_id = await self._find_message_by_internet_id(
                client, parent_internet_message_id
            )

            # 2. Create reply draft (subject auto-prefixed "RE:", recipients copied)
            create_resp = await client.post(
                f"{_GRAPH_BASE}/me/messages/{parent_graph_id}/createReply",
                headers=self._headers(),
            )
            create_resp.raise_for_status()
            draft = create_resp.json()
            reply_draft_id = draft["id"]
            reply_internet_id = draft["internetMessageId"]

            # 3. Replace body (and optionally recipients) on the draft
            patch: dict[str, Any] = {"body": self._body_block(body, html)}
            if to is not None:
                patch["toRecipients"] = self._recipients(to)
            if cc is not None:
                patch["ccRecipients"] = self._recipients(cc)

            patch_resp = await client.patch(
                f"{_GRAPH_BASE}/me/messages/{reply_draft_id}",
                headers=self._headers(),
                json=patch,
            )
            patch_resp.raise_for_status()

            # 4. Attach any files via POST /me/messages/{id}/attachments
            #    (one POST per file - Graph reliably accepts this pattern even
            #    when the draft was created via /createReply).
            for att in self._attachment_blocks(attachments):
                att_resp = await client.post(
                    f"{_GRAPH_BASE}/me/messages/{reply_draft_id}/attachments",
                    headers=self._headers(),
                    json=att,
                )
                att_resp.raise_for_status()

            # 5. Send
            send_resp = await client.post(
                f"{_GRAPH_BASE}/me/messages/{reply_draft_id}/send",
                headers=self._headers(),
            )
            send_resp.raise_for_status()

        log_action(
            workflow=workflow,
            action="email_reply_sent",
            actor="system",
            target=str(to) if to else "(thread default)",
            details={
                "reply_to": parent_internet_message_id,
                "internet_message_id": reply_internet_id,
            },
        )
        logger.info("M365 reply sent in thread of %s", parent_internet_message_id)
        return reply_internet_id
