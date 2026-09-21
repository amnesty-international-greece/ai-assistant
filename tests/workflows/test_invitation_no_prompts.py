"""Steps state what they are missing; they never stop to ask (B2).

An unattended run - the sheet webhook, a Discord command, a scheduled job -
has nobody at a keyboard. A step that called input() there would hang until
the process was killed, holding whatever it had already created.
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.workflows.board_meeting_invitation import BoardMeetingInvitationWorkflow

WORKFLOWS = Path(__file__).resolve().parents[2] / "src" / "workflows"


def test_no_workflow_step_calls_input():
    offenders = []
    for path in WORKFLOWS.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "input":
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "Workflow steps must not prompt; return StepResult(needs_input=...) instead:\n  "
        + "\n  ".join(offenders)
    )


@pytest.fixture
def workflow():
    wf = BoardMeetingInvitationWorkflow()
    wf._google = MagicMock()
    wf._zoom = AsyncMock()
    return wf


@pytest.mark.asyncio
async def test_zoom_step_without_a_time_names_what_it_needs(workflow):
    step = next(s for s in workflow.steps if s.name == "schedule_zoom")
    result = await workflow.execute_step(step, {"meeting_date": "2026-10-01"})

    assert result.success is False
    assert result.data["needs_input"] == "meeting_time"
    assert "--time" in result.message
    workflow._zoom.create_meeting.assert_not_called()


@pytest.mark.asyncio
async def test_a_date_beyond_policy_is_refused_until_allowed(workflow):
    """No y/n prompt: the run stops and says which flag overrides the policy."""
    from datetime import date, timedelta

    far = (date.today() + timedelta(days=90)).isoformat()
    rows = [["ΑΡΙΘΜΟΣ ΣΥΝΕΔΡΙΑΣΗΣ", "", "", "ΔΣ09-2026"]]
    workflow._google.list_sheet_tabs.return_value = [{"title": "Ημερήσια Διάταξη"}]
    workflow._google.read_sheet.return_value = rows
    workflow._google.read_meeting_ref.return_value = "ΔΣ09-2026"

    step = next(s for s in workflow.steps if s.name == "read_agenda")
    with patch("src.workflows.board_meeting_invitation._scan_form_labels") as scan:
        scan.return_value = {
            "ΑΡΙΘΜΟΣ ΣΥΝΕΔΡΙΑΣΗΣ": ("ΔΣ09-2026", 5),
            "ΗΜΕΡΟΜΗΝΙΑ": (far, 9),
            "ΩΡΑ ΕΝΑΡΞΗΣ": ("20:00", 11),
            "ΤΥΠΟΣ": ("ΤΑΚΤΙΚΗ", 7),
        }
        refused = await workflow.execute_step(step, {"agenda_items": ["A"]})
        allowed = await workflow.execute_step(
            step, {"agenda_items": ["A"], "allow_far_date": True}
        )

    assert refused.success is False
    assert refused.data["needs_input"] == "allow_far_date"
    assert "--allow-far-date" in refused.message
    assert allowed.success is True
