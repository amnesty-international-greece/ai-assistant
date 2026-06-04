"""Εγκύκλιος (member-facing circular) announcement posts."""
from __future__ import annotations

import discord

from src.integrations.discord.brand import AMNESTY_YELLOW, brand_embed

# Greek display names per circular kind.  Add new kinds here.
_KIND_DISPLAY = {
    "general": "Γενική Εγκύκλιος Ενημέρωσης",
    "special": "Ειδική Εγκύκλιος",
}


def egkyklios_kind_display(kind: str) -> str:
    """Human-readable Greek label for an εγκύκλιος kind (safe default)."""
    return _KIND_DISPLAY.get(kind or "", "Εγκύκλιος Ενημέρωσης")


def egkyklios_published_embed(
    *,
    kind: str,
    title: str,
    protocol_number: str = "",
    sent_at: str = "",
    sharepoint_url: str = "",
) -> tuple[discord.Embed, discord.ui.View | None]:
    """📄 Announcement that a new circular has been published to members."""
    kind_display = egkyklios_kind_display(kind)
    embed = brand_embed(
        title=f"📄 Νέα {kind_display}",
        description=f"**{title}**",
        color=AMNESTY_YELLOW,
    )
    if protocol_number:
        embed.add_field(name="Αρ. Πρωτ.", value=f"`{protocol_number}`", inline=True)
    if sent_at:
        embed.add_field(name="Δημοσιεύθηκε", value=sent_at[:19], inline=True)

    view: discord.ui.View | None = None
    if sharepoint_url:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="Κατεβάστε την Εγκύκλιο",
            style=discord.ButtonStyle.link,
            url=sharepoint_url,
            emoji="📄",
        ))
    return embed, view
