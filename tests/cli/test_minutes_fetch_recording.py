"""Tests for the `ai-assistant minutes fetch-recording` CLI command."""
from __future__ import annotations

import argparse
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def mock_db(tmp_path):
    db_path = tmp_path / "test.db"
    with patch("src.core.audit._DB_PATH", db_path), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        yield


def _ns(**kwargs) -> argparse.Namespace:
    base = {
        "meeting_uuid": "abc==",
        "dest": None,
        "participants": False,
        "minutes_command": "fetch-recording",
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


def _fake_manifest() -> dict:
    return {
        "meeting_uuid": "abc==",
        "topic": "Συνεδρίαση ΔΣ",
        "start_time": "2026-05-30T18:00:00Z",
        "dest_dir": "data/recordings/abc__",
        "files": [
            {
                "id": "f1",
                "source": "recording_files",
                "file_type": "MP4",
                "recording_type": "shared_screen_with_speaker_view",
                "participant": "",
                "recording_start": "2026-05-30T18:00:05Z",
                "recording_end": "2026-05-30T19:00:00Z",
                "file_extension": "MP4",
                "file_size": 123,
                "local_path": "data/recordings/abc__/shared_screen_f1.MP4",
            },
            {
                "id": "f2",
                "source": "participant_audio_files",
                "file_type": "M4A",
                "recording_type": "audio_only",
                "participant": "Γιώργος Αθανασίας",
                "recording_start": "2026-05-30T18:00:05Z",
                "recording_end": "2026-05-30T19:00:00Z",
                "file_extension": "M4A",
                "file_size": 456,
                "local_path": "data/recordings/abc__/audio_only_f2.M4A",
            },
        ],
    }


def test_fetch_recording_happy_path(mock_db, capsys):
    """Downloads assets and prints the topic + a file row (no participants)."""
    with patch(
        "src.integrations.zoom.ZoomClient.download_recording_assets",
        new=AsyncMock(return_value=_fake_manifest()),
    ):
        from src.cli.commands import cmd_minutes_fetch_recording
        cmd_minutes_fetch_recording(_ns())

    out = capsys.readouterr().out
    assert "Συνεδρίαση ΔΣ" in out
    assert "participant_audio_files" in out
    assert "Γιώργος Αθανασίας" in out


def test_fetch_recording_with_participants(mock_db, capsys):
    """`--participants` also fetches + prints the attendance list."""
    parts = [
        {"name": "Μαρία", "user_email": "maria@example.org",
         "join_time": "2026-05-30T18:00:00Z", "leave_time": "2026-05-30T19:00:00Z"},
        {"name": "Κώστας"},  # missing keys → defensive
    ]
    with patch(
        "src.integrations.zoom.ZoomClient.download_recording_assets",
        new=AsyncMock(return_value=_fake_manifest()),
    ), patch(
        "src.integrations.zoom.ZoomClient.get_past_participants",
        new=AsyncMock(return_value=parts),
    ):
        from src.cli.commands import cmd_minutes_fetch_recording
        cmd_minutes_fetch_recording(_ns(participants=True))

    out = capsys.readouterr().out
    assert "Participants: 2" in out
    assert "Μαρία" in out
    assert "Κώστας" in out


def test_fetch_recording_error_path(mock_db, capsys):
    """A download error prints `ERROR:` and does not propagate."""
    with patch(
        "src.integrations.zoom.ZoomClient.download_recording_assets",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        from src.cli.commands import cmd_minutes_fetch_recording
        # Must not raise.
        cmd_minutes_fetch_recording(_ns())

    out = capsys.readouterr().out
    assert "ERROR:" in out
    assert "boom" in out
