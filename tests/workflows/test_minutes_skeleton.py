"""Tests for the pure minutes skeleton-builder.

These lock the deterministic contract of ``build_minutes_skeleton`` (see the
module docstring in ``src/workflows/minutes_skeleton.py``): agenda time-windowing,
off-topic flagging (never dropping), break handling, presence resolution, and
vote attachment. All fixtures are hand-built; the function is pure so no DB,
network, or model is involved.

Assertions check structure/shape and a few Greek values round-tripping, kept
ASCII-safe where practical per project convention.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.workflows.minutes_skeleton import TranscriptSegment, build_minutes_skeleton

# Fixed meeting clock: 2026-05-15 18:00 UTC + N minutes.
_BASE = datetime(2026, 5, 15, 18, 0, 0, tzinfo=timezone.utc)


def _ts(minutes: float) -> str:
    """ISO-8601 string at BASE + minutes (for event `ts` fields)."""
    return (_BASE + timedelta(minutes=minutes)).isoformat()


def _dt(minutes: float) -> datetime:
    """Aware datetime at BASE + minutes (for TranscriptSegment start/end)."""
    return _BASE + timedelta(minutes=minutes)


def _seg(speaker: str, text: str, start_min: float, end_min: float) -> TranscriptSegment:
    return TranscriptSegment(speaker=speaker, text=text, start=_dt(start_min), end=_dt(end_min))


def _advance(to_index: int, title: str, at_min: float) -> dict:
    return {
        "event_type": "agenda_advance",
        "ts": _ts(at_min),
        "payload": {"to_index": to_index, "title": title},
        "confidence": "confirmed",
    }


_ROSTER = [
    {"name": "Ελένη Κοντού", "role": "Πρόεδρος"},
    {"name": "Γρηγόρης Μουζακίτης", "role": "Ταμίας"},
    {"name": "Σπύρος Απέργης", "role": "Μέλος"},
]
_AGENDA = ["Έγκριση ημερήσιας διάταξης", "Οικονομικά", "Λοιπά θέματα"]


def test_segments_land_in_correct_item_by_time_window():
    events = [_advance(1, _AGENDA[0], 0), _advance(2, _AGENDA[1], 10), _advance(3, _AGENDA[2], 20)]
    segments = [
        _seg("Ελένη Κοντού", "Άνοιγμα.", 2, 3),          # item 1
        _seg("Γρηγόρης Μουζακίτης", "Ο προϋπολογισμός.", 12, 13),  # item 2
        _seg("Σπύρος Απέργης", "Τελευταίο σχόλιο.", 22, 23),       # item 3
    ]
    sk = build_minutes_skeleton(
        meeting_ref="ΔΣ05-2026", agenda_items=_AGENDA,
        events=events, segments=segments, roster=_ROSTER,
    )
    items = sk["items"]
    assert [len(it["segments"]) for it in items] == [1, 1, 1]
    assert items[1]["segments"][0]["speaker"] == "Γρηγόρης Μουζακίτης"
    assert sk["unassigned_segments"] == []


def test_segment_before_first_advance_is_unassigned():
    events = [_advance(1, _AGENDA[0], 10)]
    segments = [_seg("Ελένη Κοντού", "Προ-συνεδρίαση.", 2, 3)]
    sk = build_minutes_skeleton(
        meeting_ref="ΔΣ05-2026", agenda_items=_AGENDA,
        events=events, segments=segments, roster=_ROSTER,
    )
    assert len(sk["unassigned_segments"]) == 1
    assert sum(len(it["segments"]) for it in sk["items"]) == 0


def test_agenda_item_without_advance_still_appears_empty():
    events = [_advance(1, _AGENDA[0], 0)]  # only item 1 advanced
    sk = build_minutes_skeleton(
        meeting_ref="ΔΣ05-2026", agenda_items=_AGENDA,
        events=events, segments=[], roster=_ROSTER,
    )
    assert [it["title"] for it in sk["items"]] == _AGENDA  # all present, in order
    assert sk["items"][2]["start"] is None
    assert sk["items"][2]["end"] is None
    assert sk["items"][2]["segments"] == []


def test_offtopic_span_flags_but_does_not_drop():
    events = [
        _advance(1, _AGENDA[0], 0),
        {"event_type": "off_topic", "ts": _ts(5), "payload": {"state": "begin"}, "confidence": "confirmed"},
        {"event_type": "off_topic", "ts": _ts(8), "payload": {"state": "end"}, "confidence": "confirmed"},
    ]
    segments = [
        _seg("Σπύρος Απέργης", "Εντός θέματος.", 2, 3),
        _seg("Σπύρος Απέργης", "Εκτός θέματος κουβέντα.", 6, 7),  # inside off-topic span
    ]
    sk = build_minutes_skeleton(
        meeting_ref="ΔΣ05-2026", agenda_items=_AGENDA,
        events=events, segments=segments, roster=_ROSTER,
    )
    item_segs = sk["items"][0]["segments"]
    assert len(item_segs) == 2  # nothing dropped
    flags = {s["text"]: s["off_topic"] for s in item_segs}
    assert flags["Εντός θέματος."] is False
    assert flags["Εκτός θέματος κουβέντα."] is True


def test_break_window_sends_segments_to_unassigned():
    events = [
        _advance(1, _AGENDA[0], 0),
        {"event_type": "phase", "ts": _ts(5), "payload": {"phase": "break"}, "confidence": "confirmed"},
        {"event_type": "phase", "ts": _ts(10), "payload": {"phase": "resume"}, "confidence": "confirmed"},
    ]
    segments = [
        _seg("Ελένη Κοντού", "Πριν το διάλειμμα.", 2, 3),   # item 1
        _seg("Ελένη Κοντού", "Κατά το διάλειμμα.", 6, 7),    # in break -> unassigned
        _seg("Ελένη Κοντού", "Μετά το διάλειμμα.", 11, 12),  # item 1 again
    ]
    sk = build_minutes_skeleton(
        meeting_ref="ΔΣ05-2026", agenda_items=_AGENDA,
        events=events, segments=segments, roster=_ROSTER,
    )
    assert len(sk["unassigned_segments"]) == 1
    assert sk["unassigned_segments"][0]["text"] == "Κατά το διάλειμμα."
    assert len(sk["items"][0]["segments"]) == 2


def test_presence_speaker_present_explicit_absent_and_nonroster():
    events = [
        _advance(1, _AGENDA[0], 0),
        # Explicit absent for the Treasurer (overrides any speaking inference).
        {"event_type": "presence", "ts": _ts(1), "payload": {"member": "Γρηγόρης Μουζακίτης", "status": "absent"}, "confidence": "confirmed"},
    ]
    segments = [
        _seg("Ελένη Κοντού", "Παρούσα και ομιλεί.", 2, 3),         # roster, spoke -> present
        _seg("Τοπική Ομάδα Θεσσαλονίκης", "Γεια σας.", 4, 5),       # not in roster -> present, role ""
    ]
    sk = build_minutes_skeleton(
        meeting_ref="ΔΣ05-2026", agenda_items=_AGENDA,
        events=events, segments=segments, roster=_ROSTER,
    )
    present_names = {p["name"] for p in sk["presence"]["present"]}
    absent_names = {a["name"] for a in sk["presence"]["absent"]}
    assert "Ελένη Κοντού" in present_names          # spoke -> present
    assert "Γρηγόρης Μουζακίτης" in absent_names     # explicit absent wins
    assert "Σπύρος Απέργης" in absent_names          # never seen -> absent
    # Non-roster speaker included in present with empty role.
    extra = [p for p in sk["presence"]["present"] if p["name"] == "Τοπική Ομάδα Θεσσαλονίκης"]
    assert extra and extra[0]["role"] == ""


def test_vote_attaches_to_correct_item_with_tally():
    events = [
        _advance(1, _AGENDA[0], 0),
        _advance(2, _AGENDA[1], 10),
        {
            "event_type": "vote",
            "ts": _ts(12),  # inside item 2's window
            "payload": {
                "label": "Έγκριση προϋπολογισμού 2026",
                "result": "passed",
                "tally": {"υπέρ": 4, "κατά": 1, "αποχή": 0},
                "method": "majority",
            },
            "confidence": "confirmed",
        },
    ]
    sk = build_minutes_skeleton(
        meeting_ref="ΔΣ05-2026", agenda_items=_AGENDA,
        events=events, segments=[], roster=_ROSTER,
    )
    assert sk["items"][0]["votes"] == []
    votes = sk["items"][1]["votes"]
    assert len(votes) == 1
    assert votes[0]["label"] == "Έγκριση προϋπολογισμού 2026"
    assert votes[0]["tally"] == {"υπέρ": 4, "κατά": 1, "αποχή": 0}
    assert votes[0]["ts"] == _ts(12)


def test_empty_inputs_return_valid_skeleton():
    sk = build_minutes_skeleton(
        meeting_ref="ΔΣ05-2026", agenda_items=_AGENDA,
        events=[], segments=[], roster=_ROSTER,
    )
    assert sk["meeting_ref"] == "ΔΣ05-2026"
    assert [it["title"] for it in sk["items"]] == _AGENDA
    assert all(it["segments"] == [] and it["votes"] == [] for it in sk["items"])
    assert sk["unassigned_segments"] == []
    assert sk["phases"] == []
    # No events, nobody spoke -> everyone on the roster is absent.
    assert {a["name"] for a in sk["presence"]["absent"]} == {e["name"] for e in _ROSTER}
    assert sk["presence"]["present"] == []
