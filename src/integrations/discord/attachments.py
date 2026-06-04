"""AttachmentService — convert EmailAttachment objects to discord.File for upload."""
from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

import discord

from src.integrations.discord.constants import EMAIL_ATTACHMENT_MAX_BYTES

if TYPE_CHECKING:
    from src.integrations.discord.email_gateway import EmailAttachment

logger = logging.getLogger(__name__)


class AttachmentService:
    """Convert email attachments to Discord file objects."""

    def to_discord_files(
        self,
        attachments: "list[EmailAttachment]",
    ) -> list[discord.File]:
        """Return a list of discord.File objects, dropping any that exceed the size limit."""
        files: list[discord.File] = []
        for att in attachments:
            if len(att.data) > EMAIL_ATTACHMENT_MAX_BYTES:
                logger.warning(
                    "Dropping attachment %r — %d bytes exceeds limit",
                    att.filename,
                    len(att.data),
                )
                continue
            files.append(discord.File(io.BytesIO(att.data), filename=att.filename))
        return files

    def attachment_summary(self, attachments: "list[EmailAttachment]") -> str:
        """Return a short human-readable summary line, or empty string if none."""
        if not attachments:
            return ""
        names = ", ".join(a.filename for a in attachments)
        return f"📎 {len(attachments)} attachment(s): {names}"
