"""Tests for the board meeting invitation workflow (Wave 2)."""

import pytest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.workflow import StepResult, WorkflowStep


@pytest.fixture
def mock_db(tmp_path):
    with patch("src.core.audit._DB_PATH", tmp_path / "test.db"), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        yield


@pytest.fixture
def workflow(mock_db):
    with patch("src.workflows.board_meeting_invitation.GoogleClient"), \
         patch("src.workflows.board_meeting_invitation.ZoomClient"), \
         patch("src.workflows.board_meeting_invitation.OneDriveClient"), \
         patch("src.workflows.board_meeting_invitation.BrevoClient"):

        from src.workflows.board_meeting_invitation import BoardMeetingInvitationWorkflow

        wf = BoardMeetingInvitationWorkflow()
        wf._google = MagicMock()
        # Sensible default for the D5-based meeting_ref lookup; individual
        # tests can override via ``wf._google.read_meeting_ref.return_value``
        # if they need a different reference.
        wf._google.read_meeting_ref.return_value = "ΔΣ04-2026"
        wf._zoom = AsyncMock()
        wf._onedrive = AsyncMock()
        wf._brevo = AsyncMock()
        yield wf


def _future_date(days: int = 14) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _make_agenda_rows(date_str: str | None = None, time_str: str = "18:00",
                      durations: tuple[int, ...] = (30, 45)) -> list:
    """Build a minimal sheet rows array (single-tab layout).

    Column I (index 8) holds the per-item duration in minutes.
    """
    date_str = date_str or _future_date(14)

    def row(c_val="", d_val="", h_val="", i_val=""):
        return ["", "", c_val, d_val, "", "", "", h_val, i_val]

    return [
        row(),                                              # row 1
        row(),                                              # row 2
        row(),                                              # row 3
        row(),                                              # row 4
        row(d_val="ΔΣ04-2026"),                             # row 5 - meeting number
        row(),                                              # row 6
        row("ΗΜΕΡΟΜΗΝΙΑ", date_str,
            "Approval of minutes", str(durations[0])),      # row 7
        row(),                                              # row 8
        row("ΩΡΑ ΕΝΑΡΞΗΣ", time_str,
            "Budget review", str(durations[1])),            # row 9
        row(),                                              # row 10
        row("ΤΟΠΟΘΕΣΙΑ", "ΔΙΑΔΙΚΤΥΑΚΑ"),                     # row 11
        row(),                                              # row 12
        row("ΠΡΟΣΚΛΗΣΗ", ""),                                # row 13
    ]


# ─── read_agenda ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_read_agenda_uses_tabs_zero(workflow):
    """read_agenda uses tabs[0] directly (no filtering) and reads duration."""
    future_date = _future_date(14)
    workflow._google.list_sheet_tabs.return_value = [
        {"title": "ΔΣ04-2026"},
        {"title": "ΔΣ05-2026"},  # second tab should be IGNORED
    ]
    # Two reads now: (1) the D16:D18 approval-box guard, (2) the agenda itself.
    # First call returns all-TRUE boxes so the guard passes; second returns the agenda.
    workflow._google.read_sheet.side_effect = [
        [[True], [True], [True]],          # D16, D17, D18 all approved
        _make_agenda_rows(date_str=future_date),
    ]

    with patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.google.agenda_sheet_id = "test-sheet-id"
        mock_settings.workflows.board_meeting.max_advance_days = 60
        result = await workflow._step_read_agenda({"agenda_sheet_id": "test-sheet-id"})

    assert result.success, result.message
    assert result.data["meeting_number"] == "4"
    assert result.data["meeting_date"] == future_date
    assert result.data["meeting_time"] == "18:00"
    assert len(result.data["agenda_items"]) == 2
    assert result.data["meeting_duration_minutes"] == 75   # 30 + 45
    # Both reads must reference tabs[0] (ΔΣ04-2026), never tabs[1] (ΔΣ05-2026).
    all_ranges = [call.args[1] for call in workflow._google.read_sheet.call_args_list]
    assert all("ΔΣ04-2026" in r for r in all_ranges), all_ranges
    assert not any("ΔΣ05-2026" in r for r in all_ranges), all_ranges


