"""Tests for download_recording_assets audio_only video-skipping behaviour."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_db(tmp_path):
    with patch("src.core.audit._DB_PATH", tmp_path / "audit.db"), \
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


def _recording():
    return {
        "uuid": "av-uuid",
        "topic": "T",
        "start_time": "2026-05-20T18:00:00Z",
        "recording_files": [
            {
                "id": "video",
                "file_type": "MP4",
                "recording_type": "shared_screen_with_speaker_view",
                "file_extension": "mp4",
                "recording_start": "2026-05-20T18:00:00Z",
                "recording_end": "2026-05-20T19:00:00Z",
                "download_url": "https://zoom.us/rec/download/video",
            },
            {
                "id": "mixed",
                "file_type": "M4A",
                "recording_type": "audio_only",
                "file_extension": "m4a",
                "recording_start": "2026-05-20T18:00:00Z",
                "recording_end": "2026-05-20T19:00:00Z",
                "download_url": "https://zoom.us/rec/download/mixed",
            },
            {
                "id": "timeline",
                "file_type": "TIMELINE",
                "recording_type": "timeline",
                "file_extension": "json",
                "recording_start": "2026-05-20T18:00:00Z",
                "recording_end": "2026-05-20T19:00:00Z",
                "download_url": "https://zoom.us/rec/download/timeline",
            },
        ],
    }


_RESPONSES = {
    "https://zoom.us/rec/download/video": b"VIDEO",
    "https://zoom.us/rec/download/mixed": b"MIXED",
    "https://zoom.us/rec/download/timeline": b'{"timeline": []}',
}


@pytest.mark.asyncio
async def test_audio_only_skips_video_keeps_audio_and_timeline(zoom_client, tmp_path):
    zoom_client.get_recording = AsyncMock(return_value=_recording())
    with patch("httpx.AsyncClient") as mock_http:
        mock_client = AsyncMock()

        async def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.content = _RESPONSES[url]
            resp.raise_for_status = MagicMock()
            return resp

        mock_client.get = AsyncMock(side_effect=fake_get)
        mock_http.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_http.return_value.__aexit__ = AsyncMock(return_value=False)

        manifest = await zoom_client.download_recording_assets(
            "av-uuid", dest_dir=str(tmp_path), audio_only=True
        )

    ids = {f["id"] for f in manifest["files"]}
    assert ids == {"mixed", "timeline"}  # video skipped
    types = {f["recording_type"] for f in manifest["files"]}
    assert "audio_only" in types and "timeline" in types
    assert "shared_screen_with_speaker_view" not in types


@pytest.mark.asyncio
async def test_include_video_keeps_video(zoom_client, tmp_path):
    zoom_client.get_recording = AsyncMock(return_value=_recording())
    with patch("httpx.AsyncClient") as mock_http:
        mock_client = AsyncMock()

        async def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.content = _RESPONSES[url]
            resp.raise_for_status = MagicMock()
            return resp

        mock_client.get = AsyncMock(side_effect=fake_get)
        mock_http.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_http.return_value.__aexit__ = AsyncMock(return_value=False)

        manifest = await zoom_client.download_recording_assets(
            "av-uuid", dest_dir=str(tmp_path), audio_only=False
        )

    ids = {f["id"] for f in manifest["files"]}
    assert ids == {"video", "mixed", "timeline"}  # video included
