"""End-of-run cleanup for `invite` / `invite resume`: nothing stray is left in Zoom."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.cli.commands as cmds


def _wf(**ctx):
    return SimpleNamespace(context=dict(ctx), rollback=AsyncMock())


@pytest.fixture
def no_prompts(monkeypatch):
    answers = {"confirm": True}
    monkeypatch.setattr("builtins.input", lambda *_: "")
    monkeypatch.setattr(cmds, "_confirm", lambda *_: answers["confirm"])
    return answers


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_test_run_always_rolls_back(no_prompts, status):
    wf = _wf(zoom_meeting_id="843", test_mode=True)
    await cmds._invite_end_of_run_cleanup(wf, {"status": status}, test_mode=True)
    wf.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_live_run_before_board_email_offers_rollback(no_prompts):
    """The ΔΣ07 case: PDF timed out after Zoom had registered board members."""
    wf = _wf(zoom_meeting_id="892")
    await cmds._invite_end_of_run_cleanup(wf, {"status": "failed"}, test_mode=False)
    wf.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_live_run_declined_keeps_everything(no_prompts):
    no_prompts["confirm"] = False
    wf = _wf(zoom_meeting_id="892")
    await cmds._invite_end_of_run_cleanup(wf, {"status": "failed"}, test_mode=False)
    wf.rollback.assert_not_called()


@pytest.mark.asyncio
async def test_live_run_never_rolled_back_once_board_was_emailed(no_prompts):
    wf = _wf(zoom_meeting_id="814", board_email_message_id="<id>")
    await cmds._invite_end_of_run_cleanup(wf, {"status": "failed"}, test_mode=False)
    await cmds._invite_end_of_run_cleanup(wf, {"status": "completed"}, test_mode=False)
    wf.rollback.assert_not_called()
