"""Tests for the Director's briefing auto-archive feature.

Covers:
  - Filename classification (τόνος + case insensitive; Εισηγητικό wins ties)
  - Briefing attachment finder (Εισηγητικά prioritised; images skipped)
  - Pre-filled metadata builders (title, labels per kind, Κύρια Σημεία)
  - SQLite ``director_briefings`` table + helpers
  - End-to-end intake glue: ``process_director_briefing_email`` invokes
    ``ArchiveWorkflow`` with the right initial data and publishes the
    ``EVENT_DIRECTOR_BRIEFING_ARCHIVED`` event for the Discord milestone.

ArchiveWorkflow and M365InboxClient are mocked - these tests focus on the
glue, not the downstream archive plumbing (which has its own suite).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Fixture: in-memory DB ────────────────────────────────────────────────────


@pytest.fixture
def mock_db(tmp_path):
    """Spin up a fresh SQLite DB for the test (autoapplies the full schema)."""
    with patch("src.core.audit._DB_PATH", tmp_path / "test.db"), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        yield


# ── board_in_recipients ─────────────────────────────────────────────────────


def test_board_in_recipients_finds_to():
    from src.workflows.director_briefing import board_in_recipients

    msg = {"toRecipients": [
        {"emailAddress": {"address": "board@amnesty.org.gr", "name": "Board"}}
    ]}
    assert board_in_recipients(msg) is True


def test_board_in_recipients_finds_cc():
    from src.workflows.director_briefing import board_in_recipients

    msg = {
        "toRecipients": [{"emailAddress": {"address": "members@amnesty.org.gr"}}],
        "ccRecipients": [{"emailAddress": {"address": "BOARD@amnesty.org.gr"}}],
    }
    assert board_in_recipients(msg) is True


def test_board_in_recipients_finds_bcc():
    from src.workflows.director_briefing import board_in_recipients

    msg = {"bccRecipients": [{"emailAddress": {"address": "board@AMNESTY.ORG.GR"}}]}
    assert board_in_recipients(msg) is True


def test_board_in_recipients_false_when_only_members():
    from src.workflows.director_briefing import board_in_recipients

    msg = {"toRecipients": [{"emailAddress": {"address": "members@amnesty.org.gr"}}]}
    assert board_in_recipients(msg) is False


def test_board_in_recipients_false_on_empty_or_none():
    from src.workflows.director_briefing import board_in_recipients

    assert board_in_recipients({}) is False
    assert board_in_recipients({"toRecipients": []}) is False


# ── Filename classification ─────────────────────────────────────────────────


def test_classify_filename_detects_eisigitiko():
    from src.workflows.director_briefing import KIND_EISIGITIKO, classify_filename

    assert classify_filename("Εισηγητικό_ΔΣ05.pdf") == KIND_EISIGITIKO
    assert classify_filename("εισηγητικο.pdf") == KIND_EISIGITIKO
    assert classify_filename("ΕΙΣΗΓΗΤΙΚΟ-ΔΣ01-2026.docx") == KIND_EISIGITIKO


def test_classify_filename_detects_enimerotiko():
    from src.workflows.director_briefing import KIND_ENIMEROTIKO, classify_filename

    assert classify_filename("Ενημερωτικό-ΔΣ05.pdf") == KIND_ENIMEROTIKO
    assert classify_filename("ενημερωτικο.pdf") == KIND_ENIMEROTIKO


def test_classify_filename_eisigitiko_wins_when_both_appear():
    """If both words appear in one filename, Εισηγητικό wins - it's the
    more specific kind (adds the Εισηγήσεις label)."""
    from src.workflows.director_briefing import KIND_EISIGITIKO, classify_filename

    assert classify_filename("Εισηγητικό-και-Ενημερωτικό.pdf") == KIND_EISIGITIKO


def test_classify_filename_returns_none_for_unrelated():
    from src.workflows.director_briefing import classify_filename

    assert classify_filename("budget.xlsx") is None
    assert classify_filename("") is None
    assert classify_filename("Random_document_2026.pdf") is None


# ── Briefing attachment finder ──────────────────────────────────────────────


def test_find_briefing_attachment_returns_matching_pdf():
    from src.workflows.director_briefing import (
        KIND_EISIGITIKO,
        find_briefing_attachment,
    )

    attachments = [
        {"id": "1", "name": "budget.xlsx"},
        {"id": "2", "name": "Εισηγητικό_ΔΣ05.pdf"},
        {"id": "3", "name": "minutes.docx"},
    ]
    match = find_briefing_attachment(attachments)
    assert match is not None
    chosen, kind = match
    assert chosen["id"] == "2"
    assert kind == KIND_EISIGITIKO


def test_find_briefing_attachment_returns_none_when_no_filename_matches():
    """Director sent PDFs but none have the briefing keyword in the name →
    no briefing classification, caller should fall through to mirror only."""
    from src.workflows.director_briefing import find_briefing_attachment

    attachments = [
        {"id": "1", "name": "report.pdf"},
        {"id": "2", "name": "draft_v2.docx"},
    ]
    assert find_briefing_attachment(attachments) is None


def test_find_briefing_attachment_returns_none_when_only_images():
    from src.workflows.director_briefing import find_briefing_attachment

    attachments = [
        {"id": "1", "name": "signature.png"},
        {"id": "2", "name": "Εισηγητικό-photo.jpg"},  # keyword in image - still skipped
    ]
    assert find_briefing_attachment(attachments) is None


def test_find_briefing_attachment_handles_tonos_in_filename():
    """The filename-match should be τόνος-insensitive too."""
    from src.workflows.director_briefing import (
        KIND_EISIGITIKO,
        find_briefing_attachment,
    )

    attachments = [
        {"id": "1", "name": "Σχέδιο.pdf"},
        {"id": "2", "name": "εισηγητικό-Διευθυντή.pdf"},  # has τόνος
    ]
    match = find_briefing_attachment(attachments)
    assert match is not None
    chosen, kind = match
    assert chosen["id"] == "2"
    assert kind == KIND_EISIGITIKO


def test_find_briefing_attachment_eisigitiko_wins_over_enimerotiko_across_attachments():
    """If one attachment is Εισηγητικό and another is Ενημερωτικό, the
    Εισηγητικό wins (kind-priority rule applied across the whole set)."""
    from src.workflows.director_briefing import (
        KIND_EISIGITIKO,
        find_briefing_attachment,
    )

    attachments = [
        {"id": "1", "name": "Ενημερωτικό-old.pdf"},   # earlier in list
        {"id": "2", "name": "Εισηγητικό-new.pdf"},    # but Εισηγητικά wins
    ]
    match = find_briefing_attachment(attachments)
    assert match is not None
    chosen, kind = match
    assert chosen["id"] == "2"
    assert kind == KIND_EISIGITIKO


# ── Metadata builders ───────────────────────────────────────────────────────


def test_briefing_title_format():
    from src.workflows.director_briefing import briefing_title

    assert briefing_title("ΔΣ05-2026", "ΕΙΣΗΓΗΤΙΚΟ") == "Εισηγητικό Διευθυντή - Συνεδρίαση ΔΣ05-2026"
    assert briefing_title("ΔΣ01-2026", "ΕΝΗΜΕΡΩΤΙΚΟ") == "Ενημερωτικό Διευθυντή - Συνεδρίαση ΔΣ01-2026"


def test_briefing_labels_per_kind():
    from src.workflows.director_briefing import briefing_labels

    assert briefing_labels("ΕΙΣΗΓΗΤΙΚΟ") == ["Εισηγήσεις", "Αναφορές", "Γραφείο"]
    assert briefing_labels("ΕΝΗΜΕΡΩΤΙΚΟ") == ["Αναφορές", "Γραφείο"]


def test_briefing_kuria_simeia_is_not_hardcoded():
    """Per SecGen 2026-05-30: the bot must NOT bake in a Κύρια Σημεία
    template for Director briefings.  The field is left empty so the
    SecGen fills it in by hand based on each cycle's actual content."""
    from src.workflows.director_briefing import prefill_archive_context

    ctx = prefill_archive_context(meeting_ref="ΔΣ05-2026", kind="ΕΙΣΗΓΗΤΙΚΟ")
    assert ctx["llm_result"]["kuria_simeia"] == ""

    ctx = prefill_archive_context(meeting_ref="ΔΣ05-2026", kind="ΕΝΗΜΕΡΩΤΙΚΟ")
    assert ctx["llm_result"]["kuria_simeia"] == ""


