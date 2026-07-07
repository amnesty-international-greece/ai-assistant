"""Tests for the archive workflow (Phase 1 + 2)."""
from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_db(tmp_path):
    db_path = tmp_path / "test.db"
    with patch("src.core.audit._DB_PATH", db_path), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        yield


@pytest.fixture
def workflow(mock_db):
    with patch("src.workflows.archive.OneDriveClient"):
        from src.workflows.archive import ArchiveWorkflow
        wf = ArchiveWorkflow()
        wf._onedrive = AsyncMock()
        wf._onedrive.get_current_year_max_seq = AsyncMock(return_value=0)
        wf._onedrive.read_recent_entries = AsyncMock(return_value=[])
        yield wf


def _write_minimal_pdf(path: Path, text: str = "Hello world. " * 20) -> None:
    """Create a real (tiny) PDF that PyPDF2 can read."""
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    c.drawString(72, 720, text)
    c.showPage()
    c.save()


# ── _step_intake ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_intake_rejects_non_pdf(workflow, tmp_path):
    not_pdf = tmp_path / "doc.docx"
    not_pdf.write_text("hello")
    result = await workflow._step_intake({"pdf_path": str(not_pdf)})
    assert not result.success
    assert "PDF" in result.message
    assert "PDF" in result.message  # mentions PDF


@pytest.mark.asyncio
async def test_intake_rejects_missing_file(workflow, tmp_path):
    result = await workflow._step_intake({"pdf_path": str(tmp_path / "nope.pdf")})
    assert not result.success
    assert "not found" in result.message


@pytest.mark.asyncio
async def test_intake_rejects_encrypted_pdf(workflow, tmp_path):
    pdf = tmp_path / "doc.pdf"
    _write_minimal_pdf(pdf)
    with patch("src.workflows.archive.extract_pdf_text") as mock_extract:
        from src.utils.pdf_text import EncryptedPDFError
        mock_extract.side_effect = EncryptedPDFError("Το PDF είναι κρυπτογραφημένο…")
        result = await workflow._step_intake({"pdf_path": str(pdf)})
    assert not result.success
    assert "κρυπτογραφημένο" in result.message


@pytest.mark.asyncio
async def test_intake_succeeds_for_valid_pdf(workflow, tmp_path):
    pdf = tmp_path / "doc.pdf"
    _write_minimal_pdf(pdf)
    result = await workflow._step_intake({"pdf_path": str(pdf), "sender_email": "x@y.gr"})
    assert result.success, result.message
    assert result.data["pdf_filename_orig"] == "doc.pdf"
    assert result.data["sender_email"] == "x@y.gr"
    assert "pdf_metadata" in result.data


# ── _step_extract_metadata ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_metadata_calls_classify_and_returns_result(workflow):
    fake_initial = {
        "title": "Foo",
        "labels": ["Διοικητικά"],
        "key_points": "kp",
        "existing_protocol": None,
        "category_matched": "Πρακτικά",
        "confidence": 0.9,
        "reasoning_brief": "ok",
    }
    with patch("src.workflows.archive.archive_llm") as mock_llm:
        mock_llm.classify_document = AsyncMock(return_value=fake_initial)
        result = await workflow._step_extract_metadata({
            "pdf_filename_orig": "x.pdf",
            "pdf_text": "body",
        })
    assert result.success
    assert result.data["llm_result"]["title"] == "Foo"
    assert not result.data["llm_fallback_used"]


@pytest.mark.asyncio
async def test_extract_metadata_fallback_on_low_confidence(workflow):
    fake_initial = {
        "title": "Foo",
        "labels": ["Διοικητικά"],
        "key_points": "kp",
        "existing_protocol": None,
        "category_matched": "Πρακτικά",
        "confidence": 0.3,
        "reasoning_brief": "shaky",
    }
    fake_refined = {**fake_initial, "title": "Foo refined", "confidence": 0.8}
    workflow._onedrive.read_recent_entries = AsyncMock(return_value=[{"proto": "2025_001"}])
    with patch("src.workflows.archive.archive_llm") as mock_llm:
        mock_llm.classify_document = AsyncMock(return_value=fake_initial)
        mock_llm.refine_against_recent = AsyncMock(return_value=fake_refined)
        result = await workflow._step_extract_metadata({"pdf_filename_orig": "x.pdf"})
    assert result.success
    assert result.data["llm_fallback_used"] is True
    assert result.data["llm_result"]["title"] == "Foo refined"


