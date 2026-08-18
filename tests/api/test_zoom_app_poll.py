"""The poll endpoint must target the meeting the sidebar is actually in."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.zoom_app import router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_poll_uses_live_meeting_id_from_sidebar(client):
    """Regression: creating the poll on the last SCHEDULED meeting put it on a
    past meeting where nobody in the call could see it. The live id wins."""
    created = AsyncMock(return_value={"id": "poll-1"})
    with patch("src.integrations.zoom.ZoomClient.create_poll", created), \
         patch("src.api.zoom_app._lookup_zoom_meeting_id", return_value="99999999"):
        res = client.post("/zoom-app/poll", json={
            "meeting_ref": "DS06-2026", "question": "Approve?",
            "meeting_id": "12345678",          # the meeting we are really in
        })
    assert res.status_code == 200 and res.json()["ok"] is True
    assert created.await_args.args[0] == "12345678"   # not the stored 99999999


def test_poll_falls_back_to_stored_meeting_id(client):
    """Older sidebars send no meeting_id; the stored one is still better than none."""
    created = AsyncMock(return_value={"id": "poll-2"})
    with patch("src.integrations.zoom.ZoomClient.create_poll", created), \
         patch("src.api.zoom_app._lookup_zoom_meeting_id", return_value="99999999"):
        res = client.post("/zoom-app/poll", json={
            "meeting_ref": "DS06-2026", "question": "Approve?",
        })
    assert res.status_code == 200
    assert created.await_args.args[0] == "99999999"


def test_poll_requires_question(client):
    res = client.post("/zoom-app/poll", json={"meeting_ref": "DS06-2026", "question": "  "})
    assert res.status_code == 400


def test_poll_reports_zoom_failure_clearly(client):
    """A disabled-polls account must produce an actionable message, not silence."""
    with patch("src.integrations.zoom.ZoomClient.create_poll", AsyncMock(return_value=None)), \
         patch("src.api.zoom_app._lookup_zoom_meeting_id", return_value="123"):
        res = client.post("/zoom-app/poll", json={
            "meeting_ref": "DS06-2026", "question": "Approve?", "meeting_id": "123",
        })
    assert res.status_code == 502
    assert "Meeting Polls" in res.json()["error"]