@pytest.mark.asyncio
async def test_step_read_agenda_no_sheet_id(workflow):
    with patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.google.agenda_sheet_id = ""
        result = await workflow._step_read_agenda({})
        assert not result.success


@pytest.mark.asyncio
async def test_step_read_agenda_guard_refuses_when_all_boxes_false(workflow):
    """Guard C: if D16, D17, D18 are all FALSE the step refuses to proceed.

    The board hasn't approved → reading stale agenda data would be wrong.
    """
    workflow._google.list_sheet_tabs.return_value = [{"title": "ΔΣ04-2026"}]
    # Only one read happens - the guard read - because the step short-circuits.
    workflow._google.read_sheet.return_value = [[False], [False], [False]]

    with patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.google.agenda_sheet_id = "test-sheet-id"
        result = await workflow._step_read_agenda({"agenda_sheet_id": "test-sheet-id"})

    assert not result.success
    assert "D16" in result.message or "approval" in result.message.lower() or "FALSE" in result.message
    # The agenda data range was never read - guard fired first.
    all_ranges = [call.args[1] for call in workflow._google.read_sheet.call_args_list]
    assert all("D16" in r for r in all_ranges), all_ranges


# ─── send_scheduling_email ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_scheduling_email_runs_first(workflow):
    """send_scheduling_email is define_steps()[0]."""
    assert workflow.steps[0].name == "send_scheduling_email"


@pytest.mark.asyncio
async def test_send_scheduling_email_test_mode_sends_to_test_email(workflow):
    """In test_mode the email goes to test_email, NOT skipped."""
    mock_client = AsyncMock()
    mock_client.send_email.return_value = "<anchor@amnesty.org.gr>"

    workflow._google.list_sheet_tabs.return_value = [{"title": "ΔΣ04-2026"}]

    ctx = {"test_mode": True, "poll_url": "https://when2meet.com/abc"}
    with patch("src.workflows.board_meeting_invitation.M365MailClient", return_value=mock_client), \
         patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.ms_client_id = "x"
        mock_settings.ms_tenant_id = "y"
        mock_settings.google.agenda_sheet_id = "sheet-id"
        mock_settings.testing.test_email = "test@example.com"

        result = await workflow._step_send_scheduling_email(ctx)

    assert result.success, result.message
    assert result.data["email_thread_anchor"] == "<anchor@amnesty.org.gr>"
    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["to"] == "test@example.com"
    assert kwargs["html"] is True
    assert "ΔΣ04-2026" in kwargs["subject"]


@pytest.mark.asyncio
async def test_send_scheduling_email_body_with_poll_url(workflow):
    mock_client = AsyncMock()
    mock_client.send_email.return_value = "<anchor>"
    workflow._google.list_sheet_tabs.return_value = [{"title": "ΔΣ04-2026"}]

    ctx = {"poll_url": "https://when2meet.com/poll"}
    with patch("src.workflows.board_meeting_invitation.M365MailClient", return_value=mock_client), \
         patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.ms_client_id = "x"
        mock_settings.ms_tenant_id = "y"
        mock_settings.google.agenda_sheet_id = "sheet-id"
        mock_settings.testing.test_email = ""
        await workflow._step_send_scheduling_email(ctx)

    body = mock_client.send_email.call_args.kwargs["body"]
    assert "https://when2meet.com/poll" in body
    assert "ΔΙΑΘΕΣΙΜΟΤΗΤΕΣ" in body        # poll CTA button present
    assert "ΗΜΕΡΗΣΙΑ ΔΙΑΤΑΞΗ" in body      # agenda CTA button present


