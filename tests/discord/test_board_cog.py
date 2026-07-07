"""Tests for BoardCog slash commands.

Focuses on the logic layer (URL validation, workflow lookup, rollback dispatch)
without requiring a live Discord connection.  Discord interaction objects are
mocked with MagicMock / AsyncMock.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_interaction() -> MagicMock:
    """Return a minimal mock discord.Interaction with async response/followup."""
    interaction = MagicMock()
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    interaction.user = MagicMock()
    interaction.user.id = 123456789
    return interaction


def _make_bot() -> MagicMock:
    """Return a minimal mock discord.ext.commands.Bot."""
    bot = MagicMock()
    bot.tree = MagicMock()
    bot.tree.add_command = MagicMock()
    bot.tree.remove_command = MagicMock()
    return bot


def _make_workflow_state(
    workflow_id: str = "wf_test_001",
    state: str = "awaiting_approval",
    step_index: int = 0,
    email_thread_anchor: str = "<anchor@example.com>",
) -> dict:
    ctx = {"email_thread_anchor": email_thread_anchor}
    data = {"step_index": step_index, "context": ctx}
    return {
        "workflow_id": workflow_id,
        "workflow_name": "board_meeting_invitation",
        "state": state,
        "data": json.dumps(data),
    }


# ── Instantiation ─────────────────────────────────────────────────────────────


def test_board_cog_instantiates_without_error():
    """BoardCog and its inner _BoardCommands must construct cleanly."""
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    assert cog.bot is bot
    assert cog._commands is not None
    assert cog._commands.name == "board"


@pytest.mark.asyncio
async def test_cog_load_registers_command():
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    await cog.cog_load()
    bot.tree.add_command.assert_called_once_with(cog._commands)


@pytest.mark.asyncio
async def test_cog_unload_removes_command():
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    await cog.cog_unload()
    bot.tree.remove_command.assert_called_once_with("board")


# ── /board share-poll: URL validation ────────────────────────────────────────


@pytest.mark.asyncio
async def test_share_poll_invalid_url_no_scheme_rejects():
    """A bare URL without http/https must be rejected with a Greek error embed."""
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    interaction = _make_interaction()

    await cog._commands.cmd_share_poll.callback(
        cog._commands,
        interaction,
        url="not-a-url",
        workflow_id=None,
    )

    interaction.followup.send.assert_called_once()
    call_kwargs = interaction.followup.send.call_args
    embed = call_kwargs.kwargs.get("embed") or call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("embed")
    # The embed title should signal an error
    assert embed is not None
    assert "Μη έγκυρο URL" in embed.title


@pytest.mark.asyncio
async def test_share_poll_ftp_url_rejects():
    """ftp:// URLs must also be rejected."""
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    interaction = _make_interaction()

    await cog._commands.cmd_share_poll.callback(
        cog._commands,
        interaction,
        url="ftp://example.com/poll",
        workflow_id=None,
    )

    call_kwargs = interaction.followup.send.call_args
    embed = call_kwargs.kwargs.get("embed")
    assert embed is not None
    assert "Μη έγκυρο URL" in embed.title


# ── /board share-poll: workflow lookup ───────────────────────────────────────


@pytest.mark.asyncio
async def test_share_poll_no_in_flight_returns_clean_message():
    """With no in-flight workflows, command must return a clean message, not crash."""
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    interaction = _make_interaction()

    # DB returns no rows
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = []

    with (
        patch("src.core.audit._get_connection", return_value=mock_conn),
        patch("src.core.audit.get_workflow_state", return_value=None),
    ):
        await cog._commands.cmd_share_poll.callback(
            cog._commands,
            interaction,
            url="https://when2meet.com/poll",
            workflow_id=None,
        )

    interaction.followup.send.assert_called_once()
    call_kwargs = interaction.followup.send.call_args
    embed = call_kwargs.kwargs.get("embed")
    assert embed is not None
    assert "εκκρεμής" in embed.title.lower() or "εκκρεμ" in embed.description.lower()


