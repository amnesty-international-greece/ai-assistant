"""Tests for the invitation webhook + idempotency / start_at_step plumbing."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def db(tmp_path):
    """Isolated SQLite for workflow_state queries."""
    db_path = tmp_path / "test.db"
    with patch("src.core.audit._DB_PATH", db_path), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        yield db_path


# ── InviteWebhookPayload fields ──────────────────────────────────────────────


def test_payload_accepts_new_fields():
    """The InviteWebhookPayload model accepts start_at_step + raw_meeting_id."""
    from src.api.webhooks import InviteWebhookPayload

    p = InviteWebhookPayload(
        meeting_number="4",
        meeting_date="2026-05-21",
        meeting_time="18:00",
        meeting_type="ΤΑΚΤΙΚΗ",
        location="ΔΙΑΔΙΚΤΥΑΚΑ",
        raw_meeting_id="ΔΣ04-2026",
        start_at_step="read_agenda",
        trigger_row=16,
        test_mode=True,
    )
    assert p.raw_meeting_id == "ΔΣ04-2026"
    assert p.start_at_step == "read_agenda"
    assert p.test_mode is True


def test_payload_defaults_keep_legacy_callers_working():
    """Old payloads without the new fields still validate (start_at_step='')."""
    from src.api.webhooks import InviteWebhookPayload

    p = InviteWebhookPayload(
        meeting_number="4",
        meeting_date="2026-05-21",
        meeting_time="18:00",
    )
    assert p.raw_meeting_id == ""
    assert p.start_at_step == ""
    assert p.test_mode is False


# ── _find_in_progress_invite ─────────────────────────────────────────────────


def test_find_in_progress_returns_none_when_no_match(db):
    from src.api.webhooks import _find_in_progress_invite

    assert _find_in_progress_invite("ΔΣ04-2026") is None


def test_find_in_progress_returns_none_for_empty_meeting_ref(db):
    from src.api.webhooks import _find_in_progress_invite

    assert _find_in_progress_invite("") is None
    assert _find_in_progress_invite("   ") is None


def test_find_in_progress_detects_matching_active_workflow(db):
    """A workflow with state='paused' and matching raw_meeting_id is detected."""
    from src.api.webhooks import _find_in_progress_invite
    from src.core.audit import _get_connection

    conn = _get_connection()
    conn.execute(
        "INSERT INTO workflow_state (workflow_id, workflow_name, state, data, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
        (
            "wf-123",
            "board_meeting_invitation",
            "paused",
            json.dumps({"context": {"raw_meeting_id": "ΔΣ04-2026"}}),
        ),
    )
    conn.commit()

    assert _find_in_progress_invite("ΔΣ04-2026") == "wf-123"


def test_find_in_progress_ignores_completed(db):
    """Completed workflows don't block re-runs (e.g. after --cancel)."""
    from src.api.webhooks import _find_in_progress_invite
    from src.core.audit import _get_connection

    conn = _get_connection()
    conn.execute(
        "INSERT INTO workflow_state (workflow_id, workflow_name, state, data, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
        (
            "wf-old",
            "board_meeting_invitation",
            "completed",
            json.dumps({"context": {"raw_meeting_id": "ΔΣ04-2026"}}),
        ),
    )
    conn.commit()

    assert _find_in_progress_invite("ΔΣ04-2026") is None


def test_find_in_progress_ignores_different_meeting(db):
    """A different raw_meeting_id doesn't trigger the idempotency block."""
    from src.api.webhooks import _find_in_progress_invite
    from src.core.audit import _get_connection

    conn = _get_connection()
    conn.execute(
        "INSERT INTO workflow_state (workflow_id, workflow_name, state, data, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
        (
            "wf-other",
            "board_meeting_invitation",
            "paused",
            json.dumps({"context": {"raw_meeting_id": "ΔΣ03-2026"}}),
        ),
    )
    conn.commit()

    # Asking about ΔΣ04 → no match
    assert _find_in_progress_invite("ΔΣ04-2026") is None
    # Asking about ΔΣ03 → matches
    assert _find_in_progress_invite("ΔΣ03-2026") == "wf-other"


# ── Webhook handler short-circuit ────────────────────────────────────────────


def test_webhook_short_circuits_on_duplicate(db):
    """If an active workflow exists, return 'already_in_progress' instead of starting a new one."""
    from fastapi import BackgroundTasks
    from src.api.webhooks import InviteWebhookPayload, webhook_invite
    from src.core.audit import _get_connection
    import asyncio

    conn = _get_connection()
    conn.execute(
        "INSERT INTO workflow_state (workflow_id, workflow_name, state, data, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
        (
            "wf-dup",
            "board_meeting_invitation",
            "paused",
            json.dumps({"context": {"raw_meeting_id": "ΔΣ04-2026"}}),
        ),
    )
    conn.commit()

    payload = InviteWebhookPayload(
        meeting_number="4",
        meeting_date="2026-05-21",
        meeting_time="18:00",
        raw_meeting_id="ΔΣ04-2026",
    )

    tasks = BackgroundTasks()
    result = asyncio.run(webhook_invite(payload, tasks))

    assert result["status"] == "already_in_progress"
    assert result["workflow_id"] == "wf-dup"
    # Crucially, no background task was queued
    assert len(tasks.tasks) == 0


