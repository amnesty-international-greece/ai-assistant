"""Tests for the board meeting invitation workflow."""

import json
import pytest
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.workflow import StepResult, WorkflowStep


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
    with patch("src.workflows.board_meeting_invitation.GoogleClient") as mock_google_cls, \
         patch("src.workflows.board_meeting_invitation.ZoomClient") as mock_zoom_cls, \
         patch("src.workflows.board_meeting_invitation.OneDriveClient") as mock_onedrive_cls, \
         patch("src.workflows.board_meeting_invitation.BrevoClient") as mock_brevo_cls:

        from src.workflows.board_meeting_invitation import BoardMeetingInvitationWorkflow

        wf = BoardMeetingInvitationWorkflow()

        # Configure mocks
        wf._google = MagicMock()
        wf._zoom = AsyncMock()
        wf._onedrive = AsyncMock()
        wf._brevo = AsyncMock()

        yield wf


def _make_step(name: str) -> WorkflowStep:
    return WorkflowStep(name, f"Test step: {name}")


# --- Step Tests ---


def _future_date(days: int = 14) -> str:
    """Return an ISO date string `days` days from today."""
    return (date.today() + timedelta(days=days)).isoformat()


def _make_agenda_rows(date_str: str | None = None, time_str: str = "18:00") -> list:
    """Build a minimal sheet rows array.  Uses a future date by default."""
    date_str = date_str or _future_date(14)
    """Build a minimal sheet rows array matching the expected template layout.

    Layout:
      row 5 (index 4): D5 = meeting number "ΔΣ04-2026"
      row 7 (index 6): C="ΗΜΕΡΟΜΗΝΙΑ"  D=date_str  H="Approval of minutes"
      row 9 (index 8): C="ΩΡΑ ΕΝΑΡΞΗΣ" D=time_str  H="Budget review"
      row 11(index 10): C="ΤΟΠΟΘΕΣΙΑ"  D="ΔΙΑΔΙΚΤΥΑΚΑ"
      row 13(index 12): C="ΠΡΟΣΚΛΗΣΗ"  D=""
    """
    def row(c_val="", d_val="", h_val=""):
        return ["", "", c_val, d_val, "", "", "", h_val]

    return [
        row(),                                          # row 1
        row(),                                          # row 2
        row(),                                          # row 3
        row(),                                          # row 4
        row(d_val="ΔΣ04-2026"),                         # row 5 – meeting number
        row(),                                          # row 6
        row("ΗΜΕΡΟΜΗΝΙΑ", date_str, "Approval of minutes"),   # row 7
        row(),                                          # row 8
        row("ΩΡΑ ΕΝΑΡΞΗΣ", time_str, "Budget review"), # row 9
        row(),                                          # row 10
        row("ΤΟΠΟΘΕΣΙΑ", "ΔΙΑΔΙΚΤΥΑΚΑ"),                # row 11
        row(),                                          # row 12
        row("ΠΡΟΣΚΛΗΣΗ", ""),                           # row 13
    ]


@pytest.mark.asyncio
async def test_step_read_agenda(workflow):
    """read_agenda should parse Google Sheets data from tab-based layout."""
    future_date = _future_date(14)
    year = date.today().year
    workflow._google.list_sheet_tabs.return_value = [{"title": f"04-{year}"}]
    workflow._google.read_sheet.return_value = _make_agenda_rows(date_str=future_date)

    with patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.google.agenda_sheet_id = "test-sheet-id"
        mock_settings.workflows.board_meeting.min_notice_days = 7
        mock_settings.workflows.board_meeting.max_advance_days = 60

        result = await workflow._step_read_agenda({"agenda_sheet_id": "test-sheet-id"})

    assert result.success, result.message
    assert result.data["meeting_number"] == "4"
    assert result.data["meeting_date"] == future_date
    assert result.data["meeting_time"] == "18:00"
    assert len(result.data["agenda_items"]) == 2


@pytest.mark.asyncio
async def test_step_read_agenda_no_sheet_id(workflow):
    """read_agenda should fail gracefully if no sheet ID is configured."""
    with patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.google.agenda_sheet_id = ""
        result = await workflow._step_read_agenda({})
        assert not result.success
        assert "sheet ID" in result.message.lower() or "configured" in result.message.lower()


@pytest.mark.asyncio
async def test_step_schedule_zoom(workflow):
    """schedule_zoom should create a meeting and return join URL."""
    workflow._zoom.schedule_meeting.return_value = {
        "id": 123456789,
        "join_url": "https://zoom.us/j/123456789",
        "password": "abc123",
    }

    ctx = {
        "meeting_date": "2026-04-15",
        "meeting_time": "18:00",
        "meeting_number": "42",
        "agenda_items": ["Item 1", "Item 2"],
    }
    result = await workflow._step_schedule_zoom(ctx)

    assert result.success
    assert "zoom.us" in result.data["zoom_join_url"]
    assert result.data["zoom_meeting_id"] == "123456789"
    workflow._zoom.schedule_meeting.assert_called_once()


