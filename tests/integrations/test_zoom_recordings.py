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
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_http:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_http.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_http.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await zoom_client.list_recordings(from_date="2026-01-01", to_date="2026-03-01")

    assert len(result) == 2
    assert result[0]["topic"] == "Συνεδρίαση ΔΣ01-2026"