# ── _find_scheduling_context ─────────────────────────────────────────────────


def test_find_scheduling_context_returns_none_when_no_anchor(db):
    """Returns None if no prior workflow has an email_thread_anchor."""
    from src.api.webhooks import _find_scheduling_context
    from src.core.audit import _get_connection

    conn = _get_connection()
    conn.execute(
        "INSERT INTO workflow_state (workflow_id, workflow_name, state, data, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
        (
            "wf-no-anchor",
            "board_meeting_invitation",
            "awaiting_approval",
            json.dumps({"context": {"raw_meeting_id": "ΔΣ05-2026"}}),
        ),
    )
    conn.commit()

    assert _find_scheduling_context("ΔΣ05-2026") is None


def test_find_scheduling_context_returns_ctx_with_anchor(db):
    """Returns the context dict when a prior workflow has the anchor."""
    from src.api.webhooks import _find_scheduling_context
    from src.core.audit import _get_connection

    anchor = "<msg-id@example.com>"
    conn = _get_connection()
    conn.execute(
        "INSERT INTO workflow_state (workflow_id, workflow_name, state, data, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
        (
            "wf-sched",
            "board_meeting_invitation",
            "awaiting_approval",
            json.dumps({"context": {
                "raw_meeting_id": "ΔΣ05-2026",
                "email_thread_anchor": anchor,
                "poll_url": "https://doodle.com/xyz",
            }}),
        ),
    )
    conn.commit()

    ctx = _find_scheduling_context("ΔΣ05-2026")
    assert ctx is not None
    assert ctx["email_thread_anchor"] == anchor
    assert ctx["poll_url"] == "https://doodle.com/xyz"


def test_find_scheduling_context_ignores_other_meetings(db):
    from src.api.webhooks import _find_scheduling_context
    from src.core.audit import _get_connection

    conn = _get_connection()
    conn.execute(
        "INSERT INTO workflow_state (workflow_id, workflow_name, state, data, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
        (
            "wf-other",
            "board_meeting_invitation",
            "awaiting_approval",
            json.dumps({"context": {
                "raw_meeting_id": "ΔΣ04-2026",
                "email_thread_anchor": "<old@example.com>",
            }}),
        ),
    )
    conn.commit()

    assert _find_scheduling_context("ΔΣ05-2026") is None


# ── _auto_resume_gates ───────────────────────────────────────────────────────


def test_auto_resume_gates_passes_through_completed():
    """When the workflow is already completed, returns immediately."""
    import asyncio
    from src.api.webhooks import _auto_resume_gates

    class FakeWf:
        workflow_id = "wf-done"
        async def approve_and_resume(self):
            raise AssertionError("should not be called")

    result = asyncio.run(_auto_resume_gates(FakeWf(), {"status": "completed"}))
    assert result["status"] == "completed"


def test_auto_resume_gates_resumes_through_two_gates():
    """Drives through two approval gates, each time returning awaiting_approval
    then completed on the third call."""
    import asyncio
    from src.api.webhooks import _auto_resume_gates

    calls = []

    class FakeWf:
        workflow_id = "wf-gates"
        async def approve_and_resume(self):
            calls.append(1)
            if len(calls) < 2:
                return {"status": "awaiting_approval", "step": "confirm_newsletter"}
            return {"status": "completed"}

    result = asyncio.run(
        _auto_resume_gates(FakeWf(), {"status": "awaiting_approval", "step": "approval"})
    )
    assert result["status"] == "completed"
    assert len(calls) == 2


def test_auto_resume_gates_aborts_on_infinite_loop():
    """Safety guard: stops after 5 gates and returns the last result."""
    import asyncio
    from src.api.webhooks import _auto_resume_gates

    class FakeWf:
        workflow_id = "wf-loop"
        async def approve_and_resume(self):
            return {"status": "awaiting_approval", "step": "stuck"}

    result = asyncio.run(
        _auto_resume_gates(FakeWf(), {"status": "awaiting_approval", "step": "stuck"})
    )
    # Loop guard breaks after 5 iterations and returns whatever the last result was.
    assert result["status"] == "awaiting_approval"


# ── _start_at_step consumed after first run ──────────────────────────────────


def test_start_at_step_not_re_applied_on_resume(db):
    """After jumping to a step, _start_at_step is removed from context so
    approve_and_resume → run() does not loop back to the same step."""
    import asyncio
    from src.core.workflow import BaseWorkflow, WorkflowStep, StepResult

    class TwoStepWf(BaseWorkflow):
        @property
        def name(self):
            return "two_step"

        def define_steps(self):
            return [
                WorkflowStep("first", "First step"),
                WorkflowStep("second", "Second step", requires_approval=True),
                WorkflowStep("third", "Third step"),
            ]

        async def execute_step(self, step, ctx):
            return StepResult(success=True, data={"ran": step.name})

    wf = TwoStepWf()
    result = asyncio.run(wf.run({"_start_at_step": "second"}))
    assert result["status"] == "awaiting_approval"
    assert result["step"] == "second"
    assert "_start_at_step" not in wf.context
    result = asyncio.run(wf.approve_and_resume())
    assert result["status"] == "completed"