@pytest.mark.asyncio
async def test_extract_metadata_fallback_on_ad_hoc(workflow):
    fake_initial = {
        "title": "Foo", "labels": [], "key_points": "",
        "existing_protocol": None, "category_matched": "ad-hoc",
        "confidence": 0.95, "reasoning_brief": "",
    }
    workflow._onedrive.read_recent_entries = AsyncMock(return_value=[{"proto": "2025_001"}])
    with patch("src.workflows.archive.archive_llm") as mock_llm:
        mock_llm.classify_document = AsyncMock(return_value=fake_initial)
        mock_llm.refine_against_recent = AsyncMock(return_value=fake_initial)
        result = await workflow._step_extract_metadata({"pdf_filename_orig": "x.pdf"})
    assert result.data["llm_fallback_used"] is True


@pytest.mark.asyncio
async def test_extract_metadata_low_confidence_adds_sentinel(workflow):
    fake = {
        "title": "T", "labels": [], "key_points": "summary",
        "existing_protocol": None, "category_matched": "Πρακτικά",
        "confidence": 0.4, "reasoning_brief": "",
    }
    with patch("src.workflows.archive.archive_llm") as mock_llm:
        mock_llm.classify_document = AsyncMock(return_value=fake)
        mock_llm.refine_against_recent = AsyncMock(return_value=fake)
        result = await workflow._step_extract_metadata({"pdf_filename_orig": "x.pdf"})
    assert result.data["llm_result"]["key_points"].startswith("[ΥΠΟ ΕΞΕΤΑΣΗ]")


@pytest.mark.asyncio
async def test_extract_metadata_respects_overrides(workflow):
    fake = {
        "title": "Foo", "labels": ["A"], "key_points": "",
        "existing_protocol": None, "category_matched": "X",
        "confidence": 0.99, "reasoning_brief": "",
    }
    with patch("src.workflows.archive.archive_llm") as mock_llm:
        mock_llm.classify_document = AsyncMock(return_value=fake)
        result = await workflow._step_extract_metadata({
            "pdf_filename_orig": "x.pdf",
            "override_title": "Custom title",
            "override_labels": ["Διοικητικά", "Πρακτικά"],
        })
    assert result.data["llm_result"]["title"] == "Custom title"
    assert result.data["llm_result"]["labels"] == ["Διοικητικά", "Πρακτικά"]


# ── _step_resolve_protocol ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_protocol_cli_override_wins(workflow):
    result = await workflow._step_resolve_protocol({
        "override_protocol": "2026_042",
        "llm_result": {"existing_protocol": "2026_999"},
    })
    assert result.success
    assert result.data["protocol_number"] == "2026_042"
    assert result.data["protocol_source"] == "cli_override"


@pytest.mark.asyncio
async def test_resolve_protocol_invalid_cli_override_fails(workflow):
    result = await workflow._step_resolve_protocol({"override_protocol": "garbage"})
    assert not result.success


@pytest.mark.asyncio
async def test_resolve_protocol_reuses_existing_from_document(workflow):
    result = await workflow._step_resolve_protocol({
        "llm_result": {"existing_protocol": "2026_017"},
    })
    assert result.success
    assert result.data["protocol_number"] == "2026_017"
    assert result.data["protocol_source"] == "document"


@pytest.mark.asyncio
async def test_resolve_protocol_reserves_next(workflow):
    workflow._onedrive.get_current_year_max_seq = AsyncMock(return_value=10)
    result = await workflow._step_resolve_protocol({"llm_result": {}})
    assert result.success
    assert result.data["protocol_source"] == "reserved"
    # Format YYYY_NNN
    import re
    assert re.match(r"^\d{4}_\d{3}$", result.data["protocol_number"])
    # Sequence is xlsx_max + 1
    assert result.data["protocol_number"].endswith("_011")


# ── _step_upload_and_register ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_and_register_test_mode_skips_io(workflow):
    result = await workflow._step_upload_and_register({
        "test_mode": True,
        "protocol_number": "2026_001",
        "llm_result": {"title": "Hello", "labels": ["A"], "key_points": ""},
    })
    assert result.success
    assert result.data["register_skipped"] is True
    workflow._onedrive.upload_file.assert_not_called()
    workflow._onedrive.append_protocol_row.assert_not_called()


