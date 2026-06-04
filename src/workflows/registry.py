"""Central registry mapping workflow name → class, for generic tooling (debug CLI)."""
from __future__ import annotations

from src.core.workflow import BaseWorkflow
from src.workflows.archive import ArchiveWorkflow
from src.workflows.board_meeting_invitation import BoardMeetingInvitationWorkflow
from src.workflows.board_meeting_minutes import BoardMeetingMinutesWorkflow
from src.workflows.egkyklios_general import EgkykliosGeneralWorkflow

WORKFLOWS: dict[str, type[BaseWorkflow]] = {
    "archive": ArchiveWorkflow,
    "board_meeting_invitation": BoardMeetingInvitationWorkflow,
    "board_meeting_minutes": BoardMeetingMinutesWorkflow,
    "egkyklios_general": EgkykliosGeneralWorkflow,
}


def get_workflow(name: str) -> type[BaseWorkflow] | None:
    return WORKFLOWS.get(name)
