"""Tests for src/workflows/timeline_speakers.py (pure, stdlib only)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from src.workflows.timeline_speakers import (
    SpeakerInterval,
    _hhmmss_ms_to_seconds,
    attribute_segments,
    dominant_speaker,
    parse_timeline,
)

_BASE = datetime(2026, 5, 20, 18, 0, 0, tzinfo=timezone.utc)


def test_hhmmss_ms_to_seconds():
    assert abs(_hhmmss_ms_to_seconds("00:04:19.410") - 259.41) < 1e-6
    assert _hhmmss_ms_to_seconds("00:00:00.900") == 0.9
    assert _hhmmss_ms_to_seconds("01:02:03") == 3723.0


def test_parse_timeline_builds_intervals_and_skips_silence():
    timeline = {
        "timeline": [
            {"ts": "00:00:00.000", "users": []},  # silence at start -> skipped
            {"ts": "00:00:10.000",
             "users": [{"username": "Giorgos Athanasias", "user_id": "1"}]},
            {"ts": "00:00:20.000", "users": []},  # silence gap -> skipped
            {"ts": "00:00:30.000",
             "users": [{"username": "Eleni Kontou", "user_id": "2"}]},
            {"ts": "00:00:45.000", "users": []},  # closes the last speaker
        ]
    }
    intervals = parse_timeline(timeline)
    # silence windows skipped; the two speaking windows kept
    assert len(intervals) == 2
    assert intervals[0] == SpeakerInterval(10.0, 20.0, "Giorgos Athanasias")
    assert intervals[1] == SpeakerInterval(30.0, 45.0, "Eleni Kontou")


def test_parse_timeline_accepts_bare_list_and_path(tmp_path):
    bare = [
        {"ts": "00:00:05.000", "users": [{"username": "A"}]},
        {"ts": "00:00:15.000", "users": []},
    ]
    intervals = parse_timeline(bare)
    assert intervals == [SpeakerInterval(5.0, 15.0, "A")]

    p = tmp_path / "timeline.json"
    p.write_text(json.dumps({"timeline": bare}), encoding="utf-8")
    from_path = parse_timeline(str(p))
    assert from_path == [SpeakerInterval(5.0, 15.0, "A")]


def test_dominant_speaker_picks_max_overlap():
    intervals = [
        SpeakerInterval(0.0, 10.0, "A"),
        SpeakerInterval(10.0, 30.0, "B"),
    ]
    # [5, 25): 5s of A, 15s of B -> B wins.
    assert dominant_speaker(5.0, 25.0, intervals) == "B"


def test_dominant_speaker_none_when_no_overlap():
    intervals = [SpeakerInterval(0.0, 10.0, "A")]
    assert dominant_speaker(50.0, 60.0, intervals) is None


def test_attribute_segments_labels_and_wall_clock():
    intervals = [
        SpeakerInterval(0.0, 10.0, "Giorgos Athanasias"),
        SpeakerInterval(10.0, 20.0, "Eleni Kontou"),
    ]
    raw = [
        ("first piece", 0.0, 8.0),    # all overlaps A
        ("second piece", 11.0, 19.0),  # all overlaps B
        ("orphan piece", 100.0, 110.0),  # no overlap -> fallback
    ]
    segs = attribute_segments(raw, intervals, base=_BASE)
    assert [s.speaker for s in segs] == [
        "Giorgos Athanasias",
        "Eleni Kontou",
        "Άγνωστος ομιλητής",  # default UNKNOWN_SPEAKER fallback (no overlap)
    ]
    # wall-clock = base + offset
    assert segs[0].start == datetime(2026, 5, 20, 18, 0, 0, tzinfo=timezone.utc)
    assert segs[1].start == datetime(2026, 5, 20, 18, 0, 11, tzinfo=timezone.utc)
    assert segs[2].end == datetime(2026, 5, 20, 18, 1, 50, tzinfo=timezone.utc)


def test_attribute_segments_custom_fallback():
    segs = attribute_segments(
        [("x", 0.0, 1.0)], [], base=_BASE, fallback="Speaker"
    )
    assert segs[0].speaker == "Speaker"
