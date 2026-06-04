"""Tests for the `ai-assistant archive` CLI commands."""
from __future__ import annotations

import argparse
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def mock_db(tmp_path):
    db_path = tmp_path / "test.db"
    with patch("src.core.audit._DB_PATH", db_path), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        yield


def _write_minimal_pdf(path: Path) -> None:
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    c.drawString(72, 720, "hello world " * 20)
    c.showPage()
    c.save()


def _ns(**kwargs) -> argparse.Namespace:
    base = {
        # submit fields
        "pdf_path": None, "title": None, "labels": None, "proto": None,
        "sender": None, "actor": "secgen", "test": False,
        # shared / review / cancel fields
        "workflow_id": None, "archive_command": None, "text": None,
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


# ── happy-path submit ─────────────────────────────────────────────────────────


def test_archive_submit_happy_path(mock_db, tmp_path, capsys):
    """Submitting a PDF runs the full pipeline with mocked OneDrive + LLM."""
    pdf = tmp_path / "doc.pdf"
    _write_minimal_pdf(pdf)

    fake_llm = {
        "title": "Πρακτικά", "labels": ["Διοικητικά"], "key_points": "",
        "existing_protocol": None, "category_matched": "Πρακτικά",
        "confidence": 0.9, "reasoning_brief": "",
    }
    with patch("src.workflows.archive.OneDriveClient") as OD, \
         patch("src.workflows.archive.archive_llm") as L:
        OD.return_value.get_current_year_max_seq = AsyncMock(return_value=0)
        OD.return_value.upload_file = AsyncMock(return_value={"id": "item-1"})
        OD.return_value.append_protocol_row = AsyncMock()
        OD.return_value.get_share_link = AsyncMock(return_value="https://x")
        OD.return_value.read_recent_entries = AsyncMock(return_value=[])
        L.classify_document = AsyncMock(return_value=fake_llm)

        from src.cli.commands import cmd_archive
        cmd_archive(_ns(archive_command="submit", pdf_path=str(pdf)))

    out = capsys.readouterr().out
    assert "ARCHIVE COMPLETED" in out
    assert "Πρωτόκολλο:" in out


def test_archive_submit_test_mode_skips_upload(mock_db, tmp_path, capsys, monkeypatch):
    pdf = tmp_path / "doc.pdf"
    _write_minimal_pdf(pdf)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "")  # ack the cleanup prompt

    fake_llm = {
        "title": "T", "labels": ["A"], "key_points": "",
        "existing_protocol": None, "category_matched": "X",
        "confidence": 0.9, "reasoning_brief": "",
    }
    with patch("src.workflows.archive.OneDriveClient") as OD, \
         patch("src.workflows.archive.archive_llm") as L:
        OD.return_value.get_current_year_max_seq = AsyncMock(return_value=0)
        OD.return_value.upload_file = AsyncMock()
        OD.return_value.append_protocol_row = AsyncMock()
        OD.return_value.delete_protocol_row = AsyncMock()
        OD.return_value.delete_file = AsyncMock()
        OD.return_value.read_recent_entries = AsyncMock(return_value=[])
        L.classify_document = AsyncMock(return_value=fake_llm)

        from src.cli.commands import cmd_archive
        cmd_archive(_ns(archive_command="submit", pdf_path=str(pdf), test=True))

    OD.return_value.upload_file.assert_not_called()
    OD.return_value.append_protocol_row.assert_not_called()


# ── review ────────────────────────────────────────────────────────────────────


