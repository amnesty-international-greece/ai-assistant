"""Tests for the base workflow engine."""

import pytest
from typing import Any

from src.core.workflow import (
    BaseWorkflow,
    WorkflowStep,
    StepResult,
    WorkflowState,
)


class MockWorkflow(BaseWorkflow):
    """A simple mock workflow for testing."""

    def __init__(self, steps_config: list[dict] | None = None, **kwargs):
        self._steps_config = steps_config or [
            {"name": "step1", "desc": "First step"},
            {"name": "step2", "desc": "Second step"},
        ]
        self._step_results: dict[str, StepResult] = {}
        super().__init__(**kwargs)

    @property
    def name(self) -> str:
        return "mock_workflow"

    def define_steps(self) -> list[WorkflowStep]:
        return [
            WorkflowStep(s["name"], s["desc"], s.get("approval", False))
            for s in self._steps_config
        ]

    async def execute_step(self, step: WorkflowStep, context: dict[str, Any]) -> StepResult:
        if step.name in self._step_results:
            return self._step_results[step.name]
        return StepResult(success=True, data={f"{step.name}_done": True}, message="OK")

    def set_step_result(self, step_name: str, result: StepResult):
        self._step_results[step_name] = result


@pytest.mark.asyncio
async def test_workflow_completes(tmp_path):
    """A simple workflow should run to completion."""
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", "test")
    from src.core.audit import init_db
    from unittest.mock import patch
    with patch("src.core.audit._DB_PATH", tmp_path / "test.db"), \
         patch("src.core.audit._CONNECTION", None):
        init_db()
        wf = MockWorkflow()
        result = await wf.run()
        assert result["status"] == "completed"
        assert wf.state == WorkflowState.COMPLETED


@pytest.mark.asyncio
async def test_workflow_fails_on_step_failure(tmp_path):
    """Workflow should fail if a step returns success=False."""
    from src.core.audit import init_db
    from unittest.mock import patch
    with patch("src.core.audit._DB_PATH", tmp_path / "test.db"), \
         patch("src.core.audit._CONNECTION", None):
        init_db()
        wf = MockWorkflow()
        wf.set_step_result("step2", StepResult(success=False, message="Something broke"))
        result = await wf.run()
        assert result["status"] == "failed"
        assert "broke" in result["error"]


@pytest.mark.asyncio
async def test_workflow_pauses_for_approval(tmp_path):
    """Workflow should pause at approval gates."""
    from src.core.audit import init_db
    from unittest.mock import patch
    with patch("src.core.audit._DB_PATH", tmp_path / "test.db"), \
         patch("src.core.audit._CONNECTION", None):
        init_db()
        wf = MockWorkflow(steps_config=[
            {"name": "step1", "desc": "Before approval"},
            {"name": "approval", "desc": "Needs approval", "approval": True},
            {"name": "step3", "desc": "After approval"},
        ])
        result = await wf.run()
        assert result["status"] == "awaiting_approval"
        assert wf.state == WorkflowState.AWAITING_APPROVAL


@pytest.mark.asyncio
async def test_workflow_resumes_after_approval(tmp_path):
    """Workflow should resume and complete after approval."""
    from src.core.audit import init_db
    from unittest.mock import patch
    with patch("src.core.audit._DB_PATH", tmp_path / "test.db"), \
         patch("src.core.audit._CONNECTION", None):
        init_db()
        wf = MockWorkflow(steps_config=[
            {"name": "step1", "desc": "Before approval"},
            {"name": "approval", "desc": "Needs approval", "approval": True},
            {"name": "step3", "desc": "After approval"},
        ])
        result = await wf.run()
        assert result["status"] == "awaiting_approval"

        # Approve and resume
        result = await wf.approve_and_resume()
        assert result["status"] == "completed"
        assert wf.state == WorkflowState.COMPLETED