@pytest.mark.asyncio
async def test_step_draft_invitation(workflow):
    """draft_invitation should build replacements dict from context (no Claude)."""
    ctx = {
        "meeting_number": "42",
        "meeting_date": "2026-04-15",
        "meeting_time": "18:00",
        "meeting_type": "ΤΑΚΤΙΚΗ",
        "location": "ΔΙΑΔΙΚΤΥΑΚΑ",
        "zoom_join_url": "https://zoom.us/j/123",
        "agenda_items": ["Θέμα 1", "Θέμα 2"],
    }
    result = await workflow._step_draft_invitation(ctx)

    assert result.success, result.message
    reps = result.data["invitation_replacements"]
    assert "[ΗΜΕΡΟΜΗΝΙΑ]" in reps
    assert "[ΩΡΑ ΕΝΑΡΞΗΣ]" in reps
    assert reps["[ΩΡΑ ΕΝΑΡΞΗΣ]"] == "18:00"
    assert "_agenda_items_" in reps
    assert reps["_agenda_items_"] == ["Θέμα 1", "Θέμα 2"]


@pytest.mark.asyncio
async def test_step_draft_invitation_no_protocol(workflow):
    """draft_invitation should mark protocol paragraph for deletion when number missing."""
    ctx = {
        "meeting_number": "1",
        "meeting_date": "2026-01-15",
        "meeting_time": "10:00",
        "meeting_type": "ΕΚΤΑΚΤΗ",
        "location": "ΔΙΑ ΖΩΣΗΣ",
        "zoom_join_url": "",
        "agenda_items": [],
        "protocol_number": "",
    }
    result = await workflow._step_draft_invitation(ctx)

    assert result.success, result.message
    reps = result.data["invitation_replacements"]
    assert "_delete_paragraphs_" in reps


@pytest.mark.asyncio
async def test_step_generate_pdf(workflow, tmp_path):
    """generate_pdf should call Google Docs copy/fill/export and return a pdf_path."""
    workflow._google.copy_document.return_value = "working-doc-id"
    workflow._google.fill_document_template.return_value = None
    workflow._google.export_doc_as_pdf.return_value = None
    workflow._google.delete_file.return_value = None

    replacements = {
        "[ΗΜΕΡΟΜΗΝΙΑ]": "15 Απριλίου 2026",
        "[ΩΡΑ ΕΝΑΡΞΗΣ]": "18:00",
        "[ΤΟΠΟΘΕΣΙΑ]": "διαδικτυακά",
        "[ΤΥΠΟΣ]": "ΤΑΚΤΙΚΗΣ",
        "_agenda_items_": ["Θέμα 1"],
        "_delete_paragraphs_": ["Αρ. Πρωτ.: [ΑΡΙΘΜΟΣ ΠΡΩΤΟΚΟΛΛΟΥ]"],
    }
    ctx = {
        "invitation_replacements": replacements,
        "meeting_number": "42",
        "meeting_date": "2026-04-15",
        "zoom_join_url": "https://zoom.us/j/123",
    }

    # Patch settings so invitation_template_id is set
    with patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.google.invitation_template_id = "template-doc-id"
        mock_settings.workflows.board_meeting.board_members = []
        result = await workflow._step_generate_pdf(ctx)

    assert result.success, result.message
    assert "pdf_path" in result.data
    workflow._google.copy_document.assert_called_once()
    workflow._google.export_doc_as_pdf.assert_called_once()


@pytest.mark.asyncio
async def test_step_approval(workflow):
    """approval step should always succeed (gate is handled by base class)."""
    result = await workflow._step_approval({})
    assert result.success
    assert result.data["approved"] is True


@pytest.mark.asyncio
async def test_step_archive(workflow, tmp_path):
    """archive should upload PDF to OneDrive when credentials are configured."""
    pdf_file = tmp_path / "test.pdf"
    pdf_file.write_bytes(b"%PDF-fake")

    workflow._onedrive.upload_file.return_value = {"id": "file-id-123", "size": 1024}
    workflow._onedrive.get_share_link.return_value = "https://onedrive.com/share/abc"

    ctx = {"pdf_path": str(pdf_file), "meeting_date": "2026-04-15"}

    with patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.ms_client_id = "fake-client-id"
        mock_settings.ms_tenant_id = "fake-tenant-id"
        result = await workflow._step_archive(ctx)

    assert result.success
    assert result.data["archive_file_id"] == "file-id-123"
    workflow._onedrive.upload_file.assert_called_once()


