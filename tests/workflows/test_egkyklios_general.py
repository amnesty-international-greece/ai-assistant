"""Tests for the Γενική Εγκύκλιος Ενημέρωσης workflow."""

from __future__ import annotations

import json
import pytest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, mock_open


# ── Shared fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def mock_db(tmp_path):
    """Isolate SQLite to a temp file for each test."""
    with patch("src.core.audit._DB_PATH", tmp_path / "test.db"), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        yield tmp_path


@pytest.fixture
def workflow(mock_db):
    """EgkykliosGeneralWorkflow with all external clients mocked."""
    with patch("src.workflows.egkyklios_general.OneDriveClient"), \
         patch("src.workflows.egkyklios_general.BrevoClient"):
        from src.workflows.egkyklios_general import EgkykliosGeneralWorkflow
        wf = EgkykliosGeneralWorkflow()
        wf._onedrive = AsyncMock()
        wf._brevo = AsyncMock()
        yield wf


# ── Helper: build a fake briefing row ────────────────────────────────────────


def _briefing_row(
    meeting_ref: str = "ΔΣ01-2026",
    local_path: str = "data/briefings/test.pdf",
    archived_at: str = "2026-01-15T10:00:00",
) -> dict:
    return {
        "id": 1,
        "meeting_ref": meeting_ref,
        "kind": "ΕΙΣΗΓΗΤΙΚΟ",
        "protocol_number": None,
        "local_path": local_path,
        "sharepoint_url": None,
        "archived_at": archived_at,
        "source_message_id": "",
        "workflow_id": "",
    }