def test_archive_review_amend(mock_db, capsys):
    """`archive review` with an 'amend' intent updates the stored context."""
    from src.core.audit import save_workflow_state, get_workflow_state
    from datetime import datetime, timezone, timedelta

    future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    ctx = {
        "llm_result": {"title": "Old", "labels": ["A"], "key_points": "x"},
        "protocol_number": "2026_001",
        "revision_open_until": future,
    }
    save_workflow_state("archive", "wf-1", "completed", {"context": ctx, "step_index": 6})

    with patch("src.workflows.archive_llm.parse_user_feedback", new=AsyncMock(return_value={
             "workflow_id": "wf-1",
             "intent": "amend",
             "amendments": {"title": "New title", "labels": None, "key_points": None, "protocol_id": None},
             "confidence": 0.9,
             "summary_for_human": "Άλλαξα τον τίτλο",
         })), \
         patch("src.workflows.archive.OneDriveClient") as OD:
        OD.return_value.rename_file = AsyncMock()
        OD.return_value.update_protocol_row = AsyncMock(return_value=True)
        from src.cli.commands import cmd_archive
        cmd_archive(_ns(archive_command="review", workflow_id="wf-1", text="rename to New title"))

    out = capsys.readouterr().out
    assert "Intent:" in out
    state = get_workflow_state("wf-1")
    data = json.loads(state["data"])
    assert data["context"]["llm_result"]["title"] == "New title"
    # SharePoint xlsx row was updated for the title change
    OD.return_value.update_protocol_row.assert_awaited_once()


def test_archive_review_cancel(mock_db, capsys):
    """`archive review` with a 'cancel' intent rolls back + marks cancelled."""
    from src.core.audit import save_workflow_state, get_workflow_state
    from datetime import datetime, timezone, timedelta

    future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    ctx = {
        "llm_result": {"title": "T", "labels": [], "key_points": ""},
        "protocol_number": "2026_001",
        "remote_filename": "[2026_001] T.pdf",
        "upload_file_id": "item-1",
        "revision_open_until": future,
    }
    save_workflow_state("archive", "wf-2", "completed", {"context": ctx, "step_index": 6})

    with patch("src.workflows.archive_llm.parse_user_feedback", new=AsyncMock(return_value={
            "workflow_id": "wf-2",
            "intent": "cancel",
            "amendments": {"title": None, "labels": None, "key_points": None, "protocol_id": None},
            "confidence": 0.95,
            "summary_for_human": "Ακυρώνω την εγγραφή",
         })), \
         patch("src.workflows.archive.OneDriveClient") as OD:
        OD.return_value.delete_protocol_row = AsyncMock()
        OD.return_value.delete_file = AsyncMock()
        from src.cli.commands import cmd_archive
        cmd_archive(_ns(archive_command="review", workflow_id="wf-2", text="ακύρωσέ το"))

    state = get_workflow_state("wf-2")
    assert state["state"] == "cancelled"


def test_archive_review_closed_window_rejected(mock_db, capsys):
    """Past revision_open_until → review is rejected with an error."""
    from src.core.audit import save_workflow_state
    from datetime import datetime, timezone, timedelta

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    ctx = {"llm_result": {}, "protocol_number": "2026_001", "revision_open_until": past}
    save_workflow_state("archive", "wf-3", "completed", {"context": ctx, "step_index": 6})

    from src.cli.commands import cmd_archive
    with pytest.raises(SystemExit):
        cmd_archive(_ns(archive_command="review", workflow_id="wf-3", text="..."))
    out = capsys.readouterr().out
    assert "revision window has closed" in out


# ── cancel ────────────────────────────────────────────────────────────────────


def test_archive_cancel_rolls_back_and_marks_cancelled(mock_db, capsys):
    from src.core.audit import save_workflow_state, get_workflow_state
    ctx = {
        "protocol_number": "2026_001",
        "remote_filename": "[2026_001] T.pdf",
        "upload_file_id": "item-1",
    }
    save_workflow_state("archive", "wf-c", "completed", {"context": ctx, "step_index": 6})

    with patch("src.workflows.archive.OneDriveClient") as OD:
        OD.return_value.delete_protocol_row = AsyncMock()
        OD.return_value.delete_file = AsyncMock()
        from src.cli.commands import cmd_archive
        cmd_archive(_ns(archive_command="cancel", workflow_id="wf-c"))

    state = get_workflow_state("wf-c")
    assert state["state"] == "cancelled"


# ── amend: live SharePoint rename + xlsx row update ───────────────────────────


