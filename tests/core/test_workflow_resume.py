"""Approving a saved run from a later process (CLI command, Discord button)."""
from __future__ import annotations

import json

import pytest

from src.core.audit import get_workflow_state, save_workflow_state
from src.core.workflow import BaseWorkflow, StepResult, WorkflowState, WorkflowStep


class _Circular(BaseWorkflow):
    """Two steps with an approval gate between them, like the circular."""

    def __init__(self, actor: str = "secgen"):
        self.rolled_back: list[dict] = []
        super().__init__(actor=actor)

    @property
    def name(self) -> str:
        return "test_circular"

    def define_steps(self) -> list[WorkflowStep]:
        return [
            WorkflowStep("draft", "Draft it"),
            WorkflowStep("approval", "Approve it", requires_approval=True),
            WorkflowStep("send", "Send it"),
        ]

    async def execute_step(self, step: WorkflowStep, context: dict) -> StepResult:
        if step.name == "send":
            return StepResult(success=True, data={"sent_to": context.get("audience", "?")})
        return StepResult(success=True, data={f"{step.name}_done": True})

    async def rollback(self, ctx: dict) -> None:
        self.rolled_back.append(dict(ctx))


async def _halted_run(audience: str = "members") -> str:
    """Start a run and let it stop at its gate, as a fresh process would find it."""
    wf = _Circular()
    result = await wf.run({"audience": audience})
    assert result["status"] == "awaiting_approval"
    return wf.workflow_id


@pytest.mark.asyncio
async def test_resume_continues_a_saved_run_in_a_new_process():
    halted = await _halted_run()
    fresh = _Circular(actor="discord:approve")   # nothing in memory from the first run
    result = await fresh.resume(halted, approval_granted=True)

    assert result["status"] == "completed"
    assert fresh.workflow_id == halted
    # The context survived, so the later steps act on the original run's data.
    assert result["context"]["sent_to"] == "members"
    assert result["context"]["draft_done"] is True
    assert get_workflow_state(halted)["state"] == WorkflowState.COMPLETED.value


@pytest.mark.asyncio
async def test_resume_denied_rolls_back_and_cancels():
    halted = await _halted_run()
    fresh = _Circular()
    result = await fresh.resume(halted, approval_granted=False)

    assert result["status"] == "cancelled"
    assert fresh.rolled_back and fresh.rolled_back[0]["audience"] == "members"
    assert get_workflow_state(halted)["state"] == WorkflowState.CANCELLED.value


@pytest.mark.asyncio
async def test_resume_of_unknown_workflow_says_so():
    with pytest.raises(ValueError):
        await _Circular().resume("nope-1234")


@pytest.mark.asyncio
async def test_resume_of_a_run_that_is_not_at_a_gate_is_refused():
    save_workflow_state(
        workflow_name="test_circular",
        workflow_id="running-1",
        state=WorkflowState.IN_PROGRESS.value,
        data={"context": {}, "step_index": 0},
    )
    with pytest.raises(RuntimeError):
        await _Circular().resume("running-1")


@pytest.mark.asyncio
async def test_resume_of_a_completed_run_is_a_no_op():
    save_workflow_state(
        workflow_name="test_circular",
        workflow_id="done-1",
        state=WorkflowState.COMPLETED.value,
        data={"context": {"protocol_number": "2026_040"}, "step_index": 3},
    )
    result = await _Circular().resume("done-1")
    assert result["status"] == "completed"
    assert result["context"]["protocol_number"] == "2026_040"


@pytest.mark.asyncio
async def test_resume_reads_state_saved_as_json_text():
    """save_workflow_state stores JSON; resume must accept the stored form."""
    save_workflow_state(
        workflow_name="test_circular",
        workflow_id="gate-2",
        state=WorkflowState.AWAITING_APPROVAL.value,
        data={"context": {"audience": "board"}, "step_index": 1},
    )
    raw = get_workflow_state("gate-2")["data"]
    assert isinstance(raw, str) and json.loads(raw)["step_index"] == 1

    result = await _Circular().resume("gate-2")
    assert result["status"] == "completed"
    assert result["context"]["sent_to"] == "board"
