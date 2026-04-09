"""Tests for the board meeting minutes workflow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.workflow import StepResult, WorkflowStep


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db(tmp_path):
    """Set up a temporary database for testing."""
    with patch("src.core.audit._DB_PATH", tmp_path / "test.db"), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        yield


@pytest.fixture
def workflow(mock_db):
    """Create a workflow instance with mocked integrations."""
    with patch("src.workflows.board_meeting_minutes.GoogleClient") as mock_google_cls, \
         patch("src.workflows.board_meeting_minutes.ZoomClient") as mock_zoom_cls:

        from src.workflows.board_meeting_minutes import BoardMeetingMinutesWorkflow

        wf = BoardMeetingMinutesWorkflow()

        # Replace integration clients with mocks
        wf._google = MagicMock()
        wf._zoom = AsyncMock()
        wf._gmail = MagicMock()
        wf._onedrive = AsyncMock()

        yield wf


def _make_step(name: str, requires_approval: bool = False) -> WorkflowStep:
    return WorkflowStep(name, f"Test step: {name}", requires_approval=requires_approval)


# ---------------------------------------------------------------------------
# Minimal draft JSON (mirrors what Claude returns)
# ---------------------------------------------------------------------------

_DRAFT_JSON = {
    "title": "Πρακτικά Συνεδρίασης Διοικητικού Συμβουλίου",
    "metadata": {
        "meeting_number": "ΔΣ03-2026",
        "date": "1 Απριλίου 2026",
        "location": "Διαδικτυακά (Zoom)",
        "author": "Γενικός Γραμματέας",
    },
    "sections": [
        {"heading": "Παρόντες", "body": "Α. Αθανασίας (Πρόεδρος)..."},
        {"heading": "Συζήτηση", "body": "Συζητήθηκε το θέμα 1..."},
    ],
    "decisions": [
        {"number": "1", "text": "Εγκρίνεται ο προϋπολογισμός.", "vote": "ομόφωνα"},
        {"number": "2", "text": "Ορίζεται επιτροπή.", "vote": "κατά πλειοψηφία"},
    ],
}


# ---------------------------------------------------------------------------
# Step 1: select_sources
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_step_select_sources_direct_doc_id(workflow):
    """When source_doc_id is provided, skips folder listing entirely."""
    workflow._google.read_doc_content.return_value = "Secretary notes content"
    workflow._zoom.list_recordings.return_value = [
        {"id": "zoom-123", "topic": "Συνεδρίαση ΔΣ03-2026"},
    ]
    workflow._zoom.get_transcript.return_value = "Speaker A: Hello\nSpeaker B: Hi"

    result = await workflow._step_select_sources({
        "meeting_ref": "ΔΣ03-2026",
        "source_doc_id": "doc-id-1",
        "source_doc_name": "Πρακτικά ΔΣ03-2026 draft",
    })

    assert result.success, result.message
    assert result.data["source_doc_id"] == "doc-id-1"
    assert result.data["secgen_notes"] == "Secretary notes content"
    assert result.data["zoom_transcript"] == "Speaker A: Hello\nSpeaker B: Hi"
    assert result.data["meeting_number"] == 3
    assert result.data["meeting_year"] == 2026
    # Should NOT have listed docs in folder
    workflow._google.list_docs_in_folder.assert_not_called()
    workflow._zoom.get_transcript.assert_called_once_with("zoom-123")


@pytest.mark.asyncio
async def test_step_select_sources_no_transcript(workflow):
    """Succeeds with empty transcript when no recordings are available."""
    workflow._google.read_doc_content.return_value = "Notes only"
    workflow._zoom.list_recordings.return_value = []

    result = await workflow._step_select_sources({
        "meeting_ref": "ΔΣ01-2026",
        "source_doc_id": "doc-id-2",
    })

    assert result.success, result.message
    assert result.data["zoom_transcript"] == ""
    assert result.data["secgen_notes"] == "Notes only"
    assert result.data["meeting_number"] == 1
    assert result.data["meeting_year"] == 2026


@pytest.mark.asyncio
async def test_step_select_sources_missing_meeting_ref(workflow):
    """Fails when meeting_ref is not provided."""
    result = await workflow._step_select_sources({})
    assert not result.success
    assert "meeting_ref" in result.message


@pytest.mark.asyncio
async def test_step_select_sources_no_folder_configured(workflow):
    """Fails when minutes_drafts_folder_id is not configured."""
    with patch("src.workflows.board_meeting_minutes.settings") as mock_settings:
        mock_settings.google.minutes_drafts_folder_id = ""
        result = await workflow._step_select_sources({"meeting_ref": "ΔΣ03-2026"})
    assert not result.success
    assert "minutes_drafts_folder_id" in result.message


@pytest.mark.asyncio
async def test_step_select_sources_with_local_transcript(workflow, tmp_path):
    """Uses a local transcript file when transcript_path is provided."""
    workflow._google.read_doc_content.return_value = "Notes here"
    # Write a fake VTT file
    vtt_file = tmp_path / "meeting.vtt"
    vtt_file.write_text("WEBVTT\n\n1\n00:00:01.000 --> 00:00:05.000\nSpeaker A: Hello\n", encoding="utf-8")

    result = await workflow._step_select_sources({
        "meeting_ref": "ΔΣ02-2026",
        "source_doc_id": "doc-id-B",
        "transcript_path": str(vtt_file),
    })

    assert result.success
    assert result.data["source_doc_id"] == "doc-id-B"
    assert "Hello" in result.data["zoom_transcript"]
    # Should NOT have tried Zoom recordings
    workflow._zoom.list_recordings.assert_not_called()


@pytest.mark.asyncio
async def test_step_select_sources_index_fallback(workflow):
    """Falls back to source_doc_index when source_doc_id not provided."""
    workflow._google.list_docs_in_folder.return_value = [
        {"id": "doc-id-A", "name": "First doc"},
        {"id": "doc-id-B", "name": "Second doc"},
    ]
    workflow._zoom.list_recordings.return_value = []
    workflow._google.read_doc_content.return_value = "content"

    with patch("src.workflows.board_meeting_minutes.settings") as mock_settings:
        mock_settings.google.minutes_drafts_folder_id = "folder-id"

        result = await workflow._step_select_sources({
            "meeting_ref": "ΔΣ02-2026",
            "source_doc_index": 1,
        })

    assert result.success
    assert result.data["source_doc_id"] == "doc-id-B"


# ---------------------------------------------------------------------------
# Step 2: draft_minutes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_step_draft_minutes(workflow, tmp_path):
    """Calls Claude, strips code fences, parses JSON response."""
    raw_response = f"```json\n{json.dumps(_DRAFT_JSON)}\n```"

    mock_client = MagicMock()
    mock_client.generate.return_value = raw_response

    with patch("src.core.claude.ClaudeClient", return_value=mock_client), \
         patch("src.workflows.board_meeting_minutes.ClaudeClient", return_value=mock_client), \
         patch("src.workflows.board_meeting_minutes.settings") as mock_settings:
        mock_settings.storage.prompts_dir = str(tmp_path)
        # Create a fake prompt file
        (tmp_path / "board_minutes.md").write_text("You are a minutes assistant.", encoding="utf-8")

        result = await workflow._step_draft_minutes({
            "secgen_notes": "Notes here",
            "zoom_transcript": "Transcript here",
            "meeting_ref": "ΔΣ03-2026",
        })

    assert result.success, result.message
    assert result.data["draft_json"]["title"] == _DRAFT_JSON["title"]
    assert len(result.data["draft_json"]["decisions"]) == 2
    mock_client.generate.assert_called_once()


@pytest.mark.asyncio
async def test_step_draft_minutes_no_prompt_file(workflow):
    """Fails gracefully if the system prompt file doesn't exist."""
    with patch("src.workflows.board_meeting_minutes.settings") as mock_settings:
        mock_settings.storage.prompts_dir = "/nonexistent_path_xyz"

        result = await workflow._step_draft_minutes({
            "secgen_notes": "Notes",
            "zoom_transcript": "",
            "meeting_ref": "ΔΣ03-2026",
        })

    assert not result.success
    assert "prompt" in result.message.lower() or "not found" in result.message.lower()


