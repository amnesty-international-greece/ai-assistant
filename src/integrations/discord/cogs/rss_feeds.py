"""RSS → Discord poster cog (replaces MonitoRSS).

Owns the Discord side of the RSS pipeline:
  • fetches every enabled feed via :mod:`src.integrations.rss`
  • walks each feed's routes, posting matching new items into the configured
    channel as a brand_embed (forum threads get the configured tag applied)
  • updates the per-feed ``last_seen_guid`` cursor so the next poll only
    posts genuinely-new content

Triggered by:
  • The scheduler's ``rss.poll_feeds`` job (every 15 min) - see
    :mod:`src.core.scheduler`.
  • CLI ``ai-assistant rss poll-now`` (manual / debugging).
  • Slash command ``/ai-assistant rss-poll-now`` (Admin).

Test-mode awareness
===================
When ``STATE_TEST_MODE_ACTIVE`` is on, posts get redirected to
``settings.discord.admin.test_admin_channel_id`` so admins can verify
formatting without spamming real channels.
"""
from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from src.config import settings
from src.core.audit import (
    list_rss_feeds,
    list_rss_routes,
    log_action,
    update_rss_feed_cursor,
)
from src.integrations.discord.brand import (
    AMNESTY_YELLOW,
    brand_embed,
    fmt_ts,
)
from src.integrations.discord.constants import (
    DISCORD_THREAD_NAME_MAX,
    STATE_TEST_MODE_ACTIVE,
)
from src.integrations.discord.state import BotStateStore
from src.integrations.rss import (
    FeedItem,
    fetch_feed,
    filter_new_items,
    item_matches_route,
)

logger = logging.getLogger(__name__)

# Max items we'll post for a feed on its FIRST poll (when last_seen_guid is
# empty).  Prevents a 50-item blast when a feed is newly registered.
_BOOTSTRAP_POST_LIMIT = 1