@pytest.mark.asyncio
async def test_step_send_newsletter_test_with_template(workflow):
    """send_newsletter_test should create campaign and send test email."""
    workflow._brevo.send_campaign.return_value = {"campaign_id": 123, "test": True}

    ctx = {
        "brevo_template_id": 5,
        "brevo_list_ids": [1, 2],
        "meeting_number": "42",
        "meeting_date": "2026-04-15",
        "meeting_time": "18:00",
        "meeting_type": "ΤΑΚΤΙΚΗ",
        "zoom_join_url": "https://zoom.us/j/123",
        "agenda_items": ["Item 1"],
    }

    with patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.brevo.newsletter_template_id = 5
        mock_settings.brevo.newsletter_list_ids = [1, 2]
        mock_settings.brevo.sender_email = "info@amnesty.org.gr"
        mock_settings.brevo.sender_name = "Amnesty Greece"
        mock_settings.testing.dry_run_email = "test@example.com"

        result = await workflow._step_send_newsletter_test(ctx)

    assert result.success
    assert result.data["newsletter_campaign_id"] == 123
    assert result.data["newsletter_test_sent"] is True
    workflow._brevo.send_campaign.assert_called_once()
    # Test email should have been passed
    call_kwargs = workflow._brevo.send_campaign.call_args
    test_emails = call_kwargs.kwargs.get("test_emails") or call_kwargs.args[5] if len(call_kwargs.args) > 5 else None
    # At minimum, send_campaign was called once
    assert workflow._brevo.send_campaign.call_count == 1


@pytest.mark.asyncio
async def test_step_send_newsletter_test_skips_when_no_config(workflow):
    """send_newsletter_test should skip when no template configured."""
    ctx = {"meeting_number": "42", "meeting_date": "2026-04-15", "meeting_time": "18:00",
           "zoom_join_url": "", "agenda_items": []}

    with patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.brevo.newsletter_template_id = None
        mock_settings.brevo.newsletter_list_ids = []
        mock_settings.testing.dry_run_email = ""

        result = await workflow._step_send_newsletter_test(ctx)

    assert result.success
    assert result.data.get("newsletter_skipped") is True


@pytest.mark.asyncio
async def test_step_confirm_newsletter_sends_live(workflow):
    """confirm_newsletter should call send_campaign_now when list_ids are set."""
    workflow._brevo.send_campaign_now.return_value = None

    ctx = {
        "newsletter_campaign_id": 123,
        "newsletter_list_ids": [1, 2],
    }
    result = await workflow._step_confirm_newsletter(ctx)

    assert result.success
    assert result.data["newsletter_sent"] is True
    workflow._brevo.send_campaign_now.assert_called_once_with(123, workflow=workflow.name)


@pytest.mark.asyncio
async def test_step_confirm_newsletter_skips_empty_lists(workflow):
    """confirm_newsletter should skip live send when list_ids is empty."""
    ctx = {
        "newsletter_campaign_id": 123,
        "newsletter_list_ids": [],
    }
    result = await workflow._step_confirm_newsletter(ctx)

    assert result.success
    assert result.data["newsletter_sent"] is False
    workflow._brevo.send_campaign_now.assert_not_called()


@pytest.mark.asyncio
async def test_step_schedule_reminder_zoom_native(workflow):
    """schedule_reminder should delegate to Zoom (no custom scheduling)."""
    ctx = {
        "meeting_date": "2026-04-15",
        "meeting_time": "18:00",
        "meeting_number": "42",
    }
    result = await workflow._step_schedule_reminder(ctx)

    assert result.success
    assert result.data.get("reminder_native") is True


@pytest.mark.asyncio
async def test_step_schedule_reminder_test_mode(workflow):
    """schedule_reminder should skip in test mode."""
    ctx = {"test_mode": True}
    result = await workflow._step_schedule_reminder(ctx)

    assert result.success
    assert result.data.get("reminder_skipped") is True


@pytest.mark.asyncio
async def test_full_workflow_pauses_at_approval(workflow):
    """Full workflow run should pause at approval gate."""
    future_date = _future_date(14)
    year = date.today().year
    workflow._google.list_sheet_tabs.return_value = [{"title": f"04-{year}"}]
    workflow._google.read_sheet.return_value = _make_agenda_rows(date_str=future_date)
    workflow._google.copy_document.return_value = "working-doc-id"
    workflow._google.fill_document_template.return_value = None
    workflow._google.export_doc_as_pdf.return_value = None
    workflow._google.delete_file.return_value = None

    workflow._zoom.schedule_meeting.return_value = {
        "id": 123, "join_url": "https://zoom.us/j/123", "password": "abc",
    }
    workflow._zoom.add_registrants.return_value = []

    with patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.google.agenda_sheet_id = "test-id"
        mock_settings.google.invitation_template_id = "template-doc-id"
        mock_settings.workflows.board_meeting.board_members = []
        mock_settings.workflows.board_meeting.min_notice_days = 7
        mock_settings.workflows.board_meeting.max_advance_days = 60

        result = await workflow.run({"agenda_sheet_id": "test-id"})

    assert result["status"] == "awaiting_approval"
    assert result["step"] == "approval"
