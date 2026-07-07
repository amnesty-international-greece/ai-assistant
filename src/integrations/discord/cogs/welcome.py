"""Welcome cog - DM new members with a brand-aware introduction (M1).

Tone: semi-formal (per user spec).  Links Καταστατικό + Εσωτερικοί Κανονισμοί
when their URLs are configured in ``settings.urls.*``; otherwise the link
fields are omitted (no broken-link risk).
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from src.config import settings
from src.core.audit import log_action
from src.integrations.discord.brand import AMNESTY_YELLOW, brand_embed

logger = logging.getLogger(__name__)


class WelcomeCog(commands.Cog):
    """DM a welcome message to every new member."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        # Honor the global test_mode toggle: redirect DMs to a single test
        # recipient when the test_admin_channel is configured.  (We don't yet
        # have a "test user" setting, so for now test-mode just skips welcome.)
        if member.bot:
            return

        # Build the welcome embed
        candle_emoji = ""
        try:
            app_emojis = getattr(self.bot, "app_emojis", {}) or {}
            if "amnesty" in app_emojis:
                e = app_emojis["amnesty"]
                candle_emoji = f"<:{e.name}:{e.id}> "
        except Exception:
            pass

        embed = brand_embed(
            title=f"{candle_emoji}Καλώς ήρθες στην Αμνηστία",
            description=(
                f"Καλώς ήρθες, {member.mention}, στον Discord server της "
                "**Διεθνούς Αμνηστίας - Ελληνικό Τμήμα**.\n\n"
                "Ο server είναι ο χώρος επικοινωνίας και συντονισμού των μελών μας."
            ),
            color=AMNESTY_YELLOW,
        )

        # Useful links - only included when configured
        link_lines: list[str] = []
        if settings.urls.katastatiko:
            link_lines.append(f"• [Καταστατικό]({settings.urls.katastatiko})")
        if settings.urls.esoterikoi_kanonismoi:
            link_lines.append(f"• [Εσωτερικοί Κανονισμοί]({settings.urls.esoterikoi_kanonismoi})")
        if settings.urls.website:
            link_lines.append(f"• [Ιστοσελίδα]({settings.urls.website})")
        if link_lines:
            embed.add_field(
                name="Χρήσιμοι σύνδεσμοι",
                value="\n".join(link_lines),
                inline=False,
            )

        embed.add_field(
            name="Επόμενα βήματα",
            value=(
                "• Ρίξε μια ματιά στα διαθέσιμα κανάλια.\n"
                "• Αν είσαι μέλος συγκεκριμένης ομάδας, ζήτησε από τον Συντονιστή "
                "να σε προσθέσει με την εντολή `/team add`.\n"
                "• Στείλε `/ai-assistant about` για να δεις τι κάνει αυτό το bot."
            ),
            inline=False,
        )
        embed.set_footer(text="Διεθνής Αμνηστία - Ελληνικό Τμήμα")

        try:
            dm = await member.create_dm()
            await dm.send(embed=embed)
            log_action(
                workflow="discord.welcome",
                action="welcome_dm_sent",
                actor="system",
                target=str(member.id),
            )
            logger.info("Welcome DM sent to %s (id=%s)", member.display_name, member.id)
        except discord.Forbidden:
            logger.info(
                "Could not DM new member %s (DMs disabled). Skipping welcome.",
                member.display_name,
            )
            log_action(
                workflow="discord.welcome",
                action="welcome_dm_blocked",
                actor="system",
                target=str(member.id),
                status="rejected",
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Welcome DM failed for %s: %s", member.display_name, exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeCog(bot))
