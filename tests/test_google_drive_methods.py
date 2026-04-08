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
    """list_docs_in_folder should pass a mimeType filter and return only Docs."""
    # The Drive API filters server-side; mock returns only what the query would yield.
    google_client._drive_service.files().list.return_value.execute.return_value = {
        "files": [
            {"id": "a", "name": "Notes ΔΣ01", "mimeType": "application/vnd.google-apps.document", "modifiedTime": "2026-01-01T00:00:00Z"},
        ]
    }
    result = google_client.list_docs_in_folder("folder-123")
    assert len(result) == 1
    assert result[0]["id"] == "a"
    # Verify the query included the mimeType filter
    call_kwargs = google_client._drive_service.files().list.call_args
    assert "application/vnd.google-apps.document" in call_kwargs.kwargs.get("q", "")

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
    # batchUpdate should have been called via the retry helper
    assert google_client._docs_service.documents().batchUpdate.called

def test_rename_file(google_client):
    """rename_file should call Drive files().update with new name."""
    google_client.rename_file("file-123", "New Name")
    google_client._drive_service.files().update.assert_called()

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