def test_archive_review_amend_renames_sharepoint_file(mock_db, capsys):
    """Title change → rename SharePoint file AND update xlsx row."""
    from src.core.audit import save_workflow_state
    from datetime import datetime, timezone, timedelta

    future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    ctx = {
        "llm_result": {"title": "Old title", "labels": ["A"], "key_points": ""},
        "protocol_number": "2026_007",
        "remote_folder": "Αρχείο ανά έτος/2026",
        "remote_filename": "[2026_007] Old title.pdf",
        "revision_open_until": future,
    }
    save_workflow_state("archive", "wf-rename", "completed",
                        {"context": ctx, "step_index": 6})

    with patch("src.workflows.archive_llm.parse_user_feedback", new=AsyncMock(return_value={
            "workflow_id": "wf-rename",
            "intent": "amend",
            "amendments": {"title": "Brand new title", "labels": None,
                           "key_points": None, "protocol_id": None},
            "confidence": 0.92,
            "summary_for_human": "Νέος τίτλος",
        })), \
        patch("src.workflows.archive.OneDriveClient") as OD:
        OD.return_value.rename_file = AsyncMock(return_value={"id": "x"})
        OD.return_value.update_protocol_row = AsyncMock(return_value=True)
        from src.cli.commands import cmd_archive
        cmd_archive(_ns(archive_command="review", workflow_id="wf-rename",
                        text="rename to Brand new title"))

    # rename_file called with the OLD remote path + the NEW leaf filename
    OD.return_value.rename_file.assert_awaited_once()
    args_, kwargs_ = OD.return_value.rename_file.call_args
    assert "Αρχείο ανά έτος/2026/[2026_007] Old title.pdf" in args_
    assert "[2026_007] Brand new title.pdf" in args_
    # xlsx row update was also called
    OD.return_value.update_protocol_row.assert_awaited_once_with(
        "2026_007", title="Brand new title", main_points=None, tags=None,
    )


def test_archive_review_amend_cross_year_protocol_warning(mock_db, capsys):
    """Cross-year protocol_id changes are NOT applied to xlsx — warning emitted."""
    from src.core.audit import save_workflow_state
    from datetime import datetime, timezone, timedelta

    future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    ctx = {
        "llm_result": {"title": "T", "labels": [], "key_points": ""},
        "protocol_number": "2026_100",
        "remote_folder": "Αρχείο ανά έτος/2026",
        "remote_filename": "[2026_100] T.pdf",
        "revision_open_until": future,
    }
    save_workflow_state("archive", "wf-cross", "completed",
                        {"context": ctx, "step_index": 6})

    with patch("src.workflows.archive_llm.parse_user_feedback", new=AsyncMock(return_value={
            "workflow_id": "wf-cross",
            "intent": "amend",
            "amendments": {"title": None, "labels": None, "key_points": None,
                           "protocol_id": "2025_100"},
            "confidence": 0.9,
            "summary_for_human": "Αλλάζω χρονιά",
        })), \
        patch("src.workflows.archive.OneDriveClient") as OD:
        OD.return_value.rename_file = AsyncMock()
        OD.return_value.update_protocol_row = AsyncMock()
        OD.return_value.delete_protocol_row = AsyncMock()
        OD.return_value.append_protocol_row = AsyncMock()
        from src.cli.commands import cmd_archive
        cmd_archive(_ns(archive_command="review", workflow_id="wf-cross",
                        text="βγαλε το στο 2025_100"))

    out = capsys.readouterr().out
    assert "Cross-year" in out
    # Neither delete nor append on the xlsx should fire for cross-year
    OD.return_value.delete_protocol_row.assert_not_awaited()
    OD.return_value.append_protocol_row.assert_not_awaited()


# ── Phase 4: collision gate + resolve flow ────────────────────────────────────


@pytest.mark.asyncio
async def test_collision_check_passes_when_protocol_is_reserved(mock_db):
    """A freshly-reserved protocol number can never collide — instant pass."""
    from src.workflows.archive import ArchiveWorkflow

    wf = ArchiveWorkflow()
    ctx = {"protocol_source": "reserved", "protocol_number": "2026_500"}
    result = await wf._step_collision_check(ctx)
    assert result.success is True


@pytest.mark.asyncio
async def test_collision_check_passes_when_no_existing_row(mock_db):
    from src.workflows.archive import ArchiveWorkflow

    wf = ArchiveWorkflow()
    wf._onedrive = MagicMock()
    wf._onedrive.find_protocol_row = AsyncMock(return_value=None)
    ctx = {
        "protocol_source": "document",
        "protocol_number": "2026_017",
        "llm_result": {"title": "Some new doc"},
    }
    result = await wf._step_collision_check(ctx)
    assert result.success is True
    assert "free" in result.message.lower()