@pytest.mark.asyncio
async def test_share_poll_valid_url_calls_m365_client():
    """With a valid URL + single in-flight workflow, M365MailClient.send_reply is called."""
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    interaction = _make_interaction()

    wf_state = _make_workflow_state(
        workflow_id="wf_abc",
        state="awaiting_approval",
        step_index=0,
    )

    # sqlite3.Row-like object: supports __getitem__ by key
    db_row = {"workflow_id": "wf_abc", "state": "awaiting_approval"}
    row_mock = MagicMock()
    row_mock.__getitem__ = MagicMock(side_effect=lambda k: db_row[k])

    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = [row_mock]

    mock_mail_client = AsyncMock()
    mock_mail_client.send_reply = AsyncMock(return_value="reply_msg_id_001")

    with (
        patch("src.core.audit._get_connection", return_value=mock_conn),
        patch("src.core.audit.get_workflow_state", return_value=wf_state),
        patch(
            "src.integrations.m365_mail.M365MailClient",
            return_value=mock_mail_client,
        ),
    ):
        await cog._commands.cmd_share_poll.callback(
            cog._commands,
            interaction,
            url="https://when2meet.com/test-poll",
            workflow_id=None,
        )

    mock_mail_client.send_reply.assert_called_once()
    call_kwargs = mock_mail_client.send_reply.call_args
    assert call_kwargs.kwargs["body"] == "Poll διαθεσιμότητας: https://when2meet.com/test-poll"

    # Success embed should be sent
    followup_call = interaction.followup.send.call_args
    embed = followup_call.kwargs.get("embed")
    assert embed is not None
    assert "Poll απεστάλη" in embed.title


@pytest.mark.asyncio
async def test_share_poll_with_explicit_workflow_id_uses_it():
    """When workflow_id is provided, the DB auto-lookup is skipped."""
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    interaction = _make_interaction()

    wf_state = _make_workflow_state(
        workflow_id="wf_explicit_999",
        state="awaiting_approval",
        step_index=0,
    )

    mock_conn = MagicMock()
    mock_mail_client = AsyncMock()
    mock_mail_client.send_reply = AsyncMock(return_value="reply_explicit")

    with (
        patch("src.core.audit._get_connection", return_value=mock_conn),
        patch("src.core.audit.get_workflow_state", return_value=wf_state),
        patch(
            "src.integrations.m365_mail.M365MailClient",
            return_value=mock_mail_client,
        ),
    ):
        await cog._commands.cmd_share_poll.callback(
            cog._commands,
            interaction,
            url="https://doodle.com/my-poll",
            workflow_id="wf_explicit_999",
        )

    # DB fetchall should NOT be called since workflow_id was explicit
    mock_conn.execute.assert_not_called()
    mock_mail_client.send_reply.assert_called_once()


# ── /board cancel ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_calls_rollback_on_matching_workflow():
    """/board cancel should load state, instantiate workflow, and call rollback."""
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    interaction = _make_interaction()

    wf_state = _make_workflow_state(
        workflow_id="wf_cancel_001",
        state="awaiting_approval",
        step_index=0,
    )

    mock_wf_instance = MagicMock()
    mock_wf_instance.rollback = AsyncMock()

    with (
        patch("src.core.audit.get_workflow_state", return_value=wf_state),
        patch("src.core.audit.save_workflow_state") as mock_save,
        patch(
            "src.workflows.board_meeting_invitation.BoardMeetingInvitationWorkflow",
            return_value=mock_wf_instance,
        ),
    ):
        await cog._commands.cmd_cancel.callback(
            cog._commands,
            interaction,
            workflow_id="wf_cancel_001",
        )

    mock_wf_instance.rollback.assert_called_once()
    mock_save.assert_called_once()
    save_kwargs = mock_save.call_args.kwargs
    assert save_kwargs["state"] == "cancelled"
    assert save_kwargs["workflow_id"] == "wf_cancel_001"

    followup_call = interaction.followup.send.call_args
    embed = followup_call.kwargs.get("embed")
    assert embed is not None
    assert "ακυρώθηκε" in embed.title.lower()