@pytest.mark.asyncio
async def test_send_scheduling_email_creates_crabfit_poll_from_dates(workflow):
    """When crabfit_dates are supplied (and no poll_url), a Crab Fit event is
    created and its URL becomes the poll link in the email."""
    mock_client = AsyncMock()
    mock_client.send_email.return_value = "<anchor>"
    workflow._google.list_sheet_tabs.return_value = [{"title": "ΔΣ04-2026"}]

    fake_crabfit = AsyncMock()
    fake_crabfit.create_event.return_value = {
        "id": "synedriasi-ds04-2026-123456",
        "url": "https://crab.fit/synedriasi-ds04-2026-123456",
    }

    ctx = {"crabfit_dates": ["2026-06-17", "2026-06-29"]}
    with patch("src.workflows.board_meeting_invitation.M365MailClient", return_value=mock_client), \
         patch("src.integrations.crabfit.CrabFitClient", return_value=fake_crabfit), \
         patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.ms_client_id = "x"
        mock_settings.ms_tenant_id = "y"
        mock_settings.google.agenda_sheet_id = "sheet-id"
        mock_settings.testing.test_email = ""
        result = await workflow._step_send_scheduling_email(ctx)

    # The Crab Fit event was created with the two candidate dates.
    fake_crabfit.create_event.assert_awaited_once()
    call = fake_crabfit.create_event.call_args
    assert [d.isoformat() for d in call.kwargs["dates"]] == ["2026-06-17", "2026-06-29"]
    # Its URL flows into the email body and the step output.
    body = mock_client.send_email.call_args.kwargs["body"]
    assert "https://crab.fit/synedriasi-ds04-2026-123456" in body
    assert "ΔΙΑΘΕΣΙΜΟΤΗΤΕΣ" in body
    assert result.data["crabfit_url"] == "https://crab.fit/synedriasi-ds04-2026-123456"


@pytest.mark.asyncio
async def test_explicit_poll_url_skips_crabfit(workflow):
    """An explicit poll_url wins - Crab Fit is not called."""
    mock_client = AsyncMock()
    mock_client.send_email.return_value = "<anchor>"
    workflow._google.list_sheet_tabs.return_value = [{"title": "ΔΣ04-2026"}]

    fake_crabfit = AsyncMock()
    ctx = {"poll_url": "https://when2meet.com/poll", "crabfit_dates": ["2026-06-17"]}
    with patch("src.workflows.board_meeting_invitation.M365MailClient", return_value=mock_client), \
         patch("src.integrations.crabfit.CrabFitClient", return_value=fake_crabfit), \
         patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.ms_client_id = "x"
        mock_settings.ms_tenant_id = "y"
        mock_settings.google.agenda_sheet_id = "sheet-id"
        mock_settings.testing.test_email = ""
        await workflow._step_send_scheduling_email(ctx)

    fake_crabfit.create_event.assert_not_called()
    assert "when2meet.com/poll" in mock_client.send_email.call_args.kwargs["body"]


@pytest.mark.asyncio
async def test_send_scheduling_email_body_without_poll_url(workflow):
    """Without --poll-url the body strips 'τον προγραμματισμό' and 'συμπληρώστε διαθεσιμότητες'."""
    mock_client = AsyncMock()
    mock_client.send_email.return_value = "<anchor>"
    workflow._google.list_sheet_tabs.return_value = [{"title": "ΔΣ04-2026"}]

    with patch("src.workflows.board_meeting_invitation.M365MailClient", return_value=mock_client), \
         patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.ms_client_id = "x"
        mock_settings.ms_tenant_id = "y"
        mock_settings.google.agenda_sheet_id = "sheet-id"
        mock_settings.testing.test_email = ""
        await workflow._step_send_scheduling_email({})

    body = mock_client.send_email.call_args.kwargs["body"]
    assert "ΗΜΕΡΗΣΙΑ ΔΙΑΤΑΞΗ" in body       # agenda CTA present in no-poll variant
    assert "ΔΙΑΘΕΣΙΜΟΤΗΤΕΣ" not in body   # poll CTA absent when no poll_url


