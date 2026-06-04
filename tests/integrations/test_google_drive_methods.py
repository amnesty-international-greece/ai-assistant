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


# ── reset_agenda_sheet ───────────────────────────────────────────────────────


def _stub_list_tabs(client, title="ΔΣ04-2026", sheet_id=1234):
    """Mock list_sheet_tabs to return a single tab."""
    client.list_sheet_tabs = MagicMock(return_value=[{"title": title, "sheetId": sheet_id}])


def _stub_d5_read(client, current_value):
    """Mock the Sheets values().get() chain to return D5 = current_value."""
    client._sheets_service.spreadsheets().values().get.return_value.execute.return_value = {
        "values": [[current_value]] if current_value else [],
    }


def _stub_meta_no_protections(client, sheet_id=1234):
    client._sheets_service.spreadsheets().get.return_value.execute.return_value = {
        "sheets": [{"properties": {"sheetId": sheet_id}, "protectedRanges": []}],
    }


def test_reset_agenda_sheet_increments_meeting_ref(google_client):
    """Normal case: ΔΣ04-2026 → ΔΣ05-2026."""
    _stub_list_tabs(google_client)
    _stub_d5_read(google_client, "ΔΣ04-2026")
    _stub_meta_no_protections(google_client)

    info = google_client.reset_agenda_sheet("test-sheet-id")

    assert info["old_meeting_ref"] == "ΔΣ04-2026"
    assert info["new_meeting_ref"] == "ΔΣ05-2026"
    assert info["protections_removed"] == 0
    # batchUpdate for values was called with the correct payload
    values_call = google_client._sheets_service.spreadsheets().values().batchUpdate.call_args
    data = values_call.kwargs["body"]["data"]
    # D5 set to new ref, D16/D17/D18 set to False (boolean, not "FALSE")
    d5_entry = next(d for d in data if d["range"].endswith("!D5"))
    assert d5_entry["values"] == [["ΔΣ05-2026"]]
    d16_entry = next(d for d in data if d["range"].endswith("!D16"))
    assert d16_entry["values"] == [[False]]


def test_reset_agenda_sheet_year_rollover(google_client):
    """ΔΣ12-2026 rolls over to ΔΣ01-2027."""
    _stub_list_tabs(google_client, title="ΔΣ12-2026")
    _stub_d5_read(google_client, "ΔΣ12-2026")
    _stub_meta_no_protections(google_client)

    info = google_client.reset_agenda_sheet("test-sheet-id")

    assert info["old_meeting_ref"] == "ΔΣ12-2026"
    assert info["new_meeting_ref"] == "ΔΣ01-2027"


def test_reset_agenda_sheet_clears_h7_to_k(google_client):
    """The agenda block H7:K10000 is cleared via values().clear()."""
    _stub_list_tabs(google_client)
    _stub_d5_read(google_client, "ΔΣ04-2026")
    _stub_meta_no_protections(google_client)

    google_client.reset_agenda_sheet("test-sheet-id")

    clear_call = google_client._sheets_service.spreadsheets().values().clear.call_args
    assert clear_call.kwargs["range"].endswith("!H7:K10000")


def test_reset_agenda_sheet_removes_only_named_protection(google_client):
    """Only the protection with description 'ai-assistant:cycle-locked' is removed."""
    _stub_list_tabs(google_client)
    _stub_d5_read(google_client, "ΔΣ04-2026")
    google_client._sheets_service.spreadsheets().get.return_value.execute.return_value = {
        "sheets": [{
            "properties": {"sheetId": 1234},
            "protectedRanges": [
                {"protectedRangeId": 1, "description": "ai-assistant:cycle-locked"},
                {"protectedRangeId": 2, "description": "user-protection-on-D16"},  # user's own — KEEP
                {"protectedRangeId": 3, "description": "ai-assistant:cycle-locked"},  # duplicate — also remove
            ],
        }],
    }

    info = google_client.reset_agenda_sheet("test-sheet-id")

    assert info["protections_removed"] == 2
    delete_call = google_client._sheets_service.spreadsheets().batchUpdate.call_args
    requests = delete_call.kwargs["body"]["requests"]
    deleted_ids = {r["deleteProtectedRange"]["protectedRangeId"] for r in requests}
    assert deleted_ids == {1, 3}
    # User's protection (id=2) MUST NOT be in the delete list
    assert 2 not in deleted_ids


