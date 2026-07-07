# Phase 2: Board Meeting Minutes - Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement the full board meeting minutes workflow - from source selection through Claude-assisted drafting, board sharing, finalization with signatures, archiving, and decision extraction.

**Architecture:** Extends the existing `BaseWorkflow` with a new `BoardMeetingMinutesWorkflow` class following the same pattern as `BoardMeetingInvitationWorkflow`. Adds Google Docs write/clear methods, Zoom recording listing, Gmail sending, Google Sheets append, and PDF signature embedding. CLI gets a `minutes` subcommand group.

**Tech Stack:** Python 3.11+, Google APIs (Drive v3, Docs v1, Sheets v4, Gmail v1), Zoom API v2, ReportLab (PDF), python-docx, httpx, pytest + AsyncMock.

---

## Task 1: Config additions - new fields for minutes workflow

**Files:**
- Modify: `config.yaml`
- Modify: `src/config.py`

**Step 1: Add config fields to config.yaml**

Add under `google:`:
```yaml
  minutes_drafts_folder_id: ""   # Google Drive folder with SecGen's draft notes
  protokollo_sheet_id: ""        # [Πρωτόκολλο] Αρχείο ΔΣ spreadsheet
```

Add under `workflows.board_meeting:`:
```yaml
    minutes_share_message: "Σας κοινοποιούνται τα πρόχειρα πρακτικά προς σχολιασμό. Παρακαλώ αφήστε τα σχόλιά σας απευθείας στο έγγραφο."
```

**Step 2: Add to Pydantic models in `src/config.py`**

In `GoogleConfig` add:
```python
    minutes_drafts_folder_id: str = ""
    protokollo_sheet_id: str = ""
```

In `BoardMeetingConfig` add:
```python
    minutes_share_message: str = "Σας κοινοποιούνται τα πρόχειρα πρακτικά προς σχολιασμό."
```

**Step 3: Run tests**

Run: `python -m pytest tests/test_config.py -v`
Expected: All pass (existing config tests still work).

**Step 4: Commit**

```bash
git add config.yaml src/config.py
git commit -m "config: add minutes workflow fields (drafts folder, protokollo sheet, share message)"
```

---

## Task 2: Google Drive - add Docs write/clear/rename and folder listing methods

**Files:**
- Modify: `src/integrations/google_drive.py`
- Create: `tests/test_google_drive_methods.py`

**Step 1: Write failing tests**

