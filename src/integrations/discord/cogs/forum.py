"""/forum cog — forum routing & channel configuration (B6 full implementation).

Replaces:
  • /discord-admin add-channel         → button "Add" inside /forum channels
  • /discord-admin remove-channel      → button "Remove" per row
  • /discord-admin classify-toggle     → /forum auto-classify
  • (planned) /discord-admin list-channels / update-channel → all rolled in

Single entry point ``/forum channels`` opens an interactive embed:
  • A table of every configured routing row (channel mention + auto-derived tag)
  • "➕ Νέο κανάλι" button → opens a ChannelSelect view
  • Per-row "🗑️ Remove" + "ℹ️ Details" buttons (when ≤6 rows)
  • SelectMenu fallback when there are >6 rows
"""
from __future__ import annotations

import logging
from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands

from src.config import settings
from src.core.audit import log_action
from src.core.email_templates import greek_upper
from src.integrations.discord.brand import AMNESTY_YELLOW, brand_embed
from src.integrations.discord.constants import STATE_AUTO_CLASSIFY, STATE_TEST_MODE_ACTIVE, WORKFLOW_NAME
from src.integrations.discord.state import BotStateStore, EnabledChannelsStore

logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _auto_tag(bot: commands.Bot, channel_id: str) -> str:
    """Derive the routing tag from the Discord channel name via greek_upper.

    Falls back to '—' if the channel can't be resolved.
    """
    try:
        ch = bot.get_channel(int(channel_id))
        if ch is not None:
            return greek_upper(ch.name)
    except Exception:
        pass
    return "—"


# ── Add-channel select view ──────────────────────────────────────────────────


class _AddChannelSelect(discord.ui.ChannelSelect):
    """ChannelSelect restricted to forum channels."""

    def __init__(self) -> None:
        super().__init__(
            channel_types=[discord.ChannelType.forum],
            placeholder="Επιλέξτε forum κανάλι…",
            min_values=1,
            max_values=1,
            custom_id="forum:channels:add_select",
        )
        self._selected_channel_id: str | None = None

    async def callback(self, interaction: discord.Interaction) -> None:
        # Store the selection; the Προσθήκη button will act on it.
        self._selected_channel_id = str(self.values[0].id)
        await interaction.response.defer()