class RssFeedsCog(commands.Cog):
    """Polls RSS feeds and posts new items to Discord channels."""

    # Poll cadence - every 15 minutes per the user-confirmed choice.  Edit
    # here (not in scheduler.py) since RSS lives in the bot's event loop,
    # not the FastAPI scheduler's.
    POLL_INTERVAL_MINUTES = 15

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._state_store: BotStateStore = BotStateStore()
        self._commands_group = self._RssCommands(cog=self)

    async def cog_load(self) -> None:
        # Register the slash subgroup under the existing /ai-assistant tree.
        # We look up the existing group by name (added by AiAssistantCog on
        # its own cog_load) and graft our commands onto it.
        ai_group = self.bot.tree.get_command("ai-assistant")
        if ai_group is None or not isinstance(ai_group, app_commands.Group):
            logger.warning(
                "RssFeedsCog: /ai-assistant group not found on tree - "
                "RSS slash commands will not be registered.  Check cog load order."
            )
        else:
            try:
                ai_group.add_command(self._commands_group)
                logger.info("RssFeedsCog loaded - /ai-assistant rss subgroup registered")
            except discord.app_commands.errors.CommandAlreadyRegistered:
                logger.debug("RssFeedsCog: /ai-assistant rss already registered (cog reload?)")

        # Start the polling loop.  Lives inside the bot's event loop - no
        # APScheduler dependency.  Skips the first immediate run (we don't
        # want to blast posts within seconds of every bot reboot); the loop
        # naturally fires POLL_INTERVAL_MINUTES later.
        if not self._poll_loop.is_running():
            self._poll_loop.start()

    async def cog_unload(self) -> None:
        if self._poll_loop.is_running():
            self._poll_loop.cancel()
        ai_group = self.bot.tree.get_command("ai-assistant")
        if isinstance(ai_group, app_commands.Group):
            try:
                ai_group.remove_command("rss")
            except Exception:
                pass

    @tasks.loop(minutes=POLL_INTERVAL_MINUTES)
    async def _poll_loop(self) -> None:
        """Background poll - runs every POLL_INTERVAL_MINUTES."""
        try:
            await self.poll_all_feeds()
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("RSS poll loop crashed: %s", exc)

    @_poll_loop.before_loop
    async def _before_poll_loop(self) -> None:
        """Wait until the bot is connected to Discord before the first poll."""
        await self.bot.wait_until_ready()
        logger.info(
            "RSS poll loop will run every %d minutes",
            self.POLL_INTERVAL_MINUTES,
        )

    # ── Core poll loop (callable from scheduler + CLI + slash command) ───────

    async def poll_all_feeds(self) -> dict[str, Any]:
        """Poll every enabled feed; post new items per their routes.

        Returns a summary dict for logging:
            {feed_url: {"new_items": N, "posted": M, "errors": K}}
        """
        feeds = list_rss_feeds(enabled_only=True)
        if not feeds:
            return {}

        summary: dict[str, Any] = {}
        for feed in feeds:
            feed_url = feed["feed_url"]
            try:
                feed_summary = await self._poll_one_feed(feed)
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("RSS poll failed for %s: %s", feed_url, exc)
                feed_summary = {"errors": 1, "new_items": 0, "posted": 0}
            summary[feed_url] = feed_summary

        log_action(
            workflow="rss",
            action="poll_cycle_completed",
            actor="scheduler",
            details=summary,
        )
        return summary

    async def _poll_one_feed(self, feed: dict[str, Any]) -> dict[str, int]:
        """Inner: fetch one feed, route + post matching new items."""
        feed_url = feed["feed_url"]
        last_seen_guid = feed.get("last_seen_guid")

        items = await fetch_feed(feed_url)
        if not items:
            return {"new_items": 0, "posted": 0, "errors": 0}

        new_items = filter_new_items(items, last_seen_guid=last_seen_guid)
        if last_seen_guid is None and new_items:
            # First poll for this feed - don't replay the entire archive.
            # Keep just the most recent so the user gets a sample.
            new_items = new_items[:_BOOTSTRAP_POST_LIMIT]

        if not new_items:
            # Even on a no-new-items poll, refresh last_polled_at so the
            # CLI 'rss list' shows freshness.
            update_rss_feed_cursor(feed_url, last_seen_guid)
            return {"new_items": 0, "posted": 0, "errors": 0}

        routes = list_rss_routes(feed_url)
        if not routes:
            logger.info("RSS feed %s has no routes - new items dropped", feed_url)
            # Still advance the cursor so we don't keep re-evaluating the same items
            update_rss_feed_cursor(feed_url, new_items[0].guid)
            return {"new_items": len(new_items), "posted": 0, "errors": 0}

        # Walk in chronological order (oldest first) so Discord ordering
        # mirrors publication ordering.
        posted = 0
        errors = 0
        for item in reversed(new_items):
            for route in routes:
                if not item_matches_route(
                    item,
                    url_pattern=route.get("url_pattern"),
                    title_pattern=route.get("title_pattern"),
                ):
                    continue
                try:
                    await self._post_item(item, route)
                    posted += 1
                except Exception as exc:
                    logger.warning(
                        "RSS post failed for %s → channel %s: %s",
                        item.link, route.get("channel_id"), exc,
                    )
                    errors += 1

        # Advance cursor to the newest item we processed (regardless of whether
        # it matched any route - we never want to revisit it).
        update_rss_feed_cursor(feed_url, new_items[0].guid)
        return {"new_items": len(new_items), "posted": posted, "errors": errors}

    async def _post_item(self, item: FeedItem, route: dict[str, Any]) -> None:
        """Post one item to one channel per a single route's config."""
        # Test-mode override: redirect everything to test_admin_channel
        test_mode = await self._state_store.get_bool(STATE_TEST_MODE_ACTIVE, default=False)
        target_channel_id = route["channel_id"]
        if test_mode and settings.discord.admin.test_admin_channel_id:
            target_channel_id = settings.discord.admin.test_admin_channel_id

        channel = self.bot.get_channel(int(target_channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(target_channel_id))
            except Exception as exc:
                logger.warning("RSS: target channel %s unreachable: %s", target_channel_id, exc)
                return

        embed = self._build_embed(item, test_mode=test_mode, route_label=route.get("label"))

        if isinstance(channel, discord.ForumChannel):
            await self._post_to_forum(channel, item, embed, route)
        elif hasattr(channel, "send"):
            await channel.send(embed=embed)  # type: ignore[union-attr]
        else:
            logger.warning(
                "RSS: target channel %s is %s - cannot send",
                target_channel_id, type(channel).__name__,
            )
            return

        log_action(
            workflow="rss",
            action="item_posted",
            actor="scheduler",
            target=item.link,
            details={
                "guid": item.guid,
                "title": item.title[:120],
                "channel_id": target_channel_id,
                "route_id": route.get("id"),
                "test_mode": test_mode,
            },
        )

    async def _post_to_forum(
        self,
        channel: discord.ForumChannel,
        item: FeedItem,
        embed: discord.Embed,
        route: dict[str, Any],
    ) -> None:
        """Create a new forum thread per item.

        Tag application strategy:
          1. If route.forum_tag_id is set → use that snowflake directly.
          2. Else if route.forum_tag_name is set → resolve against
             channel.available_tags by name (case-insensitive match).
          3. Else → no tag applied.
        """
        applied_tags: list[discord.Object] = []
        forum_tag_id = route.get("forum_tag_id") or ""
        forum_tag_name = (route.get("forum_tag_name") or "").strip().lower()
        if forum_tag_id:
            try:
                applied_tags = [discord.Object(id=int(forum_tag_id))]
            except ValueError:
                logger.warning("RSS: forum_tag_id %r is not numeric", forum_tag_id)
        elif forum_tag_name:
            for t in channel.available_tags:
                if t.name.strip().lower() == forum_tag_name:
                    applied_tags = [discord.Object(id=t.id)]
                    break
            else:
                logger.warning(
                    "RSS: forum_tag_name %r not found on #%s (available: %s)",
                    forum_tag_name,
                    channel.name,
                    [t.name for t in channel.available_tags],
                )

        # Forum thread name = item title, truncated.  Discord caps at 100 chars.
        thread_name = (item.title or "(untitled)")[:DISCORD_THREAD_NAME_MAX]
        # Forum threads MUST have either content or an embed.  We send the
        # embed AND a tiny plain-text fallback so the Google Group email
        # bridge (which can't render Discord embeds) still gets a useful
        # one-liner.
        content = f"**{item.title}**\n{item.link}"
        kwargs: dict[str, Any] = {
            "name": thread_name,
            "content": content,
            "embed": embed,
        }
        if applied_tags:
            kwargs["applied_tags"] = applied_tags
        await channel.create_thread(**kwargs)

    def _build_embed(
        self,
        item: FeedItem,
        *,
        test_mode: bool,
        route_label: str | None,
    ) -> discord.Embed:
        """Render a FeedItem as a brand-styled embed."""
        title_prefix = "[TEST MODE] " if test_mode else ""
        embed = brand_embed(
            title=f"{title_prefix}{item.title[:240]}",
            description=(item.description_plain or "")[:600],
            color=AMNESTY_YELLOW,
            url=item.link or None,
            timestamp=item.published_at,
        )
        if item.thumbnail_url:
            # set_image (big banner) not set_thumbnail (tiny corner): news
            # cards look much better with the article's hero image visible.
            embed.set_image(url=item.thumbnail_url)
        if item.published_at:
            embed.add_field(
                name="Δημοσιεύτηκε",
                value=fmt_ts(item.published_at, "R"),
                inline=True,
            )
        if route_label:
            embed.add_field(name="Κατηγορία", value=route_label, inline=True)
        return embed

    # ── Slash subgroup: /ai-assistant rss ───────────────────────────────────

    class _RssCommands(app_commands.Group):
        def __init__(self, cog: "RssFeedsCog") -> None:
            super().__init__(name="rss", description="RSS feed management")
            self.cog = cog

        @app_commands.command(
            name="list",
            description="Show configured RSS feeds & routes",
        )
        @app_commands.default_permissions(administrator=True)
        async def cmd_list(self, interaction: discord.Interaction) -> None:
            feeds = list_rss_feeds()
            embed = brand_embed(
                title="RSS Feeds",
                color=AMNESTY_YELLOW,
                description=f"{len(feeds)} feed(s) configured.",
            )
            if not feeds:
                embed.add_field(
                    name="(empty)",
                    value="Add a feed with `ai-assistant rss add-feed <url>` from the CLI.",
                    inline=False,
                )
            for feed in feeds[:10]:
                routes = list_rss_routes(feed["feed_url"])
                cursor = feed.get("last_seen_guid") or "-"
                polled = feed.get("last_polled_at") or "never"
                value = (
                    f"**Label:** {feed.get('label') or '-'}\n"
                    f"**Enabled:** {'✓' if feed.get('enabled') else '✗'}\n"
                    f"**Routes:** {len(routes)}\n"
                    f"**Last poll:** {polled}\n"
                    f"**Cursor:** `{cursor[:40]}…`" if len(cursor) > 40 else f"**Cursor:** `{cursor}`"
                )
                embed.add_field(
                    name=feed["feed_url"][:80],
                    value=value,
                    inline=False,
                )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @app_commands.command(
            name="poll-now",
            description="Trigger an RSS poll cycle immediately",
        )
        @app_commands.default_permissions(administrator=True)
        async def cmd_poll_now(self, interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            summary = await self.cog.poll_all_feeds()
            lines = []
            for feed_url, stats in summary.items():
                lines.append(
                    f"`{feed_url[:60]}` - new={stats['new_items']}, "
                    f"posted={stats['posted']}, errors={stats['errors']}"
                )
            await interaction.followup.send(
                "RSS poll cycle complete:\n" + ("\n".join(lines) if lines else "(no feeds configured)"),
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RssFeedsCog(bot))
