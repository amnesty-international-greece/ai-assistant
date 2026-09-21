"""The notice the Board is owed comes from the section's rules, and is checked.

`min_notice_days` sat in config.yaml for months with nothing reading it, so a
meeting could be called for tomorrow and nothing said a word. It is a rule of
the section, not a setting of the installation, so it lives in rules.yaml with
the article it comes from - and the run stops until someone decides to call
the meeting on short notice anyway.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import settings
from src.profile.loader import load_section
from src.workflows.board_meeting_invitation import BoardMeetingInvitationWorkflow


@pytest.fixture
def workflow():
    wf = BoardMeetingInvitationWorkflow()
    wf._google = MagicMock()
    wf._zoom = AsyncMock()
    wf._google.list_sheet_tabs.return_value = [{"title": "Ημερήσια Διάταξη"}]
    wf._google.read_sheet.return_value = [["ΑΡΙΘΜΟΣ ΣΥΝΕΔΡΙΑΣΗΣ", "", "", "ΔΣ09-2026"]]
    wf._google.read_meeting_ref.return_value = "ΔΣ09-2026"
    return wf


async def _read_agenda(workflow, days_ahead: int, ctx_extra: dict | None = None):
    when = (date.today() + timedelta(days=days_ahead)).isoformat()
    step = next(s for s in workflow.steps if s.name == "read_agenda")
    with patch("src.workflows.board_meeting_invitation._scan_form_labels") as scan:
        scan.return_value = {
            "ΑΡΙΘΜΟΣ ΣΥΝΕΔΡΙΑΣΗΣ": ("ΔΣ09-2026", 5),
            "ΗΜΕΡΟΜΗΝΙΑ": (when, 9),
            "ΩΡΑ ΕΝΑΡΞΗΣ": ("20:00", 11),
            "ΤΥΠΟΣ": ("ΤΑΚΤΙΚΗ", 7),
        }
        return await workflow.execute_step(step, {"agenda_items": ["A"], **(ctx_extra or {})})


def test_the_notice_period_reaches_the_settings_from_the_rules_file():
    rules = load_section(settings.section).rules
    assert settings.workflows.board_meeting.min_notice_days == rules.board.min_notice_days
    assert settings.workflows.general_assembly.min_notice_days == rules.assembly.min_notice_days


@pytest.mark.asyncio
async def test_a_meeting_called_too_soon_stops_and_says_so(workflow):
    result = await _read_agenda(workflow, days_ahead=2)

    assert result.success is False
    assert result.data["needs_input"] == "allow_short_notice"
    assert "--allow-short-notice" in result.message
    assert str(settings.workflows.board_meeting.min_notice_days) in result.message


@pytest.mark.asyncio
async def test_the_board_can_still_call_it_on_short_notice(workflow):
    """The statute allows an extraordinary meeting; the rule is a check, not a veto."""
    result = await _read_agenda(workflow, days_ahead=2, ctx_extra={"allow_short_notice": True})
    assert result.success is True


@pytest.mark.asyncio
async def test_proper_notice_passes_without_comment(workflow):
    result = await _read_agenda(workflow, days_ahead=10)
    assert result.success is True


@pytest.mark.asyncio
async def test_a_section_that_states_no_notice_period_is_not_second_guessed(workflow, monkeypatch):
    monkeypatch.setattr(settings.workflows.board_meeting, "min_notice_days", 0)
    result = await _read_agenda(workflow, days_ahead=1)
    assert result.success is True
