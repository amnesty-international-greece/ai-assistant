"""Tests for the board-meeting events store (SQLite-backed)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import patch


@pytest.fixture
def temp_db(tmp_path):
    """Fresh DB per test, with module-level connection cache reset."""
    db_path = tmp_path / "test.db"
    with patch("src.core.audit._DB_PATH", db_path), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        yield db_path


def test_table_created(temp_db):
    """init_db creates the meeting_events table + index."""
    import sqlite3
    conn = sqlite3.connect(str(temp_db))
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    assert "meeting_events" in tables
    indexes = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
    )]
    assert "idx_meeting_events_ref" in indexes
    conn.close()


def test_record_and_list_round_trip_greek(temp_db):
    """Greek payload values survive the round-trip and payload is a dict."""
    with patch("src.core.audit._DB_PATH", temp_db), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        from src.core.meeting_events import MeetingEventsStore
        init_db()
        store = MeetingEventsStore()

        vote_id = store.record_event(
            meeting_ref="ΔΣ05-2026",
            event_type="vote",
            payload={
                "label": "Έγκριση προϋπολογισμού",
                "result": "passed",
                "tally": {"υπέρ": 5, "κατά": 1, "αποχή": 1},
                "method": "majority",
            },
        )
        presence_id = store.record_event(
            meeting_ref="ΔΣ05-2026",
            event_type="presence",
            payload={"member": "Γιώργος Αθανασιάς", "status": "present"},
        )
        assert vote_id > 0
        assert presence_id > 0

        events = store.list_events("ΔΣ05-2026")
        assert len(events) == 2

        by_type = {e["event_type"]: e for e in events}
        vote = by_type["vote"]
        # payload is a dict, not a JSON string
        assert isinstance(vote["payload"], dict)
        assert vote["payload"]["label"] == "Έγκριση προϋπολογισμού"
        assert vote["payload"]["tally"]["υπέρ"] == 5
        assert vote["payload"]["result"] == "passed"

        presence = by_type["presence"]
        assert presence["payload"]["member"] == "Γιώργος Αθανασιάς"
        assert presence["confidence"] == "confirmed"
        # expected dict keys present
        for key in ("id", "meeting_ref", "event_type", "ts", "payload",
                    "confidence", "created_at"):
            assert key in vote


def test_invalid_event_type_raises(temp_db):
    """A bad event_type raises ValueError and does not insert."""
    with patch("src.core.audit._DB_PATH", temp_db), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        from src.core.meeting_events import MeetingEventsStore
        init_db()
        store = MeetingEventsStore()
        with pytest.raises(ValueError):
            store.record_event(
                meeting_ref="ΔΣ05-2026",
                event_type="bogus_type",
                payload={"text": "nope"},
            )
        assert store.list_events("ΔΣ05-2026") == []


def test_list_ordered_by_ts_and_type_filter(temp_db):
    """list_events orders by ts ASC and honours the event_type filter."""
    with patch("src.core.audit._DB_PATH", temp_db), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        from src.core.meeting_events import MeetingEventsStore
        init_db()
        store = MeetingEventsStore()

        base = datetime(2026, 5, 31, 18, 0, tzinfo=timezone.utc)
        # Insert out of chronological order to prove sorting is by ts, not id.
        store.record_event(
            meeting_ref="ΔΣ05-2026", event_type="note",
            payload={"text": "third"}, ts=base + timedelta(minutes=30),
        )
        store.record_event(
            meeting_ref="ΔΣ05-2026", event_type="phase",
            payload={"phase": "start"}, ts=base,
        )
        store.record_event(
            meeting_ref="ΔΣ05-2026", event_type="note",
            payload={"text": "second"}, ts=base + timedelta(minutes=15),
        )

        all_events = store.list_events("ΔΣ05-2026")
        ts_order = [e["ts"] for e in all_events]
        assert ts_order == sorted(ts_order)
        assert all_events[0]["payload"].get("phase") == "start"

        notes = store.list_events("ΔΣ05-2026", event_type="note")
        assert len(notes) == 2
        assert [n["payload"]["text"] for n in notes] == ["second", "third"]


def test_delete_event_returns_true_then_false(temp_db):
    """delete_event returns True when a row is removed, False otherwise."""
    with patch("src.core.audit._DB_PATH", temp_db), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        from src.core.meeting_events import MeetingEventsStore
        init_db()
        store = MeetingEventsStore()

        event_id = store.record_event(
            meeting_ref="ΔΣ05-2026", event_type="off_topic",
            payload={"state": "begin"},
        )
        assert store.delete_event(event_id) is True
        assert store.list_events("ΔΣ05-2026") == []
        # second delete of the same id finds nothing
        assert store.delete_event(event_id) is False
        assert store.delete_event(999999) is False
