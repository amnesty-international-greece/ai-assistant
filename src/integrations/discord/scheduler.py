"""Persistent reminder queue for the Discord bot.

Provides `PendingActionsStore` for inserting deferred actions and a background
worker `run_pending_actions_worker` that polls the table every N seconds and
dispatches due actions to registered handlers.

Handlers are registered via `register_action_handler(action_type, async_callable)`.
The dispatcher passes the JSON-decoded payload to the handler.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from src.core.audit import _get_connection

logger = logging.getLogger(__name__)

ActionHandler = Callable[[dict[str, Any]], Awaitable[None]]

_HANDLERS: dict[str, ActionHandler] = {}


def register_action_handler(action_type: str, handler: ActionHandler) -> None:
    """Register *handler* as the dispatcher for *action_type*.

    Replaces any existing handler for the same type. Multiple handlers per
    action_type are not supported - use the event bus if you need fan-out.
    """
    _HANDLERS[action_type] = handler
    logger.debug("scheduler: registered handler for %s", action_type)


@dataclass(slots=True)
class PendingAction:
    id: int
    action_type: str
    payload: dict[str, Any]
    due_at: datetime
    status: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PendingActionsStore:
    """SQLite-backed queue. Async write methods, sync read methods (single-thread)."""

    _lock: asyncio.Lock

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def enqueue(
        self,
        *,
        action_type: str,
        payload: dict[str, Any],
        due_at: datetime,
    ) -> int:
        """Insert a row and return its id."""
        async with self._lock:
            conn = _get_connection()
            cur = conn.execute(
                """INSERT INTO discord_pending_actions
                       (action_type, payload, due_at, status, created_at)
                   VALUES (?, ?, ?, 'pending', ?)""",
                (
                    action_type,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    due_at.isoformat(),
                    _now_iso(),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    async def due_now(self, *, now: datetime, limit: int = 50) -> list[PendingAction]:
        """Return pending rows whose due_at <= now, oldest first."""
        conn = _get_connection()
        rows = conn.execute(
            """SELECT id, action_type, payload, due_at, status
               FROM discord_pending_actions
               WHERE status = 'pending' AND due_at <= ?
               ORDER BY due_at ASC
               LIMIT ?""",
            (now.isoformat(), limit),
        ).fetchall()
        result: list[PendingAction] = []
        for row in rows:
            payload = json.loads(row["payload"]) if row["payload"] else {}
            due_at_str = row["due_at"].rstrip("Z")
            due_at_dt = datetime.fromisoformat(due_at_str)
            if due_at_dt.tzinfo is None:
                due_at_dt = due_at_dt.replace(tzinfo=timezone.utc)
            result.append(PendingAction(
                id=int(row["id"]),
                action_type=row["action_type"],
                payload=payload,
                due_at=due_at_dt,
                status=row["status"],
            ))
        return result

    async def mark_done(self, action_id: int) -> None:
        async with self._lock:
            conn = _get_connection()
            conn.execute(
                "UPDATE discord_pending_actions SET status='done', executed_at=? WHERE id=?",
                (_now_iso(), action_id),
            )
            conn.commit()

    async def mark_failed(self, action_id: int, error: str) -> None:
        async with self._lock:
            conn = _get_connection()
            conn.execute(
                "UPDATE discord_pending_actions SET status='failed', executed_at=?, error=? WHERE id=?",
                (_now_iso(), error[:500], action_id),
            )
            conn.commit()

    async def cancel(self, action_id: int) -> None:
        """Mark a pending action as cancelled (uses 'done' status to keep the
        state machine binary - the audit trail records the reason)."""
        async with self._lock:
            conn = _get_connection()
            conn.execute(
                "UPDATE discord_pending_actions SET status='done', executed_at=?, error='cancelled' WHERE id=? AND status='pending'",
                (_now_iso(), action_id),
            )
            conn.commit()


async def run_pending_actions_worker(
    *,
    store: PendingActionsStore | None = None,
    poll_interval_seconds: int = 30,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Background loop: poll the queue, dispatch due actions, sleep, repeat.

    Stops when *stop_event* is set, or when cancelled.
    """
    store = store or PendingActionsStore()
    stop_event = stop_event or asyncio.Event()

    while not stop_event.is_set():
        try:
            now = datetime.now(timezone.utc)
            due = await store.due_now(now=now)
            for action in due:
                handler = _HANDLERS.get(action.action_type)
                if handler is None:
                    logger.warning(
                        "scheduler: no handler registered for action_type=%s (id=%s) - marking failed",
                        action.action_type, action.id,
                    )
                    await store.mark_failed(action.id, f"no handler for {action.action_type}")
                    continue
                try:
                    await handler(action.payload)
                    await store.mark_done(action.id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(
                        "scheduler: handler for %s (id=%s) raised: %s",
                        action.action_type, action.id, exc,
                    )
                    await store.mark_failed(action.id, str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("scheduler: poll loop error: %s", exc)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
        except asyncio.TimeoutError:
            pass


class WorkflowResourcesStore:
    """Track which Discord resources a workflow created, so cancellation/cleanup
    can find them."""

    _lock: asyncio.Lock

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def record(
        self,
        *,
        workflow_id: str,
        resource_type: str,
        discord_id: str,
        channel_id: str | None = None,
    ) -> None:
        async with self._lock:
            conn = _get_connection()
            conn.execute(
                """INSERT OR REPLACE INTO discord_workflow_resources
                       (workflow_id, resource_type, discord_id, channel_id, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (workflow_id, resource_type, discord_id, channel_id, _now_iso()),
            )
            conn.commit()

    async def list_for_workflow(self, workflow_id: str) -> list[dict[str, Any]]:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT * FROM discord_workflow_resources WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    async def delete_for_workflow(self, workflow_id: str) -> int:
        async with self._lock:
            conn = _get_connection()
            cur = conn.execute(
                "DELETE FROM discord_workflow_resources WHERE workflow_id = ?",
                (workflow_id,),
            )
            conn.commit()
            return cur.rowcount