@pytest.mark.asyncio
async def test_upload_and_register_uploads_and_appends(workflow, tmp_path):
    pdf = tmp_path / "doc.pdf"
    _write_minimal_pdf(pdf)
    workflow._onedrive.upload_file = AsyncMock(return_value={"id": "item-1"})
    workflow._onedrive.append_protocol_row = AsyncMock()
    workflow._onedrive.get_share_link = AsyncMock(return_value="https://share")

    with patch("src.workflows.archive.settings") as s:
        s.onedrive.yearly_subfolder = "Αρχείο ανά έτος"
        result = await workflow._step_upload_and_register({
            "pdf_path": str(pdf),
            "protocol_number": "2026_007",
            "llm_result": {
                "title": "Πρακτικά Συνεδρίασης",
                "labels": ["Διοικητικά", "Πρακτικά"],
                "key_points": "1. foo",
            },
        })

    assert result.success, result.message
    # Filename + folder
    workflow._onedrive.upload_file.assert_awaited_once()
    call = workflow._onedrive.upload_file.await_args
    assert call.kwargs["filename"] == "[2026_007] Πρακτικά Συνεδρίασης.pdf"
    assert call.kwargs["remote_folder"] == "Αρχείο ανά έτος/2026"
    # Protocol row appended
    workflow._onedrive.append_protocol_row.assert_awaited_once()
    row = workflow._onedrive.append_protocol_row.await_args.kwargs
    assert row["protocol_id"] == "2026_007"
    assert row["title"] == "Πρακτικά Συνεδρίασης"
    assert row["main_points"] == "1. foo"
    assert "Διοικητικά" in row["tags"]


@pytest.mark.asyncio
async def test_upload_and_register_rolls_back_upload_on_xlsx_failure(workflow, tmp_path):
    pdf = tmp_path / "doc.pdf"
    _write_minimal_pdf(pdf)
    workflow._onedrive.upload_file = AsyncMock(return_value={"id": "item-1"})
    workflow._onedrive.get_share_link = AsyncMock(return_value="")
    workflow._onedrive.append_protocol_row = AsyncMock(side_effect=RuntimeError("xlsx locked"))
    workflow._onedrive.delete_file = AsyncMock()

    with patch("src.workflows.archive.settings") as s:
        s.onedrive.yearly_subfolder = "Αρχείο ανά έτος"
        result = await workflow._step_upload_and_register({
            "pdf_path": str(pdf),
            "protocol_number": "2026_007",
            "llm_result": {"title": "T", "labels": [], "key_points": ""},
        })

    assert not result.success
    workflow._onedrive.delete_file.assert_awaited_once()


# ── rollback ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rollback_unwinds_in_correct_order(workflow, tmp_path):
    """delete_protocol_row → delete_file → release_reservation → delete local copy."""
    from src.core.audit import reserve_next_protocol_number, get_reservations_for_year

    # Set up a reservation for this workflow
    reserve_next_protocol_number(2026, workflow.workflow_id)
    assert len(get_reservations_for_year(2026)) == 1

    local_copy = tmp_path / "doc.pdf"
    _write_minimal_pdf(local_copy)
    assert local_copy.exists()

    workflow._onedrive.delete_protocol_row = AsyncMock()
    workflow._onedrive.delete_file = AsyncMock()

    call_order: list[str] = []
    workflow._onedrive.delete_protocol_row.side_effect = lambda *a, **k: call_order.append("row")
    workflow._onedrive.delete_file.side_effect = lambda *a, **k: call_order.append("file")

    with patch("src.workflows.archive.settings") as s:
        s.onedrive.yearly_subfolder = "Αρχείο ανά έτος"
        await workflow.rollback({
            "protocol_number": "2026_001",
            "remote_filename": "[2026_001] T.pdf",
            "upload_file_id": "item-1",
            "local_copy_path": str(local_copy),
        })

    assert call_order == ["row", "file"]
    # Reservation released
    assert len(get_reservations_for_year(2026)) == 0
    # Local copy deleted
    assert not local_copy.exists()


@pytest.mark.asyncio
async def test_rollback_in_test_mode_never_touches_sharepoint(workflow, tmp_path):
    """Regression test for the 2026-05-27 production incident.

    In test mode, the workflow never writes to SharePoint, so the rollback
    must NOT call delete_protocol_row or delete_file - otherwise it would
    delete REAL pre-existing rows that happen to share the protocol number
    we reserved (which is what actually happened in prod, deleting 2 rows
    before this guard was added).
    """
    from src.core.audit import reserve_next_protocol_number

    reserve_next_protocol_number(2026, workflow.workflow_id)
    workflow._onedrive.delete_protocol_row = AsyncMock()
    workflow._onedrive.delete_file = AsyncMock()

    await workflow.rollback({
        "test_mode": True,
        "protocol_number": "2026_001",
        "remote_filename": "[2026_001] T.pdf",
        "upload_file_id": "item-1",   # even with a stale upload_file_id, must not delete
        "local_copy_path": "",
    })

    # CRITICAL: zero SharePoint calls in test mode.
    workflow._onedrive.delete_protocol_row.assert_not_called()
    workflow._onedrive.delete_file.assert_not_called()


