"""Base workflow engine - state machine for multi-step workflows with approval gates."""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.core.audit import log_action, save_workflow_state, get_workflow_state

logger = logging.getLogger(__name__)


class WorkflowState(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class StepResult:
    """Result of a workflow step execution."""
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    needs_approval: bool = False


class WorkflowStep:
    """A single step in a workflow."""

    def __init__(
        self,
        name: str,
        description: str,
        requires_approval: bool = False,
    ):
        self.name = name
        self.description = description
        self.requires_approval = requires_approval


class BaseWorkflow(ABC):
    """Abstract base class for all workflows.

    Subclasses must implement:
        - define_steps(): return list of WorkflowStep
        - execute_step(step, context): execute a single step
    """

    def __init__(self, actor: str = "secgen"):
        self.workflow_id = str(uuid.uuid4())[:8]
        self.actor = actor
        self.state = WorkflowState.PENDING
        self.context: dict[str, Any] = {}
        self.current_step_index = 0
        self.steps = self.define_steps()

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique workflow name (e.g., 'board_meeting_invitation')."""
        ...

    @abstractmethod
    def define_steps(self) -> list[WorkflowStep]:
        """Define the sequence of steps for this workflow."""
        ...

    @abstractmethod
    async def execute_step(self, step: WorkflowStep, context: dict[str, Any]) -> StepResult:
        """Execute a single workflow step."""
        ...

    async def run(self, initial_data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run the workflow from start to finish.

        If ``initial_data`` contains a ``_start_at_step`` key matching one of
        ``self.steps`` names, the runner jumps to that step index (skipping
        all earlier steps).  Used by the CLI's ``minutes finalize`` command
        and by the auto-trigger webhook (``start_at_step="read_agenda"``).
        Unknown names are ignored with a warning.
        """
        self.context = initial_data or {}
        start_at = (self.context.get("_start_at_step") or "").strip()
        if start_at:
            idx = next(
                (i for i, s in enumerate(self.steps) if s.name == start_at),
                None,
            )
            if idx is None:
                logger.warning(
                    "[%s] _start_at_step=%r not found in steps; starting at 0",
                    self.workflow_id, start_at,
                )
            else:
                self.current_step_index = idx
                logger.info(
                    "[%s] Jumping to step %r (index %d)",
                    self.workflow_id, start_at, idx,
                )
            # Consume the flag: approve_and_resume() re-enters run() with the
            # same context, and a second jump would loop back to this step.
            self.context.pop("_start_at_step", None)
        self._transition(WorkflowState.IN_PROGRESS)
        log_action(
            workflow=self.name,
            action="workflow_started",
            actor=self.actor,
            details={"workflow_id": self.workflow_id, "steps": len(self.steps)},
        )

        try:
            while self.current_step_index < len(self.steps):
                step = self.steps[self.current_step_index]
                logger.info(
                    "[%s] Step %d/%d: %s",
                    self.workflow_id,
                    self.current_step_index + 1,
                    len(self.steps),
                    step.description,
                )

                if step.requires_approval:
                    self._transition(WorkflowState.AWAITING_APPROVAL)
                    log_action(
                        workflow=self.name,
                        action="awaiting_approval",
                        actor="system",
                        target=step.name,
                        details={"step": step.name, "description": step.description},
                    )
                    # Caller must call approve_and_resume() to continue
                    self._persist()
                    return {
                        "status": "awaiting_approval",
                        "step": step.name,
                        "workflow_id": self.workflow_id,
                    }

                result = await self._run_step(step)
                if not result.success:
                    self._transition(WorkflowState.FAILED)
                    return {"status": "failed", "step": step.name, "error": result.message}

                self.current_step_index += 1

            self._transition(WorkflowState.COMPLETED)
            log_action(
                workflow=self.name,
                action="workflow_completed",
                actor=self.actor,
                details={"workflow_id": self.workflow_id},
            )
            return {"status": "completed", "context": self.context}

        except Exception as e:
            self._transition(WorkflowState.FAILED)
            log_action(
                workflow=self.name,
                action="workflow_error",
                actor="system",
                details={"error": str(e)},
                status="failure",
            )
            raise

    async def approve_and_resume(self) -> dict[str, Any]:
        """Approve the current step and resume workflow execution."""
        if self.state != WorkflowState.AWAITING_APPROVAL:
            raise RuntimeError(f"Workflow not awaiting approval (state: {self.state})")

        step = self.steps[self.current_step_index]
        self._transition(WorkflowState.APPROVED)
        log_action(
            workflow=self.name,
            action="approval_given",
            actor=self.actor,
            target=step.name,
        )

        result = await self._run_step(step)
        if not result.success:
            self._transition(WorkflowState.FAILED)
            return {"status": "failed", "step": step.name, "error": result.message}

        self.current_step_index += 1
        self._transition(WorkflowState.IN_PROGRESS)
        return await self.run(self.context)

    async def _run_step(self, step: WorkflowStep) -> StepResult:
        """Execute a step with logging."""
        self._transition(WorkflowState.EXECUTING)
        log_action(
            workflow=self.name,
            action="step_started",
            actor="system",
            target=step.name,
        )

        result = await self.execute_step(step, self.context)
        self.context.update(result.data)

        log_action(
            workflow=self.name,
            action="step_completed" if result.success else "step_failed",
            actor="system",
            target=step.name,
            details={"message": result.message},
            status="success" if result.success else "failure",
        )
        return result

    def _transition(self, new_state: WorkflowState) -> None:
        """Transition to a new state."""
        logger.debug("[%s] %s → %s", self.workflow_id, self.state.value, new_state.value)
        self.state = new_state
        self._persist()

    def _persist(self) -> None:
        """Save current state to database."""
        save_workflow_state(
            workflow_name=self.name,
            workflow_id=self.workflow_id,
            state=self.state.value,
            data={"context": self.context, "step_index": self.current_step_index},
        )