@pytest.mark.asyncio
async def test_step_draft_minutes_invalid_json(workflow, tmp_path):
    """Fails gracefully when LLM returns non-JSON."""
    mock_client = MagicMock()
    mock_client.generate.return_value = "This is not valid JSON at all."

    with patch("src.workflows.board_meeting_minutes.ClaudeClient", return_value=mock_client), \
         patch("src.workflows.board_meeting_minutes.settings") as mock_settings:
        mock_settings.storage.prompts_dir = str(tmp_path)
        (tmp_path / "board_minutes.md").write_text("System prompt", encoding="utf-8")

        result = await workflow._step_draft_minutes({
            "secgen_notes": "Notes",
            "zoom_transcript": "",
            "meeting_ref": "ΔΣ03-2026",
        })

    assert not result.success
    assert "parse" in result.message.lower() or "json" in result.message.lower()


# ---------------------------------------------------------------------------
# Step 3: write_draft_to_doc
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_step_write_draft_to_doc(workflow):
    """Writes structured sections to doc, then renames it."""
    workflow._google.write_structured_doc.return_value = None
    workflow._google.rename_file.return_value = None

    result = await workflow._step_write_draft_to_doc({
        "draft_json": _DRAFT_JSON,
        "source_doc_id": "doc-id-1",
        "meeting_ref": "ΔΣ03-2026",
    })

    assert result.success, result.message
    assert result.data["draft_doc_id"] == "doc-id-1"
    assert "docs.google.com" in result.data["draft_doc_url"]
    assert "doc-id-1" in result.data["draft_doc_url"]

    workflow._google.write_structured_doc.assert_called_once()
    # Verify sections list contains expected content
    sections = workflow._google.write_structured_doc.call_args[0][1]
    assert any("Πρακτικά" in s["text"] for s in sections)
    assert any(s["type"] == "title" for s in sections)
    assert any(s["type"] == "heading" for s in sections)

    workflow._google.rename_file.assert_called_once_with(
        "doc-id-1",
        "[Πρόχειρο] Πρακτικά - Συνεδρίαση ΔΣ03-2026",
    )