@pytest.mark.asyncio
async def test_filename_title_overrides_llm_hallucination(workflow, tmp_path):
    """Regression test for the LLM swapping the candidate's name with the sender's.

    When the filename matches "[YYYY_NNN] <Title>.pdf", we MUST take the
    title verbatim from the filename even if the LLM returns something
    different.  Real incident: filename was "[2026_027] Υποψηφιότητα - ΕΕΔΑ -
    Απέργης Σπύρος.pdf" but the LLM returned title "Υποψηφιότητα -
    Διοικητικό Συμβούλιο - Γιώργος Αθανασιάς" (sender's name substituted
    for the candidate's).
    """
    from src.workflows import archive_llm

    pdf = tmp_path / "[2026_027] Υποψηφιότητα - ΕΕΔΑ - Απέργης Σπύρος.pdf"
    _write_minimal_pdf(pdf)

    hallucinated = {
        "title": "Υποψηφιότητα - Διοικητικό Συμβούλιο - Γιώργος Αθανασιάς",  # WRONG
        "labels": ["Υποψηφιότητες"],
        "key_points": "",
        "existing_protocol": "2026_027",
        "category_matched": "Υποψηφιότητα",
        "confidence": 0.95,
        "reasoning_brief": "",
    }
    with patch.object(archive_llm, "classify_document", AsyncMock(return_value=hallucinated)):
        ctx = {
            "pdf_filename_orig": pdf.name,
            "pdf_text": "candidate file body",
            "pdf_metadata": {},
            "sender_email": "georgeathanasias@gmail.com",
            "sender_name": "Γιώργος Αθανασιάς",
            "email_subject": "",
            "email_body": "",
        }
        result = await workflow._step_extract_metadata(ctx)

    assert result.success
    # The filename's title MUST win over the LLM's hallucination
    assert result.data["llm_result"]["title"] == "Υποψηφιότητα - ΕΕΔΑ - Απέργης Σπύρος"
    # LLM tags + confidence are preserved (we only override the title)
    assert result.data["llm_result"]["labels"] == ["Υποψηφιότητες"]


@pytest.mark.asyncio
async def test_rollback_keeps_committed_reservations(workflow):
    from src.core.audit import (
        reserve_next_protocol_number, commit_protocol_reservation,
        get_reservations_for_year,
    )
    reserve_next_protocol_number(2026, workflow.workflow_id)
    commit_protocol_reservation(workflow.workflow_id)

    workflow._onedrive.delete_protocol_row = AsyncMock()
    workflow._onedrive.delete_file = AsyncMock()
    await workflow.rollback({})
    rows = get_reservations_for_year(2026)
    assert len(rows) == 1  # committed row preserved
    assert rows[0]["committed"] == 1


# ── concurrent workflows ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_workflows_get_different_protocol_numbers(mock_db):
    """Two ArchiveWorkflow instances reserving in the same year get distinct seqs."""
    with patch("src.workflows.archive.OneDriveClient"):
        from src.workflows.archive import ArchiveWorkflow
        wf1 = ArchiveWorkflow()
        wf2 = ArchiveWorkflow()
        wf1._onedrive = AsyncMock()
        wf2._onedrive = AsyncMock()
        wf1._onedrive.get_current_year_max_seq = AsyncMock(return_value=0)
        wf2._onedrive.get_current_year_max_seq = AsyncMock(return_value=0)
        r1 = await wf1._step_resolve_protocol({"llm_result": {}})
        r2 = await wf2._step_resolve_protocol({"llm_result": {}})
        assert r1.data["protocol_number"] != r2.data["protocol_number"]


# ── revision window helper ────────────────────────────────────────────────────


def test_revision_window_open():
    from datetime import datetime, timezone, timedelta
    from src.workflows.archive import is_revision_window_open
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert is_revision_window_open({"revision_open_until": future})
    assert not is_revision_window_open({"revision_open_until": past})
    assert not is_revision_window_open({})
    assert not is_revision_window_open({"revision_open_until": "garbage"})
