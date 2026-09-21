"""The Βιβλίο Αποφάσεων records what the Board actually resolved (B5).

The book is the legal record. Its wording must be the text captured live as
the decision was put and carried, not the LLM's retelling of it in the minutes
draft, and its references must come from the one implementation of the
ΔΣ{NN}-{MM}-{YYYY} format rather than a second copy of that format string.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.core.meeting_events import MeetingEventsStore
from src.workflows.board_meeting_minutes import BoardMeetingMinutesWorkflow

VERBATIM = (
    "Το Διοικητικό Συμβούλιο εγκρίνει τον απολογισμό δράσης 2025-2026 "
    "και εξουσιοδοτεί τον Ταμία να τον υποβάλει."
)
PARAPHRASE = "Εγκρίθηκε ο απολογισμός δράσης."


@pytest.fixture
def workflow():
    wf = BoardMeetingMinutesWorkflow()
    wf._google = MagicMock()
    wf._google.read_sheet.return_value = []
    wf._google.write_sheet.return_value = {}
    return wf


def _ctx(**over):
    ctx = {
        "test_mode": False,
        "meeting_number": 7,
        "meeting_year": 2026,
        "raw_meeting_id": "ΔΣ07-2026",
        "draft_json": {"decisions": [{"text": PARAPHRASE}]},
    }
    ctx.update(over)
    return ctx


def _capture(meeting_ref: str, seq: int, text: str, ref: str | None = None) -> None:
    MeetingEventsStore().record_event(
        meeting_ref=meeting_ref,
        event_type="decision",
        payload={
            "ref": ref or f"ΔΣ{seq:02d}-07-2026",
            "seq": seq,
            "decision_text": text,
            "outcome": "Έγκριση",
        },
    )


async def _write(workflow, ctx):
    step = next(s for s in workflow.steps if "decision" in s.name)
    return await workflow.execute_step(step, ctx)


@pytest.mark.asyncio
async def test_the_book_gets_the_wording_captured_in_the_meeting(workflow, monkeypatch):
    monkeypatch.setattr("src.workflows.board_meeting_minutes.settings.google.decisions_sheet_id",
                        "sheet-1", raising=False)
    _capture("ΔΣ07-2026", 1, VERBATIM)

    result = await _write(workflow, _ctx())

    assert result.success
    rows = workflow._google.write_sheet.call_args[0][2]
    assert rows == [["ΔΣ01-07-2026", VERBATIM]]
    assert PARAPHRASE not in rows[0][1]
    assert result.data["decisions_verbatim"] == 1


@pytest.mark.asyncio
async def test_without_a_capture_the_draft_text_is_still_written(workflow, monkeypatch):
    monkeypatch.setattr("src.workflows.board_meeting_minutes.settings.google.decisions_sheet_id",
                        "sheet-1", raising=False)
    result = await _write(workflow, _ctx())

    rows = workflow._google.write_sheet.call_args[0][2]
    assert rows == [["ΔΣ01-07-2026", PARAPHRASE]]
    assert result.data["decisions_verbatim"] == 0


@pytest.mark.asyncio
async def test_numbering_continues_after_what_the_sheet_already_holds(workflow, monkeypatch):
    monkeypatch.setattr("src.workflows.board_meeting_minutes.settings.google.decisions_sheet_id",
                        "sheet-1", raising=False)
    workflow._google.read_sheet.return_value = [
        ["ΔΣ01-07-2026", "..."], ["ΔΣ02-07-2026", "..."],
    ]
    _capture("ΔΣ07-2026", 3, VERBATIM)
    ctx = _ctx(draft_json={"decisions": [{"text": PARAPHRASE}]})

    await _write(workflow, ctx)

    rows = workflow._google.write_sheet.call_args[0][2]
    assert rows[0][0] == "ΔΣ03-07-2026"
    assert rows[0][1] == VERBATIM


@pytest.mark.asyncio
async def test_refs_use_the_meeting_ref_not_a_second_format_string(workflow, monkeypatch):
    """A one-digit meeting number still yields ΔΣ01-07-2026, as the sidebar writes it."""
    monkeypatch.setattr("src.workflows.board_meeting_minutes.settings.google.decisions_sheet_id",
                        "sheet-1", raising=False)
    await _write(workflow, _ctx(meeting_number=7, raw_meeting_id="ΔΣ07-2026"))

    rows = workflow._google.write_sheet.call_args[0][2]
    from src.workflows.decision_drafter import compute_decision_ref
    assert rows[0][0] == compute_decision_ref("ΔΣ07-2026", 1)
