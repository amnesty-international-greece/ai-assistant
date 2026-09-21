"""Recordings and transcripts are deleted once the minutes are done (B6).

Hours of board members speaking freely are the most sensitive thing here.
FOUNDATION.md §3.1 keeps them only until the minutes are finalised. Equally,
the job must not delete the source of minutes that are still being written.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.core import retention
from src.core.audit import save_workflow_state


def _recording(root, uuid: str, topic: str, days_old: int, *, size: int = 2048) -> None:
    folder = root / uuid
    folder.mkdir(parents=True)
    started = datetime.now(timezone.utc) - timedelta(days=days_old)
    (folder / "manifest.json").write_text(
        json.dumps({"topic": topic, "start_time": started.isoformat()}), encoding="utf-8"
    )
    (folder / "audio.m4a").write_bytes(b"x" * size)
    (folder / "captions.vtt").write_text("WEBVTT", encoding="utf-8")


def _finalised(meeting_ref: str) -> None:
    save_workflow_state(
        workflow_name="board_meeting_minutes",
        workflow_id=f"wf-{meeting_ref}",
        state="completed",
        data={"context": {"meeting_ref": meeting_ref}, "step_index": 9},
    )


@pytest.fixture
def folders(tmp_path, monkeypatch):
    recordings, transcripts = tmp_path / "recordings", tmp_path / "transcripts"
    recordings.mkdir()
    transcripts.mkdir()
    monkeypatch.setattr(retention.settings.minutes_pipeline, "recordings_dir", str(recordings))
    monkeypatch.setattr(retention.settings.minutes_pipeline, "transcripts_dir", str(transcripts))
    monkeypatch.setattr(retention.settings.retention, "grace_days", 14)
    monkeypatch.setattr(retention.settings.retention, "max_age_days", 365)
    return recordings, transcripts


def _by_ref(items, ref):
    return next(i for i in items if i.meeting_ref == ref)


def test_a_recording_is_kept_while_the_minutes_are_unfinished(folders):
    recordings, _ = folders
    _recording(recordings, "uuid-a", "Συνεδρίαση ΔΣ05-2026", days_old=200)

    item = _by_ref(retention.plan(), "ΔΣ05-2026")
    assert item.delete is False
    assert "not finalised" in item.reason


def test_a_recording_goes_once_the_minutes_are_finalised_and_the_grace_passes(folders):
    recordings, _ = folders
    _recording(recordings, "uuid-a", "Συνεδρίαση ΔΣ05-2026", days_old=30)
    _finalised("ΔΣ05-2026")

    item = _by_ref(retention.plan(), "ΔΣ05-2026")
    assert item.delete is True
    assert "finalised" in item.reason


def test_the_grace_period_protects_a_just_finalised_meeting(folders):
    recordings, _ = folders
    _recording(recordings, "uuid-a", "Συνεδρίαση ΔΣ05-2026", days_old=3)
    _finalised("ΔΣ05-2026")

    item = _by_ref(retention.plan(), "ΔΣ05-2026")
    assert item.delete is False
    assert "grace" in item.reason


def test_nothing_survives_the_maximum_age(folders):
    recordings, _ = folders
    _recording(recordings, "uuid-old", "Συνεδρίαση ΔΣ01-2024", days_old=400)

    item = _by_ref(retention.plan(), "ΔΣ01-2024")
    assert item.delete is True
    assert "maximum" in item.reason


def test_test_runs_do_not_count_as_finalised_minutes(folders):
    recordings, _ = folders
    _recording(recordings, "uuid-a", "Συνεδρίαση ΔΣ05-2026", days_old=30)
    save_workflow_state(
        workflow_name="board_meeting_minutes",
        workflow_id="wf-test",
        state="completed",
        data={"context": {"meeting_ref": "ΔΣ05-2026", "test_mode": True}, "step_index": 9},
    )
    assert _by_ref(retention.plan(), "ΔΣ05-2026").delete is False


def test_applying_deletes_the_media_and_keeps_the_manifest(folders):
    recordings, _ = folders
    _recording(recordings, "uuid-a", "Συνεδρίαση ΔΣ05-2026", days_old=30, size=4096)
    _finalised("ΔΣ05-2026")

    summary = retention.apply(retention.plan())

    folder = recordings / "uuid-a"
    assert not (folder / "audio.m4a").exists()
    assert not (folder / "captions.vtt").exists()
    assert (folder / "manifest.json").exists()   # no speech in it, and minutes cite it
    assert summary["bytes_freed"] >= 4096


def test_applying_leaves_everything_that_is_kept(folders):
    recordings, _ = folders
    _recording(recordings, "uuid-a", "Συνεδρίαση ΔΣ05-2026", days_old=200)

    retention.apply(retention.plan())
    assert (recordings / "uuid-a" / "audio.m4a").exists()


def test_purge_state_drops_a_transcript_from_a_saved_context():
    """A transcript is derived data: the transcript file is the source."""
    from src.core.audit import get_workflow_state

    save_workflow_state(
        workflow_name="board_meeting_minutes",
        workflow_id="wf-fat",
        state="completed",
        data={"context": {"meeting_ref": "ΔΣ05-2026", "transcript": "ω" * 80_000}},
    )
    result = retention.purge_state_transcripts()

    assert result["workflows"] == ["wf-fat"]
    assert result["bytes_freed"] > 64 * 1024
    assert "purged" in json.loads(get_workflow_state("wf-fat")["data"])["context"]["transcript"]


def test_a_bulky_context_value_does_not_bloat_the_database():
    """Oversized values live beside the DB, and reading puts them back."""
    from src.core.audit import _blob_dir, get_workflow_state

    save_workflow_state(
        workflow_name="board_meeting_minutes",
        workflow_id="wf-big",
        state="in_progress",
        data={"context": {"meeting_ref": "ΔΣ05-2026", "draft_json": {"body": "ω" * 90_000}}},
    )

    row = get_workflow_state("wf-big")
    restored = json.loads(row["data"])["context"]["draft_json"]
    assert restored == {"body": "ω" * 90_000}
    assert list((_blob_dir() / "wf-big").glob("*.json"))
