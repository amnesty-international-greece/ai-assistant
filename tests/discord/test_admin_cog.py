"""Tests for the AdminCog slash commands.

Focuses on the data-layer logic wrapped by each command, not the Discord
wiring (wiring is integration-tested against the test server).

Pattern mirrors test_brand_embed.py / test_teams_cog.py:
  * No real Discord bot is constructed - we use MagicMock for interactions.
  * Async tests use pytest-asyncio (``@pytest.mark.asyncio``).
  * External integrations (ArchiveWorkflow, OneDriveClient, GoogleClient,
    GraphSubscriptionsClient, run_safety_poll) are mocked with
    unittest.mock.patch / AsyncMock so no real credentials are needed.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

# Import the cog and subgroup classes under test
from src.integrations.discord.cogs.admin import (
    AdminCog,
    _ArchiveCommands,
    _M365Commands,
    _MinutesCommands,
    _OneDriveCommands,
)

# Pre-import src.integrations.m365.subscriptions so its module-level
# ``from src.core.audit import get_active_graph_subscriptions`` binding is
# established BEFORE any test-level patch on src.core.audit runs.
# Without this, the first test that calls cmd_subscriptions() triggers the
# import inside a patch context, leaving the module's name permanently bound
# to the mock - breaking later tests that call renew_expiring() directly.
import src.integrations.m365.subscriptions  # noqa: F401


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_interaction(*, sent_messages: list | None = None) -> MagicMock:
    """Return a mock discord.Interaction whose response/followup are recorded."""
    interaction = MagicMock()
    interaction.user = MagicMock()
    interaction.user.id = 123456789

    # Track everything that was sent via followup.send
    if sent_messages is None:
        sent_messages = []

    async def _defer(**kwargs):
        pass

    async def _followup_send(content=None, *, embed=None, ephemeral=True, **kwargs):
        sent_messages.append({"content": content, "embed": embed})

    interaction.response.defer = _defer
    interaction.followup.send = _followup_send
    return interaction, sent_messages


# ── Instantiation ─────────────────────────────────────────────────────────────


def test_admin_cog_instantiates_without_error():
    """AdminCog must initialise cleanly with a mock bot."""
    bot = MagicMock()
    cog = AdminCog(bot)
    assert cog.bot is bot
    assert cog._commands is not None
    assert cog._commands.name == "admin"


def test_admin_commands_group_has_subgroups():
    """The _AdminCommands group should have archive/minutes/m365/onedrive children."""
    bot = MagicMock()
    cog = AdminCog(bot)
    child_names = {c.name for c in cog._commands.commands}
    assert "archive" in child_names
    assert "minutes" in child_names
    assert "m365" in child_names
    assert "onedrive" in child_names


def test_admin_commands_group_has_audit_command():
    """The top-level /admin audit command must be registered."""
    bot = MagicMock()
    cog = AdminCog(bot)
    child_names = {c.name for c in cog._commands.commands}
    assert "audit" in child_names


def test_admin_group_default_permissions():
    """default_permissions must be set to administrator=True."""
    import discord
    bot = MagicMock()
    cog = AdminCog(bot)
    perms = cog._commands.default_permissions
    assert perms is not None
    assert perms.administrator is True


def test_cog_load_registers_command(monkeypatch):
    """cog_load must call tree.add_command with the /admin group."""
    bot = MagicMock()
    cog = AdminCog(bot)
    asyncio.run(cog.cog_load())
    bot.tree.add_command.assert_called_once_with(cog._commands)


def test_cog_unload_removes_command(monkeypatch):
    """cog_unload must call tree.remove_command('admin')."""
    bot = MagicMock()
    cog = AdminCog(bot)
    asyncio.run(cog.cog_unload())
    bot.tree.remove_command.assert_called_once_with("admin")


# ── /admin audit ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_passes_workflow_filter_to_get_audit_log(in_memory_db):
    """audit command must forward the workflow parameter to get_audit_log."""
    from src.core.audit import log_action

    # Seed two entries with different workflows
    log_action(workflow="archive", action="test_a", actor="tester")
    log_action(workflow="rss", action="test_b", actor="tester")

    interaction, sent = _make_interaction()
    bot = MagicMock()
    cog = AdminCog(bot)

    await cog._commands.cmd_audit.callback(cog._commands, interaction, workflow="archive", limit=10)

    assert len(sent) == 1
    embed = sent[0]["embed"]
    assert embed is not None
    # Should mention "archive" in the title
    assert "archive" in embed.title.lower()
    # Should have at least the one archive field
    field_names = [f.name for f in embed.fields]
    assert any("test_a" in n for n in field_names)
    # rss entry must NOT appear
    assert not any("test_b" in n for n in field_names)


@pytest.mark.asyncio
async def test_audit_default_limit_and_max_cap(in_memory_db):
    """Limit must be clamped: default=25, max=100."""
    from src.core.audit import get_audit_log

    interaction, sent = _make_interaction()
    bot = MagicMock()
    cog = AdminCog(bot)

    # Patch get_audit_log to capture the call args
    captured = {}

    async def _run():
        real_get_audit_log = get_audit_log
        with patch(
            "src.integrations.discord.cogs.admin.get_audit_log",
            side_effect=lambda **kw: (captured.update(kw) or []),
        ) if False else patch("src.core.audit.get_audit_log", wraps=real_get_audit_log):
            pass

    # Simpler: patch at the import site in the cog module
    with patch("src.core.audit.get_audit_log", return_value=[]) as mock_fn:
        await cog._commands.cmd_audit.callback(
            cog._commands, interaction, workflow="", limit=999
        )
        _, kwargs = mock_fn.call_args
        assert kwargs["limit"] <= 100

    with patch("src.core.audit.get_audit_log", return_value=[]) as mock_fn:
        await cog._commands.cmd_audit.callback(
            cog._commands, interaction, workflow="", limit=0
        )
        _, kwargs = mock_fn.call_args
        assert kwargs["limit"] >= 1


# ── /admin archive cancel ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_archive_cancel_calls_rollback_with_correct_args():
    """archive cancel must load workflow state and call ArchiveWorkflow.rollback(ctx)."""
    fake_ctx = {"protocol_number": "2026_001", "some_key": "val"}
    fake_state = {
        "workflow_name": "archive",
        "state": "completed",
        "data": json.dumps({"context": fake_ctx}),
    }

    interaction, sent = _make_interaction()
    cmd = _ArchiveCommands()

    with (
        patch("src.core.audit.get_workflow_state", return_value=fake_state),
        patch("src.core.audit.save_workflow_state") as mock_save,
        patch("src.workflows.archive.ArchiveWorkflow") as MockWF,
    ):
        mock_instance = MockWF.return_value
        mock_instance.rollback = AsyncMock()

        await cmd.cmd_cancel.callback(cmd, interaction, workflow_id="abc123")

    # rollback must be called exactly once with the deserialized ctx
    mock_instance.rollback.assert_awaited_once_with(fake_ctx)
    # workflow_id must be set before rollback
    assert mock_instance.workflow_id == "abc123"
    # save_workflow_state must mark it as cancelled
    mock_save.assert_called_once()
    _, kw = mock_save.call_args
    assert kw.get("state") == "cancelled" or mock_save.call_args[0][2] == "cancelled"


@pytest.mark.asyncio
async def test_archive_cancel_rejects_non_archive_workflow():
    """archive cancel must bail out if the workflow_name is not 'archive'."""
    fake_state = {
        "workflow_name": "board_meeting_invitation",
        "state": "completed",
        "data": "{}",
    }

    interaction, sent = _make_interaction()
    cmd = _ArchiveCommands()

    with patch("src.core.audit.get_workflow_state", return_value=fake_state):
        await cmd.cmd_cancel.callback(cmd, interaction, workflow_id="xyz")

    # Should have sent an error message, not an embed
    assert len(sent) == 1
    assert sent[0]["content"] is not None
    assert "❌" in sent[0]["content"]
    assert "archive" in sent[0]["content"].lower()


@pytest.mark.asyncio
async def test_archive_cancel_missing_workflow():
    """archive cancel must report not-found cleanly."""
    interaction, sent = _make_interaction()
    cmd = _ArchiveCommands()

    with patch("src.core.audit.get_workflow_state", return_value=None):
        await cmd.cmd_cancel.callback(cmd, interaction, workflow_id="missing-id")

    assert len(sent) == 1
    assert "❌" in sent[0]["content"]


# ── /admin onedrive backup-status ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_onedrive_backup_status_reports_missing_when_file_absent():
    """backup-status must report NOT PRESENT when PROTOCOL_BACKUP_PATH does not exist."""
    interaction, sent = _make_interaction()
    cmd = _OneDriveCommands()

    fake_path = MagicMock(spec=Path)
    fake_path.exists.return_value = False
    fake_path.resolve.return_value = Path("/fake/path/protokollo_latest.xlsx")

    with patch(
        "src.integrations.m365.onedrive.OneDriveClient.PROTOCOL_BACKUP_PATH",
        new_callable=PropertyMock,
        return_value=fake_path,
    ):
        await cmd.cmd_backup_status.callback(cmd, interaction)

    assert len(sent) == 1
    embed = sent[0]["embed"]
    assert embed is not None
    field_values = " ".join(f.value for f in embed.fields)
    assert "ΔΕΝ ΥΠΑΡΧΕΙ" in field_values or "missing" in field_values.lower()


@pytest.mark.asyncio
async def test_onedrive_backup_status_reports_valid_when_xlsx_readable(tmp_path):
    """backup-status must show VALID when openpyxl can open the file."""
    import openpyxl

    # Create a real minimal xlsx in a temp dir
    wb = openpyxl.Workbook()
    wb.create_sheet("2026")
    xlsx_path = tmp_path / "protokollo_latest.xlsx"
    wb.save(str(xlsx_path))

    interaction, sent = _make_interaction()
    cmd = _OneDriveCommands()

    with patch(
        "src.integrations.m365.onedrive.OneDriveClient.PROTOCOL_BACKUP_PATH",
        new_callable=PropertyMock,
        return_value=xlsx_path,
    ):
        await cmd.cmd_backup_status.callback(cmd, interaction)

    assert len(sent) == 1
    embed = sent[0]["embed"]
    assert embed is not None
    field_values = " ".join(f.value for f in embed.fields)
    assert "VALID" in field_values


@pytest.mark.asyncio
async def test_onedrive_backup_status_reports_corrupt_on_bad_xlsx(tmp_path):
    """backup-status must surface CORRUPT when openpyxl can not open the file."""
    bad_file = tmp_path / "bad.xlsx"
    bad_file.write_bytes(b"not-an-xlsx-file")

    interaction, sent = _make_interaction()
    cmd = _OneDriveCommands()

    with patch(
        "src.integrations.m365.onedrive.OneDriveClient.PROTOCOL_BACKUP_PATH",
        new_callable=PropertyMock,
        return_value=bad_file,
    ):
        await cmd.cmd_backup_status.callback(cmd, interaction)

    assert len(sent) == 1
    embed = sent[0]["embed"]
    field_values = " ".join(f.value for f in embed.fields)
    assert "CORRUPT" in field_values or "❌" in field_values


# ── /admin archive list ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_archive_list_shows_empty_message_when_no_rows(in_memory_db):
    """archive list must show a friendly empty-state message when there are no rows."""
    interaction, sent = _make_interaction()
    cmd = _ArchiveCommands()

    await cmd.cmd_list.callback(cmd, interaction)

    assert len(sent) == 1
    embed = sent[0]["embed"]
    assert embed is not None
    field_values = " ".join(f.value for f in embed.fields)
    assert "κανένα" in field_values.lower() or "30" in field_values.lower()


@pytest.mark.asyncio
async def test_archive_list_shows_workflow_rows(in_memory_db):
    """archive list must render one embed field per workflow row returned."""
    from src.core.audit import save_workflow_state

    save_workflow_state(
        workflow_name="archive",
        workflow_id="wf-abc-001",
        state="completed",
        data={"context": {"protocol_number": "2026_001"}},
    )

    interaction, sent = _make_interaction()
    cmd = _ArchiveCommands()

    await cmd.cmd_list.callback(cmd, interaction)

    assert len(sent) == 1
    embed = sent[0]["embed"]
    field_names = [f.name for f in embed.fields]
    # The short workflow_id "wf-abc-0" should appear in the field name
    assert any("wf-abc-0" in name for name in field_names)


# ── /admin m365 subscriptions ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_m365_subscriptions_shows_local_subs():
    """m365 subscriptions must render local subscriptions from the DB."""
    fake_sub = {
        "subscription_id": "sub-12345678-abcdef",
        "resource": "/me/mailFolders('Inbox')/messages",
        "client_state": "abc",
        "expiration_date_time": "2026-06-01T12:00:00Z",
    }

    interaction, sent = _make_interaction()
    cmd = _M365Commands()

    with (
        patch("src.core.audit.get_active_graph_subscriptions", return_value=[fake_sub]),
        patch(
            "src.integrations.graph_subscriptions.GraphSubscriptionsClient.list_remote",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        await cmd.cmd_subscriptions.callback(cmd, interaction)

    assert len(sent) == 1
    embed = sent[0]["embed"]
    assert embed is not None
    # The subscription id prefix must appear in at least one field
    all_text = embed.description + " ".join(f.name + f.value for f in embed.fields)
    assert "sub-12345678" in all_text


# ── /admin minutes list-drafts ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_minutes_list_drafts_shows_docs():
    """list-drafts must render one field per returned Google Doc."""
    fake_docs = [
        {"id": "doc1", "name": "Πρακτικά ΔΣ03-2026", "modifiedTime": "2026-05-01T10:00:00Z"},
        {"id": "doc2", "name": "Πρακτικά ΔΣ04-2026", "modifiedTime": "2026-05-20T10:00:00Z"},
    ]

    interaction, sent = _make_interaction()
    cmd = _MinutesCommands()

    with (
        patch("src.config.settings") as mock_settings,
        patch(
            "src.integrations.google_drive.GoogleClient.list_docs_in_folder",
            return_value=fake_docs,
        ),
    ):
        mock_settings.google.minutes_drafts_folder_id = "folder-abc"
        await cmd.cmd_list_drafts.callback(cmd, interaction)

    assert len(sent) == 1
    embed = sent[0]["embed"]
    assert embed is not None
    field_names = [f.name for f in embed.fields]
    assert any("ΔΣ03" in n for n in field_names)
    assert any("ΔΣ04" in n for n in field_names)


@pytest.mark.asyncio
async def test_minutes_list_drafts_no_folder_id():
    """list-drafts must send a clear error when folder_id is not configured."""
    interaction, sent = _make_interaction()
    cmd = _MinutesCommands()

    with patch("src.config.settings") as mock_settings:
        mock_settings.google.minutes_drafts_folder_id = ""
        await cmd.cmd_list_drafts.callback(cmd, interaction)

    assert len(sent) == 1
    assert "❌" in (sent[0]["content"] or "")


# ── /admin m365 poll-now ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_m365_poll_now_reports_summary():
    """m365 poll-now must call run_safety_poll and surface the summary."""
    fake_result = {
        "processed": 3,
        "by_outcome": {"archived": 2, "rejected_sender": 1},
    }

    interaction, sent = _make_interaction()
    cmd = _M365Commands()

    with patch(
        "src.workflows.email_intake.run_safety_poll",
        new_callable=AsyncMock,
        return_value=fake_result,
    ):
        await cmd.cmd_poll_now.callback(cmd, interaction)

    assert len(sent) == 1
    embed = sent[0]["embed"]
    assert embed is not None
    assert "3" in embed.description
    assert "archived" in embed.description


# ── /admin onedrive ls ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_onedrive_ls_renders_items():
    """/admin onedrive ls must render a file list embed."""
    fake_items = [
        {"name": "Αρχείο ανά έτος", "folder": {}, "size": 0},
        {"name": "protokollo.xlsx", "size": 98304, "lastModifiedDateTime": "2026-05-01T00:00:00Z"},
    ]

    interaction, sent = _make_interaction()
    cmd = _OneDriveCommands()

    with patch(
        "src.integrations.m365.onedrive.OneDriveClient.list_files",
        new_callable=AsyncMock,
        return_value=fake_items,
    ):
        await cmd.cmd_ls.callback(cmd, interaction, path="")

    assert len(sent) == 1
    embed = sent[0]["embed"]
    assert embed is not None
    all_text = " ".join(f.value for f in embed.fields)
    assert "protokollo.xlsx" in all_text
    assert "Αρχείο ανά έτος" in all_text


@pytest.mark.asyncio
async def test_onedrive_ls_truncates_overflow():
    """/admin onedrive ls must show +N more footer when >20 items returned."""
    fake_items = [
        {"name": f"file_{i:03d}.pdf", "size": i * 100}
        for i in range(25)
    ]

    interaction, sent = _make_interaction()
    cmd = _OneDriveCommands()

    with patch(
        "src.integrations.m365.onedrive.OneDriveClient.list_files",
        new_callable=AsyncMock,
        return_value=fake_items,
    ):
        await cmd.cmd_ls.callback(cmd, interaction, path="some/folder")

    assert len(sent) == 1
    embed = sent[0]["embed"]
    # Either embed.description or footer should mention overflow
    overflow_hint = (embed.description or "") + (embed.footer.text or "")
    assert "+5" in overflow_hint or "more" in overflow_hint.lower()
