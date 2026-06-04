"""WebhookManager — create, cache, and send via Discord channel webhooks."""
from __future__ import annotations

import logging

import discord

from src.integrations.discord.constants import WEBHOOK_NAME

logger = logging.getLogger(__name__)


class WebhookManager:
    """Manage a single named webhook per channel, cached in memory."""

    def __init__(self) -> None:
        self._cache: dict[int, discord.Webhook] = {}

    async def get_or_create(
        self,
        channel: discord.TextChannel | discord.ForumChannel,
    ) -> discord.Webhook:
        """Return the cached webhook for *channel* or create one named WEBHOOK_NAME."""
        channel_id = channel.id
        if channel_id in self._cache:
            return self._cache[channel_id]

        try:
            for wh in await channel.webhooks():
                if wh.name == WEBHOOK_NAME:
                    self._cache[channel_id] = wh
                    return wh
        except discord.Forbidden:
            raise

        wh = await channel.create_webhook(name=WEBHOOK_NAME)
        self._cache[channel_id] = wh
        logger.info("Created webhook %r in #%s", WEBHOOK_NAME, channel.name)
        return wh

    async def post(
        self,
        channel: discord.TextChannel | discord.ForumChannel,
        *,
        content: str,
        username: str | None = None,
        avatar_url: str | None = None,
        thread: discord.Thread | None = None,
        files: list[discord.File] | None = None,
    ) -> discord.WebhookMessage:
        """Post *content* via the channel webhook, optionally into *thread*."""
        wh = await self.get_or_create(channel)
        kwargs: dict = {"content": content}
        if username:
            kwargs["username"] = username
        if avatar_url:
            kwargs["avatar_url"] = avatar_url
        if thread:
            kwargs["thread"] = thread
        if files:
            kwargs["files"] = files
        return await wh.send(**kwargs)

    def invalidate(self, channel_id: int) -> None:
        """Drop the cached webhook for *channel_id* so the next call re-fetches."""
        self._cache.pop(channel_id, None)