@pytest.mark.asyncio
async def test_send_scheduling_email_deadline_default(workflow):
    """Deadline defaults to today + 4 days, formatted as Greek long-form
    (e.g. ``15 Ιουνίου``) - see _step_send_scheduling_email."""
    from src.workflows.board_meeting_invitation import _GREEK_MONTHS

    mock_client = AsyncMock()
    mock_client.send_email.return_value = "<anchor>"
    workflow._google.list_sheet_tabs.return_value = [{"title": "ΔΣ04-2026"}]

    with patch("src.workflows.board_meeting_invitation.M365MailClient", return_value=mock_client), \
         patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.ms_client_id = "x"
        mock_settings.ms_tenant_id = "y"
        mock_settings.google.agenda_sheet_id = "sheet-id"
        mock_settings.testing.test_email = ""
        await workflow._step_send_scheduling_email({"poll_url": "https://x"})

    body = mock_client.send_email.call_args.kwargs["body"]
    expected = date.today() + timedelta(days=4)
    expected_str = f"{expected.day} {_GREEK_MONTHS[expected.month]}"
    assert expected_str in body


@pytest.mark.asyncio
async def test_send_scheduling_email_deadline_override(workflow):
    mock_client = AsyncMock()
    mock_client.send_email.return_value = "<anchor>"
    workflow._google.list_sheet_tabs.return_value = [{"title": "ΔΣ04-2026"}]

    with patch("src.workflows.board_meeting_invitation.M365MailClient", return_value=mock_client), \
         patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.ms_client_id = "x"
        mock_settings.ms_tenant_id = "y"
        mock_settings.google.agenda_sheet_id = "sheet-id"
        mock_settings.testing.test_email = ""
        await workflow._step_send_scheduling_email({
            "poll_url": "https://x",
            "response_deadline": "2026-06-15",
        })

    body = mock_client.send_email.call_args.kwargs["body"]
    # Greek long-form: "15 Ιουνίου" (no year - deadline is short context)
    assert "15 Ιουνίου" in body


@pytest.mark.asyncio
async def test_send_scheduling_email_skips_without_ms_creds(workflow):
    with patch("src.workflows.board_meeting_invitation.M365MailClient") as mock_cls, \
         patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.ms_client_id = ""
        mock_settings.ms_tenant_id = ""
        result = await workflow._step_send_scheduling_email({})
    assert result.success
    assert result.data.get("scheduling_email_skipped") is True
    mock_cls.assert_not_called()


# ─── await_approval ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_await_approval_always_halts(workflow):
    """The step itself returns success; halting is enforced by requires_approval=True."""
    result = await workflow._step_await_approval({})
    assert result.success
    assert result.data["awaiting_approval"] is True
    # WorkflowStep flag must be unconditionally True
    step = next(s for s in workflow.steps if s.name == "await_approval")
    assert step.requires_approval is True


# ─── schedule_zoom ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_schedule_zoom_uses_computed_duration_and_header(workflow):
    workflow._zoom.schedule_meeting.return_value = {
        "id": 123, "join_url": "https://zoom.us/j/123", "password": "abc",
    }
    workflow._zoom.add_registrants.return_value = []
    ctx = {
        "meeting_date": "2026-04-15",
        "meeting_time": "18:00",
        "meeting_number": "42",
        "agenda_items": ["Item 1", "Item 2"],
        "meeting_duration_minutes": 90,
    }
    with patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.workflows.board_meeting.board_members = []
        mock_settings.zoom.meeting_defaults.duration = 120
        result = await workflow._step_schedule_zoom(ctx)

    assert result.success
    kwargs = workflow._zoom.schedule_meeting.call_args.kwargs
    assert kwargs["duration"] == 90
    assert kwargs["agenda"].startswith("Ημερήσια Διάταξη\n")
    assert "Item 1" in kwargs["agenda"]