def test_local_copy_path_sanitizes_separators():
    from src.workflows.director_briefing import LOCAL_BRIEFING_DIR, local_copy_path

    p = local_copy_path("ΔΣ05/2026", "Εισηγητικό/v1.pdf")
    assert str(p).startswith(str(LOCAL_BRIEFING_DIR))
    assert "/" not in p.parent.name  # meeting_ref segment sanitised
    assert "/" not in p.name         # filename segment sanitised


def test_prefill_archive_context_sets_skip_llm_and_canonical_metadata():
    from src.workflows.director_briefing import prefill_archive_context

    ctx = prefill_archive_context(meeting_ref="ΔΣ05-2026", kind="ΕΙΣΗΓΗΤΙΚΟ")
    assert ctx["_skip_llm"] is True
    llm = ctx["llm_result"]
    assert llm["title"] == "Εισηγητικό Διευθυντή - Συνεδρίαση ΔΣ05-2026"
    assert llm["labels"] == ["Εισηγήσεις", "Αναφορές", "Γραφείο"]
    # Κύρια Σημεία intentionally NOT pre-filled - SecGen fills per cycle.
    assert llm["kuria_simeia"] == ""
    assert ctx["sender_email"] == "director@amnesty.org.gr"


# ── DB helpers ──────────────────────────────────────────────────────────────


