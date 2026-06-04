"""Tests for the audit logging system."""

import json
import pytest
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch


@pytest.fixture
def temp_db(tmp_path):
    """Create a temporary database for testing."""
    db_path = tmp_path / "test.db"
    with patch("src.core.audit._DB_PATH", db_path), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db, _get_connection
        init_db()
        yield db_path


def test_init_db(temp_db):
    """Database initialization should create required tables."""
    conn = sqlite3.connect(str(temp_db))
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    assert "audit_log" in tables
    assert "workflow_state" in tables
    assert "oauth_tokens" in tables


def test_log_action(temp_db):
    """log_action should insert a record and return its ID."""
    with patch("src.core.audit._DB_PATH", temp_db), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db, log_action, _get_connection
        init_db()
        entry_id = log_action(
            workflow="test_workflow",
            action="test_action",
            actor="test_actor",
            target="test_target",
            details={"key": "value"},
        )
        assert entry_id is not None
        assert entry_id > 0

        # Verify the record
        conn = _get_connection()
        row = conn.execute(
            "SELECT * FROM audit_log WHERE id = ?", (entry_id,)
        ).fetchone()
        assert row is not None
        assert row["workflow"] == "test_workflow"
        assert row["action"] == "test_action"
        assert row["actor"] == "test_actor"
        assert row["target"] == "test_target"
        assert row["status"] == "success"
        details = json.loads(row["details"])
        assert details["key"] == "value"


def test_get_audit_log(temp_db):
    """get_audit_log should return recent entries."""
    with patch("src.core.audit._DB_PATH", temp_db), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db, log_action, get_audit_log
        init_db()

        # Insert several entries
        for i in range(5):
            log_action(
                workflow="test_workflow",
                action=f"action_{i}",
                actor="tester",
            )

        entries = get_audit_log(workflow="test_workflow")
        assert len(entries) == 5


def test_get_audit_log_filter(temp_db):
    """get_audit_log should filter by workflow."""
    with patch("src.core.audit._DB_PATH", temp_db), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db, log_action, get_audit_log
        init_db()

        log_action(workflow="workflow_a", action="a", actor="test")
        log_action(workflow="workflow_b", action="b", actor="test")
        log_action(workflow="workflow_a", action="c", actor="test")

        entries_a = get_audit_log(workflow="workflow_a")
        entries_b = get_audit_log(workflow="workflow_b")
        assert len(entries_a) == 2
        assert len(entries_b) == 1


def test_save_and_get_workflow_state(temp_db):
    """Workflow state should be saved and retrievable."""
    with patch("src.core.audit._DB_PATH", temp_db), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db, save_workflow_state, get_workflow_state
        init_db()

        save_workflow_state(
            workflow_name="test_wf",
            workflow_id="wf-123",
            state="in_progress",
            data={"step": 2},
        )

        state = get_workflow_state("wf-123")
        assert state is not None
        assert state["state"] == "in_progress"
        assert state["workflow_name"] == "test_wf"

        # Update state
        save_workflow_state(
            workflow_name="test_wf",
            workflow_id="wf-123",
            state="completed",
            data={"step": 5},
        )

        state = get_workflow_state("wf-123")
        assert state["state"] == "completed"
