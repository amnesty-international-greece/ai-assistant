"""Tests for the Zoom recording webhook (CRC handshake + signature + dispatch)."""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.webhooks import router


@pytest.fixture
def mock_db(tmp_path):
    """Isolated SQLite so log_action calls don't touch the real audit DB."""
    with patch("src.core.audit._DB_PATH", tmp_path / "test.db"), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        yield


@pytest.fixture
def client(mock_db):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _sign(secret: str, timestamp: str, raw_body: bytes) -> str:
    message = f"v0:{timestamp}:{raw_body.decode('utf-8')}"
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"v0={digest}"


# ── CRC handshake ────────────────────────────────────────────────────────────


def test_crc_handshake_returns_correct_encrypted_token(client):
    secret = "my-zoom-secret"
    plain_token = "abc123"
    with patch("src.api.webhooks.settings.zoom_webhook_secret_token", secret):
        resp = client.post(
            "/webhooks/zoom/recording",
            json={"event": "endpoint.url_validation",
                  "payload": {"plainToken": plain_token}},
        )

    assert resp.status_code == 200
    data = resp.json()
    expected = hmac.new(secret.encode(), plain_token.encode(), hashlib.sha256).hexdigest()
    assert data["plainToken"] == plain_token
    assert data["encryptedToken"] == expected


# ── Signature rejection ──────────────────────────────────────────────────────


def test_bad_signature_returns_401_and_no_background_task(client):
    secret = "my-zoom-secret"
    body = {"event": "recording.completed",
            "payload": {"object": {"uuid": "abc/def=="}}}
    with patch("src.api.webhooks.settings.zoom_webhook_secret_token", secret), \
         patch("src.api.webhooks._process_zoom_recording") as proc:
        resp = client.post(
            "/webhooks/zoom/recording",
            json=body,
            headers={"x-zm-signature": "v0=deadbeef",
                     "x-zm-request-timestamp": "1700000000"},
        )

    assert resp.status_code == 401
    proc.assert_not_called()


# ── Happy path ───────────────────────────────────────────────────────────────


def test_recording_completed_valid_signature_schedules_download(client):
    secret = "my-zoom-secret"
    timestamp = "1700000000"
    meeting_uuid = "abc/def=="
    body = {"event": "recording.completed",
            "payload": {"object": {"uuid": meeting_uuid}}}
    raw = json.dumps(body).encode("utf-8")
    signature = _sign(secret, timestamp, raw)

    with patch("src.api.webhooks.settings.zoom_webhook_secret_token", secret), \
         patch("src.api.webhooks._process_zoom_recording") as proc:
        resp = client.post(
            "/webhooks/zoom/recording",
            content=raw,
            headers={"content-type": "application/json",
                     "x-zm-signature": signature,
                     "x-zm-request-timestamp": timestamp},
        )

    assert resp.status_code == 202
    proc.assert_called_once_with(meeting_uuid)


def test_recording_completed_falls_back_to_id_when_no_uuid(client):
    secret = "my-zoom-secret"
    timestamp = "1700000000"
    body = {"event": "recording.completed",
            "payload": {"object": {"id": 99887766}}}
    raw = json.dumps(body).encode("utf-8")
    signature = _sign(secret, timestamp, raw)

    with patch("src.api.webhooks.settings.zoom_webhook_secret_token", secret), \
         patch("src.api.webhooks._process_zoom_recording") as proc:
        resp = client.post(
            "/webhooks/zoom/recording",
            content=raw,
            headers={"content-type": "application/json",
                     "x-zm-signature": signature,
                     "x-zm-request-timestamp": timestamp},
        )

    assert resp.status_code == 202
    proc.assert_called_once_with("99887766")


# ── Unconfigured token (dev mode) ────────────────────────────────────────────


def test_empty_secret_accepts_without_signature(client):
    body = {"event": "recording.completed",
            "payload": {"object": {"uuid": "plain-uuid"}}}
    with patch("src.api.webhooks.settings.zoom_webhook_secret_token", ""), \
         patch("src.api.webhooks._process_zoom_recording") as proc:
        # No signature headers at all - dev mode should still accept.
        resp = client.post("/webhooks/zoom/recording", json=body)

    assert resp.status_code == 202
    proc.assert_called_once_with("plain-uuid")


# ── Other events / bad JSON ──────────────────────────────────────────────────


def test_other_event_is_acknowledged_202(client):
    with patch("src.api.webhooks.settings.zoom_webhook_secret_token", ""):
        resp = client.post(
            "/webhooks/zoom/recording",
            json={"event": "meeting.started", "payload": {"object": {}}},
        )
    assert resp.status_code == 202


def test_bad_json_returns_202(client):
    resp = client.post(
        "/webhooks/zoom/recording",
        content=b"not-json{{",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 202