def test_record_and_list_director_briefing(mock_db):
    from src.core.audit import (
        list_director_briefings_for_meeting,
        record_director_briefing,
    )

    bid = record_director_briefing(
        meeting_ref="ΔΣ05-2026",
        kind="ΕΙΣΗΓΗΤΙΚΟ",
        local_path="data/director_briefings/ΔΣ05-2026/briefing.pdf",
        source_message_id="<x@example.com>",
        workflow_id="wf_abc",
    )
    assert bid > 0

    rows = list_director_briefings_for_meeting("ΔΣ05-2026")
    assert len(rows) == 1
    assert rows[0]["kind"] == "ΕΙΣΗΓΗΤΙΚΟ"
    assert rows[0]["protocol_number"] is None  # not yet assigned
    assert rows[0]["local_path"].endswith("briefing.pdf")


def test_update_director_briefing_archive_result(mock_db):
    from src.core.audit import (
        list_director_briefings_for_meeting,
        record_director_briefing,
        update_director_briefing_archive_result,
    )

    bid = record_director_briefing(
        meeting_ref="ΔΣ06-2026",
        kind="ΕΝΗΜΕΡΩΤΙΚΟ",
        local_path="/tmp/x.pdf",
    )
    update_director_briefing_archive_result(
        bid, protocol_number="2026_017", sharepoint_url="https://sharepoint/x",
    )
    rows = list_director_briefings_for_meeting("ΔΣ06-2026")
    assert rows[0]["protocol_number"] == "2026_017"
    assert rows[0]["sharepoint_url"] == "https://sharepoint/x"


# ── End-to-end intake glue ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_director_email_returns_none(mock_db):
    """Sender not director → fast bail, no Archive workflow invoked."""
    from src.workflows.director_briefing_intake import (
        process_director_briefing_email,
    )

    result = await process_director_briefing_email(
        message={"hasAttachments": True},
        meeting_id="board_meeting:ΔΣ05-2026",
        sender_email="someone@else.com",
        subject="Re: Συνεδρίαση ΔΣ05",
    )
    assert result is None


