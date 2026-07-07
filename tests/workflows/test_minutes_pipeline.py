"""Tests for the minutes pipeline orchestrator (src/workflows/minutes_pipeline.py).

These cover the wiring seam only: roster/glossary building, the transcriber
factory, the FakeTranscriber, transcript-file parsing (Zoom-copy + VTT), and
``assemble_minutes`` end-to-end via both source paths. No real ASR and no LLM
are exercised (draft=False everywhere).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from unittest.mock import patch

from src.workflows.minutes_pipeline import (
    FakeTranscriber,
    assemble_minutes,
    build_glossary,
    build_roster,
    get_transcriber,
    parse_transcript_file,
)

_BASE = datetime(2026, 6, 15, 0, 0, 0, tzinfo=timezone.utc)


def test_remap_speakers_applies_aliases():
    """Zoom display names map to canonical Greek roster names (case-insensitive)."""
    from src.workflows.minutes_pipeline import _remap_speakers
    from src.workflows.minutes_transcription import TranscriptSegment
    segs = [
        TranscriptSegment("Giorgos Athanasias", "x", _BASE, _BASE),
        TranscriptSegment("eleni kontou", "y", _BASE, _BASE),  # different case
        TranscriptSegment("Unmapped Person", "z", _BASE, _BASE),
    ]
    out = _remap_speakers(segs, {"Giorgos Athanasias": "Γεώργιος Αθανασιάς",
                                 "ELENI KONTOU": "Ελένη Κοντού"})
    assert [s.speaker for s in out] == [
        "Γεώργιος Αθανασιάς", "Ελένη Κοντού", "Unmapped Person",
    ]


def test_parse_llm_json_strips_fences_and_recovers():
    from src.workflows.minutes_pipeline import _parse_llm_json
    # fenced json
    assert _parse_llm_json('```json\n{"title": "X", "sections": []}\n```')["title"] == "X"
    # bare json
    assert _parse_llm_json('{"a": 1}')["a"] == 1
    # prose + json block
    assert _parse_llm_json('Ορίστε:\n{"a": 2}\nΤέλος.')["a"] == 2
    # unrecoverable → raw
    assert _parse_llm_json("totally not json")["raw"] == "totally not json"


def test_reuse_transcript_round_trip(tmp_path, temp_db):
    """A cached transcript.json is loaded (no ASR) and rebuilt into a skeleton."""
    import json as _json
    from src.workflows.minutes_pipeline import assemble_minutes
    settings = _fake_settings(tmp_path)
    ref = "ΔΣ05-2026"
    cache = tmp_path / "transcripts" / ref / "transcript.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(_json.dumps([
        {"speaker": "Ελένη Κοντού", "text": "Καλησπέρα.",
         "start": "2026-05-20T18:00:00+00:00", "end": "2026-05-20T18:00:05+00:00"},
    ], ensure_ascii=False), encoding="utf-8")
    res = assemble_minutes(settings=settings, meeting_ref=ref, reuse_transcript=True)
    assert res["source"] == "cache"
    assert res["segment_count"] == 1


def test_unknown_speaker_excluded_from_presence():
    """The UNKNOWN_SPEAKER fallback must not be counted as an attendee."""
    from src.workflows.minutes_skeleton import build_minutes_skeleton
    from src.workflows.minutes_transcription import TranscriptSegment
    from src.workflows.timeline_speakers import UNKNOWN_SPEAKER
    segs = [
        TranscriptSegment("Φοίβος Ιατρέλλης", "a", _BASE, _BASE),
        TranscriptSegment(UNKNOWN_SPEAKER, "b", _BASE, _BASE),
    ]
    sk = build_minutes_skeleton(
        meeting_ref="ΔΣ05-2026", agenda_items=[], events=[], segments=segs,
        roster=[], ignore_speakers={UNKNOWN_SPEAKER},
    )
    present = [p["name"] for p in sk["presence"]["present"]]
    assert "Φοίβος Ιατρέλλης" in present
    assert UNKNOWN_SPEAKER not in present


def test_parse_zoom_copy_contiguous_no_blank_lines(tmp_path):
    """Real Zoom 'copy transcript' output is contiguous (no blank lines between
    turns); each turn is Speaker / HH:MM:SS / text. All turns must parse."""
    content = (
        "Γρηγόρης Μουζακίτης\n00:00:05\n"
        "Καλημέρα, ξεκινάμε με την επικύρωση.\n"
        "Ελένη Κοντού\n00:00:20\n"
        "Συμφωνώ, εντάξει.\n"
        "Σπύρος Απέργης\n00:02:45\n"
        "Να αυξηθεί το αποθεματικό.\n"
    )
    f = tmp_path / "zoom_copy.txt"
    f.write_text(content, encoding="utf-8")
    segs = parse_transcript_file(str(f), base=_BASE)
    assert len(segs) == 3
    assert [s.speaker for s in segs] == [
        "Γρηγόρης Μουζακίτης", "Ελένη Κοντού", "Σπύρος Απέργης",
    ]
    # Offsets applied to base: second turn at 00:00:20.
    assert segs[1].start == datetime(2026, 6, 15, 0, 0, 20, tzinfo=timezone.utc)
    # Multi-word speaker text captured intact.
    assert "αποθεματικό" in segs[2].text


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeMember:
    def __init__(self, first_name, last_name, role=""):
        self.first_name = first_name
        self.last_name = last_name
        self.email = "x@example.org"
        if role:
            self.role = role


class _FakeBoardMeeting:
    def __init__(self, members):
        self.board_members = members


class _FakeWorkflows:
    def __init__(self, members):
        self.board_meeting = _FakeBoardMeeting(members)


class _FakeMinutesPipeline:
    def __init__(self, transcripts_dir):
        self.transcriber = "fake"
        self.whisper_model = "large-v3"
        self.whisper_device = "cpu"
        self.whisper_compute_type = "int8"
        self.language = "el"
        self.recordings_dir = "data/recordings"
        self.transcripts_dir = transcripts_dir
        self.articles_path = "assets/governance/articles.json"


class _FakeSettings:
    def __init__(self, members, transcripts_dir, transcriber="fake"):
        self.workflows = _FakeWorkflows(members)
        self.minutes_pipeline = _FakeMinutesPipeline(transcripts_dir)
        self.minutes_pipeline.transcriber = transcriber


def _fake_settings(tmp_path, transcriber="fake"):
    members = [
        _FakeMember("Ελένη", "Κοντού"),
        _FakeMember("Δημήτρης", "Μαρουλίδης"),
    ]
    return _FakeSettings(members, str(tmp_path / "transcripts"), transcriber)


@pytest.fixture
def temp_db(tmp_path):
    """Fresh DB per test with the audit module connection cache reset."""
    db_path = tmp_path / "test.db"
    with patch("src.core.audit._DB_PATH", db_path), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        yield db_path


# ---------------------------------------------------------------------------
# build_glossary / build_roster
# ---------------------------------------------------------------------------

def test_build_glossary_has_names_and_org(tmp_path):
    settings = _fake_settings(tmp_path)
    glossary = build_glossary(settings)
    assert "Ελένη Κοντού" in glossary
    assert "Δημήτρης Μαρουλίδης" in glossary
    assert "Διεθνής Αμνηστία" in glossary
    assert "Αμνηστία" in glossary
    # De-duped, stable: org names appended after members.
    assert len(glossary) == len(set(glossary))


def test_build_roster_shape(tmp_path):
    settings = _fake_settings(tmp_path)
    roster = build_roster(settings)
    assert roster[0] == {"name": "Ελένη Κοντού", "role": ""}
    assert all(set(r) == {"name", "role"} for r in roster)


def test_build_glossary_real_settings():
    """The real settings carry board members + org names."""
    from src.config import settings as real_settings
    glossary = build_glossary(real_settings)
    assert "Διεθνής Αμνηστία" in glossary
    assert len(glossary) > 2


# ---------------------------------------------------------------------------
# get_transcriber
# ---------------------------------------------------------------------------

def test_get_transcriber_fake(tmp_path):
    settings = _fake_settings(tmp_path, transcriber="fake")
    assert isinstance(get_transcriber(settings), FakeTranscriber)


def test_get_transcriber_unknown_raises(tmp_path):
    settings = _fake_settings(tmp_path, transcriber="bogus")
    with pytest.raises(ValueError):
        get_transcriber(settings)


# ---------------------------------------------------------------------------
# FakeTranscriber
# ---------------------------------------------------------------------------

def test_fake_transcriber_with_sidecar(tmp_path):
    audio = tmp_path / "track.m4a"
    audio.write_bytes(b"\x00")
    (tmp_path / "track.m4a.txt").write_text("Καλημέρα σε όλους", encoding="utf-8")
    pieces = FakeTranscriber().transcribe(str(audio))
    assert pieces == [("Καλημέρα σε όλους", 0.0, 60.0)]


def test_fake_transcriber_without_sidecar(tmp_path):
    audio = tmp_path / "track.m4a"
    pieces = FakeTranscriber().transcribe(str(audio))
    assert len(pieces) == 1
    text, start, end = pieces[0]
    assert "fake transcript" in text
    assert "track.m4a" in text
    assert (start, end) == (0.0, 5.0)


# ---------------------------------------------------------------------------
# parse_transcript_file
# ---------------------------------------------------------------------------

def test_parse_zoom_copy(tmp_path):
    base = datetime(2026, 3, 1, 18, 0, 0, tzinfo=timezone.utc)
    content = (
        "Ελένη Κοντού\n"
        "00:00:05\n"
        "Καλησπέρα σε όλους, ξεκινάμε τη συνεδρίαση.\n"
        "\n"
        "Δημήτρης Μαρουλίδης\n"
        "00:01:10\n"
        "Συμφωνώ με την ημερήσια διάταξη.\n"
    )
    path = tmp_path / "transcript.txt"
    path.write_text(content, encoding="utf-8")

    segments = parse_transcript_file(path, base=base)
    assert len(segments) == 2
    assert segments[0].speaker == "Ελένη Κοντού"
    assert segments[0].start == datetime(2026, 3, 1, 18, 0, 5, tzinfo=timezone.utc)
    assert segments[1].speaker == "Δημήτρης Μαρουλίδης"
    assert segments[1].start == datetime(2026, 3, 1, 18, 1, 10, tzinfo=timezone.utc)
    assert "ημερήσια" in segments[1].text


def test_parse_vtt(tmp_path):
    base = datetime(2026, 3, 1, 18, 0, 0, tzinfo=timezone.utc)
    content = (
        "WEBVTT\n"
        "\n"
        "00:00:02.000 --> 00:00:06.500\n"
        "<v Ελένη Κοντού>Καλησπέρα σε όλους.\n"
        "\n"
        "00:00:07.000 --> 00:00:10.000\n"
        "Δημήτρης Μαρουλίδης: Συμφωνώ.\n"
    )
    path = tmp_path / "transcript.vtt"
    path.write_text(content, encoding="utf-8")

    segments = parse_transcript_file(path, base=base)
    assert len(segments) == 2
    assert segments[0].speaker == "Ελένη Κοντού"
    assert segments[0].start == datetime(2026, 3, 1, 18, 0, 2, tzinfo=timezone.utc)
    assert segments[1].speaker == "Δημήτρης Μαρουλίδης"
    assert "Συμφωνώ" in segments[1].text


# ---------------------------------------------------------------------------
# assemble_minutes
# ---------------------------------------------------------------------------

def _seed_events(meeting_ref, base):
    """Seed an agenda_advance + a vote and return the MeetingEventsStore."""
    from src.core.meeting_events import MeetingEventsStore

    store = MeetingEventsStore()
    store.record_event(
        meeting_ref=meeting_ref,
        event_type="agenda_advance",
        payload={"to_index": 1, "title": "Έγκριση προϋπολογισμού"},
        ts=base,
    )
    store.record_event(
        meeting_ref=meeting_ref,
        event_type="vote",
        payload={
            "label": "Προϋπολογισμός",
            "result": "passed",
            "tally": {"υπέρ": 5, "κατά": 0, "αποχή": 0},
            "method": "unanimous",
        },
        ts=base.replace(minute=2),
    )
    return store


def test_assemble_via_transcript(tmp_path, temp_db):
    with patch("src.core.audit._DB_PATH", temp_db), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()

        meeting_ref = "ΔΣ05-2026"
        base = datetime(2026, 3, 1, 18, 0, 0, tzinfo=timezone.utc)
        store = _seed_events(meeting_ref, base)

        content = (
            "Ελένη Κοντού\n"
            "00:00:30\n"
            "Συζητάμε τον προϋπολογισμό.\n"
            "\n"
            "Δημήτρης Μαρουλίδης\n"
            "00:01:30\n"
            "Συμφωνώ με την εισήγηση.\n"
        )
        transcript = tmp_path / "transcript.txt"
        transcript.write_text(content, encoding="utf-8")

        settings = _fake_settings(tmp_path)
        result = assemble_minutes(
            settings=settings,
            meeting_ref=meeting_ref,
            transcript_path=str(transcript),
            events_store=store,
        )

        assert result["source"] == "transcript"
        assert result["segment_count"] == 2
        skeleton = result["skeleton"]
        titles = [it["title"] for it in skeleton["items"]]
        assert "Έγκριση προϋπολογισμού" in titles
        # Segments landed inside the agenda item window.
        placed = sum(len(it["segments"]) for it in skeleton["items"])
        assert placed > 0
        # One vote attached.
        assert sum(len(it["votes"]) for it in skeleton["items"]) == 1
        # skeleton.json written.
        from pathlib import Path
        sk_path = Path(result["skeleton_path"])
        assert sk_path.exists()
        on_disk = json.loads(sk_path.read_text(encoding="utf-8"))
        assert on_disk["meeting_ref"] == meeting_ref


def test_assemble_via_manifest_with_fake_transcriber(tmp_path, temp_db):
    with patch("src.core.audit._DB_PATH", temp_db), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()

        meeting_ref = "ΔΣ06-2026"
        base = datetime(2026, 4, 1, 18, 0, 0, tzinfo=timezone.utc)
        store = _seed_events(meeting_ref, base)

        # Dummy audio file + sidecar transcript text.
        audio = tmp_path / "speaker1.m4a"
        audio.write_bytes(b"\x00")
        (tmp_path / "speaker1.m4a.txt").write_text(
            "Καλησπέρα, ξεκινάμε.", encoding="utf-8"
        )

        manifest = {
            "meeting_uuid": "abc==",
            "topic": "ΔΣ",
            "start_time": base.isoformat(),
            "dest_dir": str(tmp_path),
            "files": [
                {
                    "id": "f1",
                    "source": "participant_audio_files",
                    "file_type": "audio",
                    "recording_type": "audio_only",
                    "participant": "Ελένη Κοντού",
                    "recording_start": base.isoformat(),
                    "recording_end": base.replace(minute=5).isoformat(),
                    "file_extension": "m4a",
                    "file_size": 1,
                    "local_path": str(audio),
                }
            ],
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        settings = _fake_settings(tmp_path)
        result = assemble_minutes(
            settings=settings,
            meeting_ref=meeting_ref,
            manifest_path=str(manifest_path),
            events_store=store,
            transcriber=FakeTranscriber(),
        )

        assert result["source"] == "manifest"
        assert result["segment_count"] >= 1
        from pathlib import Path
        assert Path(result["skeleton_path"]).exists()


def test_assemble_via_manifest_timeline_attribution(tmp_path, temp_db):
    """A manifest with a mixed audio_only file + a timeline JSON attributes the
    transcript segment's speaker from the timeline (not the roster)."""
    with patch("src.core.audit._DB_PATH", temp_db), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()

        meeting_ref = "ΔΣ08-2026"
        base = datetime(2026, 4, 1, 18, 0, 0, tzinfo=timezone.utc)
        store = _seed_events(meeting_ref, base)

        # Mixed (whole-meeting) audio + sidecar text → FakeTranscriber returns
        # one piece at offset [0, 60).
        audio = tmp_path / "mixed.m4a"
        audio.write_bytes(b"\x00")
        (tmp_path / "mixed.m4a.txt").write_text(
            "Καλησπέρα, ξεκινάμε τη συνεδρίαση.", encoding="utf-8"
        )

        # Timeline JSON: at t=0..120 the active speaker is "Giorgos Athanasias".
        timeline = tmp_path / "timeline.json"
        timeline.write_text(
            json.dumps({
                "timeline": [
                    {"ts": "00:00:00.000",
                     "users": [{"username": "Giorgos Athanasias", "user_id": "1"}]},
                    {"ts": "00:02:00.000", "users": []},
                ]
            }),
            encoding="utf-8",
        )

        manifest = {
            "meeting_uuid": "abc==",
            "topic": "ΔΣ",
            "start_time": base.isoformat(),
            "dest_dir": str(tmp_path),
            "files": [
                {
                    "id": "mixed",
                    "source": "recording_files",
                    "file_type": "M4A",
                    "recording_type": "audio_only",
                    "participant": "",
                    "recording_start": base.isoformat(),
                    "recording_end": base.replace(minute=30).isoformat(),
                    "file_extension": "m4a",
                    "file_size": 999999,
                    "local_path": str(audio),
                },
                {
                    "id": "timeline",
                    "source": "recording_files",
                    "file_type": "TIMELINE",
                    "recording_type": "timeline",
                    "participant": "",
                    "recording_start": base.isoformat(),
                    "recording_end": base.replace(minute=30).isoformat(),
                    "file_extension": "json",
                    "file_size": 123,
                    "local_path": str(timeline),
                },
            ],
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        settings = _fake_settings(tmp_path)
        result = assemble_minutes(
            settings=settings,
            meeting_ref=meeting_ref,
            manifest_path=str(manifest_path),
            events_store=store,
            transcriber=FakeTranscriber(),
        )

        assert result["source"] == "manifest"
        assert result["segment_count"] >= 1
        # The one segment must be attributed to the timeline username verbatim.
        skeleton = result["skeleton"]
        speakers = set()
        for item in skeleton["items"]:
            for seg in item.get("segments", []):
                speakers.add(seg["speaker"])
        for seg in skeleton.get("unassigned_segments", []):
            speakers.add(seg["speaker"])
        assert "Giorgos Athanasias" in speakers


def test_assemble_requires_a_source(tmp_path, temp_db):
    with patch("src.core.audit._DB_PATH", temp_db), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        settings = _fake_settings(tmp_path)
        with pytest.raises(ValueError):
            assemble_minutes(
                settings=settings,
                meeting_ref="ΔΣ07-2026",
            )


# ── Per-agenda-item (chunked) drafting ────────────────────────────────────────


def test_draft_minutes_chunked_one_call_per_item(monkeypatch):
    """Chunked drafting makes one bounded LLM call per agenda item (+ opening),
    stitches a markdown doc, and renders decisions deterministically."""
    from src.workflows import minutes_pipeline as mp

    calls = []

    class FakeClient:
        def load_prompt(self, name):
            return "SYS"

        def generate(self, *, user_prompt, system_prompt, workflow, max_tokens):
            calls.append({"prompt": user_prompt, "max_tokens": max_tokens})
            return "Σωμα τμηματος."

    monkeypatch.setattr("src.core.claude.ClaudeClient", FakeClient)

    skeleton = {
        "meeting_ref": "DS05-2026",
        "presence": {"present": ["A"], "absent": ["B"]},
        "items": [
            {"index": 0, "title": "Item 1", "segments": [{"speaker": "A", "text": "x"}], "votes": []},
            {"index": 1, "title": "Item 2", "segments": [{"speaker": "B", "text": "y"}], "votes": []},
        ],
        "unassigned_segments": [{"speaker": "A", "text": "open"}],
        "decisions": [
            {"ref": "R1", "seq": 1, "decision_text": "Approve X",
             "outcome": "OK", "considerations": ["reason a"]},
        ],
    }

    out = mp.draft_minutes_chunked(skeleton, ["NEC"], settings=None)

    assert out is not None
    # 2 items + 1 opening bucket = 3 bounded calls
    assert len(calls) == 3
    assert all(c["max_tokens"] <= 4000 for c in calls)  # bounded, not single-shot
    assert len(out["sections"]) == 3
    assert all(s.get("body") for s in out["sections"])
    assert len(out["decisions"]) == 1
    md = out["markdown"]
    assert isinstance(md, str) and "Approve X" in md  # decision rendered verbatim


def test_draft_minutes_chunked_returns_none_without_llm(monkeypatch):
    """If the LLM client can't be constructed, drafting degrades to None."""
    from src.workflows import minutes_pipeline as mp

    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("no LLM")

    monkeypatch.setattr("src.core.claude.ClaudeClient", Boom)
    out = mp.draft_minutes_chunked({"items": []}, [], settings=None)
    assert out is None


def test_assign_decisions_to_items_by_timestamp():
    """Decisions with no agenda_index are placed into the item whose time window
    contains their timestamp; already-linked or out-of-range ones are untouched."""
    from src.workflows.minutes_pipeline import _assign_decisions_to_items

    items = [
        {"index": 0, "title": "A", "start": "2026-06-09T17:10:00Z", "end": "2026-06-09T17:40:00Z"},
        {"index": 1, "title": "B", "start": "2026-06-09T17:40:00Z", "end": "2026-06-09T18:30:00Z"},
    ]
    decisions = [
        {"ref": "R1", "ts": "2026-06-09T17:25:00Z"},                      # -> item 0
        {"ref": "R2", "ts": "2026-06-09T18:00:00Z"},                      # -> item 1
        {"ref": "R3", "ts": "2026-06-09T19:00:00Z"},                      # after end -> unplaced
        {"ref": "R4", "agenda_index": 0, "ts": "2026-06-09T18:00:00Z"},   # already linked -> keep
    ]
    _assign_decisions_to_items(decisions, items)

    assert decisions[0]["agenda_index"] == 0
    assert decisions[0]["agenda_assigned_by"] == "timestamp"
    assert decisions[1]["agenda_index"] == 1
    assert decisions[2].get("agenda_index") is None          # unresolved stays None
    assert decisions[3]["agenda_index"] == 0                  # not overwritten
    assert "agenda_assigned_by" not in decisions[3]