@pytest.mark.asyncio
async def test_step_write_draft_to_doc_missing_doc_id(workflow):
    """Fails when source_doc_id is missing."""
    result = await workflow._step_write_draft_to_doc({
        "draft_json": _DRAFT_JSON,
        "meeting_ref": "ΔΣ03-2026",
    })
    assert not result.success
    assert "source_doc_id" in result.message or "missing" in result.message


# ---------------------------------------------------------------------------
# Step 4: approval_and_share
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_step_approval_and_share(workflow):
    """Sends email to board members after approval."""
    workflow._gmail.send_email.return_value = {"id": "msg-123"}

    with patch("src.workflows.board_meeting_minutes.settings") as mock_settings:
        from src.config import BoardMemberConfig
        mock_settings.workflows.board_meeting.board_members = [
            BoardMemberConfig(email="member1@example.com", first_name="Α", last_name="Β"),
            BoardMemberConfig(email="member2@example.com", first_name="Γ", last_name="Δ"),
        ]
        mock_settings.workflows.board_meeting.minutes_share_message = (
            "Σας κοινοποιούνται τα πρόχειρα πρακτικά."
        )
        mock_settings.testing.dry_run_email = ""

        result = await workflow._step_approval_and_share({
            "meeting_ref": "ΔΣ03-2026",
            "draft_doc_url": "https://docs.google.com/document/d/abc/edit",
            "test_mode": False,
        })

    assert result.success, result.message
    assert result.data["shared"] is True
    assert result.data["shared_at"]

    workflow._gmail.send_email.assert_called_once()
    call_kwargs = workflow._gmail.send_email.call_args
    recipients = call_kwargs.kwargs.get("to") or call_kwargs.args[0]
    assert "member1@example.com" in recipients
    assert "member2@example.com" in recipients

    subject = call_kwargs.kwargs.get("subject") or call_kwargs.args[1]
    assert "ΔΣ03-2026" in subject


@pytest.mark.asyncio
async def test_step_approval_and_share_test_mode(workflow):
    """In test_mode, redirects email to dry_run_email."""
    workflow._gmail.send_email.return_value = {"id": "msg-test"}

    with patch("src.workflows.board_meeting_minutes.settings") as mock_settings:
        from src.config import BoardMemberConfig
        mock_settings.workflows.board_meeting.board_members = [
            BoardMemberConfig(email="real@example.com", first_name="Α", last_name="Β"),
        ]
        mock_settings.workflows.board_meeting.minutes_share_message = "Test share message."
        mock_settings.testing.dry_run_email = "secgen@test.org"

        result = await workflow._step_approval_and_share({
            "meeting_ref": "ΔΣ03-2026",
            "draft_doc_url": "https://docs.google.com/document/d/abc/edit",
            "test_mode": True,
        })

    assert result.success
    workflow._gmail.send_email.assert_called_once()
    call_kwargs = workflow._gmail.send_email.call_args
    recipients = call_kwargs.kwargs.get("to") or call_kwargs.args[0]
    assert "secgen@test.org" in recipients
    assert "real@example.com" not in recipients


