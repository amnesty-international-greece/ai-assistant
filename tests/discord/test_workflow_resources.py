"""Tests for WorkflowResourcesStore in src.integrations.discord.scheduler."""
from __future__ import annotations

import pytest

from src.integrations.discord.scheduler import WorkflowResourcesStore


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_then_list_round_trips(in_memory_db):
    """record() then list_for_workflow() returns the stored row."""
    store = WorkflowResourcesStore()
    await store.record(
        workflow_id="board_meeting:2026-05-21",
        resource_type="event",
        discord_id="123456789",
        channel_id=None,
    )

    rows = await store.list_for_workflow("board_meeting:2026-05-21")
    assert len(rows) == 1
    row = rows[0]
    assert row["workflow_id"] == "board_meeting:2026-05-21"
    assert row["resource_type"] == "event"
    assert row["discord_id"] == "123456789"
    assert row["channel_id"] is None


@pytest.mark.asyncio
async def test_multiple_resources_for_same_workflow(in_memory_db):
    """Multiple calls to record() with the same workflow_id all appear in list."""
    store = WorkflowResourcesStore()
    wid = "board_meeting:2026-06-01"

    await store.record(workflow_id=wid, resource_type="event", discord_id="111")
    await store.record(workflow_id=wid, resource_type="thread", discord_id="222", channel_id="999")
    await store.record(workflow_id=wid, resource_type="message", discord_id="333", channel_id="999")

    rows = await store.list_for_workflow(wid)
    assert len(rows) == 3

    types = {r["resource_type"] for r in rows}
    assert types == {"event", "thread", "message"}


@pytest.mark.asyncio
async def test_list_for_workflow_isolates_by_workflow_id(in_memory_db):
    """list_for_workflow only returns rows for the requested workflow_id."""
    store = WorkflowResourcesStore()

    await store.record(workflow_id="meeting:A", resource_type="event", discord_id="aaa")
    await store.record(workflow_id="meeting:B", resource_type="event", discord_id="bbb")

    rows_a = await store.list_for_workflow("meeting:A")
    assert len(rows_a) == 1
    assert rows_a[0]["discord_id"] == "aaa"

    rows_b = await store.list_for_workflow("meeting:B")
    assert len(rows_b) == 1
    assert rows_b[0]["discord_id"] == "bbb"


@pytest.mark.asyncio
async def test_delete_for_workflow_removes_all_rows(in_memory_db):
    """delete_for_workflow() removes every row for that workflow_id."""
    store = WorkflowResourcesStore()
    wid = "board_meeting:2026-07-01"

    await store.record(workflow_id=wid, resource_type="event", discord_id="e1")
    await store.record(workflow_id=wid, resource_type="thread", discord_id="t1")

    deleted = await store.delete_for_workflow(wid)
    assert deleted == 2

    remaining = await store.list_for_workflow(wid)
    assert remaining == []


@pytest.mark.asyncio
async def test_delete_for_workflow_returns_zero_when_nothing_to_delete(in_memory_db):
    """delete_for_workflow returns 0 if no rows match."""
    store = WorkflowResourcesStore()
    deleted = await store.delete_for_workflow("nonexistent:workflow")
    assert deleted == 0


@pytest.mark.asyncio
async def test_delete_does_not_affect_other_workflows(in_memory_db):
    """Deleting one workflow's resources leaves another workflow's rows intact."""
    store = WorkflowResourcesStore()

    await store.record(workflow_id="meeting:keep", resource_type="event", discord_id="keep1")
    await store.record(workflow_id="meeting:del", resource_type="event", discord_id="del1")

    await store.delete_for_workflow("meeting:del")

    kept = await store.list_for_workflow("meeting:keep")
    assert len(kept) == 1
    assert kept[0]["discord_id"] == "keep1"


@pytest.mark.asyncio
async def test_record_with_channel_id_stored(in_memory_db):
    """channel_id is persisted and returned correctly."""
    store = WorkflowResourcesStore()
    await store.record(
        workflow_id="ga:2026-09",
        resource_type="message",
        discord_id="msg999",
        channel_id="chan123",
    )
    rows = await store.list_for_workflow("ga:2026-09")
    assert rows[0]["channel_id"] == "chan123"


@pytest.mark.asyncio
async def test_insert_or_replace_upserts_existing_row(in_memory_db):
    """Recording the same (workflow_id, resource_type, discord_id) twice upserts."""
    store = WorkflowResourcesStore()

    await store.record(
        workflow_id="mtg:1", resource_type="thread", discord_id="t1", channel_id="c1"
    )
    await store.record(
        workflow_id="mtg:1", resource_type="thread", discord_id="t1", channel_id="c2"
    )

    rows = await store.list_for_workflow("mtg:1")
    # Should still be just one row (upserted)
    assert len(rows) == 1
    assert rows[0]["channel_id"] == "c2"