@pytest.mark.asyncio
async def test_cancel_nonexistent_workflow_id_returns_not_found():
    """/board cancel with an unknown ID must return a clean 'not found' message."""
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    interaction = _make_interaction()

    with patch("src.core.audit.get_workflow_state", return_value=None):
        await cog._commands.cmd_cancel.callback(
            cog._commands,
            interaction,
            workflow_id="wf_does_not_exist",
        )

    followup_call = interaction.followup.send.call_args
    embed = followup_call.kwargs.get("embed")
    assert embed is not None
    assert "δεν βρέθηκε" in embed.title.lower()


@pytest.mark.asyncio
async def test_cancel_does_not_crash_when_rollback_raises():
    """If rollback raises, the outer try/except must catch it and send error message."""
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    interaction = _make_interaction()

    wf_state = _make_workflow_state("wf_err_001", "in_progress")

    mock_wf_instance = MagicMock()
    mock_wf_instance.rollback = AsyncMock(side_effect=RuntimeError("Zoom API down"))

    with (
        patch("src.core.audit.get_workflow_state", return_value=wf_state),
        patch("src.core.audit.save_workflow_state"),
        patch(
            "src.workflows.board_meeting_invitation.BoardMeetingInvitationWorkflow",
            return_value=mock_wf_instance,
        ),
    ):
        await cog._commands.cmd_cancel.callback(
            cog._commands,
            interaction,
            workflow_id="wf_err_001",
        )

    # Should have called followup.send with an error string (❌ prefix)
    followup_call = interaction.followup.send.call_args
    sent_content = followup_call.args[0] if followup_call.args else followup_call.kwargs.get("content", "")
    assert "❌" in sent_content


# ── Default permissions sanity check ─────────────────────────────────────────


def test_board_commands_group_has_admin_default_permissions():
    """The /board group must declare administrator=True as default_permissions."""
    from src.integrations.discord.cogs.board import BoardCog
    import discord

    bot = _make_bot()
    cog = BoardCog(bot)
    perms = cog._commands.default_permissions
    assert perms is not None
    assert perms.administrator is True


# ── /board invite role gate + happy path ──────────────────────────────────────


def _role(name: str) -> MagicMock:
    r = MagicMock()
    r.name = name
    return r


def _make_president_interaction() -> MagicMock:
    """Interaction whose user has BOTH Συντονιστής AND Διοικητικό Συμβούλιο
    but is NOT a guild admin - so the role path is what grants access."""
    i = _make_interaction()
    i.user.roles = [_role("Συντονιστής"), _role("Διοικητικό Συμβούλιο"), _role("Μέλος")]
    i.user.guild_permissions = MagicMock(administrator=False)
    return i


def _make_admin_interaction() -> MagicMock:
    """Interaction whose user is a guild administrator with no special roles."""
    i = _make_interaction()
    i.user.roles = [_role("Μέλος")]
    i.user.guild_permissions = MagicMock(administrator=True)
    return i


def _make_non_president_interaction() -> MagicMock:
    """Interaction whose user is neither admin nor holds both required roles."""
    i = _make_interaction()
    # Missing Διοικητικό Συμβούλιο, and not an administrator
    i.user.roles = [_role("Συντονιστής"), _role("Μέλος")]
    i.user.guild_permissions = MagicMock(administrator=False)
    return i


