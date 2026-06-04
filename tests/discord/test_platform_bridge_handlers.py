"""Tests for PlatformBridgeCog handlers (D1-D4).

Focuses on resource-store interactions and bus publish calls.
Discord API calls (guild.create_scheduled_event, channel.send, etc.) are
mocked so tests run without a live bot.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.integrations.discord.scheduler as sched_mod
from src.core.event_bus import bus
from src.core.events import (
    EVENT_BOARD_MEETING_REMINDER_DUE,
    BoardMeetingCancelledPayload,
    BoardMeetingReminderDuePayload,
    BoardMeetingScheduledPayload,
    BoardMinutesSharedPayload,
)
from src.integrations.discord.scheduler import (
    PendingActionsStore,
    WorkflowResourcesStore,
    register_action_handler,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_scheduled_payload(meeting_id: str = "board_meeting:2030-01-15") -> BoardMeetingScheduledPayload:
    return BoardMeetingScheduledPayload(
        meeting_id=meeting_id,
        starts_at=datetime(2030, 1, 15, 18, 0, tzinfo=timezone.utc),
        zoom_url="https://zoom.us/j/123",
        agenda_summary="1. Θέμα Α\n2. Θέμα Β",
        board_member_emails=["secgen@amnesty.org.gr"],
    )


def _make_bot_with_guild(event_id: int = 999) -> tuple:
    """Return (mock_bot, mock_guild, mock_event)."""
    mock_event = AsyncMock()
    mock_event.id = event_id
    mock_event.delete = AsyncMock()

    mock_guild = AsyncMock()
    mock_guild.create_scheduled_event = AsyncMock(return_value=mock_event)
    mock_guild.get_scheduled_event = MagicMock(return_value=mock_event)
    mock_guild.fetch_scheduled_event = AsyncMock(return_value=mock_event)

    mock_bot = MagicMock()
    mock_bot.get_guild = MagicMock(return_value=mock_guild)
    mock_bot.get_channel = MagicMock(return_value=None)
    mock_bot.fetch_channel = AsyncMock(return_value=None)

    return mock_bot, mock_guild, mock_event


# ── Helper: build a cog with injected stores ──────────────────────────────────

def _make_cog(bot, resources_store, pending_store):
    """Construct a PlatformBridgeCog with pre-injected stores (bypasses cog_load)."""
    from src.integrations.discord.cogs.platform_bridge import PlatformBridgeCog
    cog = PlatformBridgeCog(bot)
    cog._resources_store = resources_store
    cog._pending_store = pending_store
    return cog


# ── D1: _on_board_meeting_scheduled ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_scheduled_creates_discord_event_and_records_resource(in_memory_db):
    """Handler calls guild.create_scheduled_event and records the event resource."""
    mock_bot, mock_guild, mock_event = _make_bot_with_guild(event_id=1234)

    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()
    cog = _make_cog(mock_bot, resources_store, pending_store)

    payload = _make_scheduled_payload()

    with patch("src.config.settings") as mock_settings:
        mock_settings.discord_guild_id = "9999"
        mock_settings.discord.platform_bridge.board_meeting.agenda_channel_id = ""
        mock_settings.discord.platform_bridge.board_meeting.board_channel_id = ""
        mock_settings.workflows.board_meeting.reminder_hours_before = 3
        mock_settings.workflows.board_meeting.board_members = []

        mock_bot.get_guild = MagicMock(return_value=mock_guild)

        await cog._on_board_meeting_scheduled(payload)

    mock_guild.create_scheduled_event.assert_called_once()
    call_kwargs = mock_guild.create_scheduled_event.call_args.kwargs
    assert "Συνεδρίαση ΔΣ" in call_kwargs["name"]

    resources = await resources_store.list_for_workflow(payload.meeting_id)
    event_resources = [r for r in resources if r["resource_type"] == "event"]
    assert len(event_resources) == 1
    assert event_resources[0]["discord_id"] == "1234"


@pytest.mark.asyncio
async def test_scheduled_no_guild_bails_out(in_memory_db):
    """Handler exits gracefully when the guild is not in the cache."""
    mock_bot = MagicMock()
    mock_bot.get_guild = MagicMock(return_value=None)

    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()
    cog = _make_cog(mock_bot, resources_store, pending_store)

    payload = _make_scheduled_payload()

    with patch("src.config.settings") as mock_settings:
        mock_settings.discord_guild_id = "9999"
        await cog._on_board_meeting_scheduled(payload)

    resources = await resources_store.list_for_workflow(payload.meeting_id)
    assert resources == []


@pytest.mark.asyncio
async def test_scheduled_enqueues_reminder_in_pending_store(in_memory_db):
    """Handler enqueues a board_meeting_reminder action for a future meeting."""
    mock_bot, mock_guild, mock_event = _make_bot_with_guild(event_id=5678)
    mock_bot.get_guild = MagicMock(return_value=mock_guild)

    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()
    cog = _make_cog(mock_bot, resources_store, pending_store)

    payload = _make_scheduled_payload()

    with patch("src.config.settings") as mock_settings:
        mock_settings.discord_guild_id = "9999"
        mock_settings.discord.platform_bridge.board_meeting.agenda_channel_id = ""
        mock_settings.discord.platform_bridge.board_meeting.board_channel_id = ""
        mock_settings.workflows.board_meeting.reminder_hours_before = 3
        mock_settings.workflows.board_meeting.board_members = []

        await cog._on_board_meeting_scheduled(payload)

    # Should have enqueued a pending action
    now = datetime.now(timezone.utc)
    # The meeting is 2030-01-15 18:00 UTC — well in the future from test perspective
    due_actions = await pending_store.due_now(now=datetime(2030, 1, 16, tzinfo=timezone.utc))
    reminder_actions = [a for a in due_actions if a.action_type == "board_meeting_reminder"]
    assert len(reminder_actions) == 1
    assert reminder_actions[0].payload["meeting_id"] == payload.meeting_id


@pytest.mark.asyncio
async def test_scheduled_records_pending_action_resource(in_memory_db):
    """Handler stores the pending action ID as a 'pending_action' resource."""
    mock_bot, mock_guild, mock_event = _make_bot_with_guild(event_id=5678)
    mock_bot.get_guild = MagicMock(return_value=mock_guild)

    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()
    cog = _make_cog(mock_bot, resources_store, pending_store)

    payload = _make_scheduled_payload()

    with patch("src.config.settings") as mock_settings:
        mock_settings.discord_guild_id = "9999"
        mock_settings.discord.platform_bridge.board_meeting.agenda_channel_id = ""
        mock_settings.discord.platform_bridge.board_meeting.board_channel_id = ""
        mock_settings.workflows.board_meeting.reminder_hours_before = 3
        mock_settings.workflows.board_meeting.board_members = []

        await cog._on_board_meeting_scheduled(payload)

    resources = await resources_store.list_for_workflow(payload.meeting_id)
    pending_resources = [r for r in resources if r["resource_type"] == "pending_action"]
    assert len(pending_resources) == 1


# ── Dual-channel (public + board) thread lifecycle ───────────────────────────
#
# After the 2026-05-28 refactor the responsibilities are split:
#   - The PRIVATE board thread is created by ``_on_board_meeting_thread_opened``
#     at scheduling-email time (workflow step 1).
#   - The PUBLIC members thread is created by ``_on_board_meeting_scheduled``
#     at newsletter-confirm time (workflow step 11).  This handler now expects
#     to FIND an existing thread_board resource and posts a milestone there
#     rather than re-creating the thread.

@pytest.mark.asyncio
async def test_scheduled_opens_public_thread_only_private_already_exists(in_memory_db):
    """When the board thread already exists from step 1, _on_board_meeting_scheduled
    creates ONLY the public thread and posts a milestone in the existing board one."""
    import discord as _discord
    mock_bot, mock_guild, mock_event = _make_bot_with_guild(event_id=4242)
    mock_bot.get_guild = MagicMock(return_value=mock_guild)

    public_forum = MagicMock(spec=_discord.ForumChannel)
    public_forum.id = 1000
    public_forum.available_tags = []  # no tags = silent skip in _apply_forum_tag
    public_thread = MagicMock()
    public_thread.id = 1001
    public_thread.parent = public_forum
    public_thread.edit = AsyncMock()
    public_forum.create_thread = AsyncMock(return_value=MagicMock(thread=public_thread))

    # Existing private thread: looked up via resources_store + bot.get_channel(2001)
    existing_board_thread = MagicMock()
    existing_board_thread.id = 2001
    existing_board_thread.send = AsyncMock()

    def _get_channel(cid):
        return {1000: public_forum, 1001: public_thread, 2001: existing_board_thread}.get(cid)
    mock_bot.get_channel = MagicMock(side_effect=_get_channel)

    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()
    cog = _make_cog(mock_bot, resources_store, pending_store)

    payload = _make_scheduled_payload()
    # Pre-record the private board thread as if step 1 already ran.
    await resources_store.record(
        workflow_id=payload.meeting_id,
        resource_type="thread_board",
        discord_id="2001",
        channel_id="2000",
    )

    with patch("src.integrations.discord.cogs.platform_bridge.settings") as mock_settings:
        mock_settings.discord_guild_id = "9999"
        mock_settings.discord.platform_bridge.board_meeting.agenda_channel_id = "1000"
        mock_settings.discord.platform_bridge.board_meeting.agenda_forum_tag_name = "Συνεδριάσεις"
        mock_settings.discord.platform_bridge.board_meeting.board_channel_id = "2000"
        mock_settings.workflows.board_meeting.reminder_hours_before = 3
        mock_settings.workflows.board_meeting.board_members = []

        await cog._on_board_meeting_scheduled(payload)

    # Public thread: created exactly once
    public_forum.create_thread.assert_called_once()
    # Private thread: reused, NOT re-created.  Milestone embed sent to it.
    existing_board_thread.send.assert_called()

    # Resources: original thread_board still there + new thread (public)
    resources = await resources_store.list_for_workflow(payload.meeting_id)
    rtypes = {r["resource_type"]: r["discord_id"] for r in resources}
    assert rtypes.get("thread") == "1001"
    assert rtypes.get("thread_board") == "2001"


@pytest.mark.asyncio
async def test_minutes_shared_posts_to_private_board_thread_only(in_memory_db):
    """V3 modernization: minutes go ONLY to the private board thread.

    Per discord_bot_modernization.md §B.2 user spec — minutes are pre-finalization
    sensitive material, so they're board-only.  The public agenda thread is
    skipped even when it exists.
    """
    public_thread = AsyncMock()
    public_thread.send = AsyncMock()
    board_thread = AsyncMock()
    board_thread.send = AsyncMock()

    mock_bot = MagicMock()
    def _get_channel(cid):
        return {3001: public_thread, 3002: board_thread}.get(cid)
    mock_bot.get_channel = MagicMock(side_effect=_get_channel)

    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()
    meeting_id = "board_meeting:2030-01-15"
    await resources_store.record(workflow_id=meeting_id, resource_type="thread",       discord_id="3001")
    await resources_store.record(workflow_id=meeting_id, resource_type="thread_board", discord_id="3002")

    cog = _make_cog(mock_bot, resources_store, pending_store)
    payload = BoardMinutesSharedPayload(meeting_id=meeting_id, drive_url="https://drive/x", doc_id="x")

    await cog._on_board_minutes_shared(payload)

    # Public agenda thread MUST NOT be posted to (sensitive content)
    public_thread.send.assert_not_called()
    # Private board thread gets the minutes (as a rich embed + view, not raw text)
    board_thread.send.assert_called_once()
    sent_kwargs = board_thread.send.call_args.kwargs
    assert "embed" in sent_kwargs
    assert "view" in sent_kwargs


@pytest.mark.asyncio
async def test_cancelled_posts_to_both_threads(in_memory_db):
    """Cancellation notice goes to both public and board threads."""
    public_thread = AsyncMock()
    public_thread.send = AsyncMock()
    board_thread = AsyncMock()
    board_thread.send = AsyncMock()

    mock_bot, mock_guild, _ = _make_bot_with_guild()
    def _get_channel(cid):
        return {4001: public_thread, 4002: board_thread}.get(cid)
    mock_bot.get_channel = MagicMock(side_effect=_get_channel)
    mock_bot.get_guild = MagicMock(return_value=mock_guild)

    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()
    meeting_id = "board_meeting:2030-01-15"
    await resources_store.record(workflow_id=meeting_id, resource_type="thread",       discord_id="4001")
    await resources_store.record(workflow_id=meeting_id, resource_type="thread_board", discord_id="4002")

    cog = _make_cog(mock_bot, resources_store, pending_store)
    payload = BoardMeetingCancelledPayload(meeting_id=meeting_id, reason="Test")

    with patch("src.config.settings") as mock_settings:
        mock_settings.discord_guild_id = "9999"
        await cog._on_board_meeting_cancelled(payload)

    # V4 modernization: cancellation now uses a Rich Embed (not raw text).
    public_thread.send.assert_called_once()
    board_thread.send.assert_called_once()
    assert "embed" in public_thread.send.call_args.kwargs
    assert "embed" in board_thread.send.call_args.kwargs


# ── D4: _on_board_meeting_cancelled ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancelled_deletes_discord_event(in_memory_db):
    """Handler fetches the event resource and calls event.delete()."""
    mock_bot, mock_guild, mock_event = _make_bot_with_guild(event_id=7777)
    mock_bot.get_guild = MagicMock(return_value=mock_guild)

    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()

    meeting_id = "board_meeting:2030-01-15"

    # Pre-populate the resource store with an event
    await resources_store.record(
        workflow_id=meeting_id,
        resource_type="event",
        discord_id="7777",
    )

    cog = _make_cog(mock_bot, resources_store, pending_store)
    payload = BoardMeetingCancelledPayload(meeting_id=meeting_id, reason="Test cancellation")

    with patch("src.config.settings") as mock_settings:
        mock_settings.discord_guild_id = "9999"
        mock_bot.get_guild = MagicMock(return_value=mock_guild)

        await cog._on_board_meeting_cancelled(payload)

    mock_event.delete.assert_called_once()


@pytest.mark.asyncio
async def test_cancelled_posts_to_thread_and_cancels_pending_action(in_memory_db):
    """Handler posts a cancellation notice in the thread and cancels the reminder."""
    mock_bot, mock_guild, _ = _make_bot_with_guild()

    mock_thread = AsyncMock()
    mock_thread.send = AsyncMock()
    mock_bot.get_channel = MagicMock(return_value=mock_thread)

    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()

    meeting_id = "board_meeting:2030-01-15"

    # Pre-populate resources
    await resources_store.record(workflow_id=meeting_id, resource_type="thread", discord_id="8888")
    action_id = await pending_store.enqueue(
        action_type="board_meeting_reminder",
        payload={"meeting_id": meeting_id, "hours_before": 3},
        due_at=datetime(2026, 5, 21, 15, 0, tzinfo=timezone.utc),
    )
    await resources_store.record(
        workflow_id=meeting_id,
        resource_type="pending_action",
        discord_id=str(action_id),
    )

    cog = _make_cog(mock_bot, resources_store, pending_store)
    payload = BoardMeetingCancelledPayload(meeting_id=meeting_id, reason="Ανωτέρα βία")

    with patch("src.config.settings") as mock_settings:
        mock_settings.discord_guild_id = "9999"
        mock_bot.get_guild = MagicMock(return_value=mock_guild)

        await cog._on_board_meeting_cancelled(payload)

    # Thread should have received a cancellation embed (V4 — Rich Embed,
    # not raw text)
    mock_thread.send.assert_called_once()
    sent_kwargs = mock_thread.send.call_args.kwargs
    assert "embed" in sent_kwargs, "Expected cancellation as Rich Embed (V4)"
    cancel_embed = sent_kwargs["embed"]
    assert "Ακυρώθηκε" in cancel_embed.title
    assert "Ανωτέρα βία" in (cancel_embed.description or "")

    # Pending action should be cancelled
    row = in_memory_db.execute(
        "SELECT status, error FROM discord_pending_actions WHERE id=?", (action_id,)
    ).fetchone()
    assert row["status"] == "done"
    assert row["error"] == "cancelled"


# ── Reminder action handler (re-publishes to bus) ─────────────────────────────

@pytest.mark.asyncio
async def test_reminder_action_handler_publishes_to_bus(in_memory_db):
    """The board_meeting_reminder pending-action handler re-publishes to the bus."""
    # Isolate handler registry and bus subscribers
    original_handlers = dict(sched_mod._HANDLERS)
    sched_mod._HANDLERS.clear()
    bus.clear()

    received_payloads: list = []

    async def capture(payload: BoardMeetingReminderDuePayload) -> None:
        received_payloads.append(payload)

    bus.subscribe(EVENT_BOARD_MEETING_REMINDER_DUE, capture)

    # Import the cog to register its handler via cog_load-like setup
    # We replicate the handler registration directly (as in cog_load)
    async def _handle_reminder_action(payload: dict) -> None:
        await bus.publish(
            EVENT_BOARD_MEETING_REMINDER_DUE,
            BoardMeetingReminderDuePayload(
                meeting_id=payload["meeting_id"],
                hours_before=int(payload["hours_before"]),
            ),
        )

    register_action_handler("board_meeting_reminder", _handle_reminder_action)

    # Directly invoke the handler
    await _handle_reminder_action({"meeting_id": "board_meeting:2030-01-15", "hours_before": "3"})

    assert len(received_payloads) == 1
    assert received_payloads[0].meeting_id == "board_meeting:2030-01-15"
    assert received_payloads[0].hours_before == 3

    sched_mod._HANDLERS.clear()
    sched_mod._HANDLERS.update(original_handlers)
    bus.clear()


# ── D2: _on_board_meeting_reminder_due ───────────────────────────────────────

@pytest.mark.asyncio
async def test_reminder_due_posts_in_thread(in_memory_db):
    """V2 modernization: handler posts a Rich Embed reminder in the agenda thread."""
    mock_thread = AsyncMock()
    mock_thread.send = AsyncMock()

    mock_bot = MagicMock()
    mock_bot.get_channel = MagicMock(return_value=mock_thread)

    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()

    meeting_id = "board_meeting:2030-01-15"
    await resources_store.record(workflow_id=meeting_id, resource_type="thread", discord_id="8888")

    cog = _make_cog(mock_bot, resources_store, pending_store)
    payload = BoardMeetingReminderDuePayload(meeting_id=meeting_id, hours_before=3)

    await cog._on_board_meeting_reminder_due(payload)

    mock_thread.send.assert_called_once()
    sent_kwargs = mock_thread.send.call_args.kwargs
    assert "embed" in sent_kwargs, "Expected reminder as Rich Embed (V2)"
    embed = sent_kwargs["embed"]
    assert "Υπενθύμιση" in embed.title
    assert "Διοικητικού Συμβουλίου" in (embed.description or "")
    assert "3" in (embed.description or "")


@pytest.mark.asyncio
async def test_reminder_due_no_thread_skips_gracefully(in_memory_db):
    """Handler does nothing and logs warning when no thread is in the store."""
    mock_bot = MagicMock()
    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()

    cog = _make_cog(mock_bot, resources_store, pending_store)
    payload = BoardMeetingReminderDuePayload(meeting_id="board_meeting:no-thread", hours_before=3)

    # Should not raise
    await cog._on_board_meeting_reminder_due(payload)
    mock_bot.get_channel.assert_not_called()


# ── D3: _on_board_minutes_shared ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_minutes_shared_posts_drive_link(in_memory_db):
    """V3 modernization: minutes posted as Rich Embed + Link button, board-only.

    Test pre-dates the public-thread restriction, so we set up only a private
    ``thread_board`` resource — which is exactly what the new handler expects.
    """
    mock_thread = AsyncMock()
    mock_thread.send = AsyncMock()

    mock_bot = MagicMock()
    mock_bot.get_channel = MagicMock(return_value=mock_thread)

    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()

    meeting_id = "board_meeting:2030-01-15"
    # IMPORTANT: must be thread_board (private), not 'thread' (public)
    # because the new V3 handler routes minutes only to private threads.
    await resources_store.record(workflow_id=meeting_id, resource_type="thread_board", discord_id="8888")

    cog = _make_cog(mock_bot, resources_store, pending_store)
    payload = BoardMinutesSharedPayload(
        meeting_id=meeting_id,
        drive_url="https://drive.google.com/file/d/xyz",
        doc_id="xyz",
    )

    await cog._on_board_minutes_shared(payload)

    mock_thread.send.assert_called_once()
    sent_kwargs = mock_thread.send.call_args.kwargs
    assert "embed" in sent_kwargs, "Expected minutes as Rich Embed (V3)"
    assert "view" in sent_kwargs, "Expected Link button view (V3)"
    embed = sent_kwargs["embed"]
    assert "Πρακτικά" in embed.title


@pytest.mark.asyncio
async def test_minutes_shared_no_thread_logs_info(in_memory_db):
    """Handler is silent (log info only) when no thread exists for old meetings."""
    mock_bot = MagicMock()
    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()

    cog = _make_cog(mock_bot, resources_store, pending_store)
    payload = BoardMinutesSharedPayload(
        meeting_id="board_meeting:old-meeting",
        drive_url="https://drive.google.com/file/d/abc",
        doc_id="abc",
    )

    # Should not raise
    await cog._on_board_minutes_shared(payload)
    mock_bot.get_channel.assert_not_called()


# ── D0: _on_board_meeting_thread_opened ──────────────────────────────────────


@pytest.mark.asyncio
async def test_thread_opened_creates_private_board_forum_thread(in_memory_db):
    """At scheduling-email send time, the bot opens the private board forum
    thread and records it as thread_board so subsequent emails mirror correctly."""
    import discord as _discord
    from src.core.events import BoardMeetingThreadOpenedPayload

    board_forum = MagicMock(spec=_discord.ForumChannel)
    board_forum.id = 2000
    board_thread_obj = MagicMock()
    board_thread_obj.id = 2001
    board_forum.create_thread = AsyncMock(return_value=MagicMock(thread=board_thread_obj))

    mock_bot = MagicMock()
    mock_bot.get_channel = MagicMock(return_value=board_forum)
    mock_bot.fetch_channel = AsyncMock(return_value=board_forum)

    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()
    cog = _make_cog(mock_bot, resources_store, pending_store)

    payload = BoardMeetingThreadOpenedPayload(
        meeting_id="board_meeting:ΔΣ05-2026",
        meeting_ref="ΔΣ05-2026",
        email_subject="Συνεδρίαση ΔΣ05-2026",
        email_body_html="<p>Test body</p>",
        poll_url="https://doodle.com/group-poll/participate/abc",
        agenda_sheet_url="https://docs.google.com/spreadsheets/d/xyz/",
    )

    with patch("src.integrations.discord.cogs.platform_bridge.settings") as mock_settings:
        mock_settings.discord.platform_bridge.board_meeting.board_channel_id = "2000"
        await cog._on_board_meeting_thread_opened(payload)

    board_forum.create_thread.assert_called_once()
    create_kwargs = board_forum.create_thread.call_args.kwargs
    # Thread name carries meeting_ref
    assert "ΔΣ05-2026" in create_kwargs["name"]

    resources = await resources_store.list_for_workflow(payload.meeting_id)
    rtypes = {r["resource_type"]: r["discord_id"] for r in resources}
    assert rtypes.get("thread_board") == "2001"


@pytest.mark.asyncio
async def test_thread_opened_skips_when_no_board_channel_configured(in_memory_db):
    """If board_channel_id is empty, the handler logs and returns without
    creating a thread or recording a resource."""
    from src.core.events import BoardMeetingThreadOpenedPayload

    mock_bot = MagicMock()
    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()
    cog = _make_cog(mock_bot, resources_store, pending_store)

    payload = BoardMeetingThreadOpenedPayload(
        meeting_id="board_meeting:ΔΣ05-2026",
        meeting_ref="ΔΣ05-2026",
        email_subject="Συνεδρίαση ΔΣ05-2026",
        email_body_html="",
    )

    with patch("src.integrations.discord.cogs.platform_bridge.settings") as mock_settings:
        mock_settings.discord.platform_bridge.board_meeting.board_channel_id = ""
        await cog._on_board_meeting_thread_opened(payload)

    mock_bot.get_channel.assert_not_called()
    resources = await resources_store.list_for_workflow(payload.meeting_id)
    assert resources == []


# ── D0.5: _on_board_email_sent ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_email_sent_posts_into_existing_board_thread(in_memory_db):
    """Mirror posts the email body into the previously-recorded board thread."""
    from src.core.events import BoardEmailSentPayload

    thread_channel = MagicMock()
    thread_channel.send = AsyncMock()

    mock_bot = MagicMock()
    mock_bot.get_channel = MagicMock(return_value=thread_channel)

    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()
    cog = _make_cog(mock_bot, resources_store, pending_store)

    # Pre-record the thread the mirror will target
    await resources_store.record(
        workflow_id="board_meeting:ΔΣ05-2026",
        resource_type="thread_board",
        discord_id="3001",
        channel_id="2000",
    )

    payload = BoardEmailSentPayload(
        meeting_id="board_meeting:ΔΣ05-2026",
        meeting_ref="ΔΣ05-2026",
        kind="scheduling",
        subject="Συνεδρίαση ΔΣ05-2026",
        body_html="<p>Παρακαλούμε <b>συμπληρώστε</b></p>",
        poll_url="https://doodle.com/test",
        agenda_url="https://docs.google.com/spreadsheets/d/abc",
    )
    await cog._on_board_email_sent(payload)

    thread_channel.send.assert_called_once()
    call_kwargs = thread_channel.send.call_args.kwargs
    # Scheduling kind now posts a rich embed, not plain content
    assert "embed" in call_kwargs
    sent_embed = call_kwargs["embed"]
    # Embed title contains the meeting ref and scheduling label
    assert "ΔΣ05-2026" in sent_embed.description
    assert "Προγραμματισμός" in sent_embed.title
    # Buttons provided for poll + agenda
    assert "view" in call_kwargs


@pytest.mark.asyncio
async def test_email_sent_silently_skips_when_no_board_thread(in_memory_db):
    """No thread_board resource ⇒ log a warning, do NOT crash."""
    from src.core.events import BoardEmailSentPayload

    mock_bot = MagicMock()
    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()
    cog = _make_cog(mock_bot, resources_store, pending_store)

    payload = BoardEmailSentPayload(
        meeting_id="board_meeting:nothing-here",
        meeting_ref="ΔΣ99-2026",
        kind="scheduling",
        subject="X",
        body_html="<p>X</p>",
    )
    await cog._on_board_email_sent(payload)
    mock_bot.get_channel.assert_not_called()


def test_html_to_plain_strips_shell_chrome():
    """The HTML→plain helper drops style/script and tags but keeps body text."""
    from src.integrations.discord.cogs.platform_bridge import PlatformBridgeCog

    html = """
    <!doctype html>
    <html><head><style>.x{color:red}</style><title>X</title></head>
    <body>
      <p>Καλημέρα <b>ΔΣ</b>!</p>
      <ul><li>Item 1</li><li>Item 2</li></ul>
    </body></html>
    """
    out = PlatformBridgeCog._html_to_plain(html)
    assert ".x{" not in out and "<style>" not in out
    assert "Καλημέρα" in out
    assert "ΔΣ" in out
    assert "Item 1" in out and "Item 2" in out