@pytest.mark.asyncio
async def test_director_email_without_attachments_returns_none(mock_db):
    from src.workflows.director_briefing_intake import (
        process_director_briefing_email,
    )

    result = await process_director_briefing_email(
        message={"hasAttachments": False},
        meeting_id="board_meeting:ΔΣ05-2026",
        sender_email="director@amnesty.org.gr",
        subject="Re: Συνεδρίαση ΔΣ05",
    )
    assert result is None


@pytest.mark.asyncio
async def test_no_briefing_keyword_in_any_filename_returns_none(mock_db):
    """Director replied with attachments but none of the filenames carry
    the briefing keyword → bail; the rest of the email-intake pipeline
    handles the mirror as a normal board reply."""
    from src.workflows.director_briefing_intake import (
        process_director_briefing_email,
    )

    fake_inbox = MagicMock()
    fake_inbox.list_attachments = AsyncMock(return_value=[
        {"id": "1", "name": "report.pdf"},
        {"id": "2", "name": "budget.xlsx"},
    ])

    with patch("src.integrations.m365.inbox.M365InboxClient", return_value=fake_inbox):
        result = await process_director_briefing_email(
            message={
                "id": "msg_graph_001",
                "hasAttachments": True,
                "internetMessageId": "<x@amnesty.org.gr>",
            },
            meeting_id="board_meeting:ΔΣ05-2026",
            sender_email="director@amnesty.org.gr",
            subject="Re: Συνεδρίαση ΔΣ05",
        )
    assert result is None


@pytest.mark.asyncio
async def test_briefing_archive_publishes_announcement_and_records_row(mock_db, tmp_path):
    """Happy path: Director replies with a briefing → the file is archived,
    a DB row is recorded, AND a bot-composed announcement is published as
    a board-email event so the board sees the same wording on both email
    and Discord.

    The Director's own email body never goes to the board."""
    from src.core.event_bus import bus
    from src.core.events import EVENT_BOARD_EMAIL_SENT

    captured = []

    async def _capture(payload):
        # Only collect our announcement events, not the ones that get
        # published by other unrelated test fixtures during this test.
        if getattr(payload, "kind", "") == "director_briefing_announcement":
            captured.append(payload)

    bus.subscribe(EVENT_BOARD_EMAIL_SENT, _capture)

    # Mock the inbox so list_attachments + download_attachment don't hit Graph
    fake_inbox = MagicMock()
    fake_inbox.list_attachments = AsyncMock(return_value=[
        {"id": "att1", "name": "Εισηγητικό-ΔΣ05.pdf"},
    ])

    async def _fake_download(_msg_id, _att_id, local_path):
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(b"%PDF-1.4 fake")

    fake_inbox.download_attachment = AsyncMock(side_effect=_fake_download)

    # Mock ArchiveWorkflow so we don't actually upload to SharePoint
    fake_wf = MagicMock()
    fake_wf.workflow_id = "wf_brief_001"
    fake_wf.context = {
        "protocol_number": "2026_042",
        "archive_share_link": "https://sharepoint/example",
    }
    fake_wf.run = AsyncMock(return_value={"status": "completed"})

    # Mock M365MailClient so we don't actually send email
    fake_mail = MagicMock()
    fake_mail.send_reply = AsyncMock(return_value="<reply-id@x>")

    # Patch the local-copy dir so we don't pollute the repo
    from src.workflows import director_briefing as _db

    try:
        with patch.object(_db, "LOCAL_BRIEFING_DIR", tmp_path / "briefings"), \
             patch("src.integrations.m365.inbox.M365InboxClient", return_value=fake_inbox), \
             patch("src.workflows.archive.ArchiveWorkflow", return_value=fake_wf), \
             patch("src.integrations.m365.mail.M365MailClient", return_value=fake_mail):
            from src.workflows import director_briefing_intake as _dbi
            result = await _dbi.process_director_briefing_email(
                message={
                    "id": "msg_graph_001",
                    "hasAttachments": True,
                    "internetMessageId": "<x@amnesty.org.gr>",
                },
                meeting_id="board_meeting:ΔΣ05-2026",
                sender_email="director@amnesty.org.gr",
                # Director simply hits Reply - subject is whatever Outlook prepends.
                subject="Re: Συνεδρίαση ΔΣ05",
            )

        assert result is not None
        assert result["kind"] == "ΕΙΣΗΓΗΤΙΚΟ"
        assert result["protocol_number"] == "2026_042"

        # The bot-composed announcement event was published, carrying the
        # protocol number + SharePoint URL the board should see.
        assert len(captured) == 1
        payload = captured[0]
        assert payload.kind == "director_briefing_announcement"
        assert payload.meeting_ref == "ΔΣ05-2026"
        assert "2026_042" in payload.body_html
        assert "https://sharepoint/example" in payload.body_html
        assert "Εισηγητικό" in payload.body_html

        # DB row exists with the archive result patched in
        from src.core.audit import list_director_briefings_for_meeting
        rows = list_director_briefings_for_meeting("ΔΣ05-2026")
        assert len(rows) == 1
        assert rows[0]["protocol_number"] == "2026_042"
        assert rows[0]["sharepoint_url"] == "https://sharepoint/example"
    finally:
        bus.unsubscribe(EVENT_BOARD_EMAIL_SENT, _capture)