@pytest.mark.asyncio
async def test_collision_check_hard_fail_when_row_and_file_both_exist(mock_db):
    """Row exists + file exists in SharePoint → HARD FAIL (no overwrite, no approval flow).

    This is the new behaviour from 2026-05-27: the bot NEVER overwrites
    archived files.  Any pre-existing entry with a file forces SecGen to
    handle the situation manually.
    """
    from src.workflows.archive import ArchiveWorkflow

    wf = ArchiveWorkflow()
    wf._onedrive = MagicMock()
    wf._onedrive.find_protocol_row = AsyncMock(return_value={
        "proto": "2026_017", "date": "2026-02-10", "title": "Πρακτικά ΔΣ03",
        "key_points": "", "tags": "Διοικητικά",
    })
    wf._onedrive.file_exists_for_protocol = AsyncMock(return_value=True)
    ctx = {
        "protocol_source": "document",
        "protocol_number": "2026_017",
        "llm_result": {"title": "πρακτικα δσ03"},  # title doesn't matter — file exists
    }
    result = await wf._step_collision_check(ctx)
    assert result.success is False
    assert "ήδη αρχειοθετηθεί" in result.message
    # CRITICAL: no pending state is set — this is a flat fail, not a deferral.
    assert "pending_reservation_confirmation" not in ctx
    assert "pending_collision" not in ctx


@pytest.mark.asyncio
async def test_collision_check_fills_reservation_when_titles_match(mock_db):
    """Row exists + NO file + titles match → workflow proceeds in reservation-fill mode."""
    from src.workflows.archive import ArchiveWorkflow

    wf = ArchiveWorkflow()
    wf._onedrive = MagicMock()
    wf._onedrive.find_protocol_row = AsyncMock(return_value={
        "proto": "2026_017", "date": "", "title": "Πρακτικά ΔΣ03",
        "key_points": "", "tags": "",
    })
    wf._onedrive.file_exists_for_protocol = AsyncMock(return_value=False)
    ctx = {
        "protocol_source": "document",
        "protocol_number": "2026_017",
        "llm_result": {"title": "πρακτικα δσ03"},   # normalised match
    }
    result = await wf._step_collision_check(ctx)
    assert result.success is True
    assert ctx["is_filling_reservation"] is True
    assert ctx["reserved_row"]["title"] == "Πρακτικά ΔΣ03"


@pytest.mark.asyncio
async def test_collision_check_defers_when_row_no_file_titles_differ(mock_db):
    """Row exists + NO file + titles don't match → defer to SecGen confirmation."""
    from src.workflows.archive import ArchiveWorkflow

    wf = ArchiveWorkflow()
    wf._onedrive = MagicMock()
    wf._onedrive.find_protocol_row = AsyncMock(return_value={
        "proto": "2026_017", "date": "2026-02-10",
        "title": "Πρακτικά ΔΣ03",
        "key_points": "", "tags": "Διοικητικά",
    })
    wf._onedrive.file_exists_for_protocol = AsyncMock(return_value=False)
    ctx = {
        "protocol_source": "document",
        "protocol_number": "2026_017",
        "llm_result": {"title": "Υποψηφιότητα Παπαδόπουλος"},
    }
    result = await wf._step_collision_check(ctx)
    assert result.success is False
    assert "RESERVATION_CONFIRMATION_NEEDED" in result.message
    assert ctx["pending_reservation_confirmation"]["protocol_number"] == "2026_017"
    assert ctx["pending_reservation_confirmation"]["existing_title"] == "Πρακτικά ΔΣ03"
    assert ctx["pending_reservation_confirmation"]["proposed_title"] == "Υποψηφιότητα Παπαδόπουλος"
    assert ctx["pending_reservation_confirmation"]["match_confidence"] < 0.7