@pytest.mark.asyncio
async def test_invite_blocks_users_without_both_roles():
    """/board invite refuses callers missing either Συντονιστής or Δ.Σ."""
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    cmd = cog._commands
    interaction = _make_non_president_interaction()

    with patch("src.workflows.board_meeting_invitation.BoardMeetingInvitationWorkflow") as WF:
        await cmd.cmd_invite.callback(cmd, interaction, poll_url="https://x", test=True)

    # The workflow was NEVER instantiated - gate blocked it before
    WF.assert_not_called()
    # We defer first (3s ack window), then refuse via an ephemeral followup
    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()
    sent = interaction.followup.send.call_args
    assert sent.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_invite_allows_guild_admins_without_roles():
    """A guild administrator may run /board invite even without the role-pair."""
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    cmd = cog._commands
    interaction = _make_admin_interaction()

    mock_wf = MagicMock()
    mock_wf.workflow_id = "wf_admin_001"
    mock_wf.context = {}
    mock_wf.run = AsyncMock(
        return_value={"status": "awaiting_approval", "step": "await_approval"}
    )

    with patch(
        "src.workflows.board_meeting_invitation.BoardMeetingInvitationWorkflow",
        return_value=mock_wf,
    ) as WF:
        await cmd.cmd_invite.callback(
            cmd, interaction, poll_url="https://x", test=True,
        )

    # Gate let the admin through - workflow ran.
    WF.assert_called_once()
    mock_wf.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_invite_starts_workflow_when_caller_has_both_roles():
    """A user with both roles successfully kicks off a workflow run."""
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    cmd = cog._commands
    interaction = _make_president_interaction()

    mock_wf = MagicMock()
    mock_wf.workflow_id = "wf_president_001"
    mock_wf.run = AsyncMock(
        return_value={
            "status": "awaiting_approval",
            "step": "await_approval",
            "workflow_id": "wf_president_001",
        }
    )

    with patch(
        "src.workflows.board_meeting_invitation.BoardMeetingInvitationWorkflow",
        return_value=mock_wf,
    ) as WF:
        await cmd.cmd_invite.callback(
            cmd, interaction,
            poll_url="https://doodle.com/group-poll/participate/abc",
            response_deadline="2026-06-10",
            test=True,
        )

    # Workflow constructed with discord:<id> actor
    WF.assert_called_once()
    _, kw = WF.call_args
    assert "discord:" in kw["actor"]

    # run() called once with poll_url + response_deadline + test_mode in initial_data
    mock_wf.run.assert_awaited_once()
    initial_data = mock_wf.run.call_args.args[0]
    assert initial_data["test_mode"] is True
    assert initial_data["poll_url"].startswith("https://doodle.com/")
    assert initial_data["response_deadline"] == "2026-06-10"

    # Operator gets an ephemeral status embed
    interaction.followup.send.assert_awaited_once()
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert "wf_president_001" in embed.description


@pytest.mark.asyncio
async def test_invite_only_passes_provided_args_to_workflow():
    """Optional args left unspecified must NOT be passed (workflow defaults apply)."""
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    cmd = cog._commands
    interaction = _make_president_interaction()

    mock_wf = MagicMock()
    mock_wf.workflow_id = "wf_002"
    mock_wf.run = AsyncMock(return_value={"status": "completed"})

    with patch(
        "src.workflows.board_meeting_invitation.BoardMeetingInvitationWorkflow",
        return_value=mock_wf,
    ):
        await cmd.cmd_invite.callback(cmd, interaction, test=False)

    initial_data = mock_wf.run.call_args.args[0]
    assert "poll_url" not in initial_data
    assert "response_deadline" not in initial_data
    assert initial_data["test_mode"] is False


# ── /board invite: candidate-date input (dates / start_date / end_date) ───────