# ---------------------------------------------------------------------------
# Step 5: finalize
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_step_finalize(workflow, tmp_path):
    """Exports PDF, skips signing (no sig files), archives, registers protocol."""
    # Prepare a fake PDF from export
    def fake_export(doc_id, output_path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"%PDF-fake")
        return output_path

    workflow._google.export_doc_as_pdf.side_effect = fake_export
    workflow._google.get_last_row_value.return_value = "2026_005"
    workflow._google.write_sheet.return_value = {}
    workflow._google.rename_file.return_value = None
    workflow._onedrive.upload_file.return_value = {"id": "od-file-id", "size": 1024}

    with patch("src.workflows.board_meeting_minutes.settings") as mock_settings, \
         patch("src.workflows.board_meeting_minutes.Path") as mock_path_cls:

        # Use real Path for pdf_dir operations
        mock_settings.google.protokollo_sheet_id = "proto-sheet-id"
        mock_settings.ms_client_id = "fake-client-id"
        mock_settings.ms_tenant_id = "fake-tenant"

        # Don't patch Path — use real file system with tmp_path workaround
        result = await workflow._step_finalize.__wrapped__(workflow, {
            "draft_doc_id": "doc-id-1",
            "meeting_ref": "ΔΣ03-2026",
            "meeting_year": 2026,
            "draft_json": _DRAFT_JSON,
        }) if hasattr(workflow._step_finalize, "__wrapped__") else None

    # Fall back to direct call with real settings patched differently
    if result is None:
        with patch("src.workflows.board_meeting_minutes.settings") as mock_settings:
            mock_settings.google.protokollo_sheet_id = "proto-sheet-id"
            mock_settings.ms_client_id = "fake-client-id"
            mock_settings.ms_tenant_id = "fake-tenant"

            result = await workflow._step_finalize({
                "draft_doc_id": "doc-id-1",
                "meeting_ref": "ΔΣ03-2026",
                "meeting_year": 2026,
                "draft_json": _DRAFT_JSON,
            })

    assert result.success, result.message
    assert "pdf_path" in result.data
    assert result.data["protocol_number"] == "2026_006"
    workflow._google.export_doc_as_pdf.assert_called_once()
    workflow._google.rename_file.assert_called_once()
    rename_args = workflow._google.rename_file.call_args[0]
    assert rename_args[0] == "doc-id-1"
    assert "[Τελικό]" in rename_args[1]


@pytest.mark.asyncio
async def test_step_finalize_no_onedrive(workflow, tmp_path):
    """Skips OneDrive archive when MS credentials are not configured."""
    def fake_export(doc_id, output_path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"%PDF-fake")
        return output_path

    workflow._google.export_doc_as_pdf.side_effect = fake_export
    workflow._google.get_last_row_value.return_value = None
    workflow._google.write_sheet.return_value = {}
    workflow._google.rename_file.return_value = None

    with patch("src.workflows.board_meeting_minutes.settings") as mock_settings:
        mock_settings.google.protokollo_sheet_id = "proto-sheet-id"
        mock_settings.ms_client_id = ""   # No MS creds
        mock_settings.ms_tenant_id = ""

        result = await workflow._step_finalize({
            "draft_doc_id": "doc-id-1",
            "meeting_ref": "ΔΣ03-2026",
            "meeting_year": 2026,
            "draft_json": _DRAFT_JSON,
        })

    assert result.success, result.message
    assert result.data["archive_info"]["status"] == "skipped"
    # OneDrive should NOT have been called
    workflow._onedrive.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_step_finalize_missing_doc_id(workflow):
    """Fails when both draft_doc_id and source_doc_id are missing."""
    result = await workflow._step_finalize({
        "meeting_ref": "ΔΣ03-2026",
        "meeting_year": 2026,
        "draft_json": _DRAFT_JSON,
    })
    assert not result.success
    assert "missing" in result.message.lower() or "doc_id" in result.message.lower()


