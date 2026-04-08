"""Gmail integration — send emails via Gmail API."""

from __future__ import annotations

import base64
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build

from src.core.audit import log_action

logger = logging.getLogger(__name__)


class GmailClient:
    """Client for sending emails via Gmail API."""

    def __init__(self, credentials) -> None:
        """Initialize with Google OAuth credentials (from GoogleClient).

        Args:
            credentials: A google.oauth2.credentials.Credentials instance,
                typically obtained from GoogleClient after authentication.
        """
        self._service = build("gmail", "v1", credentials=credentials)

    def send_email(
        self,
        to: str | list[str],
        subject: str,
        body_html: str,
        cc: str | list[str] | None = None,
        attachments: list[Path] | None = None,
        workflow: str = "gmail",
    ) -> dict[str, Any]:
        """Send an email via Gmail.

        Args:
            to: Recipient email(s).
            subject: Email subject line.
            body_html: HTML body content.
            cc: CC recipient(s).
            attachments: List of file paths to attach.
            workflow: Workflow name for audit logging.

        Returns:
            Gmail API send response dict containing at minimum 'id' and
            'threadId' of the sent message.
        """
        if isinstance(to, str):
            to = [to]
        if isinstance(cc, str):
            cc = [cc]

        msg = MIMEMultipart()
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = ", ".join(cc)

        msg.attach(MIMEText(body_html, "html"))

        if attachments:
            for attachment_path in attachments:
                with open(attachment_path, "rb") as f:
                    part = MIMEApplication(f.read(), Name=attachment_path.name)
                part["Content-Disposition"] = f'attachment; filename="{attachment_path.name}"'
                msg.attach(part)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        result = self._service.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()

        log_action(
            workflow=workflow,
            action="email_sent",
            actor="system",
            target=", ".join(to),
            details={
                "subject": subject,
                "cc": cc,
                "attachments": [p.name for p in (attachments or [])],
                "message_id": result.get("id"),
            },
        )
        logger.info("Email sent to %s: %s", ", ".join(to), subject)
        return result