def test_reset_agenda_sheet_raises_on_bad_d5(google_client):
    """If D5 doesn't match the ΔΣXX-YYYY pattern, raise."""
    _stub_list_tabs(google_client)
    _stub_d5_read(google_client, "garbage value")
    _stub_meta_no_protections(google_client)

    with pytest.raises(RuntimeError, match="not a recognised meeting ref"):
        google_client.reset_agenda_sheet("test-sheet-id")


def test_reset_agenda_sheet_raises_on_no_tabs(google_client):
    google_client.list_sheet_tabs = MagicMock(return_value=[])

    with pytest.raises(RuntimeError, match="no tabs"):
        google_client.reset_agenda_sheet("test-sheet-id")


# ── read_meeting_ref: D5-with-cache-fallback ─────────────────────────────────


def test_read_meeting_ref_returns_valid_d5_and_caches_it(google_client):
    """A valid D5 read returns the value AND writes it to the mirror cache."""
    from src.core.audit import get_meeting_ref_cache

    _stub_list_tabs(google_client, title="ΔΣ04-2026")
    _stub_d5_read(google_client, "ΔΣ04-2026")

    assert google_client.read_meeting_ref("sheet-id") == "ΔΣ04-2026"
    # Cache was refreshed as a side-effect
    assert get_meeting_ref_cache() == "ΔΣ04-2026"


def test_read_meeting_ref_falls_back_to_cache_when_d5_empty(google_client):
    """If D5 reads empty, return the most recent cached value."""
    from src.core.audit import set_meeting_ref_cache

    # Seed cache with a previously-good value
    set_meeting_ref_cache("ΔΣ03-2026")

    _stub_list_tabs(google_client)
    # D5 reads as empty
    google_client._sheets_service.spreadsheets().values().get.return_value.execute.return_value = {
        "values": [[""]],
    }

    assert google_client.read_meeting_ref("sheet-id") == "ΔΣ03-2026"


def test_read_meeting_ref_falls_back_to_cache_when_d5_malformed(google_client):
    """If D5 holds garbage, the cached value still wins."""
    from src.core.audit import set_meeting_ref_cache

    set_meeting_ref_cache("ΔΣ03-2026")
    _stub_list_tabs(google_client)
    google_client._sheets_service.spreadsheets().values().get.return_value.execute.return_value = {
        "values": [["not a valid ref"]],
    }

    assert google_client.read_meeting_ref("sheet-id") == "ΔΣ03-2026"


def test_read_meeting_ref_falls_back_to_cache_when_sheets_api_fails(google_client):
    """A Sheets API exception falls back to cache rather than propagating."""
    from src.core.audit import set_meeting_ref_cache

    set_meeting_ref_cache("ΔΣ02-2026")
    google_client.list_sheet_tabs = MagicMock(side_effect=RuntimeError("API down"))

    assert google_client.read_meeting_ref("sheet-id") == "ΔΣ02-2026"


def test_read_meeting_ref_raises_when_no_d5_and_no_cache(google_client):
    """No D5 + no cache → raise, do NOT return a placeholder."""
    _stub_list_tabs(google_client)
    google_client._sheets_service.spreadsheets().values().get.return_value.execute.return_value = {
        "values": [[""]],
    }

    with pytest.raises(RuntimeError, match="no cached value"):
        google_client.read_meeting_ref("sheet-id")


def test_read_meeting_ref_use_cache_false_skips_fallback(google_client):
    """With use_cache=False, failures propagate even if a cache exists."""
    from src.core.audit import set_meeting_ref_cache

    set_meeting_ref_cache("ΔΣ02-2026")
    _stub_list_tabs(google_client)
    google_client._sheets_service.spreadsheets().values().get.return_value.execute.return_value = {
        "values": [[""]],
    }

    with pytest.raises(RuntimeError, match="D5 is empty"):
        google_client.read_meeting_ref("sheet-id", use_cache=False)


def test_reset_agenda_sheet_refreshes_cache(google_client):
    """After incrementing D5, the local mirror should reflect the new ref."""
    from src.core.audit import get_meeting_ref_cache

    _stub_list_tabs(google_client, title="ΔΣ04-2026")
    _stub_d5_read(google_client, "ΔΣ04-2026")
    _stub_meta_no_protections(google_client)

    info = google_client.reset_agenda_sheet("test-sheet-id")
    assert info["new_meeting_ref"] == "ΔΣ05-2026"
    assert get_meeting_ref_cache() == "ΔΣ05-2026"