# ---------------------------------------------------------------------------
# Step 6: extract_decisions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_step_extract_decisions(workflow):
    """Writes decisions with correct ΔΣxx-mm-yyyy numbering (continuing from last)."""
    # Last entry for meeting 03-2026: ΔΣ02-03-2026 → next should be ΔΣ03-03-2026
    workflow._google.read_sheet.return_value = [
        ["ΑΡ. ΑΠΟΦΑΣΗΣ", "ΚΕΙΜΕΝΟ"],  # header
        ["ΔΣ01-03-2026", "First decision"],
        ["ΔΣ02-03-2026", "Second decision"],
    ]
    workflow._google.write_sheet.return_value = {}

    with patch("src.workflows.board_meeting_minutes.settings") as mock_settings:
        mock_settings.google.decisions_sheet_id = "decisions-sheet-id"

        result = await workflow._step_extract_decisions({
            "draft_json": _DRAFT_JSON,
            "meeting_number": 3,
            "meeting_year": 2026,
        })

    assert result.success, result.message
    assert result.data["decisions_written"] == 2
    numbers = result.data["decision_numbers"]
    assert numbers[0] == "ΔΣ03-03-2026"
    assert numbers[1] == "ΔΣ04-03-2026"

    workflow._google.write_sheet.assert_called_once()
    written_rows = workflow._google.write_sheet.call_args[0][2]
    assert written_rows[0][0] == "ΔΣ03-03-2026"
    assert "προϋπολογισμό" in written_rows[0][1] or "Εγκρίνεται" in written_rows[0][1]


@pytest.mark.asyncio
async def test_step_extract_decisions_first_meeting(workflow):
    """Generates ΔΣ01-mm-yyyy when no prior entries exist for this meeting."""
    workflow._google.read_sheet.return_value = [
        ["ΑΡ. ΑΠΟΦΑΣΗΣ", "ΚΕΙΜΕΝΟ"],
    ]
    workflow._google.write_sheet.return_value = {}

    with patch("src.workflows.board_meeting_minutes.settings") as mock_settings:
        mock_settings.google.decisions_sheet_id = "decisions-sheet-id"

        result = await workflow._step_extract_decisions({
            "draft_json": _DRAFT_JSON,
            "meeting_number": 1,
            "meeting_year": 2026,
        })

    assert result.success, result.message
    numbers = result.data["decision_numbers"]
    assert numbers[0] == "ΔΣ01-01-2026"
    assert numbers[1] == "ΔΣ02-01-2026"


@pytest.mark.asyncio
async def test_step_extract_decisions_no_decisions(workflow):
    """Returns success with count=0 when draft has no decisions."""
    with patch("src.workflows.board_meeting_minutes.settings") as mock_settings:
        mock_settings.google.decisions_sheet_id = "decisions-sheet-id"

        result = await workflow._step_extract_decisions({
            "draft_json": {"decisions": []},
            "meeting_number": 3,
            "meeting_year": 2026,
        })

    assert result.success
    assert result.data["decisions_written"] == 0


@pytest.mark.asyncio
async def test_step_extract_decisions_no_sheet_configured(workflow):
    """Fails when decisions_sheet_id is not configured."""
    with patch("src.workflows.board_meeting_minutes.settings") as mock_settings:
        mock_settings.google.decisions_sheet_id = ""

        result = await workflow._step_extract_decisions({
            "draft_json": _DRAFT_JSON,
            "meeting_number": 3,
            "meeting_year": 2026,
        })

    assert not result.success
    assert "decisions_sheet_id" in result.message


# ---------------------------------------------------------------------------
# Full workflow: pauses at approval gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rollback_does_not_crash(workflow):
    """Rollback method exists and runs without error."""
    await workflow.rollback({})


# ---------------------------------------------------------------------------
# Full workflow: pauses at approval gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_workflow_pauses_at_approval(workflow, tmp_path):
    """Full run should pause at the approval_and_share step (step index 3)."""
    # Step 1: select_sources (using direct doc ID — the preferred path)
    workflow._google.read_doc_content.return_value = "SecGen notes"
    workflow._zoom.list_recordings.return_value = []

    # Step 3: write_draft_to_doc
    workflow._google.write_structured_doc.return_value = None
    workflow._google.rename_file.return_value = None

    mock_client = MagicMock()
    mock_client.generate.return_value = json.dumps(_DRAFT_JSON)

    with patch("src.workflows.board_meeting_minutes.ClaudeClient", return_value=mock_client), \
         patch("src.workflows.board_meeting_minutes.settings") as mock_settings:

        mock_settings.storage.prompts_dir = str(tmp_path)
        (tmp_path / "board_minutes.md").write_text("System prompt", encoding="utf-8")

        result = await workflow.run({
            "meeting_ref": "ΔΣ03-2026",
            "source_doc_id": "doc-id-1",
            "source_doc_name": "ΔΣ03-2026 draft",
        })

    assert result["status"] == "awaiting_approval"
    assert result["step"] == "approval_and_share"
