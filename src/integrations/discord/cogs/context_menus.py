"""Context menu (right-click) commands.

Phase B5:
  • Message → "Αρχειοθέτηση συνημμένου"
        Board-only.  Right-click any message with a file attachment → run the
        ArchiveWorkflow on it.  Saves the "download → /archive submit →
        re-attach" round-trip when a board member spots an archivable doc
        already shared in Discord.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from src.config import settings
from src.integrations.discord.brand import AMNESTY_YELLOW, brand_embed, fmt_ts
from src.integrations.discord.cogs.archive import (
    ArchiveActionView,
    _is_board_member,
)
from src.integrations.discord.constants import STATE_TEST_MODE_ACTIVE
from src.integrations.discord.state import BotStateStore

logger = logging.getLogger(__name__)


class ContextMenusCog(commands.Cog):
    """Right-click context menus."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._archive_message = app_commands.ContextMenu(
            name="Αρχειοθέτηση συνημμένου",
            callback=self._cm_archive_message,
        )

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self._archive_message)
        logger.info("ContextMenusCog loaded — message context menu registered")

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(
            self._archive_message.name, type=self._archive_message.type,
        )

    # ── X1: Message → Αρχειοθέτηση συνημμένου ────────────────────────────────

    async def _cm_archive_message(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        if not _is_board_member(interaction.user):
            await interaction.response.send_message(
                "Μόνο μέλη ΔΣ μπορούν να αρχειοθετήσουν.", ephemeral=True,
            )
            return

        if not message.attachments:
            await interaction.response.send_message(
                "Το μήνυμα δεν έχει συνημμένο.", ephemeral=True,
            )
            return

        # Look for an archivable attachment — accept anything the converter
        # supports (PDF, DOCX, ODT, RTF, JPG, PNG, etc.).
        import re as _re
        accepted = _re.compile(
            r"\.(pdf|docx?|odt|rtf|xlsx?|ods|csv|pptx?|odp|jpe?g|png|bmp|tiff?|gif|heif|heic)$",
            _re.IGNORECASE,
        )
        candidates = [a for a in message.attachments if accepted.search(a.filename)]
        if not candidates:
            await interaction.response.send_message(
                "Το μήνυμα δεν έχει αρχειοθετήσιμο συνημμένο (PDF/DOCX/εικόνα).",
                ephemeral=True,
            )
            return
        if len(candidates) > 1:
            await interaction.response.send_message(
                f"Το μήνυμα έχει {len(candidates)} συνημμένα — αρχειοθετούμε ένα τη φορά. "
                "Παρακαλώ προωθήστε το επιθυμητό σε νέο μήνυμα.",
                ephemeral=True,
            )
            return

        attachment = candidates[0]
        await interaction.response.defer(ephemeral=True, thinking=True)

        tmpdir = Path(tempfile.mkdtemp(prefix="discord_ctxmenu_archive_"))
        dest = tmpdir / attachment.filename
        try:
            await attachment.save(dest)
        except Exception as exc:
            await interaction.followup.send(
                f"❌ Αποτυχία λήψης συνημμένου: {exc}", ephemeral=True,
            )
            return

        state_store = BotStateStore()
        admin_test_mode = await state_store.get_bool(STATE_TEST_MODE_ACTIVE, default=False)

        initial_data: dict = {
            "pdf_path": str(dest.resolve()),
            "sender_email": f"discord:{interaction.user.id}",
            "sender_name": getattr(interaction.user, "display_name", ""),
            "test_mode": admin_test_mode,
            "_source": "discord_context_menu",
            "_discord_message_id": str(message.id),
            "_discord_channel_id": str(message.channel.id),
            "_discord_user_id": str(interaction.user.id),
        }

        from src.workflows.archive import ArchiveWorkflow
        wf = ArchiveWorkflow(actor=f"discord:{interaction.user.id}")
        try:
            result = await wf.run(initial_data)
        except Exception as exc:
            logger.exception("Context menu archive failed: %s", exc)
            await interaction.followup.send(
                f"❌ Σφάλμα κατά την αρχειοθέτηση: {exc}", ephemeral=True,
            )
            return

        if result.get("status") != "completed":
            pending = (wf.context or {}).get("pending_reservation_confirmation")
            if pending:
                await interaction.followup.send(
                    f"📥 Ο αρ.πρωτ. `{pending.get('protocol_number')}` είναι "
                    f"δεσμευμένος από τη Γραμματεία — αναμονή επιβεβαίωσης.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"❌ Δεν ολοκληρώθηκε: {result.get('error', 'unknown')}",
                    ephemeral=True,
                )
            return

        # Success — same embed shape as /archive submit
        ctx = wf.context
        llm = ctx.get("llm_result") or {}
        embed = brand_embed(
            title="Αρχειοθέτηση Ολοκληρώθηκε",
            description=(
                ("**[TEST MODE]**\n\n" if admin_test_mode else "")
                + f"Αρχειοθετήθηκε από το μήνυμα του/της {message.author.mention}."
            ),
            color=AMNESTY_YELLOW,
        )
        embed.add_field(name="Αρ. Πρωτ.", value=f"`{ctx.get('protocol_number', '?')}`", inline=True)
        embed.add_field(name="Τίτλος", value=llm.get("title", "?"), inline=True)
        embed.add_field(name="Ετικέτες", value=", ".join(llm.get("labels", [])) or "—", inline=False)

        from datetime import datetime as _dt
        revision_until = ctx.get("revision_open_until", "")
        if revision_until:
            try:
                deadline = _dt.fromisoformat(revision_until)
                embed.add_field(
                    name="Παράθυρο Αναθεώρησης",
                    value=f"έως {fmt_ts(deadline, 'R')}",
                    inline=False,
                )
            except Exception:
                pass
        embed.set_footer(text=f"Workflow ID: {wf.workflow_id}")

        view = ArchiveActionView(workflow_id=wf.workflow_id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ContextMenusCog(bot))
