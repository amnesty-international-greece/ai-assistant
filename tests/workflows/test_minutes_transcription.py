"""Unit tests for the Zoom-recording -> minutes-skeleton orchestration layer.

A ``FakeTranscriber`` supplies canned offset-tagged pieces per audio path, so
these tests never touch faster-whisper or any audio. They exercise file
selection, wall-clock alignment, speaker resolution, end-to-end wiring, and
error robustness.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.workflows.minutes_transcription import (
    build_minutes_from_recording,
    manifest_to_segments,
)


class FakeTranscriber:
    """Returns canned ``[(text, start, end)]`` pieces keyed by audio path.

    Unknown paths return an empty list, mimicking a file with no speech.
    """

    def __init__(self, by_path: dict[str, list[tuple[str, float, float]]]):
        self.by_path = by_path
        self.calls: list[str] = []

    def transcribe(self, audio_path, *, language="el", initial_prompt=""):
        self.calls.append(audio_path)
        return list(self.by_path.get(audio_path, []))


def _audio_file(**kwargs) -> dict:
    """A manifest file entry with sensible audio defaults, overridable."""

    entry = {
        "id": "f1",
        "source": "participant_audio_files",
        "file_type": "",
        "recording_type": "audio_only",
        "participant": "",
        "recording_start": "2026-05-20T18:00:00+00:00",
        "recording_end": "2026-05-20T19:00:00+00:00",
        "file_extension": "m4a",
        "file_size": "123",
        "local_path": "/tmp/audio.m4a",
    }
    entry.update(kwargs)
    return entry


def test_wall_clock_alignment():
    manifest = {"files": [_audio_file(
        local_path="/tmp/a.m4a",
        recording_start="2026-05-20T18:00:10+00:00",
    )]}
    fake = FakeTranscriber({"/tmp/a.m4a": [("Kalimera", 5.0, 7.0)]})

    segments = manifest_to_segments(manifest, fake)

    assert len(segments) == 1
    seg = segments[0]
    assert seg.start == datetime(2026, 5, 20, 18, 0, 15, tzinfo=timezone.utc)
    assert seg.end == datetime(2026, 5, 20, 18, 0, 17, tzinfo=timezone.utc)


def test_spike_robust_two_files_different_origins():
    # File A starts later than B but B has a larger offset -> after sort, the
    # pieces interleave by absolute wall-clock time, not by file or offset.
    file_a = _audio_file(
        id="a", participant="A",
        local_path="/tmp/a.m4a",
        recording_start="2026-05-20T18:00:30+00:00",
    )
    file_b = _audio_file(
        id="b", participant="B",
        local_path="/tmp/b.m4a",
        recording_start="2026-05-20T18:00:00+00:00",
    )
    manifest = {"files": [file_a, file_b]}
    fake = FakeTranscriber({
        "/tmp/a.m4a": [("a-at-30", 0.0, 1.0)],   # absolute 18:00:30
        "/tmp/b.m4a": [("b-at-0", 0.0, 1.0),     # absolute 18:00:00
                       ("b-at-40", 40.0, 41.0)], # absolute 18:00:40
    })

    segments = manifest_to_segments(manifest, fake)

    texts = [s.text for s in segments]
    assert texts == ["b-at-0", "a-at-30", "b-at-40"]
    # Each file's own origin was applied.
    assert segments[0].start == datetime(2026, 5, 20, 18, 0, 0, tzinfo=timezone.utc)
    assert segments[1].start == datetime(2026, 5, 20, 18, 0, 30, tzinfo=timezone.utc)
    assert segments[2].start == datetime(2026, 5, 20, 18, 0, 40, tzinfo=timezone.utc)


def test_speaker_resolution_participant_and_roster():
    roster = [{"name": "Grigoris Mouzakitis", "role": "Member"}]
    manifest = {"files": [_audio_file(
        participant="Grigoris Mouzakitis",
        local_path="/tmp/g.m4a",
    )]}
    fake = FakeTranscriber({"/tmp/g.m4a": [("x", 0.0, 1.0)]})

    segments = manifest_to_segments(manifest, fake, roster=roster)

    assert segments[0].speaker == "Grigoris Mouzakitis"


def test_speaker_resolution_roster_loose_substring():
    roster = [{"name": "Grigoris Mouzakitis", "role": "Member"}]
    # Participant is a substring of the roster name -> maps to canonical name.
    manifest = {"files": [_audio_file(
        participant="grigoris",
        local_path="/tmp/g.m4a",
    )]}
    fake = FakeTranscriber({"/tmp/g.m4a": [("x", 0.0, 1.0)]})

    segments = manifest_to_segments(manifest, fake, roster=roster)

    assert segments[0].speaker == "Grigoris Mouzakitis"


def test_speaker_resolution_empty_participant_gets_stable_label():
    file_a = _audio_file(id="a", participant="", local_path="/tmp/a.m4a")
    file_b = _audio_file(id="b", participant="", local_path="/tmp/b.m4a")
    manifest = {"files": [file_a, file_b]}
    fake = FakeTranscriber({
        "/tmp/a.m4a": [("x", 0.0, 1.0)],
        "/tmp/b.m4a": [("y", 0.0, 1.0)],
    })

    segments = manifest_to_segments(manifest, fake)

    speakers = {s.speaker for s in segments}
    assert speakers == {"Ομιλητής 1",
                        "Ομιλητής 2"}


def test_file_selection_prefers_participant_audio():
    participant_file = _audio_file(
        id="p", source="participant_audio_files",
        participant="Speaker", local_path="/tmp/p.m4a",
    )
    mixed_audio = _audio_file(
        id="m", source="recording_files", recording_type="audio_only",
        participant="", local_path="/tmp/m.m4a",
    )
    chat_txt = _audio_file(
        id="c", source="recording_files", recording_type="chat_file",
        file_type="CHAT", file_extension="txt", local_path="/tmp/c.txt",
    )
    manifest = {"files": [participant_file, mixed_audio, chat_txt]}
    fake = FakeTranscriber({
        "/tmp/p.m4a": [("x", 0.0, 1.0)],
        "/tmp/m.m4a": [("y", 0.0, 1.0)],
        "/tmp/c.txt": [("z", 0.0, 1.0)],
    })

    manifest_to_segments(manifest, fake)

    # Only the per-participant file was transcribed.
    assert fake.calls == ["/tmp/p.m4a"]


def test_file_selection_falls_back_to_mixed_audio():
    mixed_audio = _audio_file(
        id="m", source="recording_files", recording_type="audio_only",
        local_path="/tmp/m.m4a",
    )
    chat_txt = _audio_file(
        id="c", source="recording_files", recording_type="chat_file",
        file_type="CHAT", file_extension="txt", local_path="/tmp/c.txt",
    )
    manifest = {"files": [mixed_audio, chat_txt]}
    fake = FakeTranscriber({
        "/tmp/m.m4a": [("y", 0.0, 1.0)],
        "/tmp/c.txt": [("z", 0.0, 1.0)],
    })

    manifest_to_segments(manifest, fake)

    # No participant_audio_files -> fall back to the mixed audio_only file;
    # the chat .txt is never transcribed.
    assert fake.calls == ["/tmp/m.m4a"]


def test_unparseable_recording_start_skips_file():
    good = _audio_file(
        id="g", participant="A", local_path="/tmp/g.m4a",
        recording_start="2026-05-20T18:00:00+00:00",
    )
    bad = _audio_file(
        id="b", participant="B", local_path="/tmp/b.m4a",
        recording_start="not-a-timestamp",
    )
    manifest = {"files": [good, bad]}
    fake = FakeTranscriber({
        "/tmp/g.m4a": [("ok", 0.0, 1.0)],
        "/tmp/b.m4a": [("nope", 0.0, 1.0)],
    })

    segments = manifest_to_segments(manifest, fake)

    assert [s.text for s in segments] == ["ok"]


def test_build_minutes_from_recording_end_to_end():
    roster = [
        {"name": "Alpha", "role": "Chair"},
        {"name": "Beta", "role": "Member"},
    ]
    agenda_items = ["Eisagogi", "Proypologismos"]

    file_a = _audio_file(
        id="a", participant="Alpha", local_path="/tmp/a.m4a",
        recording_start="2026-05-20T18:00:00+00:00",
    )
    manifest = {"meeting_uuid": "uuid-1", "files": [file_a]}

    # A piece at +5s (item 1) and +120s (after the advance into item 2 at +60s).
    fake = FakeTranscriber({"/tmp/a.m4a": [
        ("statement in item one", 5.0, 8.0),
        ("statement in item two", 120.0, 123.0),
    ]})

    events = [
        {"event_type": "agenda_advance", "ts": "2026-05-20T18:00:00+00:00",
         "payload": {"to_index": 1}},
        {"event_type": "agenda_advance", "ts": "2026-05-20T18:01:00+00:00",
         "payload": {"to_index": 2}},
        {"event_type": "phase", "ts": "2026-05-20T18:30:00+00:00",
         "payload": {"phase": "end"}},
    ]

    skeleton = build_minutes_from_recording(
        manifest=manifest,
        events=events,
        agenda_items=agenda_items,
        roster=roster,
        transcriber=fake,
        meeting_ref="2026_001",
    )

    # Shape.
    assert skeleton["meeting_ref"] == "2026_001"
    assert skeleton["agenda"] == agenda_items
    assert len(skeleton["items"]) == 2

    item_one, item_two = skeleton["items"]
    one_texts = [s["text"] for s in item_one["segments"]]
    two_texts = [s["text"] for s in item_two["segments"]]
    assert "statement in item one" in one_texts
    assert "statement in item two" in two_texts

    # Speaker landed correctly and roster presence picked up the speaker.
    assert item_one["segments"][0]["speaker"] == "Alpha"
    present_names = {p["name"] for p in skeleton["presence"]["present"]}
    assert "Alpha" in present_names


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
