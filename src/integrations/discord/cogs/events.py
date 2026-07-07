"""Events cog - announce Discord scheduled events to the εκδηλώσεις forum channel.

Post format (per user's note re: Google Group email bridge):
- A short plain-text summary at the TOP - readable in emails forwarded from
  the forum to the Google Group (where Discord embeds don't render).
- The event URL on its own line - Discord auto-expands this into a rich
  preview card with thumbnail, RSVP count, etc.  Email subscribers see just
  the URL, which is fine.
- (Optionally) a brand_embed below for Discord viewers' eyes - but the URL
  expansion already gives most of the visual win, so we keep the embed
  minimal to avoid double-rendering.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from src.config import settings
from src.core.audit import log_action
from src.integrations.discord.brand import AMNESTY_YELLOW, brand_embed, fmt_ts
from src.integrations.discord.constants import DISCORD_THREAD_NAME_MAX, WORKFLOW_NAME

logger = logging.getLogger(__name__)


class EventsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _get_events_channel(self) -> discord.ForumChannel | discord.TextChannel | None:
        channel_id_str = settings.discord.channels.events_channel_id
        if not channel_id_str:
            return None
        return self.bot.get_channel(int(channel_id_str))  # type: ignore[return-value]

    def _plaintext_announcement(self, event: discord.ScheduledEvent) -> str:
        """Plain-text summary for Google-Group email subscribers.

        Kept short on purpose - the event URL on its own line below will get
        auto-expanded by Discord into a full card for Discord viewers, so we
        don't need to duplicate the rich info here.
        """
        lines = [f"**{event.name}**"]
        if event.description:
            # Trim long descriptions - full text is in the linked event itself
            desc = event.description[:300]
            if len(event.description) > 300:
                desc += "…"
            lines.append(desc)
        if event.start_time:
            lines.append(f"Ημερομηνία: {fmt_ts(event.start_time, 'F')}")
        location = getattr(event, "location", None)
        if location:
            lines.append(f"Τόπος: {location}")
        return "\n".join(lines)

    @commands.Cog.listener()
    async def on_scheduled_event_create(self, event: discord.ScheduledEvent) -> None:
        channel = self._get_events_channel()
        if channel is None:
            logger.warning(
                "EventsCog: events channel %s not found",
                settings.discord.channels.events_channel_id or "(not configured)",
            )
            return

        summary = self._plaintext_announcement(event)
        # Event URL on its own line → Discord auto-expands into a card.
        content = f"{summary}\n\n{event.url}" if event.url else summary

        try:
            if isinstance(channel, discord.ForumChannel):
                await channel.create_thread(name=event.name[:DISCORD_THREAD_NAME_MAX], content=content)
            else:
                await channel.send(content=content)
        except Exception as exc:
            logger.error("EventsCog: failed to post event %s: %s", event.id, exc)
            return

        log_action(
            workflow=WORKFLOW_NAME,
            action="event_announced",
            target=str(event.id),
            details={"name": event.name},
        )

    @commands.Cog.listener()
    async def on_scheduled_event_update(
        self,
        before: discord.ScheduledEvent,
        after: discord.ScheduledEvent,
    ) -> None:
        if (
            before.status != discord.EventStatus.active
            and after.status == discord.EventStatus.active
        ):
            channel = self._get_events_channel()
            if channel is None:
                return

            # V5: LIVE notification - short plain-text + brand embed in Amnesty
            # yellow with a "Join" link button when the event has a URL.
            content = f"🔴 **LIVE** - {after.name} ξεκινά τώρα!"
            embed = brand_embed(
                title=f"🔴 LIVE - {after.name}",
                description="Η εκδήλωση ξεκινά τώρα.",
                color=AMNESTY_YELLOW,
            )
            location = getattr(after, "location", None)
            if location:
                embed.add_field(name="Τόπος", value=location, inline=False)

            view: discord.ui.View | None = None
            if after.url:
                view = discord.ui.View(timeout=None)
                view.add_item(discord.ui.Button(
                    label="Συμμετοχή",
                    style=discord.ButtonStyle.link,
                    url=after.url,
                    emoji="🔴",
                ))

            try:
                if isinstance(channel, discord.ForumChannel):
                    name = f"[LIVE] {after.name}"[:DISCORD_THREAD_NAME_MAX]
                    kwargs: dict = {"name": name, "content": content, "embed": embed}
                    if view is not None:
                        kwargs["view"] = view
                    await channel.create_thread(**kwargs)
                else:
                    send_kwargs: dict = {"content": content, "embed": embed}
                    if view is not None:
                        send_kwargs["view"] = view
                    await channel.send(**send_kwargs)
            except Exception as exc:
                logger.error("EventsCog: failed to post LIVE notice for %s: %s", after.id, exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventsCog(bot))
