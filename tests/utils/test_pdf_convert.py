"""Phase 5 - non-PDF inputs auto-converted via LibreOffice headless."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Pure helpers (no external dependencies) ────────────────────────────────


def test_is_pdf_recognises_extension():
    from src.utils.pdf_convert import is_pdf
    assert is_pdf(Path("foo.pdf")) is True
    assert is_pdf(Path("foo.PDF")) is True
    assert is_pdf(Path("foo.docx")) is False
    assert is_pdf(Path("noext")) is False


def test_needs_conversion_for_supported_types():
    from src.utils.pdf_convert import needs_conversion
    assert needs_conversion(Path("a.docx")) is True
    assert needs_conversion(Path("a.heic")) is True
    assert needs_conversion(Path("a.jpeg")) is True
    assert needs_conversion(Path("a.pdf")) is False        # already PDF
    assert needs_conversion(Path("a.exe")) is False        # unsupported


def test_unsupported_extension_raises(tmp_path):
    from src.utils.pdf_convert import ConversionError, convert_to_pdf

    src = tmp_path / "doc.exe"
    src.write_bytes(b"\x00")
    with pytest.raises(ConversionError, match="Unsupported source extension"):
        convert_to_pdf(src)


def test_missing_soffice_raises(tmp_path, monkeypatch):
    from src.utils import pdf_convert
    src = tmp_path / "doc.docx"
    src.write_bytes(b"PK\x03\x04")
    monkeypatch.setattr(pdf_convert, "_find_soffice", lambda: None)
    with pytest.raises(pdf_convert.ConversionError, match="LibreOffice .* not found"):
        pdf_convert.convert_to_pdf(src)


def test_convert_invokes_soffice_with_right_args(tmp_path, monkeypatch):
    """Subprocess is invoked correctly and the produced PDF is returned."""
    from src.utils import pdf_convert

    src = tmp_path / "input.docx"
    src.write_bytes(b"PK\x03\x04 not really a docx but enough for the test")
    dest_dir = tmp_path / "out"

    # Pretend LibreOffice is installed and writes a PDF to outdir
    monkeypatch.setattr(pdf_convert, "_find_soffice", lambda: "/usr/bin/soffice")

    captured: dict = {}

    def _fake_run(cmd, *, capture_output, text, timeout, check):
        captured["cmd"] = cmd
        # Simulate writing the PDF
        (dest_dir / "input.pdf").write_bytes(b"%PDF-1.4\nfake")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)

    out = pdf_convert.convert_to_pdf(src, dest_dir=dest_dir)
    assert out == dest_dir / "input.pdf"
    assert out.exists()
    assert captured["cmd"][1] == "--headless"
    assert "--convert-to" in captured["cmd"]
    assert "pdf" in captured["cmd"]
    assert str(src) in captured["cmd"]


def test_convert_timeout_raises(tmp_path, monkeypatch):
    import subprocess
    from src.utils import pdf_convert

    src = tmp_path / "x.docx"
    src.write_bytes(b"PK\x03\x04")

    monkeypatch.setattr(pdf_convert, "_find_soffice", lambda: "/usr/bin/soffice")

    def _raise_timeout(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="soffice", timeout=60)

    monkeypatch.setattr("subprocess.run", _raise_timeout)

    with pytest.raises(pdf_convert.ConversionError, match="timed out"):
        pdf_convert.convert_to_pdf(src)


def test_convert_subprocess_failure_raises(tmp_path, monkeypatch):
    from src.utils import pdf_convert
    src = tmp_path / "x.docx"
    src.write_bytes(b"PK\x03\x04")
    monkeypatch.setattr(pdf_convert, "_find_soffice", lambda: "/usr/bin/soffice")

    def _fake_run(cmd, **_k):
        return MagicMock(returncode=1, stdout="", stderr="something went wrong")

    monkeypatch.setattr("subprocess.run", _fake_run)
    with pytest.raises(pdf_convert.ConversionError, match="exit 1"):
        pdf_convert.convert_to_pdf(src)


# ── Integration with archive workflow's intake step ────────────────────────


@pytest.mark.asyncio
async def test_intake_auto_converts_docx(tmp_path):
    """A DOCX input goes through convert_to_pdf, then extract_pdf_text."""
    from src.workflows.archive import ArchiveWorkflow

    docx = tmp_path / "report.docx"
    docx.write_bytes(b"PK\x03\x04 fake docx")
    fake_pdf = tmp_path / "report.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4\nfake")

    with patch("src.utils.pdf_convert.convert_to_pdf",
               return_value=fake_pdf) as conv, \
         patch("src.workflows.archive.extract_pdf_text",
               return_value=("hello pdf text", {"page_count": 1, "char_count": 14, "is_scan": False})):
        wf = ArchiveWorkflow()
        result = await wf._step_intake({"pdf_path": str(docx)})

    assert result.success is True
    conv.assert_called_once()
    # Context carries both original filename AND converted path
    assert result.data["pdf_filename_orig"] == "report.docx"
    assert result.data["converted_from"] == ".docx"
    assert result.data["pdf_path"].endswith("report.pdf")


@pytest.mark.asyncio
async def test_intake_rejects_unsupported_extension(tmp_path):
    """A .exe (or anything not in the allow-list) gets a clear rejection."""
    from src.workflows.archive import ArchiveWorkflow

    exe = tmp_path / "totally_legit.exe"
    exe.write_bytes(b"\x00")
    wf = ArchiveWorkflow()
    result = await wf._step_intake({"pdf_path": str(exe)})
    assert result.success is False
    assert "Unsupported file type" in result.message


# ── Email intake: convertible attachments are accepted ─────────────────────


def test_find_pdf_attachment_accepts_docx():
    """The email intake's attachment picker now accepts DOCX, ODT, etc."""
    from src.workflows.email_intake import _find_pdf_attachment

    assert _find_pdf_attachment([
        {"id": "a", "name": "report.docx",
         "contentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    ]) == {
        "id": "a", "name": "report.docx",
        "contentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }


def test_find_pdf_attachment_accepts_heic():
    from src.workflows.email_intake import _find_pdf_attachment
    assert _find_pdf_attachment([
        {"id": "a", "name": "photo.HEIC", "contentType": "image/heif"},
    ])["id"] == "a"


def test_find_pdf_attachment_rejects_multiple():
    """If two convertible attachments come in, refuse to guess which to archive."""
    from src.workflows.email_intake import _find_pdf_attachment
    assert _find_pdf_attachment([
        {"id": "a", "name": "doc.pdf", "contentType": "application/pdf"},
        {"id": "b", "name": "extra.docx",
         "contentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    ]) is None


def test_find_pdf_attachment_rejects_unsupported():
    from src.workflows.email_intake import _find_pdf_attachment
    assert _find_pdf_attachment([
        {"id": "a", "name": "evil.exe", "contentType": "application/octet-stream"},
    ]) is None