@pytest.mark.asyncio
async def test_step_schedule_zoom_falls_back_to_default_duration(workflow):
    workflow._zoom.schedule_meeting.return_value = {
        "id": 1, "join_url": "u", "password": "p",
    }
    workflow._zoom.add_registrants.return_value = []
    ctx = {
        "meeting_date": "2026-04-15",
        "meeting_time": "18:00",
        "meeting_number": "42",
        "agenda_items": [],
    }
    with patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.workflows.board_meeting.board_members = []
        mock_settings.zoom.meeting_defaults.duration = 120
        await workflow._step_schedule_zoom(ctx)
    assert workflow._zoom.schedule_meeting.call_args.kwargs["duration"] == 120


@pytest.mark.asyncio
async def test_step_schedule_zoom_no_board_join_urls_in_result(workflow):
    """board_join_urls is no longer stored in ctx."""
    workflow._zoom.schedule_meeting.return_value = {
        "id": 1, "join_url": "u", "password": "p",
    }
    workflow._zoom.add_registrants.return_value = []
    ctx = {
        "meeting_date": "2026-04-15",
        "meeting_time": "18:00",
        "meeting_number": "42",
        "agenda_items": [],
    }
    with patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.workflows.board_meeting.board_members = []
        mock_settings.zoom.meeting_defaults.duration = 120
        result = await workflow._step_schedule_zoom(ctx)
    assert "board_join_urls" not in result.data


# ─── draft_invitation & generate_pdf ──────────────────────────────────────


@pytest.mark.asyncio
async def test_step_draft_invitation(workflow):
    ctx = {
        "meeting_number": "42",
        "meeting_date": "2026-04-15",
        "meeting_time": "18:00",
        "meeting_type": "ΤΑΚΤΙΚΗ",
        "location": "ΔΙΑΔΙΚΤΥΑΚΑ",
        "zoom_join_url": "https://zoom.us/j/123",
        "agenda_items": ["Θέμα 1"],
        "protocol_number": "2026_042",
    }
    result = await workflow._step_draft_invitation(ctx)
    assert result.success
    reps = result.data["invitation_replacements"]
    assert reps["[ΩΡΑ ΕΝΑΡΞΗΣ]"] == "18:00"


@pytest.mark.asyncio
async def test_step_generate_pdf_no_drive_upload(workflow, tmp_path):
    """generate_pdf must NOT upload to Drive - no pdf_drive_link in result."""
    workflow._google.copy_document.return_value = "working-doc-id"

    replacements = {
        "[ΗΜΕΡΟΜΗΝΙΑ]": "15 Απριλίου 2026",
        "[ΩΡΑ ΕΝΑΡΞΗΣ]": "18:00",
        "[ΤΟΠΟΘΕΣΙΑ]": "διαδικτυακά",
        "[ΤΥΠΟΣ]": "ΤΑΚΤΙΚΗΣ",
        "_agenda_items_": ["Θέμα 1"],
    }
    ctx = {
        "invitation_replacements": replacements,
        "meeting_number": "42",
        "meeting_date": "2026-04-15",
        "zoom_join_url": "https://zoom.us/j/123",
    }
    with patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.google.invitation_template_id = "template-doc-id"
        result = await workflow._step_generate_pdf(ctx)

    assert result.success
    assert "pdf_path" in result.data
    assert "pdf_drive_link" not in result.data
    # upload_pdf_and_share must NOT be called
    assert not workflow._google.upload_pdf_and_share.called


# ─── approval gate ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_approval_auto_passes_in_live_mode(workflow):
    result = await workflow._step_approval({"test_mode": False})
    assert result.success
    assert result.data.get("auto_approved") is True


@pytest.mark.asyncio
async def test_step_approval_in_test_mode(workflow):
    result = await workflow._step_approval({"test_mode": True})
    assert result.success
    assert result.data.get("approved") is True
    assert "auto_approved" not in result.data