def test_archive_resolve_reject_rolls_back(mock_db, capsys):
    """`archive resolve <id> reject` rolls back a workflow awaiting reservation-confirm."""
    from src.core.audit import save_workflow_state, get_workflow_state

    ctx = {
        "protocol_number": "2026_017",
        "llm_result": {"title": "Υποψηφιότητα"},
        "pending_reservation_confirmation": {
            "protocol_number": "2026_017",
            "existing_title": "Πρακτικά",
            "proposed_title": "Υποψηφιότητα",
            "match_confidence": 0.0,
            "raised_at": "2026-05-27T10:00:00+00:00",
        },
    }
    save_workflow_state("archive", "wf-rej", "failed",
                        {"context": ctx, "step_index": 3})

    with patch("src.workflows.archive.OneDriveClient") as OD:
        OD.return_value.delete_protocol_row = AsyncMock()
        OD.return_value.delete_file = AsyncMock()
        from src.cli.commands import cmd_archive
        cmd_archive(_ns(archive_command="resolve",
                        workflow_id="wf-rej", decision="reject"))

    state = get_workflow_state("wf-rej")
    assert state["state"] == "cancelled"
    out = capsys.readouterr().out
    assert "REJECTED" in out


def test_archive_resolve_approve_fills_reservation(mock_db, capsys, tmp_path):
    """`archive resolve <id> approve` resumes from upload step in reservation-fill mode.

    Critical asserts:
      • upload_file is called (file gets uploaded to SharePoint)
      • update_protocol_row is called (NOT append_protocol_row) since we're
        filling an existing row, not creating one
      • is_filling_reservation flag is set so rollback (if it ever fires)
        won't delete SecGen's row
    """
    from src.core.audit import save_workflow_state

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfake")
    ctx = {
        "pdf_path": str(pdf),
        "protocol_number": "2026_017",
        "protocol_source": "document",
        "llm_result": {
            "title": "Υποψηφιότητα", "labels": ["Υποψηφιότητες"],
            "key_points": "Νέο σημείο", "confidence": 0.9,
        },
        "pending_reservation_confirmation": {
            "protocol_number": "2026_017",
            "existing_row": {
                "proto": "2026_017", "date": "", "title": "Reserved title",
                "key_points": "", "tags": "",
            },
            "existing_title": "Reserved title",
            "proposed_title": "Υποψηφιότητα",
            "match_confidence": 0.0,
            "raised_at": "2026-05-27T10:00:00+00:00",
        },
    }
    save_workflow_state("archive", "wf-ok", "failed",
                        {"context": ctx, "step_index": 3})

    with patch("src.workflows.archive.OneDriveClient") as OD:
        OD.return_value.upload_file = AsyncMock(return_value={"id": "item-1"})
        OD.return_value.update_protocol_row = AsyncMock(return_value=True)
        OD.return_value.append_protocol_row = AsyncMock()   # MUST NOT be called
        OD.return_value.get_share_link = AsyncMock(return_value="https://x")
        from src.cli.commands import cmd_archive
        cmd_archive(_ns(archive_command="resolve",
                        workflow_id="wf-ok", decision="approve"))

    out = capsys.readouterr().out
    assert "APPROVED" in out
    OD.return_value.upload_file.assert_awaited_once()
    # CRITICAL: fill-blanks path uses update_protocol_row, not append.
    OD.return_value.append_protocol_row.assert_not_called()
    OD.return_value.update_protocol_row.assert_awaited()


# ── list ──────────────────────────────────────────────────────────────────────


def test_archive_list_shows_recent_and_in_progress(mock_db, capsys):
    from src.core.audit import save_workflow_state
    from datetime import datetime, timezone, timedelta
    future = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    save_workflow_state("archive", "wf-A", "in_progress",
                        {"context": {"protocol_number": "2026_010"}, "step_index": 2})
    save_workflow_state("archive", "wf-B", "completed",
                        {"context": {"protocol_number": "2026_011",
                                      "revision_open_until": future}, "step_index": 6})

    from src.cli.commands import cmd_archive
    cmd_archive(_ns(archive_command="list"))
    out = capsys.readouterr().out
    assert "wf-A" in out
    assert "wf-B" in out
    assert "2026_010" in out
    assert "2026_011" in out


# ── helpers ──────────────────────────────────────────────────────────────────


def _await(coro):
    """Run a coroutine to completion in the current thread (test helper)."""
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro)
