"""Tests for PlatformBridgeCog.on_message - Discord→email bridge.

Covers:
  1. Bot messages are ignored (loop prevention)
  2. Non-board thread messages are ignored
  3. Happy path: message forwarded with attribution header
  4. Attachments appear as plain-text links in the forwarded body
  5. Missing email_thread_anchor → warning posted, no email sent
  6. EVENT_BOARD_EMAIL_SENT published after successful send (loop prevention)
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.event_bus import bus
from src.core.events import EVENT_BOARD_EMAIL_SENT
from src.integrations.discord.scheduler import WorkflowResourcesStore


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_cog(bot):
    """Construct PlatformBridgeCog with mocked stores (bypasses cog_load)."""
    from src.integrations.discord.cogs.platform_bridge import PlatformBridgeCog
    cog = PlatformBridgeCog(bot)
    cog._resources_store = WorkflowResourcesStore()
    cog._pending_store = MagicMock()
    return cog


def _make_bot():
    bot = MagicMock()
    # bot.user is compared against message.author; give it a stable identity
    bot.user = MagicMock()
    bot.user.bot = True
    return bot


def _make_thread_message(
    *,
    bot,
    author_name: str = "Γιώργος",
    content: str = "Καλημέρα",
    thread_id: int = 111222333,
    guild=True,
    is_bot_author: bool = False,
    attachments=None,
    embeds=None,
):
    """Build a minimal mock discord.Message in a Thread."""
    import discord

    msg = MagicMock()
    msg.guild = MagicMock() if guild else None

    # Author
    author = MagicMock()
    author.bot = is_bot_author
    author.display_name = author_name
    if is_bot_author:
        msg.author = bot.user
    else:
        msg.author = author

    # Channel - must look like a discord.Thread
    channel = MagicMock(spec=discord.Thread)
    channel.id = thread_id
    channel.send = AsyncMock()
    msg.channel = channel

    msg.content = content
    msg.attachments = attachments or []
    msg.embeds = embeds or []
    msg.add_reaction = AsyncMock()
    return msg


def _seed_thread_resource(conn, workflow_id: str, thread_id: str):
    """Insert a discord_workflow_resources row for a tracked board thread."""
    conn.execute(
        """INSERT INTO discord_workflow_resources
               (workflow_id, resource_type, discord_id, channel_id, created_at)
           VALUES (?, 'thread_board', ?, NULL, datetime('now'))""",
        (workflow_id, thread_id),
    )
    conn.commit()


def _seed_workflow_state(conn, workflow_id: str, raw_meeting_id: str, anchor: str | None):
    """Insert a workflow_state row with an optional email_thread_anchor."""
    context: dict = {"raw_meeting_id": raw_meeting_id}
    if anchor is not None:
        context["email_thread_anchor"] = anchor
    data = json.dumps({"context": context})
    conn.execute(
        """INSERT INTO workflow_state (workflow_name, workflow_id, state, data)
           VALUES ('board_meeting_invitation', ?, 'completed', ?)""",
        (workflow_id, data),
    )
    conn.commit()


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bot_messages_are_ignored(in_memory_db):
    """When message.author == bot.user, no email is sent and no event published."""
    bus.clear()
    bot = _make_bot()
    cog = _make_cog(bot)
    msg = _make_thread_message(bot=bot, is_bot_author=True)

    events_received = []
    bus.subscribe(EVENT_BOARD_EMAIL_SENT, lambda p: events_received.append(p) or None)

    with patch("src.integrations.m365.mail.M365MailClient.send_reply") as mock_send:
        await cog.on_message(msg)

    mock_send.assert_not_called()
    assert events_received == []


@pytest.mark.asyncio
async def test_non_board_thread_messages_are_ignored(in_memory_db):
    """Messages in threads not tracked as thread_board are silently ignored."""
    bus.clear()
    bot = _make_bot()
    cog = _make_cog(bot)
    # No row in discord_workflow_resources → _find_meeting_id_for_thread returns None
    msg = _make_thread_message(bot=bot, thread_id=999888777)

    with patch("src.integrations.m365.mail.M365MailClient.send_reply") as mock_send:
        await cog.on_message(msg)

    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_board_thread_message_sends_email_with_attribution(in_memory_db):
    """Happy path: message from 'Γιώργος' produces a send_reply with correct header."""
    bus.clear()
    bot = _make_bot()
    cog = _make_cog(bot)

    THREAD_ID = "111222333"
    MEETING_ID = "board_meeting:ΔΣ05-2026"
    ANCHOR = "<anchor@amnesty.org.gr>"

    _seed_thread_resource(in_memory_db, MEETING_ID, THREAD_ID)
    _seed_workflow_state(in_memory_db, MEETING_ID, "ΔΣ05-2026", ANCHOR)

    msg = _make_thread_message(
        bot=bot,
        author_name="Γιώργος",
        content="Καλημέρα",
        thread_id=int(THREAD_ID),
    )

    mock_reply = AsyncMock(return_value="<reply@amnesty.org.gr>")
    with patch(
        "src.integrations.discord.cogs.platform_bridge.M365MailClient",
        autospec=True,
    ) as MockMailClient:
        MockMailClient.return_value.send_reply = mock_reply
        await cog.on_message(msg)

    mock_reply.assert_awaited_once()
    call_kwargs = mock_reply.call_args.kwargs
    assert call_kwargs["parent_internet_message_id"] == ANCHOR
    assert call_kwargs["html"] is False
    assert call_kwargs["to"] == "board@amnesty.org.gr"
    body: str = call_kwargs["body"]
    assert body.startswith("[Γιώργος via Discord]")
    assert "Καλημέρα" in body


@pytest.mark.asyncio
async def test_attachments_appear_as_links_in_body(in_memory_db):
    """Attachments are listed as plain-text links, not downloaded."""
    bus.clear()
    bot = _make_bot()
    cog = _make_cog(bot)

    THREAD_ID = "444555666"
    MEETING_ID = "board_meeting:ΔΣ06-2026"
    ANCHOR = "<anchor2@amnesty.org.gr>"

    _seed_thread_resource(in_memory_db, MEETING_ID, THREAD_ID)
    _seed_workflow_state(in_memory_db, MEETING_ID, "ΔΣ06-2026", ANCHOR)

    att = MagicMock()
    att.filename = "agenda.pdf"
    att.url = "https://cdn.discordapp.com/attachments/agenda.pdf"

    msg = _make_thread_message(
        bot=bot,
        thread_id=int(THREAD_ID),
        content="Βλέπετε το συνημμένο",
        attachments=[att],
    )

    mock_reply = AsyncMock(return_value="<r2@amnesty.org.gr>")
    with patch(
        "src.integrations.discord.cogs.platform_bridge.M365MailClient",
        autospec=True,
    ) as MockMailClient:
        MockMailClient.return_value.send_reply = mock_reply
        await cog.on_message(msg)

    body: str = mock_reply.call_args.kwargs["body"]
    assert "Attachments:" in body
    assert "  - agenda.pdf: https://cdn.discordapp.com/attachments/agenda.pdf" in body


@pytest.mark.asyncio
async def test_missing_email_anchor_warns_and_skips(in_memory_db):
    """When workflow state has no email_thread_anchor, no email is sent and
    a warning is posted back to the Discord thread."""
    bus.clear()
    bot = _make_bot()
    cog = _make_cog(bot)

    THREAD_ID = "777888999"
    MEETING_ID = "board_meeting:ΔΣ07-2026"

    _seed_thread_resource(in_memory_db, MEETING_ID, THREAD_ID)
    # No anchor: pass None
    _seed_workflow_state(in_memory_db, MEETING_ID, "ΔΣ07-2026", anchor=None)

    msg = _make_thread_message(bot=bot, thread_id=int(THREAD_ID), content="Γεια")

    with patch(
        "src.integrations.discord.cogs.platform_bridge.M365MailClient",
        autospec=True,
    ) as MockMailClient:
        MockMailClient.return_value.send_reply = AsyncMock()
        await cog.on_message(msg)

    MockMailClient.return_value.send_reply.assert_not_awaited()
    # A warning should have been posted back to the thread
    msg.channel.send.assert_awaited_once()
    warn_text: str = msg.channel.send.call_args.args[0]
    assert "anchor" in warn_text.lower() or "anchor" in warn_text


@pytest.mark.asyncio
async def test_publishes_email_sent_event_for_loop_prevention(in_memory_db):
    """After a successful send, EVENT_BOARD_EMAIL_SENT is published with kind='discord_bridge'."""
    bus.clear()
    bot = _make_bot()
    cog = _make_cog(bot)

    THREAD_ID = "100200300"
    MEETING_ID = "board_meeting:ΔΣ08-2026"
    ANCHOR = "<anchor3@amnesty.org.gr>"

    _seed_thread_resource(in_memory_db, MEETING_ID, THREAD_ID)
    _seed_workflow_state(in_memory_db, MEETING_ID, "ΔΣ08-2026", ANCHOR)

    msg = _make_thread_message(
        bot=bot,
        thread_id=int(THREAD_ID),
        content="Τεστ",
    )

    events_received = []

    async def _capture(payload):
        events_received.append(payload)

    bus.subscribe(EVENT_BOARD_EMAIL_SENT, _capture)

    mock_reply = AsyncMock(return_value="<r3@amnesty.org.gr>")
    with patch(
        "src.integrations.discord.cogs.platform_bridge.M365MailClient",
        autospec=True,
    ) as MockMailClient:
        MockMailClient.return_value.send_reply = mock_reply
        await cog.on_message(msg)

    assert len(events_received) == 1
    evt = events_received[0]
    assert evt.kind == "discord_bridge"
    assert evt.meeting_id == MEETING_ID
    assert evt.meeting_ref == "ΔΣ08-2026"