# ─── archive (fallback email) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_archive_success(workflow, tmp_path):
    pdf_file = tmp_path / "test.pdf"
    pdf_file.write_bytes(b"%PDF-fake")
    workflow._onedrive.upload_file.return_value = {"id": "fid", "size": 1}
    workflow._onedrive.get_share_link.return_value = "https://share/abc"
    ctx = {"pdf_path": str(pdf_file), "meeting_date": "2026-04-15"}
    with patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.ms_client_id = "x"
        mock_settings.ms_tenant_id = "y"
        result = await workflow._step_archive(ctx)
    assert result.success
    assert result.data["archive_file_id"] == "fid"


@pytest.mark.asyncio
async def test_step_archive_failure_sends_fallback_email(workflow, tmp_path):
    """When SharePoint upload fails the PDF must be emailed to members@amnesty.org.gr."""
    pdf_file = tmp_path / "[2026_001] Πρόσκληση.pdf"
    pdf_file.write_bytes(b"%PDF-fake")

    workflow._onedrive.upload_file.side_effect = RuntimeError("503 boom")

    mock_mail = AsyncMock()
    mock_mail.send_email.return_value = "<id>"
    ctx = {
        "pdf_path": str(pdf_file),
        "pdf_filename": pdf_file.name,
        "meeting_date": "2026-04-15",
    }
    with patch("src.workflows.board_meeting_invitation.M365MailClient", return_value=mock_mail), \
         patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.ms_client_id = "x"
        mock_settings.ms_tenant_id = "y"
        mock_settings.onedrive.yearly_subfolder = "Αρχείο ανά έτος"
        result = await workflow._step_archive(ctx)

    assert result.success
    assert result.data.get("archive_emailed") is True

    mock_mail.send_email.assert_awaited_once()
    kwargs = mock_mail.send_email.call_args.kwargs
    assert kwargs["to"] == "members@amnesty.org.gr"
    assert "Σφάλμα αρχειοθέτησης" in kwargs["subject"]
    assert kwargs["attachments"] == [pdf_file]


# ─── send_newsletter ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_send_newsletter_test_mode_keeps_draft(workflow):
    workflow._brevo.send_campaign.return_value = {"campaign_id": 123}
    ctx = {
        "test_mode": True,
        "brevo_template_id": 5,
        "brevo_list_ids": [1],
        "meeting_number": "42",
        "meeting_date": "2026-04-15",
        "meeting_time": "18:00",
        "meeting_type": "ΤΑΚΤΙΚΗ",
        "agenda_items": ["A"],
    }
    with patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.brevo.newsletter_template_id = 5
        mock_settings.brevo.newsletter_list_ids = [1]
        mock_settings.brevo.master_list_id = 74
        mock_settings.testing.test_email = "test@example.com"
        result = await workflow._step_send_newsletter(ctx)

    assert result.success
    assert result.data["newsletter_campaign_id"] == 123
    assert result.data["newsletter_skipped"] is True
    workflow._brevo.send_campaign_now.assert_not_called()


