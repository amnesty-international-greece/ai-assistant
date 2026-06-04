"""Single home for every Discord *post* the platform emits.

This package is the design surface: each function here is a **pure builder**
that takes plain data and returns a ready-to-send ``discord.Embed`` (and, when
the post has buttons, a ``discord.ui.View``).  No I/O, no bus, no Discord
fetching — just the visual/structural definition of each message.

Hand this whole folder to a designer: editing copy, colours, field order,
emoji, and button labels here changes the live posts without touching any
workflow or handler logic.

Domains:
    board_meeting  — the ΔΣ meeting lifecycle (thread open → scheduling →
                     invitation → reminder → minutes → cancellation).
    egkyklios      — member-facing circular announcements.

Each builder returns one of:
    * ``discord.Embed``                              (no interactive buttons)
    * ``tuple[discord.Embed, discord.ui.View|None]`` (may carry link buttons)

The cogs import these and handle only the *delivery* (which channel/thread,
resource tracking, audit logging).
"""
from __future__ import annotations

from src.integrations.discord.embeds.board_meeting import (
    board_thread_opened_embed,
    cancellation_embed,
    invitation_mirror_embed,
    milestone_published_embed,
    minutes_mirror_embed,
    minutes_shared_embed,
    public_invitation_embed,
    reminder_embed,
    scheduling_mirror_embed,
)
from src.integrations.discord.embeds.egkyklios import egkyklios_published_embed

__all__ = [
    # board_meeting
    "board_thread_opened_embed",
    "scheduling_mirror_embed",
    "invitation_mirror_embed",
    "minutes_mirror_embed",
    "public_invitation_embed",
    "milestone_published_embed",
    "cancellation_embed",
    "reminder_embed",
    "minutes_shared_embed",
    # egkyklios
    "egkyklios_published_embed",
]