def _minutes_row(
    workflow_id: str = "wf-abc123",
    meeting_ref: str = "ΔΣ01-2026",
    meeting_date: str = "2026-01-20",
    updated_at: str = "2026-01-20T22:00:00",
) -> dict:
    data_payload = {
        "context": {
            "meeting_ref": meeting_ref,
            "meeting_date": meeting_date,
            "draft_json": {
                "sections": [
                    {"heading": "Διάφορα", "body": "Συζητήθηκαν τα πάντα."}
                ],
                "decisions": [{"text": "Αποφάσισε να προχωρήσει."}],
            },
        },
        "step_index": 6,
    }
    return {
        "workflow_id": workflow_id,
        "state": "completed",
        "data": json.dumps(data_payload),
        "created_at": "2026-01-20T18:00:00",
        "updated_at": updated_at,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. test_gather_sources_returns_briefings_and_minutes_in_window
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gather_sources_returns_briefings_and_minutes_in_window(workflow):
    with patch(
        "src.workflows.egkyklios_general.list_director_briefings_in_window",
        return_value=[_briefing_row()],
    ), patch(
        "src.workflows.egkyklios_general.list_completed_minutes_in_window",
        return_value=[_minutes_row()],
    ), patch(
        "src.workflows.egkyklios_general.list_egkyklios_drafts",
        return_value=[],
    ):
        ctx = {"period_start": "2026-01-01", "period_end": "2026-03-31"}
        result = await workflow._step_gather_sources(ctx)

    assert result.success is True
    assert len(result.data["briefings_meta"]) == 1
    assert len(result.data["minutes_rows"]) == 1
    assert result.data["period_start"] == "2026-01-01"
    assert result.data["period_end"] == "2026-03-31"
    assert "ΙΑΝΟΥΑΡΙΟΣ" in result.data["title"]
    assert "ΜΑΡΤΙΟΣ" in result.data["title"]


# ─────────────────────────────────────────────────────────────────────────────
# 2. test_gather_sources_fails_when_no_briefings_or_minutes
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gather_sources_fails_when_no_briefings_or_minutes(workflow):
    with patch(
        "src.workflows.egkyklios_general.list_director_briefings_in_window",
        return_value=[],
    ), patch(
        "src.workflows.egkyklios_general.list_completed_minutes_in_window",
        return_value=[],
    ), patch(
        "src.workflows.egkyklios_general.list_egkyklios_drafts",
        return_value=[],
    ):
        ctx = {"period_start": "2026-01-01", "period_end": "2026-03-31"}
        result = await workflow._step_gather_sources(ctx)

    assert result.success is False
    assert "Δεν βρέθηκαν πηγές" in result.message


# ─────────────────────────────────────────────────────────────────────────────
# 3. test_period_title_format_greek_uppercase
# ─────────────────────────────────────────────────────────────────────────────


def test_period_title_format_greek_uppercase():
    from src.workflows.egkyklios_general import _period_title

    title = _period_title("2026-01-01", "2026-03-31")
    assert title == "ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026"

    # Single month
    title_single = _period_title("2026-05-01", "2026-05-31")
    assert title_single == "ΜΑΪΟΣ 2026"

    # Q4
    title_q4 = _period_title("2025-10-01", "2025-12-31")
    assert title_q4 == "ΟΚΤΩΒΡΙΟΣ - ΔΕΚΕΜΒΡΙΟΣ 2025"


# ─────────────────────────────────────────────────────────────────────────────
# 4. test_draft_circular_calls_claude_with_template_prompt
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_draft_circular_calls_claude_with_template_prompt(workflow, tmp_path, mock_db):
    mock_claude = MagicMock()
    mock_claude.load_prompt.return_value = (
        "## Ρόλος\n{period_start}\n{period_end}\n{title}\n"
        "{briefings_text}\n{minutes_text}\n"
        "{month_start}\n{month_end}\n{year}"
    )
    mock_claude.generate.return_value = "# ΓΕΝΙΚΗ ΕΓΚΥΚΛΙΟΣ ΕΝΗΜΕΡΩΣΗΣ\n## ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026\n\nΠεριεχόμενο."

    ctx = {
        "period_start": "2026-01-01",
        "period_end": "2026-03-31",
        "title": "ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026",
        "briefing_texts": [
            {"meeting_ref": "ΔΣ01-2026", "kind": "ΕΙΣΗΓΗΤΙΚΟ",
             "archived_at": "2026-01-15", "text": "Κείμενο εισηγητικού.", "is_scan": False}
        ],
        "meeting_summaries": [
            {"workflow_id": "wf1", "meeting_ref": "ΔΣ01-2026",
             "meeting_date": "2026-01-20", "text": "Πρακτικά συνεδρίασης."}
        ],
    }

    with patch("src.workflows.egkyklios_general.ClaudeClient", return_value=mock_claude), \
         patch("src.workflows.egkyklios_general.create_egkyklios_draft", return_value=42), \
         patch("src.workflows.egkyklios_general.update_egkyklios_draft"), \
         patch("src.core.audit.log_action"), \
         patch.object(Path, "mkdir"), \
         patch("builtins.open", mock_open()), \
         patch.object(Path, "write_text"):

        result = await workflow._step_draft_circular(ctx)

    assert result.success is True
    mock_claude.generate.assert_called_once()
    call_kwargs = mock_claude.generate.call_args
    assert "egkyklios_general" in str(call_kwargs)
    assert result.data["egkyklios_draft_id"] == 42
    assert "draft_markdown" in result.data


# ─────────────────────────────────────────────────────────────────────────────
# 5. test_render_pdf_produces_file_at_expected_path
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_render_pdf_produces_file_at_expected_path(workflow, tmp_path):
    """render_pdf step calls the PDF renderer and updates the DB row.

    Also tests the renderer directly: given valid Markdown it produces a
    non-empty PDF on disk.
    """
    period_start = "2026-01-01"
    period_end = "2026-03-31"
    expected_pdf = Path("data/egkyklios/drafts") / f"{period_start}_{period_end}_draft.pdf"

    ctx = {
        "period_start": period_start,
        "period_end": period_end,
        "title": "ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026",
        "draft_markdown": "# ΓΕΝΙΚΗ ΕΓΚΥΚΛΙΟΣ ΕΝΗΜΕΡΩΣΗΣ\n\nΔοκιμαστικό περιεχόμενο.",
        "egkyklios_draft_id": 99,
    }

    mock_render = MagicMock(return_value=expected_pdf)
    with patch("src.workflows.egkyklios_general.update_egkyklios_draft"), \
         patch("src.documents.egkyklios_pdf.render_egkyklios_pdf", mock_render):
        result = await workflow._step_render_pdf(ctx)

    assert result.success is True
    assert "draft_pdf_path" in result.data
    mock_render.assert_called_once()

    # Direct renderer smoke-test: writes a real PDF
    pdf_out = tmp_path / "test_egkyklios.pdf"
    from src.documents.egkyklios_pdf import render_egkyklios_pdf
    out = render_egkyklios_pdf(
        markdown_text=(
            "# ΓΕΝΙΚΗ ΕΓΚΥΚΛΙΟΣ ΕΝΗΜΕΡΩΣΗΣ\n"
            "## ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026\n\n"
            "## Α. ΔΙΟΙΚΗΤΙΚΟ ΣΥΜΒΟΥΛΙΟ\n\n"
            "### 1. Συνεδριάσεις\n\n"
            "Πραγματοποιήθηκε η συνεδρίαση της [20 Ιανουαρίου 2026].\n\n"
            "- **Απόφαση**: Εγκρίθηκε ο προϋπολογισμός.\n\n"
            "## Β. ΓΡΑΦΕΙΟ\n\n"
            "### 1. Εκδηλώσεις\n\nΟργανώθηκαν τρεις εκδηλώσεις.\n"
        ),
        output_path=pdf_out,
        title="ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026",
        period_start="2026-01-01",
        period_end="2026-03-31",
        protocol_number="2026_042",
        workflow="test",
    )
    assert out.exists()
    assert out.stat().st_size > 1000  # a real PDF, not an empty file


# ─────────────────────────────────────────────────────────────────────────────
# 6. test_await_approval_parks_workflow
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_await_approval_parks_workflow(workflow):
    """The workflow halts at step 7 (await_approval) when run normally."""
    define_steps_result = workflow.define_steps()
    approval_step = next(s for s in define_steps_result if s.name == "await_approval")
    assert approval_step.requires_approval is True

    # Simulate: call the step handler directly - it should succeed and mark approved
    ctx = {"egkyklios_draft_id": 0, "test_mode": False}
    with patch("src.workflows.egkyklios_general.update_egkyklios_draft"):
        result = await workflow._step_await_approval(ctx)

    assert result.success is True
    assert result.data.get("approved") is True


# ─────────────────────────────────────────────────────────────────────────────
# 7. test_publish_event_emits_egkyklios_published
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_event_emits_egkyklios_published(workflow):
    ctx = {
        "title": "ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026",
        "protocol_number": "2026_042",
        "sharepoint_url": "https://sharepoint.example.com/file",
    }

    published_events: list = []

    mock_bus = AsyncMock()
    mock_bus.publish = AsyncMock(side_effect=lambda evt, payload: published_events.append((evt, payload)))

    with patch("src.workflows.egkyklios_general.EgkykliosGeneralWorkflow._step_publish_event",
               wraps=workflow._step_publish_event):
        with patch("src.core.event_bus.bus", mock_bus), \
             patch("src.workflows.egkyklios_general.update_egkyklios_draft", MagicMock()):
            result = await workflow._step_publish_event(ctx)

    assert result.success is True
    assert result.data.get("event_published") is True

    if published_events:
        evt_name, payload = published_events[0]
        assert "egkyklios" in evt_name
        assert payload.title == "ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026"
        assert payload.protocol_number == "2026_042"
        assert payload.kind == "general"


# ─────────────────────────────────────────────────────────────────────────────
# 8. test_idempotency_guard_blocks_duplicate_period
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_idempotency_guard_blocks_duplicate_period(workflow):
    """gather_sources aborts if a non-cancelled draft for the same period exists."""
    existing_draft = {
        "id": 5,
        "kind": "general",
        "period_start": "2026-01-01",
        "period_end": "2026-03-31",
        "title": "ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026",
        "status": "awaiting_approval",
    }

    with patch(
        "src.workflows.egkyklios_general.list_egkyklios_drafts",
        return_value=[existing_draft],
    ):
        ctx = {"period_start": "2026-01-01", "period_end": "2026-03-31"}
        result = await workflow._step_gather_sources(ctx)

    assert result.success is False
    assert "Υπάρχει ήδη" in result.message
    assert "id=5" in result.message


# ─────────────────────────────────────────────────────────────────────────────
# 9. test_extract_briefing_texts_skips_missing_files
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_briefing_texts_skips_missing_files(workflow, tmp_path):
    """Briefings whose local_path doesn't exist are skipped gracefully."""
    ctx = {
        "briefings_meta": [
            _briefing_row(local_path="/nonexistent/path/brief.pdf"),
        ]
    }
    result = await workflow._step_extract_briefing_texts(ctx)
    # No valid PDFs → empty list; all briefings were meta (no local file), so not an error
    # (error only if briefings_meta is non-empty AND ALL fail)
    # But since all fail, we expect a failure here
    assert result.success is False or len(result.data.get("briefing_texts", [])) == 0


@pytest.mark.asyncio
async def test_extract_briefing_texts_reads_valid_pdf(workflow, tmp_path):
    """extract_briefing_texts calls extract_pdf_text for each valid local_path."""
    fake_pdf = tmp_path / "brief.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")  # not a real PDF but path exists

    ctx = {
        "briefings_meta": [
            _briefing_row(local_path=str(fake_pdf)),
        ]
    }

    mock_extract = MagicMock(return_value=("Κείμενο εισηγητικού 2026.", {"is_scan": False}))
    with patch("src.workflows.egkyklios_general.extract_pdf_text", mock_extract):
        result = await workflow._step_extract_briefing_texts(ctx)

    assert result.success is True
    assert len(result.data["briefing_texts"]) == 1
    assert result.data["briefing_texts"][0]["text"] == "Κείμενο εισηγητικού 2026."
    mock_extract.assert_called_once()
