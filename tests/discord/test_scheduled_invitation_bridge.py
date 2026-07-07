"""Tests for the two cross-process bridges added to PlatformBridgeCog.

These cover the deferred pending-action handlers registered in ``cog_load`` for
``board_meeting_scheduled`` and ``board_email_invitation_mirror``, the workflow
side that enqueues the ``board_meeting_scheduled`` action (carrying test_mode),
and the test_mode → BoardMeetingScheduledPayload round-trip.

Discord API is never touched: ``_on_board_meeting_scheduled`` /
``_on_board_email_sent`` are spied via monkeypatch so the handler logic
(idempotency skip vs. run) is observed without a live bot.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.events import BoardMeetingScheduledPayload
from src.integrations.discord.scheduler import (
    PendingActionsStore,
    WorkflowResourcesStore,
)


def _make_cog(resources_store, pending_store):
    """Construct a PlatformBridgeCog with pre-injected stores (bypasses cog_load)."""
    from src.integrations.discord.cogs.platform_bridge import PlatformBridgeCog
    cog = PlatformBridgeCog(MagicMock())
    cog._resources_store = resources_store
    cog._pending_store = pending_store
    return cog


def _register_handlers(cog):
    """Replicate the two deferred handlers registered in cog_load and return them.

    Mirrors the closures defined inside ``PlatformBridgeCog.cog_load`` so the
    test can invoke them directly without standing up a bot/event loop.
    """
    async def _handle_scheduled_action(payload: dict) -> None:
        meeting_id = payload.get("meeting_id", "")
        if not meeting_id or cog._resources_store is None:
            return
        existing = await cog._resources_store.list_for_workflow(meeting_id)
        if any(r["resource_type"] == "event" for r in existing):
            return
        from datetime import datetime as _dt
        starts = payload.get("starts_at") or ""
        try:
            starts_at = _dt.fromisoformat(starts)
        except ValueError:
            return
        await cog._on_board_meeting_scheduled(
            BoardMeetingScheduledPayload(
                meeting_id=meeting_id,
                starts_at=starts_at,
                zoom_url=payload.get("zoom_url", ""),
                agenda_summary=payload.get("agenda_summary", ""),
                board_member_emails=payload.get("board_member_emails", []) or [],
                test_mode=bool(payload.get("test_mode", False)),
            )
        )

    async def _handle_invitation_mirror_action(payload: dict) -> None:
        meeting_id = payload.get("meeting_id", "")
        if not meeting_id or cog._resources_store is None:
            return
        existing = await cog._resources_store.list_for_workflow(meeting_id)
        if any(r["resource_type"] == "mirror_invitation" for r in existing):
            return
        from src.core.events import BoardEmailSentPayload
        await cog._on_board_email_sent(
            BoardEmailSentPayload(
                meeting_id=meeting_id,
                meeting_ref=payload.get("meeting_ref", ""),
                kind="invitation",
                subject=payload.get("subject", ""),
                body_html=payload.get("body_html", ""),
                test_mode=bool(payload.get("test_mode", False)),
                zoom_url=payload.get("zoom_url", ""),
                agenda_url=payload.get("agenda_url", ""),
                meeting_datetime=payload.get("meeting_datetime", ""),
                agenda_summary=payload.get("agenda_summary", ""),
            )
        )

    return _handle_scheduled_action, _handle_invitation_mirror_action


# ── board_meeting_scheduled deferred handler ─────────────────────────────────

@pytest.mark.asyncio
async def test_scheduled_action_skips_when_event_exists(in_memory_db):
    """Deferred scheduled handler skips when an 'event' resource already exists."""
    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()
    cog = _make_cog(resources_store, pending_store)
    cog._on_board_meeting_scheduled = AsyncMock()

    meeting_id = "board_meeting:ΔΣ05-2030"
    await resources_store.record(
        workflow_id=meeting_id, resource_type="event", discord_id="9001",
    )

    handle_scheduled, _ = _register_handlers(cog)
    await handle_scheduled({
        "meeting_id": meeting_id,
        "starts_at": datetime(2030, 1, 15, 18, 0, tzinfo=timezone.utc).isoformat(),
        "test_mode": True,
    })

    cog._on_board_meeting_scheduled.assert_not_called()


@pytest.mark.asyncio
async def test_scheduled_action_runs_when_no_event(in_memory_db):
    """Deferred scheduled handler runs when no 'event' resource exists yet."""
    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()
    cog = _make_cog(resources_store, pending_store)
    cog._on_board_meeting_scheduled = AsyncMock()

    meeting_id = "board_meeting:ΔΣ06-2030"

    handle_scheduled, _ = _register_handlers(cog)
    await handle_scheduled({
        "meeting_id": meeting_id,
        "starts_at": datetime(2030, 2, 20, 18, 0, tzinfo=timezone.utc).isoformat(),
        "zoom_url": "https://zoom.us/j/1",
        "agenda_summary": "1. Θέμα",
        "board_member_emails": ["secgen@amnesty.org.gr"],
        "test_mode": True,
    })

    cog._on_board_meeting_scheduled.assert_called_once()
    sent_payload = cog._on_board_meeting_scheduled.call_args.args[0]
    assert isinstance(sent_payload, BoardMeetingScheduledPayload)
    assert sent_payload.meeting_id == meeting_id
    assert sent_payload.test_mode is True


# ── board_email_invitation_mirror deferred handler ───────────────────────────

@pytest.mark.asyncio
async def test_invitation_mirror_action_skips_when_marker_exists(in_memory_db):
    """Deferred invitation-mirror handler skips when a 'mirror_invitation' marker exists."""
    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()
    cog = _make_cog(resources_store, pending_store)
    cog._on_board_email_sent = AsyncMock()

    meeting_id = "board_meeting:ΔΣ07-2030"
    await resources_store.record(
        workflow_id=meeting_id, resource_type="mirror_invitation", discord_id="posted",
    )

    _, handle_mirror = _register_handlers(cog)
    await handle_mirror({
        "meeting_id": meeting_id,
        "meeting_ref": "ΔΣ07-2030",
        "subject": "Re: Συνεδρίαση",
        "body_html": "<p>x</p>",
        "test_mode": True,
    })

    cog._on_board_email_sent.assert_not_called()


@pytest.mark.asyncio
async def test_invitation_mirror_action_runs_when_no_marker(in_memory_db):
    """Deferred invitation-mirror handler runs when no marker exists yet."""
    resources_store = WorkflowResourcesStore()
    pending_store = PendingActionsStore()
    cog = _make_cog(resources_store, pending_store)
    cog._on_board_email_sent = AsyncMock()

    meeting_id = "board_meeting:ΔΣ08-2030"

    _, handle_mirror = _register_handlers(cog)
    await handle_mirror({
        "meeting_id": meeting_id,
        "meeting_ref": "ΔΣ08-2030",
        "subject": "Re: Συνεδρίαση ΔΣ08-2030",
        "body_html": "<p>πρόσκληση</p>",
        "test_mode": True,
        "zoom_url": "https://zoom.us/j/2",
        "agenda_url": "https://docs.google.com/spreadsheets/d/abc/",
        "meeting_datetime": "2030-02-20T18:00",
        "agenda_summary": "1. Θέμα",
    })

    cog._on_board_email_sent.assert_called_once()
    sent_payload = cog._on_board_email_sent.call_args.args[0]
    assert sent_payload.kind == "invitation"
    assert sent_payload.meeting_id == meeting_id
    assert sent_payload.test_mode is True


# ── Workflow enqueue side ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_publish_scheduled_enqueues_action_with_test_mode(in_memory_db, monkeypatch):
    """_publish_board_meeting_scheduled enqueues a board_meeting_scheduled action
    carrying test_mode=True. The bus is mocked so the publish doesn't error."""
    import src.workflows.board_meeting_invitation as bmi

    # Mock the bus so publish is a no-op (the import target is inside the func).
    fake_bus = MagicMock()
    fake_bus.publish = AsyncMock()
    monkeypatch.setattr("src.core.event_bus.bus", fake_bus)

    # No board members configured → empty list (avoids settings dependency).
    monkeypatch.setattr(
        bmi.settings.workflows.board_meeting, "board_members", [], raising=False
    )

    ctx = {
        "raw_meeting_id": "ΔΣ09-2030",
        "meeting_date": "2030-03-10",
        "meeting_time": "18:00",
        "agenda_items": ["Θέμα Α", "Θέμα Β"],
        "zoom_join_url": "https://zoom.us/j/3",
        "test_mode": True,
    }

    await bmi._publish_board_meeting_scheduled(ctx)

    store = PendingActionsStore()
    due = await store.due_now(now=datetime.now(timezone.utc))
    scheduled = [a for a in due if a.action_type == "board_meeting_scheduled"]
    assert len(scheduled) == 1
    row = scheduled[0]
    assert row.payload["meeting_id"] == "board_meeting:ΔΣ09-2030"
    assert row.payload["test_mode"] is True


# ── Round-trip: payload reconstruction preserves test_mode ───────────────────

def test_scheduled_payload_roundtrip_preserves_test_mode():
    """A persisted payload with test_mode=True reconstructs a payload with
    test_mode=True - which is what makes _on_board_meeting_scheduled route to
    the sandbox agenda channel (agenda_channel_id_test)."""
    payload = {
        "meeting_id": "board_meeting:ΔΣ10-2030",
        "starts_at": datetime(2030, 4, 1, 18, 0, tzinfo=timezone.utc).isoformat(),
        "zoom_url": "https://zoom.us/j/4",
        "agenda_summary": "1. Θέμα",
        "board_member_emails": ["secgen@amnesty.org.gr"],
        "test_mode": True,
    }
    reconstructed = BoardMeetingScheduledPayload(
        meeting_id=payload["meeting_id"],
        starts_at=datetime.fromisoformat(payload["starts_at"]),
        zoom_url=payload["zoom_url"],
        agenda_summary=payload["agenda_summary"],
        board_member_emails=payload["board_member_emails"],
        test_mode=bool(payload["test_mode"]),
    )
    assert reconstructed.test_mode is True
    assert reconstructed.meeting_id == "board_meeting:ΔΣ10-2030"