class AddChannelView(discord.ui.View):
    """View shown when the operator presses ➕ Νέο κανάλι.

    Contains a ChannelSelect for picking the forum channel and a
    Προσθήκη button to confirm.
    """

    def __init__(self, cog: "ForumCog") -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self._select = _AddChannelSelect()
        self.add_item(self._select)

        confirm_btn = discord.ui.Button(
            label="Προσθήκη",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id="forum:channels:add_confirm",
        )
        confirm_btn.callback = self._on_confirm
        self.add_item(confirm_btn)

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        channel_id = self._select._selected_channel_id
        if not channel_id:
            await interaction.response.send_message(
                "⚠️ Πρέπει πρώτα να επιλέξετε ένα κανάλι από το dropdown.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        test_mode = await self.cog._state_store.get_bool(STATE_TEST_MODE_ACTIVE, default=False)
        try:
            await self.cog._channels_store.add(
                channel_id,
                test_mode=test_mode,
                label="",
                classifier_keywords=[],
                forum_tag_ids=[],
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Αποτυχία: {exc}", ephemeral=True)
            return
        log_action(
            workflow=WORKFLOW_NAME,
            action="channel_added_via_discord",
            actor=str(interaction.user.id),
            target=channel_id,
            details={"label": ""},
        )
        # Refresh the table view
        view = ChannelTableView(self.cog)
        await view._populate()
        embed = await view.build_embed()
        await interaction.followup.send("✅ Προστέθηκε.", embed=embed, view=view, ephemeral=True)


# ── Table view ──────────────────────────────────────────────────────────────


# 10 rows per page: comfortably under Discord's 25-field embed limit, and
# the 10 per-page rows still fit either the inline-button branch (≤6 rows)
# or the Select-fallback (one Select holds up to 25 options).
_CHANNELS_PAGE_SIZE = 10


class ChannelTableView(discord.ui.View):
    """The main interactive table.

    Layout: Add button, optional Prev/Next pagination buttons, plus per-row
    Action buttons.  When the current page has >6 rows we fall back to a
    SelectMenu for picking which row to operate on.
    """

    def __init__(self, cog: "ForumCog", *, page: int = 0) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        # Page index is 0-based; clamped to a valid value inside _populate()
        # once we know the row count.
        self.page = max(0, page)
        # Add button always present
        add_btn = discord.ui.Button(
            label="Νέο κανάλι", style=discord.ButtonStyle.success, emoji="➕",
            custom_id="forum:channels:add",
        )
        add_btn.callback = self._on_add
        self.add_item(add_btn)

    def _slice(self, rows: list) -> tuple[list, int]:
        """Return (rows_on_current_page, total_pages).

        Also clamps ``self.page`` if it overshot (e.g. the last row of the
        last page was just removed).
        """
        total_pages = max(1, (len(rows) + _CHANNELS_PAGE_SIZE - 1) // _CHANNELS_PAGE_SIZE)
        if self.page >= total_pages:
            self.page = total_pages - 1
        start = self.page * _CHANNELS_PAGE_SIZE
        return rows[start : start + _CHANNELS_PAGE_SIZE], total_pages

    async def _populate(self) -> None:
        """Populate per-row Remove / Details buttons (or fallback Select).

        Operates on the **current page's** rows only — the prev/next buttons
        re-render the view with a different page slice.
        With 2 buttons per row, the threshold for inline buttons is raised to 6:
        6 rows × 2 buttons = 12 components, safely under Discord's 25-component
        cap with 3 chrome buttons (Add + Prev + Next).
        """
        test_mode = await self.cog._state_store.get_bool(STATE_TEST_MODE_ACTIVE, default=False)
        all_rows = await self.cog._channels_store.list(test_mode=test_mode)
        page_rows, total_pages = self._slice(all_rows)

        # Pagination chrome — only shown when there's more than one page.
        if total_pages > 1:
            prev_btn = discord.ui.Button(
                label="‹ Προηγούμενη",
                style=discord.ButtonStyle.secondary,
                custom_id=f"forum:channels:prev:{self.page}",
                disabled=(self.page == 0),
            )
            prev_btn.callback = self._on_prev
            next_btn = discord.ui.Button(
                label="Επόμενη ›",
                style=discord.ButtonStyle.secondary,
                custom_id=f"forum:channels:next:{self.page}",
                disabled=(self.page >= total_pages - 1),
            )
            next_btn.callback = self._on_next
            self.add_item(prev_btn)
            self.add_item(next_btn)

        if len(page_rows) <= 6:  # ≤6 × 2 buttons = ≤12 components
            for r in page_rows:
                self.add_item(_RowRemoveButton(self.cog, r))
                self.add_item(_RowDetailsButton(self.cog, r))
        else:
            # Pick-then-act: one Select listing the page's rows, then an
            # action picker mini-view.
            self.add_item(_RowSelect(self.cog, page_rows))

    async def build_embed(self) -> discord.Embed:
        """Render the table embed (current page only)."""
        test_mode = await self.cog._state_store.get_bool(STATE_TEST_MODE_ACTIVE, default=False)
        all_rows = await self.cog._channels_store.list(test_mode=test_mode)
        page_rows, total_pages = self._slice(all_rows)
        embed = brand_embed(
            title="Forum Channels — Δρομολόγηση Email",
            description=(
                f"Συνολικά: **{len(all_rows)}** κανάλια "
                f"({'TEST mode' if test_mode else 'production'})"
            ),
            color=AMNESTY_YELLOW,
        )
        if not all_rows:
            embed.add_field(
                name="(κανένα ρυθμισμένο κανάλι)",
                value="Πάτησε **➕ Νέο κανάλι** για να ξεκινήσεις.",
                inline=False,
            )
            return embed
        for r in page_rows:
            tag = _auto_tag(self.cog.bot, r.channel_id)
            embed.add_field(
                name=f"<#{r.channel_id}>",
                value=f"tag: `{tag}`",
                inline=False,
            )
        if total_pages > 1:
            embed.set_footer(text=f"Σελίδα {self.page + 1}/{total_pages}")
        return embed

    async def _on_add(self, interaction: discord.Interaction) -> None:
        view = AddChannelView(self.cog)
        await interaction.response.send_message(
            "Επιλέξτε forum κανάλι για προσθήκη:",
            view=view,
            ephemeral=True,
        )

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        view = ChannelTableView(self.cog, page=self.page - 1)
        await view._populate()
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        view = ChannelTableView(self.cog, page=self.page + 1)
        await view._populate()
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class _RowRemoveButton(discord.ui.Button):
    def __init__(self, cog: "ForumCog", row) -> None:
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="🗑️",
            custom_id=f"forum:channels:remove:{row.channel_id}",
        )
        self.cog = cog
        self.row = row

    async def callback(self, interaction: discord.Interaction) -> None:
        test_mode = await self.cog._state_store.get_bool(STATE_TEST_MODE_ACTIVE, default=False)
        await self.cog._channels_store.remove(self.row.channel_id, test_mode=test_mode)
        log_action(
            workflow=WORKFLOW_NAME,
            action="channel_removed_via_discord",
            actor=str(interaction.user.id),
            target=self.row.channel_id,
        )
        view = ChannelTableView(self.cog)
        await view._populate()
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class _RowDetailsButton(discord.ui.Button):
    def __init__(self, cog: "ForumCog", row) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="ℹ️",
            custom_id=f"forum:channels:details:{row.channel_id}",
        )
        self.cog = cog
        self.row = row

    async def callback(self, interaction: discord.Interaction) -> None:
        tag = _auto_tag(self.cog.bot, self.row.channel_id)
        # Count recent routed messages from discord_stats
        try:
            from src.core.audit import _get_connection
            conn = _get_connection()
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM discord_stats "
                "WHERE channel_id = ? AND datetime(timestamp) > datetime('now', '-30 days')",
                (self.row.channel_id,),
            ).fetchone()
            recent = int(row["cnt"]) if row else 0
        except Exception:
            recent = 0

        embed = brand_embed(
            title=f"Λεπτομέρειες — <#{self.row.channel_id}>",
            color=AMNESTY_YELLOW,
        )
        embed.add_field(name="Κανάλι", value=f"<#{self.row.channel_id}>", inline=False)
        embed.add_field(name="Routing tag", value=f"`{tag}`", inline=True)
        embed.add_field(name="Test mode", value="✅" if self.row.test_mode else "—", inline=True)
        embed.add_field(name="Δρομολογήσεις (30d)", value=str(recent), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class _RowSelect(discord.ui.Select):
    """Fallback when there are >6 rows — pick row first, then action."""

    def __init__(self, cog: "ForumCog", rows: list) -> None:
        self.cog = cog
        def _label(r) -> str:
            try:
                ch = cog.bot.get_channel(int(r.channel_id))
                if ch is not None:
                    return ch.name[:100]
            except Exception:
                pass
            return r.channel_id

        options = [
            discord.SelectOption(
                label=_label(r),
                value=r.channel_id,
                description=f"tag: {_auto_tag(cog.bot, r.channel_id)}"[:100],
            )
            for r in rows[:25]
        ]
        super().__init__(
            placeholder="Επιλέξτε κανάλι για ενέργεια…",
            min_values=1, max_values=1, options=options,
            custom_id="forum:channels:row_select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        # Show row-action mini-view (Remove / Details)
        test_mode = await self.cog._state_store.get_bool(STATE_TEST_MODE_ACTIVE, default=False)
        row = await self.cog._channels_store.get(self.values[0], test_mode=test_mode)
        if not row:
            await interaction.response.send_message("Δεν βρέθηκε.", ephemeral=True)
            return
        mini = discord.ui.View(timeout=300)
        mini.add_item(_RowRemoveButton(self.cog, row))
        mini.add_item(_RowDetailsButton(self.cog, row))
        tag = _auto_tag(self.cog.bot, row.channel_id)
        await interaction.response.send_message(
            f"Επιλεγμένο κανάλι: <#{row.channel_id}> (`{tag}`)",
            view=mini, ephemeral=True,
        )


# ── /forum cog ──────────────────────────────────────────────────────────────


class ForumCog(commands.Cog):
    """`/forum` slash command group — admin-only."""

    class _ForumCommands(app_commands.Group):
        def __init__(self, cog: "ForumCog") -> None:
            super().__init__(
                name="forum",
                description="Ρυθμίσεις forum καναλιών & email→forum δρομολόγησης",
                default_permissions=discord.Permissions(administrator=True),
            )
            self.cog = cog

        @app_commands.command(name="channels", description="Διαχείριση ρυθμισμένων forum καναλιών")
        async def cmd_channels(self, interaction: discord.Interaction) -> None:
            view = ChannelTableView(self.cog)
            await view._populate()
            embed = await view.build_embed()
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        @app_commands.command(name="auto-classify", description="Ενεργοποίηση/απενεργοποίηση αυτόματης δρομολόγησης")
        @app_commands.describe(value="on ή off")
        @app_commands.choices(value=[
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
        ])
        async def cmd_auto_classify(self, interaction: discord.Interaction, value: str) -> None:
            enabled = value == "on"
            await self.cog._state_store.set_bool(STATE_AUTO_CLASSIFY, enabled)
            log_action(
                workflow=WORKFLOW_NAME,
                action="auto_classify_toggled",
                actor=str(interaction.user.id),
                details={"value": enabled},
            )
            await interaction.response.send_message(
                f"Auto-classify is now **{'ON' if enabled else 'OFF'}**.",
                ephemeral=True,
            )

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot
        self._state_store: BotStateStore = BotStateStore()
        self._channels_store: EnabledChannelsStore = EnabledChannelsStore()
        self._commands = self._ForumCommands(cog=self)

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self._commands)
        logger.info("ForumCog loaded — /forum group registered")

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command("forum")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ForumCog(bot))
