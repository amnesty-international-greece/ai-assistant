"""Tests for the email→Discord board-reply bridge (Phase 4b).

Covers:
  1. match_board_meeting_anchor  — anchor lookup via References header
  2. match_board_meeting_anchor  — unknown anchor returns None
  3. Happy-path inbound reply    — publishes BoardEmailSentPayload(kind='board_reply')
  4. Loop prevention             — bot own echo is dropped, not mirrored
  5. Attribution                 — display name derived from From header
  6. Body truncation             — 3000-char body capped at 1800 chars
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def mock_db(tmp_path):
    """Isolated SQLite DB for each test."""
    db_path = tmp_path / "test_bridge.db"
    with patch("src.core.audit._DB_PATH", db_path), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        yield


def _seed_workflow_state(
    meeting_id: str,
    anchor: str,
    *,
    state: str = "completed",
) -> None:
    """Insert a fake board_meeting_invitation row into workflow_state."""
    from src.core.audit import _get_connection
    data = json.dumps({
        "context": {
            "meeting_id": meeting_id,
            "email_thread_anchor": anchor,
        }
    }, ensure_ascii=False)
    conn = _get_connection()
    conn.execute(
        """INSERT INTO workflow_state
               (workflow_name, workflow_id, state, data)
           VALUES (?, ?, ?, ?)""",
        ("board_meeting_invitation", meeting_id, state, data),
    )
    conn.commit()


def _make_message(
    *,
    sender: str = "member@example.com",
    sender_name: str = "Test Member",
    subject: str = "Re: Συνεδρίαση ΔΣ",
    imid: str = "<reply-1@x>",
    graph_id: str = "g-reply-1",
    internet_message_headers: list[dict] | None = None,
    body_content: str = "Hello board.",
    body_preview: str = "Hello board.",
    content_type: str = "text",
) -> dict[str, Any]:
    """Build a minimal Graph-style message envelope for tests."""
    return {
        "id": graph_id,
        "internetMessageId": imid,
        "subject": subject,
        "from": {"emailAddress": {"address": sender, "name": sender_name}},
        "hasAttachments": False,
        "bodyPreview": body_preview,
        "isRead": False,
        "body": {"contentType": content_type, "content": body_content},
        "internetMessageHeaders": internet_message_headers or [],
    }


# ── Test 1: anchor match finds meeting_id ────────────────────────────────────


def test_anchor_match_finds_meeting_id(mock_db):
    """A References header containing a known anchor resolves to the meeting_id."""
    anchor = "<board_meeting:ΔΣ05-2026@amnesty.org.gr>"
    meeting_id = "board_meeting:ΔΣ05-2026"
    _seed_workflow_state(meeting_id, anchor)

    from src.workflows.email_intake import match_board_meeting_anchor

    headers = {
        "references": f"<some-earlier@x> {anchor} <even-earlier@x>",
        "in-reply-to": "",
    }
    result = match_board_meeting_anchor(headers)
    assert result == meeting_id


# ── Test 2: anchor match returns None for unknown anchor ─────────────────────


def test_anchor_match_returns_none_when_no_workflow(mock_db):
    """An unrecognised anchor (no matching workflow_state row) returns None."""
    from src.workflows.email_intake import match_board_meeting_anchor

    headers = {
        "references": "<totally-unknown@example.com>",
        "in-reply-to": "<also-unknown@example.com>",
    }
    result = match_board_meeting_anchor(headers)
    assert result is None


# ── Test 3: happy path publishes BoardEmailSentPayload(kind='board_reply') ───


@pytest.mark.asyncio
async def test_inbound_reply_publishes_board_email_sent_event(mock_db):
    """Incoming email matching a board anchor publishes kind='board_reply'."""
    anchor = "<board_meeting:ΔΣ05-2026@amnesty.org.gr>"
    meeting_id = "board_meeting:ΔΣ05-2026"
    _seed_workflow_state(meeting_id, anchor)

    msg = _make_message(
        sender="member@example.com",
        sender_name="Μέλος ΔΣ",
        subject="Re: Συνεδρίαση ΔΣ05-2026",
        imid="<reply-happy@x>",
        internet_message_headers=[
            {"name": "In-Reply-To", "value": anchor},
            {"name": "References",  "value": anchor},
        ],
    )

    published_payloads: list = []

    from src.core.event_bus import bus as real_bus
    from src.core.events import EVENT_BOARD_EMAIL_SENT

    async def _capture(payload):
        published_payloads.append(payload)

    real_bus.subscribe(EVENT_BOARD_EMAIL_SENT, _capture)
    try:
        from src.workflows.email_intake import process_inbox_message
        result = await process_inbox_message(msg, source="webhook")
    finally:
        real_bus.unsubscribe(EVENT_BOARD_EMAIL_SENT, _capture)

    assert result["outcome"] == "board_reply_mirrored"
    assert result["meeting_id"] == meeting_id
    assert len(published_payloads) == 1
    p = published_payloads[0]
    assert p.kind == "board_reply"
    assert p.meeting_id == meeting_id
    assert p.meeting_ref == "ΔΣ05-2026"
    assert p.test_mode is False


# ── Test 4: loop prevention skips bot's own Discord-bridge echo ──────────────


@pytest.mark.asyncio
async def test_loop_prevention_skips_bot_own_emails_with_discord_marker(mock_db):
    """Email from members@amnesty.org.gr with Discord marker is dropped, not mirrored."""
    anchor = "<board_meeting:ΔΣ05-2026@amnesty.org.gr>"
    meeting_id = "board_meeting:ΔΣ05-2026"
    _seed_workflow_state(meeting_id, anchor)

    msg = _make_message(
        sender="members@amnesty.org.gr",
        sender_name="AI Assistant",
        subject="Re: Συνεδρίαση ΔΣ05-2026",
        imid="<loop-echo@x>",
        internet_message_headers=[
            {"name": "In-Reply-To", "value": anchor},
        ],
        body_content="[Γιώργος via Discord]\n\nHello from Discord.",
        body_preview="[Γιώργος via Discord]",
    )

    published_payloads: list = []
    from src.core.event_bus import bus as real_bus
    from src.core.events import EVENT_BOARD_EMAIL_SENT

    async def _capture(payload):
        published_payloads.append(payload)

    real_bus.subscribe(EVENT_BOARD_EMAIL_SENT, _capture)
    try:
        from src.workflows.email_intake import process_inbox_message
        result = await process_inbox_message(msg, source="webhook")
    finally:
        real_bus.unsubscribe(EVENT_BOARD_EMAIL_SENT, _capture)

    assert result["outcome"] == "loop_skipped"
    assert len(published_payloads) == 0


# ── Test 5: attribution uses display name from From header ────────────────────


@pytest.mark.asyncio
async def test_attribution_uses_display_name_from_header(mock_db):
    """From: display name is used in the Discord post header line."""
    anchor = "<board_meeting:ΔΣ05-2026@amnesty.org.gr>"
    meeting_id = "board_meeting:ΔΣ05-2026"
    _seed_workflow_state(meeting_id, anchor)

    msg = _make_message(
        sender="ga@example.com",
        sender_name="Γιώργος Αθανασιάς",
        subject="Σχόλιο",
        imid="<attr-test@x>",
        internet_message_headers=[
            {"name": "In-Reply-To", "value": anchor},
        ],
    )

    published_payloads: list = []
    from src.core.event_bus import bus as real_bus
    from src.core.events import EVENT_BOARD_EMAIL_SENT

    async def _capture(payload):
        published_payloads.append(payload)

    real_bus.subscribe(EVENT_BOARD_EMAIL_SENT, _capture)
    try:
        from src.workflows.email_intake import process_inbox_message
        await process_inbox_message(msg, source="webhook")
    finally:
        real_bus.unsubscribe(EVENT_BOARD_EMAIL_SENT, _capture)

    assert len(published_payloads) == 1
    content = published_payloads[0].body_html  # pre-rendered Discord text
    assert "💬 **Γιώργος Αθανασιάς** (ga@example.com)" in content


# ── Test 6: long bodies are truncated at 1800 chars ──────────────────────────


@pytest.mark.asyncio
async def test_truncates_long_bodies_at_1800_chars(mock_db):
    """A 3000-char body is truncated to 1800 chars + '...' suffix."""
    anchor = "<board_meeting:ΔΣ05-2026@amnesty.org.gr>"
    meeting_id = "board_meeting:ΔΣ05-2026"
    _seed_workflow_state(meeting_id, anchor)

    long_body = "A" * 3000
    msg = _make_message(
        sender="member@example.com",
        sender_name="Test",
        subject="Long email",
        imid="<long-body@x>",
        internet_message_headers=[
            {"name": "In-Reply-To", "value": anchor},
        ],
        body_content=long_body,
        body_preview=long_body[:255],
    )

    published_payloads: list = []
    from src.core.event_bus import bus as real_bus
    from src.core.events import EVENT_BOARD_EMAIL_SENT

    async def _capture(payload):
        published_payloads.append(payload)

    real_bus.subscribe(EVENT_BOARD_EMAIL_SENT, _capture)
    try:
        from src.workflows.email_intake import process_inbox_message
        await process_inbox_message(msg, source="webhook")
    finally:
        real_bus.unsubscribe(EVENT_BOARD_EMAIL_SENT, _capture)

    assert len(published_payloads) == 1
    content = published_payloads[0].body_html
    # The body portion should end with "..."
    assert content.endswith("...")
    # And the body portion itself is at most 1800 chars
    # (extract text after the two blank-line-separated header lines)
    body_part = content.split("\n\n", 2)[-1]  # skip header + subject
    assert len(body_part) <= 1803  # 1800 + "..."