@pytest.mark.asyncio
async def test_invite_dates_sets_crabfit_dates_in_workflow():
    """Passing dates='2026-06-17,2026-06-18' feeds crabfit_dates to the workflow."""
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    cmd = cog._commands
    interaction = _make_president_interaction()

    mock_wf = MagicMock()
    mock_wf.workflow_id = "wf_dates_001"
    mock_wf.context = {}
    mock_wf.run = AsyncMock(return_value={"status": "awaiting_approval", "step": "await_approval"})

    with patch(
        "src.workflows.board_meeting_invitation.BoardMeetingInvitationWorkflow",
        return_value=mock_wf,
    ):
        await cmd.cmd_invite.callback(
            cmd,
            interaction,
            dates="2026-06-17,2026-06-18",
        )

    mock_wf.run.assert_awaited_once()
    initial_data = mock_wf.run.call_args.args[0]
    assert "crabfit_dates" in initial_data
    assert initial_data["crabfit_dates"] == ["2026-06-17", "2026-06-18"]
    # Workflow was started - followup embed sent
    interaction.followup.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_invite_invalid_date_sends_ephemeral_error_without_running_workflow():
    """An invalid date in the dates param sends an ephemeral error; workflow is NOT run."""
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    cmd = cog._commands
    interaction = _make_president_interaction()

    mock_wf = MagicMock()
    mock_wf.run = AsyncMock(return_value={"status": "completed"})

    with patch(
        "src.workflows.board_meeting_invitation.BoardMeetingInvitationWorkflow",
        return_value=mock_wf,
    ):
        await cmd.cmd_invite.callback(
            cmd,
            interaction,
            dates="2026-06-17,not-a-date",
        )

    # Workflow must NOT have been run
    mock_wf.run.assert_not_awaited()
    # An ephemeral error must have been sent via followup
    interaction.followup.send.assert_awaited_once()
    call_kwargs = interaction.followup.send.call_args
    assert call_kwargs.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_invite_date_range_expands_correctly():
    """start_date + end_date range (2026-06-17 to 2026-06-19) expands to 3 dates."""
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    cmd = cog._commands
    interaction = _make_president_interaction()

    mock_wf = MagicMock()
    mock_wf.workflow_id = "wf_range_001"
    mock_wf.context = {}
    mock_wf.run = AsyncMock(return_value={"status": "awaiting_approval", "step": "await_approval"})

    with patch(
        "src.workflows.board_meeting_invitation.BoardMeetingInvitationWorkflow",
        return_value=mock_wf,
    ):
        await cmd.cmd_invite.callback(
            cmd,
            interaction,
            start_date="2026-06-17",
            end_date="2026-06-19",
        )

    mock_wf.run.assert_awaited_once()
    initial_data = mock_wf.run.call_args.args[0]
    assert "crabfit_dates" in initial_data
    assert len(initial_data["crabfit_dates"]) == 3
    assert initial_data["crabfit_dates"] == ["2026-06-17", "2026-06-18", "2026-06-19"]


@pytest.mark.asyncio
async def test_invite_dates_not_passed_when_poll_url_given():
    """When poll_url is also provided, crabfit_dates must NOT be in initial_data."""
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    cmd = cog._commands
    interaction = _make_president_interaction()

    mock_wf = MagicMock()
    mock_wf.workflow_id = "wf_pollurl_priority"
    mock_wf.context = {}
    mock_wf.run = AsyncMock(return_value={"status": "awaiting_approval", "step": "await_approval"})

    with patch(
        "src.workflows.board_meeting_invitation.BoardMeetingInvitationWorkflow",
        return_value=mock_wf,
    ):
        await cmd.cmd_invite.callback(
            cmd,
            interaction,
            poll_url="https://doodle.com/some-poll",
            dates="2026-06-17,2026-06-18",
        )

    initial_data = mock_wf.run.call_args.args[0]
    # poll_url present
    assert initial_data["poll_url"] == "https://doodle.com/some-poll"
    # crabfit_dates must be absent (poll_url wins)
    assert "crabfit_dates" not in initial_data


@pytest.mark.asyncio
async def test_invite_crabfit_url_surfaced_in_embed():
    """If the workflow sets crabfit_url in context, the embed gets a 'Crab.fit poll' field."""
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    cmd = cog._commands
    interaction = _make_president_interaction()

    mock_wf = MagicMock()
    mock_wf.workflow_id = "wf_crabfit_url"
    mock_wf.context = {"crabfit_url": "https://crab.fit/abc123"}
    mock_wf.run = AsyncMock(return_value={"status": "awaiting_approval", "step": "await_approval"})

    with patch(
        "src.workflows.board_meeting_invitation.BoardMeetingInvitationWorkflow",
        return_value=mock_wf,
    ):
        await cmd.cmd_invite.callback(
            cmd,
            interaction,
            dates="2026-06-17",
        )

    interaction.followup.send.assert_awaited_once()
    embed = interaction.followup.send.call_args.kwargs["embed"]
    field_names = [f.name for f in embed.fields]
    assert "Crab.fit poll" in field_names
    # Verify the URL value is correct
    crabfit_field = next(f for f in embed.fields if f.name == "Crab.fit poll")
    assert crabfit_field.value == "https://crab.fit/abc123"