```python
# tests/test_google_drive_methods.py
"""Tests for new Google Drive methods needed by minutes workflow."""
import pytest
from unittest.mock import MagicMock, patch

@pytest.fixture
def mock_db(tmp_path):
    with patch("src.core.audit._DB_PATH", tmp_path / "test.db"), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        yield

@pytest.fixture
def google_client(mock_db):
    with patch("src.integrations.google_drive.GoogleClient.authenticate"):
        from src.integrations.google_drive import GoogleClient
        client = GoogleClient()
        client._creds = MagicMock()
        client._drive_service = MagicMock()
        client._docs_service = MagicMock()
        client._sheets_service = MagicMock()
        return client

def test_list_docs_in_folder(google_client):
    """list_docs_in_folder should filter to Google Docs only."""
    google_client._drive_service.files().list().execute.return_value = {
        "files": [
            {"id": "a", "name": "Notes ΔΣ01", "mimeType": "application/vnd.google-apps.document", "modifiedTime": "2026-01-01T00:00:00Z"},
            {"id": "b", "name": "Budget.xlsx", "mimeType": "application/vnd.google-apps.spreadsheet", "modifiedTime": "2026-01-02T00:00:00Z"},
        ]
    }
    result = google_client.list_docs_in_folder("folder-123")
    assert len(result) == 1
    assert result[0]["id"] == "a"

def test_read_doc_content(google_client):
    """read_doc_content should extract plain text from a Google Doc."""
    google_client._docs_service.documents().get().execute.return_value = {
        "body": {
            "content": [
                {"paragraph": {"elements": [{"textRun": {"content": "Hello world\n"}}]}},
                {"paragraph": {"elements": [{"textRun": {"content": "Second line\n"}}]}},
            ]
        }
    }
    text = google_client.read_doc_content("doc-123")
    assert "Hello world" in text
    assert "Second line" in text

def test_clear_and_write_doc(google_client):
    """clear_and_write_doc should delete all content then insert new text."""
    google_client._docs_service.documents().get().execute.return_value = {
        "body": {"content": [{"endIndex": 50}]}
    }
    google_client.clear_and_write_doc("doc-123", "New content here")
    # Should have called batchUpdate
    assert google_client._docs_service.documents().batchUpdate.called

def test_rename_file(google_client):
    """rename_file should call Drive files().update with new name."""
    google_client.rename_file("file-123", "New Name")
    google_client._drive_service.files().update.assert_called()
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_google_drive_methods.py -v`
Expected: FAIL (methods don't exist yet).

**Step 3: Implement the methods in `src/integrations/google_drive.py`**

Add these methods to `GoogleClient` class after `list_folder()`:

```python
    def list_docs_in_folder(self, folder_id: str) -> list[dict[str, Any]]:
        """List only Google Docs in a Drive folder (no Sheets, Slides, etc.).

        Args:
            folder_id: Google Drive folder ID.

        Returns:
            List of file metadata dicts (id, name, mimeType, modifiedTime)
            for Google Docs only, sorted by modifiedTime descending.
        """
        self._ensure_authenticated()
        results = self._drive_service.files().list(
            q=(
                f"'{folder_id}' in parents "
                "and mimeType='application/vnd.google-apps.document' "
                "and trashed=false"
            ),
            fields="files(id, name, mimeType, modifiedTime)",
            orderBy="modifiedTime desc",
        ).execute()
        return results.get("files", [])

    def read_doc_content(self, doc_id: str) -> str:
        """Read the full plain-text content of a Google Doc.

        Args:
            doc_id: Google Doc file ID.

        Returns:
            Plain text of the document.
        """
        self._ensure_authenticated()
        doc = self._docs_service.documents().get(documentId=doc_id).execute()
        parts: list[str] = []
        for elem in doc.get("body", {}).get("content", []):
            para = elem.get("paragraph")
            if not para:
                continue
            for pe in para.get("elements", []):
                tr = pe.get("textRun")
                if tr:
                    parts.append(tr.get("content", ""))
        return "".join(parts)

    def clear_and_write_doc(self, doc_id: str, content: str) -> None:
        """Replace all content in a Google Doc with new text.

        Deletes everything except the mandatory first newline, then inserts
        the new content at index 1.

        Args:
            doc_id: Google Doc file ID.
            content: New plain text content to write.
        """
        self._ensure_authenticated()
        doc = self._docs_service.documents().get(documentId=doc_id).execute()
        body = doc.get("body", {})
        end_index = max(
            (elem.get("endIndex", 1) for elem in body.get("content", [])),
            default=1,
        )

        requests: list[dict] = []
        # Delete all content except the mandatory first character
        if end_index > 1:
            requests.append({
                "deleteContentRange": {
                    "range": {"startIndex": 1, "endIndex": end_index - 1}
                }
            })
        # Insert new content at position 1
        requests.append({
            "insertText": {
                "location": {"index": 1},
                "text": content,
            }
        })

        _batch_update_with_retry(self._docs_service, doc_id, {"requests": requests})
        log_action(
            workflow="google_docs",
            action="doc_content_replaced",
            actor="system",
            target=doc_id,
            details={"content_length": len(content)},
        )
        logger.info("Replaced content of doc %s (%d chars)", doc_id, len(content))

    def rename_file(self, file_id: str, new_name: str) -> None:
        """Rename a file in Google Drive.

        Args:
            file_id: Drive file ID.
            new_name: New file name.
        """
        self._ensure_authenticated()
        self._drive_service.files().update(
            fileId=file_id, body={"name": new_name}
        ).execute()
        log_action(
            workflow="google_drive",
            action="file_renamed",
            actor="system",
            target=file_id,
            details={"new_name": new_name},
        )
        logger.info("Renamed Drive file %s → %s", file_id, new_name)
```

**Step 4: Run tests**

Run: `python -m pytest tests/test_google_drive_methods.py -v`
Expected: All 4 tests PASS.

**Step 5: Commit**

```bash
git add src/integrations/google_drive.py tests/test_google_drive_methods.py
git commit -m "feat(google): add Docs read/write/clear/rename + folder doc listing"
```

---

## Task 3: Zoom - add list_recordings method

**Files:**
- Modify: `src/integrations/zoom.py`
- Create: `tests/test_zoom_recordings.py`

**Step 1: Write failing test**

```python
# tests/test_zoom_recordings.py
"""Tests for Zoom recording listing."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

@pytest.fixture
def mock_db(tmp_path):
    with patch("src.core.audit._DB_PATH", tmp_path / "test.db"), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        yield

@pytest.fixture
def zoom_client(mock_db):
    from src.integrations.zoom import ZoomClient
    client = ZoomClient()
    client._token = "fake-token"
    return client

@pytest.mark.asyncio
async def test_list_recordings(zoom_client):
    """list_recordings should return meetings with their IDs and topics."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "meetings": [
            {"id": 111, "topic": "Συνεδρίαση ΔΣ01-2026", "start_time": "2026-01-12T18:00:00Z"},
            {"id": 222, "topic": "Συνεδρίαση ΔΣ02-2026", "start_time": "2026-02-22T18:00:00Z"},
        ]
    }
    mock_response.is_success = True
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_http:
        mock_http.return_value.__aenter__ = AsyncMock(return_value=MagicMock(
            get=AsyncMock(return_value=mock_response)
        ))
        mock_http.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await zoom_client.list_recordings(from_date="2026-01-01", to_date="2026-03-01")

    assert len(result) == 2
    assert result[0]["topic"] == "Συνεδρίαση ΔΣ01-2026"
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_zoom_recordings.py -v`
Expected: FAIL (method doesn't exist).

**Step 3: Implement in `src/integrations/zoom.py`**

Add after `get_transcript()`:

```python
    async def list_recordings(
        self,
        from_date: str = "",
        to_date: str = "",
    ) -> list[dict[str, Any]]:
        """List cloud recordings for the account.

        Args:
            from_date: Start date (YYYY-MM-DD). Defaults to 30 days ago.
            to_date: End date (YYYY-MM-DD). Defaults to today.

        Returns:
            List of meeting dicts with id, topic, start_time, duration.
        """
        if not from_date:
            from datetime import timedelta
            from_date = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
        if not to_date:
            to_date = datetime.utcnow().strftime("%Y-%m-%d")

        params = {"from": from_date, "to": to_date, "page_size": 100}
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{_ZOOM_API_BASE}/users/me/recordings",
                headers=await self._headers(),
                params=params,
            )
            response.raise_for_status()
            data = response.json()

        meetings = data.get("meetings", [])
        logger.info("Found %d recordings between %s and %s", len(meetings), from_date, to_date)
        return meetings
```

**Step 4: Run test**

Run: `python -m pytest tests/test_zoom_recordings.py -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add src/integrations/zoom.py tests/test_zoom_recordings.py
git commit -m "feat(zoom): add list_recordings for meeting transcript discovery"
```

---

## Task 4: Google Sheets - append_rows helper for Βιβλίο Αποφάσεων and Πρωτόκολλο

The `write_sheet()` method already exists and uses `spreadsheets.values.append`. We just need a thin helper to read the last row for auto-numbering.

**Files:**
- Modify: `src/integrations/google_drive.py`
- Add test to: `tests/test_google_drive_methods.py`

**Step 1: Write failing test**

Append to `tests/test_google_drive_methods.py`:

```python
def test_get_last_row_value(google_client):
    """get_last_row_value should return the last non-empty value in column A."""
    google_client._sheets_service.spreadsheets().values().get().execute.return_value = {
        "values": [["ΑΡΙΘΜΟΣ", "ΑΠΟΦΑΣΗ"], ["ΔΣ01-03-2026", "Decision 1"], ["ΔΣ02-03-2026", "Decision 2"]]
    }
    result = google_client.get_last_row_value("sheet-id", "2026!A:A")
    assert result == "ΔΣ02-03-2026"

def test_get_last_row_value_empty(google_client):
    """get_last_row_value should return None for empty sheet."""
    google_client._sheets_service.spreadsheets().values().get().execute.return_value = {
        "values": [["ΑΡΙΘΜΟΣ"]]
    }
    result = google_client.get_last_row_value("sheet-id", "2026!A:A")
    assert result is None
```

**Step 2: Run to verify fail**

Run: `python -m pytest tests/test_google_drive_methods.py::test_get_last_row_value -v`
Expected: FAIL.

**Step 3: Implement**

Add to `GoogleClient`:

```python
    def get_last_row_value(
        self,
        spreadsheet_id: str,
        range_name: str,
    ) -> str | None:
        """Read the last non-empty value in the first column of a range.

        Useful for auto-incrementing protocol/decision numbers.

        Args:
            spreadsheet_id: Google Sheets ID.
            range_name: A1 notation (e.g., "2026!A:A").

        Returns:
            Last non-empty value string, or None if only headers exist.
        """
        rows = self.read_sheet(spreadsheet_id, range_name)
        # Skip header row, find last non-empty
        for row in reversed(rows[1:]):
            if row and row[0]:
                return row[0]
        return None
```

**Step 4: Run tests**

Run: `python -m pytest tests/test_google_drive_methods.py -v`
Expected: All 6 PASS.

**Step 5: Commit**

```bash
git add src/integrations/google_drive.py tests/test_google_drive_methods.py
git commit -m "feat(sheets): add get_last_row_value for auto-numbering"
```

---

## Task 5: PDF signature embedding

**Files:**
- Modify: `src/documents/pdf_generator.py`
- Create: `tests/test_pdf_signatures.py`

**Step 1: Write failing test**

```python
# tests/test_pdf_signatures.py
"""Tests for PDF signature embedding."""
import pytest
from pathlib import Path
from unittest.mock import patch

@pytest.fixture
def mock_db(tmp_path):
    with patch("src.core.audit._DB_PATH", tmp_path / "test.db"), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        yield

def test_embed_signatures(mock_db, tmp_path):
    """embed_signatures should overlay signature images on a PDF."""
    from src.documents.pdf_generator import generate_pdf, embed_signatures

    # Generate a simple PDF first
    content = {"title": "Test Document", "sections": [{"heading": "Test", "body": "Content"}]}
    pdf_path = tmp_path / "test.pdf"
    generate_pdf(content, pdf_path)
    assert pdf_path.exists()

    # Create fake signature images (1x1 white PNG)
    import struct, zlib
    def make_tiny_png(path):
        # Minimal valid PNG
        sig = b'\x89PNG\r\n\x1a\n'
        ihdr_data = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
        ihdr_crc = zlib.crc32(b'IHDR' + ihdr_data) & 0xffffffff
        ihdr = struct.pack('>I', 13) + b'IHDR' + ihdr_data + struct.pack('>I', ihdr_crc)
        raw = zlib.compress(b'\x00\xff\xff\xff')
        idat_crc = zlib.crc32(b'IDAT' + raw) & 0xffffffff
        idat = struct.pack('>I', len(raw)) + b'IDAT' + raw + struct.pack('>I', idat_crc)
        iend_crc = zlib.crc32(b'IEND') & 0xffffffff
        iend = struct.pack('>I', 0) + b'IEND' + struct.pack('>I', iend_crc)
        path.write_bytes(sig + ihdr + idat + iend)

    sig1 = tmp_path / "sig_president.png"
    sig2 = tmp_path / "sig_secgen.png"
    make_tiny_png(sig1)
    make_tiny_png(sig2)

    output_path = tmp_path / "signed.pdf"
    result = embed_signatures(
        pdf_path,
        output_path,
        signatures=[
            {"image_path": str(sig1), "x": 100, "y": 100, "width": 80, "height": 30, "label": "Ο Πρόεδρος"},
            {"image_path": str(sig2), "x": 350, "y": 100, "width": 80, "height": 30, "label": "Ο Γενικός Γραμματέας"},
        ],
    )
    assert result.exists()
    assert result.stat().st_size > 0
```

**Step 2: Run to verify fail**

Run: `python -m pytest tests/test_pdf_signatures.py -v`
Expected: FAIL (function doesn't exist).

**Step 3: Implement**

Add to `src/documents/pdf_generator.py`:

```python
def embed_signatures(
    input_pdf: Path,
    output_pdf: Path,
    signatures: list[dict[str, Any]],
    page_number: int = -1,
    workflow: str = "pdf_generator",
) -> Path:
    """Overlay signature images on the last page of a PDF.

    Args:
        input_pdf: Source PDF path.
        output_pdf: Output path for signed PDF.
        signatures: List of signature configs, each with:
            - image_path: str - path to signature image (PNG/JPG)
            - x: float - x position from left edge (points)
            - y: float - y position from bottom edge (points)
            - width: float - display width (points)
            - height: float - display height (points)
            - label: str - text label below signature (e.g., "Ο Πρόεδρος")
        page_number: Page to sign (default -1 = last page).
        workflow: Workflow name for audit logging.

    Returns:
        Path to the signed PDF.
    """
    from PyPDF2 import PdfReader, PdfWriter
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as pdf_canvas
    from io import BytesIO

    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    # Create an overlay PDF with signatures
    overlay_buffer = BytesIO()
    c = pdf_canvas.Canvas(overlay_buffer, pagesize=A4)
    for sig in signatures:
        c.drawImage(
            sig["image_path"],
            sig["x"], sig["y"],
            width=sig["width"], height=sig["height"],
            preserveAspectRatio=True, mask="auto",
        )
        if sig.get("label"):
            c.setFont("Helvetica", 8)
            c.drawCentredString(
                sig["x"] + sig["width"] / 2,
                sig["y"] - 12,
                sig["label"],
            )
    c.save()
    overlay_buffer.seek(0)

    # Merge overlay onto the target page
    reader = PdfReader(str(input_pdf))
    overlay_reader = PdfReader(overlay_buffer)
    writer = PdfWriter()

    target_page = page_number if page_number >= 0 else len(reader.pages) + page_number
    for i, page in enumerate(reader.pages):
        if i == target_page:
            page.merge_page(overlay_reader.pages[0])
        writer.add_page(page)

    with open(output_pdf, "wb") as f:
        writer.write(f)

    log_action(
        workflow=workflow,
        action="signatures_embedded",
        actor="system",
        target=str(output_pdf),
        details={"signatures": len(signatures), "page": target_page},
    )
    logger.info("Embedded %d signatures on page %d → %s", len(signatures), target_page, output_pdf)
    return output_pdf
```

**Step 4: Install PyPDF2 if needed**

Run: `pip install PyPDF2`

**Step 5: Run test**

Run: `python -m pytest tests/test_pdf_signatures.py -v`
Expected: PASS.

**Step 6: Commit**

```bash
git add src/documents/pdf_generator.py tests/test_pdf_signatures.py
git commit -m "feat(pdf): add signature embedding via overlay merge"
```

---

## Task 6: Enhance board_minutes.md prompt for dual-source merging

**Files:**
- Modify: `data/prompts/board_minutes.md`

**Step 1: Update the system prompt**

Replace the content of `data/prompts/board_minutes.md` with enhanced instructions that handle dual-source merging (SecGen notes + Zoom transcript). See design doc for the merge priority rules.

Key additions:
- Clear instructions that SecGen notes are authoritative for decisions, protocol refs, formal wording
- Zoom transcript fills discussion flow, attendance tracking, speaker attribution
- Output JSON schema remains the same
- Add instructions for extracting attendees from Zoom transcript participant list

**Step 2: Commit**

```bash
git add data/prompts/board_minutes.md
git commit -m "docs: enhance minutes prompt for dual-source merging"
```

---

## Task 7: Implement BoardMeetingMinutesWorkflow - core steps 1-4

This is the main implementation. Replace the skeleton in `src/workflows/board_meeting_minutes.py`.

**Files:**
- Modify: `src/workflows/board_meeting_minutes.py` (replace skeleton)
- Create: `tests/test_minutes_workflow.py`

**Step 1: Write failing tests for steps 1-4**

```python
# tests/test_minutes_workflow.py
"""Tests for the board meeting minutes workflow."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.core.workflow import WorkflowStep

@pytest.fixture
def mock_db(tmp_path):
    with patch("src.core.audit._DB_PATH", tmp_path / "test.db"), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        yield

@pytest.fixture
def workflow(mock_db):
    with patch("src.workflows.board_meeting_minutes.GoogleClient") as mock_g, \
         patch("src.workflows.board_meeting_minutes.ZoomClient") as mock_z, \
         patch("src.workflows.board_meeting_minutes.GmailClient") as mock_gm, \
         patch("src.workflows.board_meeting_minutes.OneDriveClient") as mock_od:
        from src.workflows.board_meeting_minutes import BoardMeetingMinutesWorkflow
        wf = BoardMeetingMinutesWorkflow()
        wf._google = MagicMock()
        wf._zoom = AsyncMock()
        wf._gmail = MagicMock()
        wf._onedrive = AsyncMock()
        yield wf

# --- Step 1: select_sources ---

@pytest.mark.asyncio
async def test_step_select_sources_auto(workflow):
    """select_sources should find the matching Zoom recording and Google Doc."""
    workflow._google.list_docs_in_folder.return_value = [
        {"id": "doc-1", "name": "Πρακτικά ΔΣ03-2026", "modifiedTime": "2026-04-01"},
    ]
    workflow._zoom.list_recordings.return_value = [
        {"id": 999, "topic": "Συνεδρίαση ΔΣ03-2026", "start_time": "2026-04-01T18:00:00Z"},
    ]
    workflow._zoom.get_transcript.return_value = "Transcript text here"
    workflow._google.read_doc_content.return_value = "SecGen notes here"

    ctx = {
        "meeting_ref": "ΔΣ03-2026",
        "source_doc_index": 0,
        "recording_index": 0,
    }
    result = await workflow._step_select_sources(ctx)

    assert result.success
    assert "secgen_notes" in result.data
    assert "zoom_transcript" in result.data
    assert result.data["source_doc_id"] == "doc-1"

@pytest.mark.asyncio
async def test_step_select_sources_no_transcript(workflow):
    """select_sources should succeed with notes only when no transcript found."""
    workflow._google.list_docs_in_folder.return_value = [
        {"id": "doc-1", "name": "Πρακτικά ΔΣ03-2026", "modifiedTime": "2026-04-01"},
    ]
    workflow._zoom.list_recordings.return_value = []
    workflow._google.read_doc_content.return_value = "SecGen notes here"

    ctx = {"meeting_ref": "ΔΣ03-2026", "source_doc_index": 0}
    result = await workflow._step_select_sources(ctx)

    assert result.success
    assert result.data["zoom_transcript"] == ""

# --- Step 2: draft_minutes ---

@pytest.mark.asyncio
async def test_step_draft_minutes(workflow):
    """draft_minutes should call Claude and return structured JSON."""
    mock_json = '{"title": "Πρακτικά", "metadata": {}, "sections": [], "decisions": []}'

    with patch("src.workflows.board_meeting_minutes.ClaudeClient") as mock_claude_cls:
        mock_claude = MagicMock()
        mock_claude.generate.return_value = mock_json
        mock_claude_cls.return_value = mock_claude

        ctx = {
            "secgen_notes": "Notes",
            "zoom_transcript": "Transcript",
            "meeting_ref": "ΔΣ03-2026",
        }
        result = await workflow._step_draft_minutes(ctx)

    assert result.success
    assert "draft_json" in result.data

# --- Step 3: write_draft_to_doc ---

@pytest.mark.asyncio
async def test_step_write_draft_to_doc(workflow):
    """write_draft_to_doc should replace Google Doc content and rename."""
    ctx = {
        "source_doc_id": "doc-1",
        "draft_json": {
            "title": "Πρακτικά Συνεδρίασης ΔΣ03-2026",
            "metadata": {"meeting_number": "3", "date": "2026-04-01"},
            "sections": [{"heading": "Παρόντες", "body": "Μέλη..."}],
            "decisions": [],
        },
        "meeting_ref": "ΔΣ03-2026",
    }
    result = await workflow._step_write_draft_to_doc(ctx)

    assert result.success
    workflow._google.clear_and_write_doc.assert_called_once()
    workflow._google.rename_file.assert_called_once()
    assert "draft_doc_id" in result.data

# --- Step 4: approval + share ---

@pytest.mark.asyncio
async def test_step_approval_and_share(workflow):
    """approval_and_share should send email to board members."""
    with patch("src.workflows.board_meeting_minutes.settings") as mock_settings:
        mock_settings.workflows.board_meeting.board_members = [
            MagicMock(email="board@amnesty.org.gr"),
        ]
        mock_settings.workflows.board_meeting.minutes_share_message = "Review please"
        mock_settings.testing = MagicMock(dry_run_email="")

        workflow._gmail.send_email.return_value = {"id": "msg-1"}

        ctx = {
            "draft_doc_id": "doc-1",
            "meeting_ref": "ΔΣ03-2026",
            "test_mode": False,
        }
        result = await workflow._step_approval_and_share(ctx)

    assert result.success
    assert result.data.get("shared") is True
    workflow._gmail.send_email.assert_called_once()

# --- Full workflow pauses at approval ---

@pytest.mark.asyncio
async def test_workflow_pauses_at_approval(workflow):
    """Full run should pause at approval gate."""
    workflow._google.list_docs_in_folder.return_value = [
        {"id": "doc-1", "name": "Notes", "modifiedTime": "2026-04-01"},
    ]
    workflow._zoom.list_recordings.return_value = []
    workflow._google.read_doc_content.return_value = "Notes"

    mock_json = '{"title": "T", "metadata": {}, "sections": [], "decisions": []}'
    with patch("src.workflows.board_meeting_minutes.ClaudeClient") as mock_claude_cls, \
         patch("src.workflows.board_meeting_minutes.settings") as mock_settings:
        mock_claude = MagicMock()
        mock_claude.generate.return_value = mock_json
        mock_claude_cls.return_value = mock_claude
        mock_settings.google.minutes_drafts_folder_id = "folder-123"

        result = await workflow.run({
            "meeting_ref": "ΔΣ03-2026",
            "source_doc_index": 0,
        })

    assert result["status"] == "awaiting_approval"
```

**Step 2: Run to verify fail**

Run: `python -m pytest tests/test_minutes_workflow.py -v`
Expected: FAIL.

**Step 3: Implement the workflow**

Replace `src/workflows/board_meeting_minutes.py` with the full implementation.
Key points:
- Class init: `GoogleClient`, `ZoomClient`, lazy `GmailClient` (needs Google creds), lazy `OneDriveClient`
- `_step_select_sources`: list Drive docs, list Zoom recordings, auto-match by meeting_ref, download transcript
- `_step_draft_minutes`: load prompt from `data/prompts/board_minutes.md`, call Claude with both sources
- `_step_write_draft_to_doc`: `clear_and_write_doc` + `rename_file` to `[Πρόχειρο] Πρακτικά - Συνεδρίαση {ref}`
- `_step_approval_and_share`: send Gmail to board with Drive link
- Steps 5-6 (finalize, extract_decisions) are `_step_finalize` and `_step_extract_decisions` - see Task 8

**Step 4: Run tests**

Run: `python -m pytest tests/test_minutes_workflow.py -v`
Expected: All PASS.

**Step 5: Commit**

```bash
git add src/workflows/board_meeting_minutes.py tests/test_minutes_workflow.py
git commit -m "feat: implement board meeting minutes workflow steps 1-4"
```

---

## Task 8: Implement finalize + extract_decisions (steps 5-6)

**Files:**
- Modify: `src/workflows/board_meeting_minutes.py`
- Modify: `tests/test_minutes_workflow.py`

**Step 1: Write failing tests**

Append to `tests/test_minutes_workflow.py`:

```python
# --- Step 5: finalize ---

@pytest.mark.asyncio
async def test_step_finalize(workflow, tmp_path):
    """finalize should generate signed PDF, archive, and register in Πρωτόκολλο."""
    workflow._google.read_doc_content.return_value = "Final minutes content"
    workflow._google.export_doc_as_pdf.return_value = tmp_path / "minutes.pdf"
    (tmp_path / "minutes.pdf").write_bytes(b"%PDF-fake")
    workflow._google.get_last_row_value.return_value = "2026_015"
    workflow._google.rename_file.return_value = None
    workflow._onedrive.upload_file.return_value = {"id": "archive-id"}

    with patch("src.workflows.board_meeting_minutes.settings") as mock_settings, \
         patch("src.workflows.board_meeting_minutes.embed_signatures") as mock_sign:
        mock_settings.ms_client_id = "fake"
        mock_settings.ms_tenant_id = "fake"
        mock_settings.google.protokollo_sheet_id = "proto-sheet"
        mock_settings.onedrive.archive_root = "/Archive"
        mock_sign.return_value = tmp_path / "signed.pdf"
        (tmp_path / "signed.pdf").write_bytes(b"%PDF-signed")

        ctx = {
            "draft_doc_id": "doc-1",
            "meeting_ref": "ΔΣ03-2026",
            "meeting_number": 3,
            "meeting_year": 2026,
        }
        result = await workflow._step_finalize(ctx)

    assert result.success
    assert result.data["protocol_number"] == "2026_016"
    assert "pdf_path" in result.data

# --- Step 6: extract_decisions ---

@pytest.mark.asyncio
async def test_step_extract_decisions(workflow):
    """extract_decisions should parse decisions and write to Βιβλίο Αποφάσεων."""
    workflow._google.get_last_row_value.return_value = "ΔΣ02-03-2026"

    with patch("src.workflows.board_meeting_minutes.settings") as mock_settings:
        mock_settings.google.decisions_sheet_id = "decisions-sheet"

        ctx = {
            "meeting_ref": "ΔΣ03-2026",
            "meeting_number": 3,
            "meeting_year": 2026,
            "draft_json": {
                "decisions": [
                    {"number": "1", "text": "Approved budget", "vote": "ομόφωνα"},
                    {"number": "2", "text": "Hired director", "vote": "κατά πλειοψηφία"},
                ]
            },
        }
        result = await workflow._step_extract_decisions(ctx)

    assert result.success
    assert result.data["decisions_written"] == 2
    assert "ΔΣ03-03-2026" in result.data["decision_numbers"]
    assert "ΔΣ04-03-2026" in result.data["decision_numbers"]
    workflow._google.write_sheet.assert_called_once()
```

**Step 2: Run to verify fail**

Run: `python -m pytest tests/test_minutes_workflow.py::test_step_finalize -v`
Expected: FAIL.

**Step 3: Implement both steps**

Add `_step_finalize()` and `_step_extract_decisions()` to the workflow class.

Key logic for `_step_finalize`:
1. Export Google Doc as PDF
2. Call `embed_signatures()` with President + SecGen signature images from `brand/Signatures/`
3. Read last protocol number from Πρωτόκολλο sheet, increment
4. Upload to OneDrive
5. Register in Πρωτόκολλο: `write_sheet()` with `[protocol, date, title, key_points, tags]`
6. Rename Google Doc to `[Τελικό] Πρακτικά - Συνεδρίαση {ref}`

Key logic for `_step_extract_decisions`:
1. Read `draft_json.decisions` array
2. Read last decision number from Βιβλίο Αποφάσεων for this meeting
3. Generate next `ΔΣ{nn}-{mm}-{yyyy}` numbers
4. `write_sheet()` to append rows

**Step 4: Run tests**

Run: `python -m pytest tests/test_minutes_workflow.py -v`
Expected: All PASS.

**Step 5: Commit**

```bash
git add src/workflows/board_meeting_minutes.py tests/test_minutes_workflow.py
git commit -m "feat: implement finalize + extract_decisions steps for minutes workflow"
```

---

## Task 9: CLI - add `minutes` subcommand group

**Files:**
- Modify: `src/cli/commands.py`

**Step 1: Add the `minutes` command and subcommands**

Add `cmd_minutes()` and `cmd_minutes_finalize()` functions following the same pattern as `cmd_invite()`. Key subcommands:

- `python -m src.cli minutes` - runs steps 1-4 (interactive doc/recording selection)
- `python -m src.cli minutes finalize --meeting ΔΣ03-2026` - runs steps 5-6
- `python -m src.cli minutes list-drafts` - lists Google Docs in the drafts folder

Parser structure:
```python
minutes_parser = subparsers.add_parser("minutes", help="Board meeting minutes workflow")
minutes_sub = minutes_parser.add_subparsers(dest="minutes_command")

# Default (no subcommand) = run workflow steps 1-4
run_parser = minutes_sub.add_parser("run", help="Draft minutes from sources")
run_parser.add_argument("--meeting", required=True, help="Meeting ref (e.g., ΔΣ03-2026)")
run_parser.add_argument("--manual", action="store_true", help="Skip Zoom transcript")
run_parser.add_argument("--test", action="store_true", help="Test mode")

finalize_parser = minutes_sub.add_parser("finalize", help="Finalize and archive minutes")
finalize_parser.add_argument("--meeting", required=True, help="Meeting ref (e.g., ΔΣ03-2026)")

list_parser = minutes_sub.add_parser("list-drafts", help="List draft minutes in Drive")
```

**Step 2: Add interactive menus**

For source selection (Google Docs and Zoom recordings), use the same numbered-menu pattern as the agenda tab selector in `cmd_invite`:
```
  Available draft documents:
    1. Πρακτικά ΔΣ03-2026 (modified: 2026-04-02)
    2. Πρακτικά ΔΣ02-2026 (modified: 2026-03-10)
  Select document [1]: 
```

Same for Zoom recordings:
```
  Available recordings:
    1. Συνεδρίαση ΔΣ03-2026 (2026-04-01)
    2. Συνεδρίαση ΔΣ02-2026 (2026-02-22)
  Select recording [1] (or 0 to skip): 
```

**Step 3: Register in commands dict**

Add `"minutes": cmd_minutes` to the commands dict.

**Step 4: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All pass (existing + new).

**Step 5: Commit**

```bash
git add src/cli/commands.py
git commit -m "feat(cli): add minutes subcommand group (run, finalize, list-drafts)"
```

---

## Task 10: Fix brand yellow in pdf_generator.py and docx_generator.py

**Files:**
- Modify: `src/documents/pdf_generator.py`
- Modify: `src/documents/docx_generator.py`

The brand yellow was corrected to `#FFFF00` in the Brevo template but the PDF and DOCX generators still use `#FFD300`.

**Step 1: Fix**

In `pdf_generator.py`:
```python
AMNESTY_YELLOW = colors.HexColor("#FFFF00")  # was #FFD300
```

In `docx_generator.py`:
```python
AMNESTY_YELLOW = RGBColor(0xFF, 0xFF, 0x00)  # was (0xFF, 0xD3, 0x00)
```

**Step 2: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All pass.

**Step 3: Commit**

```bash
git add src/documents/pdf_generator.py src/documents/docx_generator.py
git commit -m "fix: correct brand yellow to #FFFF00 in PDF and DOCX generators"
```

---

## Task 11: Final integration test + all tests green

**Step 1: Run full test suite**

Run: `python -m pytest tests/ -v --tb=short`
Expected: All tests pass.

**Step 2: Verify imports**

Run: `python -c "from src.workflows.board_meeting_minutes import BoardMeetingMinutesWorkflow; print('OK')"`
Expected: `OK`

**Step 3: Verify CLI**

Run: `python -m src.cli minutes --help`
Expected: Help text showing `run`, `finalize`, `list-drafts` subcommands.

**Step 4: Final commit**

```bash
git add -A
git commit -m "phase2: board meeting minutes workflow complete"
```
