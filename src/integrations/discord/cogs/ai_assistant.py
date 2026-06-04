"""/ai-assistant cog — general-purpose admin & info commands.

Replaces the previous ``/discord-admin`` group and standalone ``/stats``.
Migrated commands:
- ``status``    (was ``/discord-admin status``)
- ``test-mode`` (was ``/discord-admin test-mode``)
- ``stats``     (was ``/stats``)

New commands added in this phase:
- ``about``     — anyone-can-run version + description info
- ``health``    — platform health (filled in by B6); stub for now

Out-of-scope here (moved elsewhere):
- ``classify-toggle``  → ``/forum auto-classify``  (built in B6 forum cog)
- ``add-channel`` / ``remove-channel``  → ``/forum channels``  (B6)
- ``add-team`` / ``remove-team`` / ``teams``  → dropped per user; use Discord
                                              role UI directly
- ``notify-me``         → dropped per user (no opt-in DM digests for now)
- ``add-team`` etc.    → dropped per user
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from src.config import settings
from src.integrations.discord.constants import (
    STATE_AUTO_CLASSIFY,
    STATE_BOT_ACTIVE,
    STATE_TEST_EMAIL,
    STATE_TEST_MODE_ACTIVE,
    STATE_WEBHOOK_ACTIVE,
)
from src.integrations.discord.state import (
    BotStateStore,
    EnabledChannelsStore,
    NotificationUsersStore,
)

logger = logging.getLogger(__name__)


# Amnesty brand palette
AMNESTY_YELLOW = discord.Color.from_str("#FFFF00")
AMNESTY_BLACK = discord.Color.from_str("#000000")


class AiAssistantCog(commands.Cog):
    """`/ai-assistant` slash command group — general-purpose admin & info."""

    class _AiAssistantCommands(app_commands.Group):
        def __init__(self, cog: "AiAssistantCog") -> None:
            super().__init__(
                name="ai-assistant",
                description="Γενικές εντολές για το AI Assistant Bot",
            )
            self.cog = cog

        @app_commands.command(name="status", description="Δείξε την τρέχουσα κατάσταση του bot")
        @app_commands.default_permissions(administrator=True)
        async def cmd_status(self, interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            snap = await self.cog._state_store.snapshot()
            test_mode = snap.get(STATE_TEST_MODE_ACTIVE, "0") == "1"
            ch_list = await self.cog._channels_store.list(test_mode=test_mode)
            notif_users = await self.cog._notif_store.list()

            embed = discord.Embed(
                title="AI Assistant — Κατάσταση",
                color=AMNESTY_YELLOW,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="bot_active", value=snap.get(STATE_BOT_ACTIVE, "?"), inline=True)
            embed.add_field(name="webhook_active", value=snap.get(STATE_WEBHOOK_ACTIVE, "?"), inline=True)
            embed.add_field(name="auto_classify", value=snap.get(STATE_AUTO_CLASSIFY, "?"), inline=True)
            embed.add_field(name="test_mode", value=snap.get(STATE_TEST_MODE_ACTIVE, "0"), inline=True)
            embed.add_field(name="test_email", value=snap.get(STATE_TEST_EMAIL, "—"), inline=True)
            embed.add_field(name="Ενεργά κανάλια", value=str(len(ch_list)), inline=True)
            embed.add_field(name="Notif users", value=str(len(notif_users)), inline=True)
            embed.set_footer(text=f"AI Assistant Platform v{settings.app.version}")
            await interaction.followup.send(embed=embed, ephemeral=True)

        @app_commands.command(name="test-mode", description="Ενεργοποίηση/απενεργοποίηση test mode")
        @app_commands.describe(value="on ή off")
        @app_commands.choices(value=[
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
        ])
        @app_commands.default_permissions(administrator=True)
        async def cmd_test_mode(self, interaction: discord.Interaction, value: str) -> None:
            enabled = value == "on"
            await self.cog._state_store.set_bool(STATE_TEST_MODE_ACTIVE, enabled)
            state_str = "ON" if enabled else "OFF"
            await interaction.response.send_message(
                f"Test mode is now **{state_str}**.", ephemeral=True,
            )

        @app_commands.command(name="about", description="Πληροφορίες για το AI Assistant")
        async def cmd_about(self, interaction: discord.Interaction) -> None:
            """Anyone-can-run info command."""
            embed = discord.Embed(
                title="AI Assistant Bot",
                description=(
                    "Πλατφόρμα αυτοματισμού για τη Διεθνή Αμνηστία — Ελληνικό Τμήμα.\n\n"
                    "Διαχειρίζεται προσκλήσεις ΔΣ, πρωτόκολλο εγγράφων, ενημερωτικά δελτία, "
                    "γέφυρα email↔Discord, και πολλά άλλα."
                ),
                color=AMNESTY_YELLOW,
            )
            embed.add_field(name="Έκδοση", value=settings.app.version, inline=True)
            website = settings.urls.website or ""
            if website:
                embed.add_field(name="Web", value=website, inline=True)
            embed.set_footer(text="Διεθνής Αμνηστία — Ελληνικό Τμήμα")
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @app_commands.command(name="health", description="Πλατφόρμα — υγεία υπηρεσιών")
        @app_commands.default_permissions(administrator=True)
        async def cmd_health(self, interaction: discord.Interaction) -> None:
            """Health summary — scheduler jobs, backups, Graph subscription expiry."""
            await interaction.response.defer(ephemeral=True)
            from src.integrations.discord.brand import fmt_ts
            from src.integrations.onedrive import OneDriveClient

            embed = discord.Embed(
                title="AI Assistant — Health",
                color=AMNESTY_YELLOW,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Bot", value="✅ running", inline=True)
            embed.add_field(name="discord.py", value=discord.__version__, inline=True)
            embed.add_field(name="Έκδοση πλατφόρμας", value=settings.app.version, inline=True)

            # Πρωτόκολλο backup status
            try:
                backup_path = OneDriveClient.PROTOCOL_BACKUP_PATH
                if backup_path.exists():
                    stat = backup_path.stat()
                    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                    embed.add_field(
                        name="Πρωτόκολλο backup",
                        value=f"✅ {fmt_ts(mtime, 'R')} ({stat.st_size:,} bytes)",
                        inline=False,
                    )
                else:
                    embed.add_field(
                        name="Πρωτόκολλο backup",
                        value="⚠️ Δεν υπάρχει — πρώτος κύκλος αρχειοθέτησης θα τον δημιουργήσει.",
                        inline=False,
                    )
            except Exception as exc:
                embed.add_field(name="Πρωτόκολλο backup", value=f"❓ {exc}", inline=False)

            # Graph subscription expiry countdown (Phase 3 email intake)
            try:
                from src.core.audit import get_active_graph_subscriptions
                subs = get_active_graph_subscriptions()
                if subs:
                    lines = []
                    for s in subs[:3]:
                        try:
                            exp = datetime.fromisoformat(
                                s["expiration_date_time"].replace("Z", "+00:00"),
                            )
                            lines.append(f"`{s['subscription_id'][:8]}…` λήγει {fmt_ts(exp, 'R')}")
                        except Exception:
                            lines.append(f"`{s['subscription_id']}` exp={s['expiration_date_time']}")
                    embed.add_field(
                        name=f"Graph subscriptions ({len(subs)})",
                        value="\n".join(lines),
                        inline=False,
                    )
                else:
                    embed.add_field(
                        name="Graph subscriptions",
                        value="—  (δεν υπάρχει ενεργή subscription)",
                        inline=False,
                    )
            except Exception as exc:
                embed.add_field(name="Graph subscriptions", value=f"❓ {exc}", inline=False)

            embed.set_footer(text=f"v{settings.app.version}")
            await interaction.followup.send(embed=embed, ephemeral=True)

        @app_commands.command(name="stats", description="Στατιστικά της γέφυρας email↔Discord")
        @app_commands.describe(range="Χρονικό παράθυρο")
        @app_commands.choices(range=[
            app_commands.Choice(name="Last 24h", value="24h"),
            app_commands.Choice(name="Last 7 days", value="7d"),
            app_commands.Choice(name="Last 30 days", value="30d"),
            app_commands.Choice(name="All time", value="all"),
        ])
        async def cmd_stats(self, interaction: discord.Interaction, range: str = "7d") -> None:
            """Stats dashboard — S1 from the modernization plan.

            Renders a Rich Embed in Amnesty palette with a SelectMenu that lets
            the viewer flip between time ranges in-place (no need to re-run
            the command).
            """
            await interaction.response.defer(ephemeral=True)
            view = StatsDashboardView(initial_range=range)
            embed = await view.build_embed(self.cog.bot)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot
        self._state_store: BotStateStore = BotStateStore()
        self._channels_store: EnabledChannelsStore = EnabledChannelsStore()
        self._notif_store: NotificationUsersStore = NotificationUsersStore()
        self._commands = self._AiAssistantCommands(cog=self)

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self._commands)
        logger.info("AiAssistantCog loaded — /ai-assistant group registered")

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command("ai-assistant")


# ── Stats dashboard (S1) ────────────────────────────────────────────────────


_RANGE_LABELS = {
    "24h": "Last 24 hours",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "all": "All time",
}


class StatsDashboardView(discord.ui.View):
    """Interactive dashboard with a range Select and a CSV export button."""

    def __init__(self, initial_range: str = "7d") -> None:
        super().__init__(timeout=600)
        self.current_range = initial_range
        self.range_select = _RangeSelect(self, initial_range)
        self.add_item(self.range_select)

    async def build_embed(self, bot: discord.Client) -> discord.Embed:
        from datetime import timedelta
        from src.integrations.discord.stats import StatsStore

        store = StatsStore()
        since: datetime | None
        if self.current_range == "24h":
            since = datetime.now(timezone.utc) - timedelta(days=1)
        elif self.current_range == "7d":
            since = datetime.now(timezone.utc) - timedelta(days=7)
        elif self.current_range == "30d":
            since = datetime.now(timezone.utc) - timedelta(days=30)
        else:
            since = None  # all time

        summary = await store.summary(since=since)
        per_ch = await store.per_channel(since=since)
        per_cls = await store.per_classification(since=since)

        embed = discord.Embed(
            title=f"Στατιστικά — {_RANGE_LABELS[self.current_range]}",
            color=AMNESTY_YELLOW,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Σύνολο", value=str(summary.total), inline=True)
        embed.add_field(name="Inbound emails", value=str(summary.inbound_email), inline=True)
        embed.add_field(name="Outbound emails", value=str(summary.outbound_email), inline=True)
        embed.add_field(name="Discord posts", value=str(summary.discord_posts), inline=True)
        if summary.avg_confidence is not None:
            embed.add_field(name="Avg confidence", value=f"{summary.avg_confidence:.0%}", inline=True)
        if per_ch:
            lines = []
            for cs in per_ch[:5]:
                ch = bot.get_channel(int(cs.channel_id)) if cs.channel_id else None
                name = f"#{ch.name}" if ch else (cs.channel_id or "—")
                lines.append(f"{name}: {cs.count}")
            embed.add_field(name="Top channels", value="\n".join(lines), inline=False)
        if per_cls:
            lines = [f"{c.classification}: {c.count}" for c in per_cls[:5]]
            embed.add_field(name="By classification", value="\n".join(lines), inline=False)
        embed.set_footer(text=f"v{settings.app.version}")
        return embed


class _RangeSelect(discord.ui.Select):
    def __init__(self, parent: StatsDashboardView, current: str) -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(label=label, value=k, default=(k == current))
            for k, label in _RANGE_LABELS.items()
        ]
        super().__init__(
            placeholder="Χρονικό παράθυρο…",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.current_range = self.values[0]
        # Update default selection
        for opt in self.options:
            opt.default = (opt.value == self.parent_view.current_range)
        embed = await self.parent_view.build_embed(interaction.client)
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AiAssistantCog(bot))
