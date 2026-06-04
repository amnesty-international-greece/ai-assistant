"""Tests for protocol-number reservations (SQLite-backed)."""
from __future__ import annotations

import pytest
from unittest.mock import patch


@pytest.fixture
def temp_db(tmp_path):
    """Fresh DB per test, with module-level cache reset."""
    db_path = tmp_path / "test.db"
    with patch("src.core.audit._DB_PATH", db_path), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        yield db_path


def test_table_created(temp_db):
    """The migration creates the protocol_reservations table + index."""
    import sqlite3
    conn = sqlite3.connect(str(temp_db))
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    assert "protocol_reservations" in tables
    indexes = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
    )]
    assert "idx_proto_workflow" in indexes
    conn.close()


def test_reserve_starts_at_001_when_empty(temp_db):
    with patch("src.core.audit._DB_PATH", temp_db), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db, reserve_next_protocol_number
        init_db()
        proto = reserve_next_protocol_number(2026, "wf-a")
        assert proto == "2026_001"


def test_reserve_continues_after_xlsx_max(temp_db):
    """xlsx_max_seq pushes the starting point forward."""
    with patch("src.core.audit._DB_PATH", temp_db), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db, reserve_next_protocol_number
        init_db()
        proto = reserve_next_protocol_number(2026, "wf-a", xlsx_max_seq=25)
        assert proto == "2026_026"


def test_concurrent_reservations_get_different_numbers(temp_db):
    """Two reservations in the same year never produce the same seq."""
    with patch("src.core.audit._DB_PATH", temp_db), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db, reserve_next_protocol_number
        init_db()
        a = reserve_next_protocol_number(2026, "wf-a")
        b = reserve_next_protocol_number(2026, "wf-b")
        c = reserve_next_protocol_number(2026, "wf-c")
        assert a == "2026_001"
        assert b == "2026_002"
        assert c == "2026_003"


def test_commit_flips_state(temp_db):
    with patch("src.core.audit._DB_PATH", temp_db), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import (
            init_db, reserve_next_protocol_number,
            commit_protocol_reservation, get_reservations_for_year,
        )
        init_db()
        reserve_next_protocol_number(2026, "wf-a")
        reserve_next_protocol_number(2026, "wf-a")
        n = commit_protocol_reservation("wf-a")
        assert n == 2
        rows = get_reservations_for_year(2026)
        assert all(r["committed"] == 1 for r in rows)


def test_release_deletes_only_uncommitted(temp_db):
    with patch("src.core.audit._DB_PATH", temp_db), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import (
            init_db, reserve_next_protocol_number,
            commit_protocol_reservation, release_protocol_reservation,
            get_reservations_for_year,
        )
        init_db()
        # wf-a reserves 1 + commits
        reserve_next_protocol_number(2026, "wf-a")
        commit_protocol_reservation("wf-a")
        # wf-a then reserves 2 more without committing
        reserve_next_protocol_number(2026, "wf-a")
        reserve_next_protocol_number(2026, "wf-a")

        released = release_protocol_reservation("wf-a")
        assert released == 2  # only the uncommitted ones

        rows = get_reservations_for_year(2026)
        assert len(rows) == 1
        assert rows[0]["seq"] == 1
        assert rows[0]["committed"] == 1


def test_year_rollover(temp_db):
    """A fresh year starts at 001 regardless of prior years."""
    with patch("src.core.audit._DB_PATH", temp_db), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db, reserve_next_protocol_number
        init_db()
        reserve_next_protocol_number(2025, "wf-a")
        reserve_next_protocol_number(2025, "wf-a")
        reserve_next_protocol_number(2025, "wf-a")
        first_2026 = reserve_next_protocol_number(2026, "wf-b")
        assert first_2026 == "2026_001"


def test_release_for_unknown_workflow_is_noop(temp_db):
    with patch("src.core.audit._DB_PATH", temp_db), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db, release_protocol_reservation
        init_db()
        assert release_protocol_reservation("nobody") == 0
