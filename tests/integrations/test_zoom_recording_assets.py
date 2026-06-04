"""Tests for ZoomClient.download_recording_assets + _encode_uuid."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

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


# ── _encode_uuid ─────────────────────────────────────────────────────────────


def test_encode_uuid_plain_unchanged():
    from src.integrations.zoom import _encode_uuid
    assert _encode_uuid("abc123==") == "abc123=="


def test_encode_uuid_leading_slash_double_encoded():
    from src.integrations.zoom import _encode_uuid
    uuid = "/abc+def=="
    expected = quote(quote(uuid, safe=""), safe="")
    assert _encode_uuid(uuid) == expected
    # Double-encoding means the '/' becomes %252F.
    assert "%252F" in _encode_uuid(uuid)


def test_encode_uuid_double_slash_double_encoded():
    from src.integrations.zoom import _encode_uuid
    uuid = "ab//cd"
    expected = quote(quote(uuid, safe=""), safe="")
    assert _encode_uuid(uuid) == expected


# ── download_recording_assets ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_recording_assets(zoom_client, tmp_path):
    recording = {
        "uuid": "abc/def==",
        "topic": "Συνεδρίαση ΔΣ05-2026",
        "start_time": "2026-05-20T18:00:00Z",
        "recording_files": [
            {
                "id": "file-mixed",
                "file_type": "M4A",
                "recording_type": "audio_only",
                "recording_start": "2026-05-20T18:00:05Z",
                "recording_end": "2026-05-20T19:30:00Z",
                "file_extension": "M4A",
                "file_size": 12345,
                "download_url": "https://zoom.us/rec/download/mixed",
            },
            {
                "id": "file-part-1",
                "file_type": "M4A",
                "recording_type": "audio_only",  # per-participant audio
                "recording_start": "2026-05-20T18:00:42Z",  # distinct origin
                "recording_end": "2026-05-20T19:29:55Z",
                "file_extension": "m4a",
                "file_size": 6789,
                "download_url": "https://zoom.us/rec/download/participant1",
            },
        ],
    }

    # Mock the recording lookup.
    zoom_client.get_recording = AsyncMock(return_value=recording)

    # Mock each httpx download with distinct content.
    def make_response(content: bytes):
        resp = MagicMock()
        resp.content = content
        resp.raise_for_status = MagicMock()
        return resp

    responses = {
        "https://zoom.us/rec/download/mixed": make_response(b"MIXED-AUDIO-BYTES"),
        "https://zoom.us/rec/download/participant1": make_response(b"PARTICIPANT-1-BYTES"),
    }

    async def fake_get(url, **kwargs):
        return responses[url]

    with patch("httpx.AsyncClient") as mock_http:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=fake_get)
        mock_http.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_http.return_value.__aexit__ = AsyncMock(return_value=False)

        manifest = await zoom_client.download_recording_assets(
            "abc/def==", dest_dir=str(tmp_path)
        )

    # Manifest top-level shape.
    assert manifest["meeting_uuid"] == "abc/def=="
    assert manifest["topic"] == "Συνεδρίαση ΔΣ05-2026"
    assert manifest["start_time"] == "2026-05-20T18:00:00Z"
    assert manifest["dest_dir"] == str(tmp_path)
    assert len(manifest["files"]) == 2

    # Both files were written with their downloaded bytes.
    written = sorted(p.name for p in tmp_path.iterdir())
    assert "manifest.json" in written
    file_paths = [Path(f["local_path"]) for f in manifest["files"]]
    assert all(p.exists() for p in file_paths)
    contents = {p.read_bytes() for p in file_paths}
    assert contents == {b"MIXED-AUDIO-BYTES", b"PARTICIPANT-1-BYTES"}

    # recording_start / recording_end preserved verbatim (per-participant origin).
    starts = {f["recording_start"] for f in manifest["files"]}
    assert starts == {"2026-05-20T18:00:05Z", "2026-05-20T18:00:42Z"}
    ends = {f["recording_end"] for f in manifest["files"]}
    assert ends == {"2026-05-20T19:30:00Z", "2026-05-20T19:29:55Z"}

    # manifest.json on disk matches the returned manifest.
    on_disk = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk["meeting_uuid"] == "abc/def=="
    assert len(on_disk["files"]) == 2


@pytest.mark.asyncio
async def test_download_captures_participant_audio_files_key(zoom_client, tmp_path):
    """Per-participant audio under the separate ``participant_audio_files`` key
    must also be captured (not only ``recording_files``), tagged by source."""
    recording = {
        "uuid": "p-uuid",
        "topic": "T",
        "start_time": "2026-05-20T18:00:00Z",
        "recording_files": [
            {
                "id": "mixed",
                "recording_type": "audio_only",
                "file_extension": "m4a",
                "recording_start": "2026-05-20T18:00:00Z",
                "recording_end": "2026-05-20T19:00:00Z",
                "download_url": "https://zoom.us/rec/download/mixed",
            },
        ],
        "participant_audio_files": [
            {
                "id": "p1",
                "file_type": "M4A",
                "participant_name": "Γρηγόρης Μουζακίτης",
                "file_extension": "m4a",
                "recording_start": "2026-05-20T18:00:09Z",
                "recording_end": "2026-05-20T19:00:00Z",
                "download_url": "https://zoom.us/rec/download/p1",
            },
        ],
    }
    zoom_client.get_recording = AsyncMock(return_value=recording)

    responses = {
        "https://zoom.us/rec/download/mixed": b"MIXED",
        "https://zoom.us/rec/download/p1": b"PART1",
    }

    async def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.content = responses[url]
        resp.raise_for_status = MagicMock()
        return resp

    with patch("httpx.AsyncClient") as mock_http:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=fake_get)
        mock_http.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_http.return_value.__aexit__ = AsyncMock(return_value=False)

        manifest = await zoom_client.download_recording_assets(
            "p-uuid", dest_dir=str(tmp_path)
        )

    assert len(manifest["files"]) == 2
    by_source = {f["source"]: f for f in manifest["files"]}
    assert set(by_source) == {"recording_files", "participant_audio_files"}
    # The per-participant entry carries the speaker name through.
    assert by_source["participant_audio_files"]["participant"] == "Γρηγόρης Μουζακίτης"


@pytest.mark.asyncio
async def test_download_skips_file_without_url_and_continues(zoom_client, tmp_path):
    recording = {
        "uuid": "plainuuid",
        "topic": "T",
        "start_time": "2026-05-20T18:00:00Z",
        "recording_files": [
            {"id": "no-url", "recording_type": "chat_file", "file_extension": "txt"},
            {
                "id": "good",
                "recording_type": "audio_only",
                "file_extension": "m4a",
                "recording_start": "2026-05-20T18:00:00Z",
                "recording_end": "2026-05-20T19:00:00Z",
                "download_url": "https://zoom.us/rec/download/good",
            },
        ],
    }
    zoom_client.get_recording = AsyncMock(return_value=recording)

    resp = MagicMock()
    resp.content = b"GOOD"
    resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_http:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=resp)
        mock_http.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_http.return_value.__aexit__ = AsyncMock(return_value=False)

        manifest = await zoom_client.download_recording_assets(
            "plainuuid", dest_dir=str(tmp_path)
        )

    # The url-less file was skipped; only the good one is in the manifest.
    assert len(manifest["files"]) == 1
    assert manifest["files"][0]["id"] == "good"