@pytest.mark.asyncio
async def test_briefing_archive_skips_announcement_when_send_announcement_false(
    mock_db, tmp_path,
):
    """When board@ is on the Director's email, the caller passes
    ``send_announcement=False``.  The briefing still archives but NO
    announcement event is published (the regular mirror flow handles
    visibility) and NO announcement email is sent."""
    from src.core.event_bus import bus
    from src.core.events import EVENT_BOARD_EMAIL_SENT

    captured: list = []

    async def _capture(payload):
        if getattr(payload, "kind", "") == "director_briefing_announcement":
            captured.append(payload)

    bus.subscribe(EVENT_BOARD_EMAIL_SENT, _capture)

    fake_inbox = MagicMock()
    fake_inbox.list_attachments = AsyncMock(return_value=[
        {"id": "att1", "name": "Εισηγητικό-ΔΣ05.pdf"},
    ])

    async def _fake_download(_msg_id, _att_id, local_path):
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(b"%PDF-1.4 fake")

    fake_inbox.download_attachment = AsyncMock(side_effect=_fake_download)

    fake_wf = MagicMock()
    fake_wf.workflow_id = "wf_brief_002"
    fake_wf.context = {
        "protocol_number": "2026_050",
        "archive_share_link": "https://sharepoint/x",
    }
    fake_wf.run = AsyncMock(return_value={"status": "completed"})

    fake_mail = MagicMock()
    fake_mail.send_reply = AsyncMock()

    from src.workflows import director_briefing as _db

    try:
        with patch.object(_db, "LOCAL_BRIEFING_DIR", tmp_path / "briefings"), \
             patch("src.integrations.m365.inbox.M365InboxClient", return_value=fake_inbox), \
             patch("src.workflows.archive.ArchiveWorkflow", return_value=fake_wf), \
             patch("src.integrations.m365.mail.M365MailClient", return_value=fake_mail):
            from src.workflows import director_briefing_intake as _dbi
            result = await _dbi.process_director_briefing_email(
                message={
                    "id": "msg_graph_002",
                    "hasAttachments": True,
                    "internetMessageId": "<x@amnesty.org.gr>",
                },
                meeting_id="board_meeting:ΔΣ05-2026",
                sender_email="director@amnesty.org.gr",
                subject="Re: Συνεδρίαση ΔΣ05",
                send_announcement=False,        # ← the new knob
            )

        # Briefing was still archived (DB row + protocol number assigned)
        assert result is not None
        assert result["protocol_number"] == "2026_050"
        from src.core.audit import list_director_briefings_for_meeting
        rows = list_director_briefings_for_meeting("ΔΣ05-2026")
        assert len(rows) == 1
        assert rows[0]["protocol_number"] == "2026_050"

        # But no announcement was published, and no email was sent
        assert captured == []
        fake_mail.send_reply.assert_not_called()
    finally:
        bus.unsubscribe(EVENT_BOARD_EMAIL_SENT, _capture)
