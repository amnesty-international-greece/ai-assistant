"""Phase 3 tests - email-route intake, webhook handler, subject matcher."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_db(tmp_path):
    db_path = tmp_path / "test.db"
    with patch("src.core.audit._DB_PATH", db_path), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        yield


@pytest.fixture
def board_allow_list_only():
    """Force sender allow-list to match the board members in config.yaml."""
    from src.config import settings
    original = list(settings.m365_inbox.sender_allow_list)
    settings.m365_inbox.sender_allow_list = []
    try:
        yield
    finally:
        settings.m365_inbox.sender_allow_list = original


# ── Subject matcher ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("subject,expected", [
    ("[Αρχείο] εισηγηση",       True),
    ("ΑΡΧΕΙΟ - Πρακτικά",       True),
    ("αρχείο: υποψηφιότητα",    True),
    ("fwd: Αρχειο τι",          True),     # accent-stripped
    ("Archive request",         True),     # English fallback pattern
    ("Re: archive - RFP",       True),
    ("Πρόσκληση ΔΣ",            False),
    ("",                        False),
    ("re: συνεδρίαση",          False),
])
def test_subject_matches(subject, expected):
    from src.integrations.m365_inbox import subject_matches
    assert subject_matches(subject) is expected


def test_subject_matches_custom_patterns():
    from src.integrations.m365_inbox import subject_matches
    assert subject_matches("Filing request", patterns=["filing"]) is True
    assert subject_matches("Filing request", patterns=["αρχειο"]) is False


# ── Sender allow-list ──────────────────────────────────────────────────────


def test_sender_allowed_uses_board_members_by_default(board_allow_list_only):
    from src.integrations.m365_inbox import sender_allowed
    from src.config import settings
    # Pick a real board member email out of the config
    sample = settings.workflows.board_meeting.board_members[0].email
    assert sender_allowed(sample) is True
    assert sender_allowed(sample.upper()) is True   # case-insensitive
    assert sender_allowed("stranger@example.com") is False
    assert sender_allowed("") is False


def test_sender_allowed_yaml_list_overrides(board_allow_list_only, monkeypatch):
    from src.integrations.m365_inbox import sender_allowed
    from src.config import settings
    monkeypatch.setattr(settings.m365_inbox, "sender_allow_list",
                        ["overridden@example.com"])
    assert sender_allowed("overridden@example.com") is True
    # Board member is no longer in the (now-explicit) list
    sample = settings.workflows.board_meeting.board_members[0].email
    assert sender_allowed(sample) is False


# ── process_inbox_message ──────────────────────────────────────────────────


def _msg(*, sender, subject, has_attachments=True,
         imid="<msg-1@x>", graph_id="g-1"):
    return {
        "id": graph_id,
        "internetMessageId": imid,
        "subject": subject,
        "from": {"emailAddress": {"address": sender, "name": "Test Sender"}},
        "hasAttachments": has_attachments,
        "bodyPreview": "body…",
        "isRead": False,
    }


@pytest.mark.asyncio
async def test_intake_rejects_unknown_sender(mock_db, board_allow_list_only):
    from src.workflows.email_intake import process_inbox_message
    from src.core.audit import has_seen_email

    msg = _msg(sender="stranger@example.com", subject="αρχειο τι")
    result = await process_inbox_message(msg)
    assert result["outcome"] == "rejected_sender"
    # Marked seen so we don't re-process
    assert has_seen_email("<msg-1@x>") is True


@pytest.mark.asyncio
async def test_intake_rejects_subject_mismatch(mock_db, board_allow_list_only):
    from src.config import settings
    sample = settings.workflows.board_meeting.board_members[0].email

    from src.workflows.email_intake import process_inbox_message
    msg = _msg(sender=sample, subject="πρόσκληση συνεδρίασης")
    result = await process_inbox_message(msg)
    assert result["outcome"] == "rejected_subject"


@pytest.mark.asyncio
async def test_intake_skips_duplicate(mock_db, board_allow_list_only):
    from src.core.audit import mark_email_seen
    mark_email_seen("<dup@x>", outcome="archived")
    from src.workflows.email_intake import process_inbox_message
    result = await process_inbox_message(
        _msg(sender="x@x.com", subject="αρχειο", imid="<dup@x>")
    )
    assert result["outcome"] == "duplicate"


@pytest.mark.asyncio
async def test_intake_no_pdf_attachment(mock_db, board_allow_list_only):
    from src.config import settings
    sample = settings.workflows.board_meeting.board_members[0].email
    from src.workflows.email_intake import process_inbox_message

    with patch("src.workflows.email_intake.M365InboxClient") as INBOX, \
         patch("src.workflows.email_intake.M365MailClient") as MAIL:
        INBOX.return_value.list_attachments = AsyncMock(return_value=[
            {"id": "a-1", "name": "notes.txt", "contentType": "text/plain"},
        ])
        MAIL.return_value.send_reply = AsyncMock(return_value="<reply-id>")
        result = await process_inbox_message(
            _msg(sender=sample, subject="αρχειο please", imid="<np@x>")
        )

    assert result["outcome"] == "no_pdf"
    MAIL.return_value.send_reply.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_intake_cleans_up_tempdir_on_success(mock_db, board_allow_list_only, tmp_path, monkeypatch):
    """Regression test for the m365_intake_* tempdir leak.

    Before this fix, process_inbox_message created a tempdir via
    tempfile.mkdtemp(dir=data/inbox/) and never removed it.  After a year
    of intake we found 27+ leaked dirs.  This test pins the new try/finally
    cleanup contract.
    """
    from src.config import settings
    sample = settings.workflows.board_meeting.board_members[0].email

    fake_archive_result = {"status": "completed", "context": {
        "protocol_number": "2026_001", "llm_result": {"title": "T", "labels": [], "key_points": ""},
        "remote_folder": "x/2026", "share_link": "", "revision_open_until": "",
    }}

    # Redirect data/inbox into the test's tmp dir so we can introspect it
    inbox_dir = tmp_path / "inbox"
    monkeypatch.setattr("src.workflows.email_intake._INBOX_DIR", inbox_dir)

    from src.workflows.email_intake import process_inbox_message
    with patch("src.workflows.email_intake.M365InboxClient") as INBOX, \
         patch("src.workflows.email_intake.M365MailClient") as MAIL, \
         patch("src.workflows.email_intake.ArchiveWorkflow") as WF:
        INBOX.return_value.list_attachments = AsyncMock(return_value=[
            {"id": "a-1", "name": "doc.pdf", "contentType": "application/pdf"},
        ])

        async def _fake_dl(_msg_id, _att_id, dest: Path) -> Path:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"%PDF-1.4\nfake")
            return dest
        INBOX.return_value.download_attachment = AsyncMock(side_effect=_fake_dl)
        INBOX.return_value.mark_read = AsyncMock()

        wf_instance = MagicMock()
        wf_instance.workflow_id = "wf-cleanup-1"
        wf_instance.context = fake_archive_result["context"]
        wf_instance.run = AsyncMock(return_value=fake_archive_result)
        WF.return_value = wf_instance
        MAIL.return_value.send_reply = AsyncMock(return_value="<r>")

        await process_inbox_message(_msg(sender=sample, subject="αρχειο τεστ", imid="<cleanup1@x>"))

    # CRITICAL: no m365_intake_* dirs should remain in the inbox dir.
    leftovers = [p for p in inbox_dir.glob("m365_intake_*")] if inbox_dir.exists() else []
    assert leftovers == [], f"Tempdir leak: {leftovers}"


@pytest.mark.asyncio
async def test_intake_happy_path(mock_db, board_allow_list_only, tmp_path):
    from src.config import settings
    sample = settings.workflows.board_meeting.board_members[0].email

    fake_archive_result = {
        "status": "completed",
        "context": {
            "protocol_number": "2026_099",
            "llm_result": {
                "title": "Υποψηφιότητα ΕΕΔΑ - Παπαδόπουλος",
                "labels": ["Υποψηφιότητες", "Εξωτερικά"],
                "key_points": "",
            },
            "remote_folder": "Αρχείο ανά έτος/2026",
            "share_link": "https://x/share",
            "revision_open_until": "2026-05-29T12:00:00+00:00",
        },
    }

    from src.workflows.email_intake import process_inbox_message
    with patch("src.workflows.email_intake.M365InboxClient") as INBOX, \
         patch("src.workflows.email_intake.M365MailClient") as MAIL, \
         patch("src.workflows.email_intake.ArchiveWorkflow") as WF:

        INBOX.return_value.list_attachments = AsyncMock(return_value=[
            {"id": "a-1", "name": "doc.pdf", "contentType": "application/pdf"},
        ])

        async def _fake_download(_msg_id, _att_id, dest_path: Path) -> Path:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(b"%PDF-1.4\n%fake")
            return dest_path
        INBOX.return_value.download_attachment = AsyncMock(side_effect=_fake_download)
        INBOX.return_value.mark_read = AsyncMock()

        wf_instance = MagicMock()
        wf_instance.workflow_id = "wf-email-1"
        wf_instance.context = fake_archive_result["context"]
        wf_instance.run = AsyncMock(return_value=fake_archive_result)
        WF.return_value = wf_instance

        MAIL.return_value.send_reply = AsyncMock(return_value="<reply-id>")

        result = await process_inbox_message(
            _msg(sender=sample, subject="αρχειο: παρακαλώ", imid="<happy@x>")
        )

    assert result["outcome"] == "archived"
    assert result["workflow_id"] == "wf-email-1"
    assert result["protocol_number"] == "2026_099"
    # Confirmation reply sent
    MAIL.return_value.send_reply.assert_awaited_once()
    # Email marked as read
    INBOX.return_value.mark_read.assert_awaited_once()


# ── /webhooks/m365/inbox FastAPI route ─────────────────────────────────────


@pytest.fixture
def client(mock_db):
    from src.main import app
    with TestClient(app) as c:
        yield c


def test_webhook_validation_token_handshake(client):
    """Graph subscription handshake - must echo the validationToken as text/plain."""
    resp = client.post(
        "/webhooks/m365/inbox?validationToken=abc-123",
        json={"any": "thing"},
    )
    assert resp.status_code == 200
    assert resp.text == "abc-123"
    assert resp.headers["content-type"].startswith("text/plain")


def test_webhook_rejects_bad_client_state(client, mock_db):
    """Notifications with unknown subscriptionId / wrong clientState are dropped."""
    from src.core.audit import upsert_graph_subscription
    upsert_graph_subscription(
        "sub-known",
        resource="/me/mailFolders('Inbox')/messages",
        client_state="real-state",
        expiration_date_time=(datetime.now(timezone.utc) + timedelta(hours=24))
            .isoformat(),
    )

    body = {
        "value": [
            {
                "subscriptionId": "sub-known",
                "clientState": "WRONG",
                "resourceData": {"id": "msg-x"},
            },
            {
                "subscriptionId": "sub-unknown",
                "clientState": "real-state",
                "resourceData": {"id": "msg-y"},
            },
        ]
    }
    with patch("src.api.webhooks._process_graph_notification",
               new=AsyncMock()) as proc:
        resp = client.post("/webhooks/m365/inbox", json=body)
    assert resp.status_code == 202
    # Neither notification should have been forwarded to processing
    proc.assert_not_called()


def test_webhook_accepts_good_client_state(client, mock_db):
    from src.core.audit import upsert_graph_subscription
    upsert_graph_subscription(
        "sub-good",
        resource="/me/mailFolders('Inbox')/messages",
        client_state="match-me",
        expiration_date_time=(datetime.now(timezone.utc) + timedelta(hours=24))
            .isoformat(),
    )
    body = {
        "value": [{
            "subscriptionId": "sub-good",
            "clientState": "match-me",
            "resourceData": {"id": "msg-z"},
        }]
    }
    with patch("src.api.webhooks._process_graph_notification",
               new=AsyncMock()) as proc:
        resp = client.post("/webhooks/m365/inbox", json=body)
    assert resp.status_code == 202
    # FastAPI background tasks run after the response - TestClient awaits them
    proc.assert_called_once()


# ── Graph subscription CRUD wrapper ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_graph_subscription_create_persists_to_db(mock_db, monkeypatch):
    from src.integrations.graph_subscriptions import GraphSubscriptionsClient
    from src.core.audit import get_active_graph_subscriptions
    from src.config import settings

    monkeypatch.setattr(settings.m365_inbox, "webhook_url", "https://tunnel.example.com")

    async def _fake_post(self, url, headers, json):
        class _R:
            status_code = 201
            text = ""
            def json(_self):
                return {
                    "id": "sub-id-1",
                    "resource": json["resource"],
                    "expirationDateTime": json["expirationDateTime"],
                    "notificationUrl": json["notificationUrl"],
                    "clientState": json["clientState"],
                }
        return _R()

    with patch("httpx.AsyncClient.post", new=_fake_post), \
         patch.object(GraphSubscriptionsClient, "_get_token", return_value="tok"):
        client = GraphSubscriptionsClient()
        body = await client.create()

    assert body["id"] == "sub-id-1"
    rows = get_active_graph_subscriptions()
    assert any(r["subscription_id"] == "sub-id-1" for r in rows)


# ── Test-mode trigger (sender == testing.test_email) ──────────────────────


def test_default_allow_list_includes_test_email(board_allow_list_only):
    """The configured test_email is implicitly allow-listed."""
    from src.integrations.m365_inbox import sender_allowed
    from src.config import settings
    assert settings.testing.test_email   # sanity - config has it set
    assert sender_allowed(settings.testing.test_email) is True


def test_is_test_sender_helper():
    from src.workflows.email_intake import _is_test_sender
    from src.config import settings
    assert _is_test_sender(settings.testing.test_email) is True
    assert _is_test_sender(settings.testing.test_email.upper()) is True
    assert _is_test_sender("someone-else@example.com") is False
    assert _is_test_sender("") is False


@pytest.mark.asyncio
async def test_intake_forces_test_mode_for_test_sender(mock_db, board_allow_list_only):
    """Emails from settings.testing.test_email run the workflow in TEST MODE
    and roll back afterwards."""
    from src.config import settings

    fake_archive_result = {
        "status": "completed",
        "context": {
            "protocol_number": "2026_999",
            "llm_result": {
                "title": "T", "labels": ["Διοικητικά"], "key_points": "",
            },
            "remote_folder": "Αρχείο ανά έτος/2026",
            "share_link": "",
            "revision_open_until": "2026-05-29T12:00:00+00:00",
            "register_skipped": True,
        },
    }

    from src.workflows.email_intake import process_inbox_message
    with patch("src.workflows.email_intake.M365InboxClient") as INBOX, \
         patch("src.workflows.email_intake.M365MailClient") as MAIL, \
         patch("src.workflows.email_intake.ArchiveWorkflow") as WF:

        INBOX.return_value.list_attachments = AsyncMock(return_value=[
            {"id": "a-1", "name": "doc.pdf", "contentType": "application/pdf"},
        ])

        async def _fake_dl(_mid, _aid, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"%PDF-1.4\nfake")
            return dest
        INBOX.return_value.download_attachment = AsyncMock(side_effect=_fake_dl)
        INBOX.return_value.mark_read = AsyncMock()

        wf_instance = MagicMock()
        wf_instance.workflow_id = "wf-test-1"
        wf_instance.context = fake_archive_result["context"]
        wf_instance.run = AsyncMock(return_value=fake_archive_result)
        wf_instance.rollback = AsyncMock()
        WF.return_value = wf_instance

        MAIL.return_value.send_reply = AsyncMock(return_value="<r1>")

        result = await process_inbox_message(_msg(
            sender=settings.testing.test_email,
            subject="αρχειο: τεστ",
            imid="<test-mode@x>",
        ))

    assert result["outcome"] == "archived"
    assert result["test_mode"] is True
    # ArchiveWorkflow was invoked with test_mode=True in initial_data
    _, kwargs_run = wf_instance.run.call_args
    initial_data = wf_instance.run.call_args.args[0]
    assert initial_data["test_mode"] is True
    # Rollback was called after the reply
    wf_instance.rollback.assert_awaited_once()
    # Reply body carries the TEST MODE banner
    reply_body = MAIL.return_value.send_reply.call_args.kwargs["body"]
    assert "TEST MODE" in reply_body


# ── Combined taxonomy + categories download ────────────────────────────────


@pytest.mark.asyncio
async def test_read_taxonomy_and_categories_uses_one_download():
    """Combined reader should resolve the workbook path exactly once.

    Since 2026-05-27 the reader goes through ``_workbook_path_for_read``
    (which serves the cached backup snapshot taken by
    ``refresh_protocol_workbook`` at the start of the workflow).  We
    assert the lookup happens exactly once per combined call.
    """
    from src.integrations.onedrive import OneDriveClient

    lookup_calls = 0

    class _FakeWS:
        def __init__(self, rows):
            self._rows = rows
            self.title = ""
        def iter_rows(self, **kwargs):
            return iter(self._rows)

    class _FakeWB:
        def __init__(self):
            self._sheets = {
                "Ετικέτες":   _FakeWS([("Διοικητικά", "γενικά διοικητικά")]),
                "Κατηγορίες": _FakeWS([("Πρακτικά - Συνεδρίαση X",
                                        "Διοικητικά, Πρακτικά",
                                        "ΔΣX-YYYY")]),
            }
        @property
        def sheetnames(self):
            return list(self._sheets)
        def __contains__(self, name):
            return name in self._sheets
        def __getitem__(self, name):
            return self._sheets[name]

    async def _fake_lookup(self):
        nonlocal lookup_calls
        lookup_calls += 1
        return Path("/tmp/fake.xlsx")  # path is unused - openpyxl is patched

    with patch.object(OneDriveClient, "_workbook_path_for_read", _fake_lookup), \
         patch("openpyxl.load_workbook", return_value=_FakeWB()):
        client = OneDriveClient()
        tax, cat = await client.read_taxonomy_and_categories()

    assert lookup_calls == 1
    assert tax == [{"tag": "Διοικητικά", "description": "γενικά διοικητικά"}]
    assert cat == [{
        "pattern": "Πρακτικά - Συνεδρίαση X",
        "tags": "Διοικητικά, Πρακτικά",
        "kuria_simeia": "ΔΣX-YYYY",
    }]


@pytest.mark.asyncio
async def test_classify_document_uses_combined_reader():
    """classify_document should fetch tax+cat via ONE call when both are missing."""
    from src.workflows import archive_llm

    with patch("src.integrations.onedrive.OneDriveClient") as OD, \
         patch.object(archive_llm, "ClaudeClient") as CC:
        OD.return_value.read_taxonomy_and_categories = AsyncMock(
            return_value=([], [])
        )
        OD.return_value.read_taxonomy = AsyncMock(return_value=[])
        OD.return_value.read_categories = AsyncMock(return_value=[])
        CC.return_value.generate.return_value = (
            '{"title":"T","labels":["A"],"key_points":"",'
            '"existing_protocol":null,"category_matched":"X",'
            '"confidence":0.9,"reasoning_brief":""}'
        )
        await archive_llm.classify_document(
            filename="t.pdf",
            sender_email="x@x.com",
            pdf_text="hello",
        )

    # Combined reader called exactly once, individual readers NOT called
    OD.return_value.read_taxonomy_and_categories.assert_awaited_once()
    OD.return_value.read_taxonomy.assert_not_awaited()
    OD.return_value.read_categories.assert_not_awaited()


# ── Local πρωτόκολλο safety backup ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_protocol_workbook_refreshes_backup(tmp_path, monkeypatch):
    """Every successful download should leave behind an up-to-date backup."""
    from src.integrations.onedrive import OneDriveClient

    backup_dest = tmp_path / "backups" / "protokollo_latest.xlsx"
    monkeypatch.setattr(OneDriveClient, "PROTOCOL_BACKUP_PATH", backup_dest)

    fake_content = b"PK\x03\x04 fake xlsx bytes"

    async def _fake_download(self, name, dest):
        # Mirror the real download_file contract: write to *dest*.
        Path(dest).write_bytes(fake_content)

    client = OneDriveClient.__new__(OneDriveClient)  # skip __init__ (no token wiring)
    monkeypatch.setattr(OneDriveClient, "download_file", _fake_download)

    tmp_xlsx = await client._download_protocol_workbook()
    try:
        assert backup_dest.exists()
        assert backup_dest.read_bytes() == fake_content
    finally:
        try:
            tmp_xlsx.unlink()
        except OSError:
            pass


def test_backup_refresh_failure_does_not_raise(tmp_path, monkeypatch):
    """Backup is best-effort: even if the copy fails, the workflow keeps running."""
    from src.integrations.onedrive import OneDriveClient

    # Point at a deliberately unwritable path
    bad = tmp_path / "nonexistent_dir" / "cant_create.xlsx"
    monkeypatch.setattr(OneDriveClient, "PROTOCOL_BACKUP_PATH", bad)

    client = OneDriveClient.__new__(OneDriveClient)
    # Patch mkdir to blow up - simulating permission error
    with patch("pathlib.Path.mkdir", side_effect=PermissionError("no write")):
        # Source doesn't even need to exist; the method must swallow all errors
        client._backup_protocol_workbook(tmp_path / "does_not_matter.xlsx")
    # If we reached here without raising, the contract holds.


@pytest.mark.asyncio
async def test_graph_subscription_renew_expiring(mock_db, monkeypatch):
    """renew_expiring picks up subs whose expiry < threshold and PATCHes them."""
    import src.core.audit as _a
    print(f"\n[graph-enter] _CONNECTION={_a._CONNECTION!r} _DB_PATH={_a._DB_PATH!r}", flush=True)
    from src.integrations.graph_subscriptions import GraphSubscriptionsClient
    from src.core.audit import upsert_graph_subscription
    from src.config import settings

    # Sub A expires in 12h (below default 24h threshold)
    soon = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    # Sub B expires in 48h (above threshold)
    later = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()
    upsert_graph_subscription("sub-A", resource="r1", client_state="cs1",
                              expiration_date_time=soon)
    upsert_graph_subscription("sub-B", resource="r2", client_state="cs2",
                              expiration_date_time=later)

    async def _fake_patch(self, url, headers, json):
        class _R:
            status_code = 200
            text = ""
            def json(_self):
                return {"id": url.rsplit("/", 1)[-1],
                        "expirationDateTime": json["expirationDateTime"]}
        return _R()

    with patch("httpx.AsyncClient.patch", new=_fake_patch), \
         patch.object(GraphSubscriptionsClient, "_get_token", return_value="tok"):
        client = GraphSubscriptionsClient()
        renewed = await client.renew_expiring()

    assert renewed == ["sub-A"]
