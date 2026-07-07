"""Tests for src.integrations.discord.scheduler - PendingActionsStore and the worker."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import src.integrations.discord.scheduler as sched_mod
from src.integrations.discord.scheduler import (
    PendingActionsStore,
    WorkflowResourcesStore,
    run_pending_actions_worker,
    register_action_handler,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _past(seconds: int = 10) -> datetime:
    return _utcnow() - timedelta(seconds=seconds)


def _future(seconds: int = 3600) -> datetime:
    return _utcnow() + timedelta(seconds=seconds)


# ── PendingActionsStore tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_then_due_now_round_trips(in_memory_db):
    store = PendingActionsStore()
    action_id = await store.enqueue(
        action_type="reminder",
        payload={"msg": "hello"},
        due_at=_past(),
    )
    assert isinstance(action_id, int)

    due = await store.due_now(now=_utcnow())
    assert len(due) == 1
    assert due[0].id == action_id
    assert due[0].action_type == "reminder"
    assert due[0].payload == {"msg": "hello"}
    assert due[0].status == "pending"


@pytest.mark.asyncio
async def test_due_now_only_returns_past_rows(in_memory_db):
    store = PendingActionsStore()

    past_id = await store.enqueue(action_type="unpin", payload={}, due_at=_past())
    await store.enqueue(action_type="unpin", payload={}, due_at=_future())

    due = await store.due_now(now=_utcnow())
    assert len(due) == 1
    assert due[0].id == past_id


@pytest.mark.asyncio
async def test_due_now_excludes_done_rows(in_memory_db):
    store = PendingActionsStore()
    action_id = await store.enqueue(action_type="reminder", payload={}, due_at=_past())
    await store.mark_done(action_id)

    due = await store.due_now(now=_utcnow())
    assert due == []


@pytest.mark.asyncio
async def test_mark_done_flips_status(in_memory_db):
    store = PendingActionsStore()
    action_id = await store.enqueue(action_type="reminder", payload={}, due_at=_past())
    await store.mark_done(action_id)

    row = in_memory_db.execute(
        "SELECT status, executed_at FROM discord_pending_actions WHERE id=?", (action_id,)
    ).fetchone()
    assert row["status"] == "done"
    assert row["executed_at"] is not None


@pytest.mark.asyncio
async def test_mark_failed_records_error(in_memory_db):
    store = PendingActionsStore()
    action_id = await store.enqueue(action_type="reminder", payload={}, due_at=_past())
    await store.mark_failed(action_id, "something went wrong")

    row = in_memory_db.execute(
        "SELECT status, error FROM discord_pending_actions WHERE id=?", (action_id,)
    ).fetchone()
    assert row["status"] == "failed"
    assert "something went wrong" in row["error"]


@pytest.mark.asyncio
async def test_cancel_marks_done_with_cancelled_error(in_memory_db):
    store = PendingActionsStore()
    action_id = await store.enqueue(action_type="reminder", payload={}, due_at=_future())
    await store.cancel(action_id)

    row = in_memory_db.execute(
        "SELECT status, error FROM discord_pending_actions WHERE id=?", (action_id,)
    ).fetchone()
    assert row["status"] == "done"
    assert row["error"] == "cancelled"


@pytest.mark.asyncio
async def test_due_now_returns_oldest_first(in_memory_db):
    store = PendingActionsStore()
    older = _past(30)
    newer = _past(5)

    id1 = await store.enqueue(action_type="a", payload={}, due_at=older)
    id2 = await store.enqueue(action_type="b", payload={}, due_at=newer)

    due = await store.due_now(now=_utcnow())
    assert [a.id for a in due] == [id1, id2]


# ── Worker tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_worker_dispatches_to_registered_handler(in_memory_db):
    """Worker calls the registered handler for a due action."""
    # Isolate handler registry for this test
    original_handlers = dict(sched_mod._HANDLERS)
    sched_mod._HANDLERS.clear()

    called_with: list = []

    async def my_handler(payload: dict):
        called_with.append(payload)

    register_action_handler("test_action", my_handler)

    store = PendingActionsStore()
    await store.enqueue(action_type="test_action", payload={"x": 42}, due_at=_past())

    stop = asyncio.Event()
    task = asyncio.get_event_loop().create_task(
        run_pending_actions_worker(store=store, poll_interval_seconds=0.05, stop_event=stop)
    )
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert called_with == [{"x": 42}]

    sched_mod._HANDLERS.clear()
    sched_mod._HANDLERS.update(original_handlers)


@pytest.mark.asyncio
async def test_worker_marks_done_after_successful_handler(in_memory_db):
    original_handlers = dict(sched_mod._HANDLERS)
    sched_mod._HANDLERS.clear()

    async def ok_handler(payload: dict):
        pass

    register_action_handler("ok_action", ok_handler)

    store = PendingActionsStore()
    action_id = await store.enqueue(action_type="ok_action", payload={}, due_at=_past())

    stop = asyncio.Event()
    task = asyncio.get_event_loop().create_task(
        run_pending_actions_worker(store=store, poll_interval_seconds=0.05, stop_event=stop)
    )
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    row = in_memory_db.execute(
        "SELECT status FROM discord_pending_actions WHERE id=?", (action_id,)
    ).fetchone()
    assert row["status"] == "done"

    sched_mod._HANDLERS.clear()
    sched_mod._HANDLERS.update(original_handlers)


@pytest.mark.asyncio
async def test_worker_marks_failed_and_continues_on_handler_error(in_memory_db):
    original_handlers = dict(sched_mod._HANDLERS)
    sched_mod._HANDLERS.clear()

    second_called: list = []

    async def bad_handler(payload: dict):
        raise ValueError("explode")

    async def good_handler(payload: dict):
        second_called.append(True)

    register_action_handler("bad_action", bad_handler)
    register_action_handler("good_action", good_handler)

    store = PendingActionsStore()
    bad_id = await store.enqueue(action_type="bad_action", payload={}, due_at=_past(20))
    good_id = await store.enqueue(action_type="good_action", payload={}, due_at=_past(10))

    stop = asyncio.Event()
    task = asyncio.get_event_loop().create_task(
        run_pending_actions_worker(store=store, poll_interval_seconds=0.05, stop_event=stop)
    )
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    bad_row = in_memory_db.execute(
        "SELECT status FROM discord_pending_actions WHERE id=?", (bad_id,)
    ).fetchone()
    good_row = in_memory_db.execute(
        "SELECT status FROM discord_pending_actions WHERE id=?", (good_id,)
    ).fetchone()

    assert bad_row["status"] == "failed"
    assert good_row["status"] == "done"
    assert second_called == [True]

    sched_mod._HANDLERS.clear()
    sched_mod._HANDLERS.update(original_handlers)


@pytest.mark.asyncio
async def test_worker_marks_failed_if_no_handler_registered(in_memory_db):
    original_handlers = dict(sched_mod._HANDLERS)
    sched_mod._HANDLERS.clear()

    store = PendingActionsStore()
    action_id = await store.enqueue(
        action_type="unregistered_action", payload={}, due_at=_past()
    )

    stop = asyncio.Event()
    task = asyncio.get_event_loop().create_task(
        run_pending_actions_worker(store=store, poll_interval_seconds=0.05, stop_event=stop)
    )
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    row = in_memory_db.execute(
        "SELECT status, error FROM discord_pending_actions WHERE id=?", (action_id,)
    ).fetchone()
    assert row["status"] == "failed"
    assert "no handler" in row["error"]

    sched_mod._HANDLERS.clear()
    sched_mod._HANDLERS.update(original_handlers)


@pytest.mark.asyncio
async def test_worker_stops_cleanly_when_stop_event_set(in_memory_db):
    """Worker exits without hanging when stop_event is set."""
    original_handlers = dict(sched_mod._HANDLERS)
    sched_mod._HANDLERS.clear()

    store = PendingActionsStore()
    stop = asyncio.Event()
    task = asyncio.get_event_loop().create_task(
        run_pending_actions_worker(store=store, poll_interval_seconds=60, stop_event=stop)
    )
    # Let it start its first poll, then signal stop
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    sched_mod._HANDLERS.clear()
    sched_mod._HANDLERS.update(original_handlers)