@pytest.mark.asyncio
async def test_step_send_newsletter_live_sends_and_publishes_event(workflow):
    workflow._brevo.send_campaign.return_value = {"campaign_id": 99}
    workflow._brevo.send_campaign_now.return_value = None

    ctx = {
        "test_mode": False,
        "brevo_template_id": 5,
        "brevo_list_ids": [1],
        "meeting_number": "42",
        "meeting_date": "2026-04-15",
        "meeting_time": "18:00",
        "meeting_type": "ΤΑΚΤΙΚΗ",
        "agenda_items": ["A"],
        "raw_meeting_id": "ΔΣ04-2026",
        "zoom_join_url": "https://zoom.us/j/123",
    }
    with patch("src.workflows.board_meeting_invitation.settings") as mock_settings, \
         patch("src.workflows.board_meeting_invitation._publish_board_meeting_scheduled",
               new_callable=AsyncMock) as mock_publish:
        mock_settings.brevo.newsletter_template_id = 5
        mock_settings.brevo.newsletter_list_ids = [1]
        mock_settings.brevo.master_list_id = 74
        mock_settings.testing.test_email = ""
        result = await workflow._step_send_newsletter(ctx)

    assert result.success
    assert result.data["newsletter_sent"] is True
    assert result.data["bus_event_published"] is True
    workflow._brevo.send_campaign_now.assert_awaited_once_with(99, workflow=workflow.name)
    mock_publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_step_send_newsletter_skips_without_template(workflow):
    ctx = {"meeting_number": "42", "meeting_date": "2026-04-15", "agenda_items": []}
    with patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.brevo.newsletter_template_id = None
        mock_settings.brevo.newsletter_list_ids = []
        mock_settings.brevo.master_list_id = 0
        mock_settings.testing.test_email = ""
        result = await workflow._step_send_newsletter(ctx)
    assert result.success
    assert result.data.get("newsletter_skipped") is True


@pytest.mark.asyncio
async def test_newsletter_params_no_pdf_link(workflow):
    """[PDF_LINK] must NOT appear in template params."""
    ctx = {
        "meeting_number": "42",
        "meeting_date": "2026-04-15",
        "meeting_time": "18:00",
        "meeting_type": "ΤΑΚΤΙΚΗ",
        "zoom_join_url": "https://zoom.us/j/123",
        "agenda_items": ["A"],
    }
    params, *_ = workflow._build_newsletter_params(ctx)
    assert "[PDF_LINK]" not in params


# ─── confirm_newsletter ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_confirm_newsletter_noops_when_already_sent(workflow):
    """In live mode, confirm_newsletter is a no-op (send already happened)."""
    ctx = {"newsletter_sent": True}
    result = await workflow._step_confirm_newsletter(ctx)
    assert result.success
    assert result.data["newsletter_sent"] is True
    workflow._brevo.send_campaign_now.assert_not_called()


@pytest.mark.asyncio
async def test_step_confirm_newsletter_test_mode_no_live_send(workflow):
    """In test_mode the draft is retained, no live send."""
    ctx = {"newsletter_skipped": True, "newsletter_campaign_id": 1}
    result = await workflow._step_confirm_newsletter(ctx)
    assert result.success
    assert result.data["newsletter_sent"] is False
    workflow._brevo.send_campaign_now.assert_not_called()


# ─── meeting_id derivation ────────────────────────────────────────────────


def test_derive_meeting_id_uses_raw_meeting_id():
    from src.workflows.board_meeting_invitation import _derive_meeting_id
    mid = _derive_meeting_id({"raw_meeting_id": "ΔΣ04-2026"})
    assert mid == "board_meeting:ΔΣ04-2026"


def test_derive_meeting_id_fallback_to_number_and_year():
    from src.workflows.board_meeting_invitation import _derive_meeting_id
    mid = _derive_meeting_id({"meeting_number": "5", "meeting_date": "2026-05-21"})
    assert mid == "board_meeting:ΔΣ05-2026"


# ─── step sequence ────────────────────────────────────────────────────────


def test_step_sequence_matches_wave2_order(workflow):
    expected = [
        "send_scheduling_email",
        "await_approval",
        "read_agenda",
        "init_meeting_thread",
        "schedule_zoom",
        "draft_invitation",
        "generate_pdf",
        "approval",
        "archive",
        "send_board_email",
        "send_newsletter",
        "confirm_newsletter",
    ]
    assert [s.name for s in workflow.steps] == expected
    assert len(workflow.steps) == 12


# ─── init_meeting_thread ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_init_meeting_thread(workflow):
    ctx = {"raw_meeting_id": "ΔΣ04-2026"}
    result = await workflow._step_init_meeting_thread(ctx)
    assert result.success
    assert result.data["meeting_id"] == "board_meeting:ΔΣ04-2026"


