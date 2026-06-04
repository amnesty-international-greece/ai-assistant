"""MessageRouter — maps a ClassificationResult to a Discord channel + thread."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from src.integrations.discord.constants import DISCORD_MESSAGE_SAFE_CHARS

if TYPE_CHECKING:
    from src.integrations.discord.classifier import ClassificationResult
    from src.integrations.discord.state import EnabledChannelsStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RoutingDecision:
    """Result of a routing lookup."""

    channel: discord.ForumChannel | discord.TextChannel | None
    thread: discord.Thread | None
    reason: str


class MessageRouter:
    """Routes a ClassificationResult to the appropriate Discord channel/thread."""

    def __init__(
        self,
        guild: discord.Guild,
        channels_store: "EnabledChannelsStore",
    ) -> None:
        self._guild = guild
        self._store = channels_store

    async def resolve(
        self,
        result: "ClassificationResult",
        *,
        existing_thread_id: str | None = None,
        test_mode: bool = False,
    ) -> RoutingDecision:
        """Resolve *result* to a channel and optionally an existing thread."""
        if result.fell_back or result.channel_id is None:
            return RoutingDecision(
                channel=None,
                thread=None,
                reason=f"Classifier returned UNCERTAIN (confidence={result.confidence:.0%})",
            )

        channel = self._guild.get_channel(int(result.channel_id))
        if channel is None:
            return RoutingDecision(
                channel=None,
                thread=None,
                reason=f"Channel {result.channel_id} not found in guild",
            )

        thread: discord.Thread | None = None
        if existing_thread_id:
            try:
                thread = self._guild.get_thread(int(existing_thread_id))
                if thread is None:
                    thread = await self._guild.fetch_channel(int(existing_thread_id))  # type: ignore[assignment]
            except Exception as exc:
                logger.debug("Could not fetch thread %s: %s", existing_thread_id, exc)

        return RoutingDecision(
            channel=channel,  # type: ignore[arg-type]
            thread=thread,
            reason=f"Routed to {result.label} (confidence={result.confidence:.0%})",
        )

    @staticmethod
    def truncate(text: str, max_chars: int = DISCORD_MESSAGE_SAFE_CHARS) -> str:
        """Truncate *text* to *max_chars* and append ellipsis if needed."""
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 1] + "…"
