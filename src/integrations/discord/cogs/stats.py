"""Stats cog — weekly digest background task and /stats slash command."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from src.config import settings
from src.core.audit import log_action
from src.integrations.discord.constants import (
    WEEKLY_DIGEST_DAY,
    WEEKLY_DIGEST_HOUR,
    WEEKLY_DIGEST_MIN_INTERVAL_SECONDS,
    WORKFLOW_NAME,
)
from src.integrations.discord.state import NotificationUsersStore
from src.integrations.discord.stats import ChannelStats, ClassificationStats, StatsSummary, StatsStore

logger = logging.getLogger(__name__)


class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._stats_store: StatsStore = StatsStore()
        self._notif_store: NotificationUsersStore = NotificationUsersStore()
        self._digest_task: asyncio.Task | None = None
        self._last_digest_sent: datetime | None = None

    async def cog_load(self) -> None:
        self._digest_task = asyncio.get_event_loop().create_task(
            self._digest_loop(), name="stats_digest_loop"
        )
        logger.info("StatsCog loaded")

    async def cog_unload(self) -> None:
        if self._digest_task:
            self._digest_task.cancel()
            try:
                await self._digest_task
            except asyncio.CancelledError:
                pass

    async def _digest_loop(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            try:
                now = datetime.now(timezone.utc)
                if now.weekday() == WEEKLY_DIGEST_DAY and now.hour == WEEKLY_DIGEST_HOUR:
                    last = self._last_digest_sent
                    if last is None or (now - last).total_seconds() > WEEKLY_DIGEST_MIN_INTERVAL_SECONDS:
                        await self._post_digest(now)
                        self._last_digest_sent = now
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("StatsCog digest loop error: %s", exc)
            await asyncio.sleep(3600)

    async def _post_digest(self, now: datetime) -> None:
        since = now - timedelta(days=7)
        summary = await self._stats_store.summary(since=since, test_mode=False)
        per_ch = await self._stats_store.per_channel(since=since, test_mode=False)
        per_cls = await self._stats_store.per_classification(since=since, test_mode=False)
        embed = self._build_embed(summary, per_ch, per_cls, "Last 7 days")

        admin_id = settings.discord.admin.admin_channel_id
        if admin_id:
            channel = self.bot.get_channel(int(admin_id))
            if channel and isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(embed=embed)
                except Exception as exc:
                    logger.warning("StatsCog: failed to post digest to admin channel: %s", exc)

        for user in await self._notif_store.due_now(now):
            try:
                discord_user = await self.bot.fetch_user(int(user.user_id))
                dm = await discord_user.create_dm()
                await dm.send(embed=embed)
                await self._notif_store.mark_sent(user.user_id, when=now)
            except Exception as exc:
                logger.warning("StatsCog: failed to DM user %s: %s", user.user_id, exc)

        log_action(
            workflow=WORKFLOW_NAME,
            action="stats_digest_posted",
            details={"total": summary.total},
        )

    def _build_embed(
        self,
        summary: StatsSummary,
        per_ch: list[ChannelStats],
        per_cls: list[ClassificationStats],
        window_label: str,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"Weekly Stats Digest — {window_label}",
            color=discord.Color.yellow(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Total", value=str(summary.total), inline=True)
        embed.add_field(name="Inbound emails", value=str(summary.inbound_email), inline=True)
        embed.add_field(name="Outbound emails", value=str(summary.outbound_email), inline=True)
        embed.add_field(name="Discord posts", value=str(summary.discord_posts), inline=True)
        if summary.avg_confidence is not None:
            embed.add_field(
                name="Avg confidence",
                value=f"{summary.avg_confidence:.0%}",
                inline=True,
            )
        if per_ch:
            lines = []
            for cs in per_ch[:5]:
                ch = self.bot.get_channel(int(cs.channel_id))
                name = f"#{ch.name}" if ch else cs.channel_id
                lines.append(f"{name}: {cs.count}")
            embed.add_field(name="Top channels", value="\n".join(lines), inline=False)
        if per_cls:
            lines = [f"{c.classification}: {c.count}" for c in per_cls[:5]]
            embed.add_field(name="By classification", value="\n".join(lines), inline=False)
        return embed

    # NOTE: the standalone `/stats` slash command was retired during the
    # Discord bot modernization (see docs/plans/discord_bot_modernization.md §B.1).
    # It now lives as `/ai-assistant stats` with a time-range Select menu
    # (built in phase B6 — see AiAssistantCog).
    # The digest loop (and StatsStore-publishing helpers) remain here.


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StatsCog(bot))