@pytest.mark.asyncio
async def test_step_init_meeting_thread_no_data(workflow):
    result = await workflow._step_init_meeting_thread({})
    assert not result.success


# ─── send_board_email ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_send_board_email_threaded_reply(workflow):
    mock_client = AsyncMock()
    mock_client.send_reply.return_value = "<reply-id>"
    ctx = {
        "email_thread_anchor": "<anchor>",
        "meeting_number": "5",
        "meeting_date": "2026-05-21",
        "meeting_time": "18:00",
        "raw_meeting_id": "ΔΣ05-2026",
        "zoom_join_url": "https://zoom.us/j/123",
        "zoom_meeting_id": "123",
        "zoom_passcode": "pwd",
        "archive_share_link": "https://sharepoint.com/share/xyz",
    }
    with patch("src.workflows.board_meeting_invitation.M365MailClient", return_value=mock_client), \
         patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.ms_client_id = "x"
        mock_settings.ms_tenant_id = "y"
        result = await workflow._step_send_board_email(ctx)

    assert result.success
    kwargs = mock_client.send_reply.call_args.kwargs
    assert kwargs["to"] == "board@amnesty.org.gr"
    assert kwargs["html"] is True
    assert "sharepoint.com/share/xyz" in kwargs["body"]
    assert "zoom.us/j/123" in kwargs["body"]
    assert "ΣΥΜΜΕΤΟΧΗ" in kwargs["body"]   # Zoom CTA button present


@pytest.mark.asyncio
async def test_step_send_board_email_test_mode_redirects_to_test_email(workflow):
    """In test_mode the reply goes to settings.testing.test_email, NOT skipped."""
    fake_client = AsyncMock()
    fake_client.send_reply = AsyncMock(return_value="<reply-id>")
    with patch("src.workflows.board_meeting_invitation.M365MailClient", return_value=fake_client), \
         patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.ms_client_id = "x"
        mock_settings.ms_tenant_id = "y"
        mock_settings.testing.test_email = "test@example.com"
        result = await workflow._step_send_board_email({
            "test_mode": True,
            "email_thread_anchor": "<anchor>",
            "meeting_date": "2026-04-21",
            "meeting_time": "18:00",
            "zoom_join_url": "https://zoom.us/j/123",
            "zoom_meeting_id": "123",
            "zoom_passcode": "abc",
            "archive_share_link": "https://share/x",
        })
    assert result.success
    assert result.data.get("board_email_skipped") is not True
    assert result.data.get("board_email_message_id") == "<reply-id>"
    # Verify the recipient was the test inbox, not the board address
    call_kwargs = fake_client.send_reply.call_args.kwargs
    assert call_kwargs["to"] == "test@example.com"


@pytest.mark.asyncio
async def test_step_send_board_email_test_mode_skips_when_no_test_email(workflow):
    """test_mode + empty test_email → skip cleanly (don't fall through to board)."""
    with patch("src.workflows.board_meeting_invitation.M365MailClient") as mock_cls, \
         patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.ms_client_id = "x"
        mock_settings.ms_tenant_id = "y"
        mock_settings.testing.test_email = ""
        result = await workflow._step_send_board_email({
            "test_mode": True,
            "email_thread_anchor": "<anchor>",
        })
    assert result.success
    assert result.data.get("board_email_skipped") is True
    mock_cls.assert_not_called()


@pytest.mark.asyncio
async def test_step_send_board_email_skips_without_anchor(workflow):
    with patch("src.workflows.board_meeting_invitation.M365MailClient") as mock_cls, \
         patch("src.workflows.board_meeting_invitation.settings") as mock_settings:
        mock_settings.ms_client_id = "x"
        mock_settings.ms_tenant_id = "y"
        result = await workflow._step_send_board_email({})
    assert result.success
    assert result.data.get("board_email_skipped") is True
    mock_cls.assert_not_called()