@pytest.mark.asyncio
async def test_invite_range_only_one_date_boundary():
    """start_date == end_date expands to exactly 1 date."""
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    cmd = cog._commands
    interaction = _make_president_interaction()

    mock_wf = MagicMock()
    mock_wf.workflow_id = "wf_single_range"
    mock_wf.context = {}
    mock_wf.run = AsyncMock(return_value={"status": "completed"})

    with patch(
        "src.workflows.board_meeting_invitation.BoardMeetingInvitationWorkflow",
        return_value=mock_wf,
    ):
        await cmd.cmd_invite.callback(
            cmd,
            interaction,
            start_date="2026-06-17",
            end_date="2026-06-17",
        )

    initial_data = mock_wf.run.call_args.args[0]
    assert initial_data["crabfit_dates"] == ["2026-06-17"]


@pytest.mark.asyncio
async def test_invite_range_inverted_sends_error():
    """end_date before start_date must send an ephemeral error without running workflow."""
    from src.integrations.discord.cogs.board import BoardCog

    bot = _make_bot()
    cog = BoardCog(bot)
    cmd = cog._commands
    interaction = _make_president_interaction()

    mock_wf = MagicMock()
    mock_wf.run = AsyncMock(return_value={"status": "completed"})

    with patch(
        "src.workflows.board_meeting_invitation.BoardMeetingInvitationWorkflow",
        return_value=mock_wf,
    ):
        await cmd.cmd_invite.callback(
            cmd,
            interaction,
            start_date="2026-06-19",
            end_date="2026-06-17",
        )

    mock_wf.run.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()
    call_kwargs = interaction.followup.send.call_args
    assert call_kwargs.kwargs.get("ephemeral") is True


# ── Date autocomplete ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_autocomplete_start_date_returns_future_weekday_choices():
    """start_date autocomplete offers up to 25 valid future weekday ISO dates."""
    import datetime
    from src.integrations.discord.cogs import board

    interaction = MagicMock()
    choices = await board._ac_start_date(interaction, "")

    assert 0 < len(choices) <= 25
    today = datetime.date.today()
    for ch in choices:
        d = datetime.date.fromisoformat(ch.value)   # value is valid ISO
        assert d > today                              # strictly future
        assert d.weekday() < 5                         # Mon-Fri only


@pytest.mark.asyncio
async def test_autocomplete_end_date_not_before_start_date():
    """end_date autocomplete only offers dates on/after the chosen start_date."""
    import datetime
    from src.integrations.discord.cogs import board

    start = datetime.date.today() + datetime.timedelta(days=21)
    interaction = MagicMock()
    interaction.namespace.start_date = start.isoformat()

    choices = await board._ac_end_date(interaction, "")
    assert choices, "expected at least one end-date suggestion"
    for ch in choices:
        assert datetime.date.fromisoformat(ch.value) >= start


@pytest.mark.asyncio
async def test_autocomplete_filters_by_current_text():
    """Typed text narrows the suggestions (matches label or ISO substring)."""
    from src.integrations.discord.cogs import board

    interaction = MagicMock()
    all_choices = await board._ac_start_date(interaction, "")
    assert all_choices
    needle = all_choices[0].value          # an exact ISO date already offered
    filtered = await board._ac_start_date(interaction, needle)
    assert any(ch.value == needle for ch in filtered)
    assert len(filtered) <= len(all_choices)
