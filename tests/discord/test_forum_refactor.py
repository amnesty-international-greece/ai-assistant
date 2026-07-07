"""Tests for the forum-cog refactor (Part A & B).

Covers:
  - AddChannelView calls EnabledChannelsStore.add() with empty metadata
  - build_embed shows only a 'tag:' field (no label/keywords)
  - bracket-tag pre-classifier routes directly without LLM
  - bracket-tag mismatch falls back to LLM path
  - bracket-tag matching is case- and τόνος-insensitive
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.integrations.discord.classifier import ClassificationResult, EmailClassifier
from src.integrations.discord.cogs.forum import (
    AddChannelView,
    ChannelTableView,
    _auto_tag,
)
from src.integrations.discord.state import EnabledChannel


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_channel(channel_id: str, test_mode: bool = False) -> EnabledChannel:
    return EnabledChannel(
        channel_id=channel_id,
        test_mode=test_mode,
        label="",
        classifier_keywords=[],
        forum_tag_ids=[],
        created_at=datetime.now(timezone.utc),
    )


def _make_fake_bot(channel_id: str, channel_name: str) -> MagicMock:
    """Return a bot mock whose get_channel() resolves one channel by ID."""
    ch = MagicMock()
    ch.name = channel_name
    bot = MagicMock()
    bot.get_channel = lambda cid: ch if cid == int(channel_id) else None
    return bot


def _make_fake_cog(
    channels: list[EnabledChannel],
    *,
    test_mode: bool = False,
    channel_id: str = "111",
    channel_name: str = "επικαιρότητα",
) -> MagicMock:
    """Build a minimal fake ForumCog suitable for view tests."""
    cog = MagicMock()
    cog.bot = _make_fake_bot(channel_id, channel_name)

    channels_store = MagicMock()
    channels_store.list = AsyncMock(return_value=channels)
    channels_store.add = AsyncMock()
    cog._channels_store = channels_store

    state_store = MagicMock()
    state_store.get_bool = AsyncMock(return_value=test_mode)
    cog._state_store = state_store

    return cog


# ---------------------------------------------------------------------------
# Part A - UI: AddChannelView adds with empty metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_channel_view_adds_with_empty_metadata() -> None:
    """Confirming the dropdown selection calls add() with empty label/keywords/tags."""
    cog = _make_fake_cog([])
    view = AddChannelView(cog)

    # Simulate the operator having picked a channel via the select
    view._select._selected_channel_id = "999"

    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await view._on_confirm(interaction)

    cog._channels_store.add.assert_awaited_once_with(
        "999",
        test_mode=False,
        label="",
        classifier_keywords=[],
        forum_tag_ids=[],
    )


# ---------------------------------------------------------------------------
# Part A - UI: embed shows only tag field, no label/keywords
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_table_embed_shows_only_tag_field() -> None:
    """Each embed field value contains 'tag:' but NOT 'label:' or 'keywords:'."""
    channel_id = "12345"
    channels = [_make_channel(channel_id)]
    cog = _make_fake_cog(channels, channel_id=channel_id, channel_name="επικαιρότητα")

    view = ChannelTableView(cog)
    embed = await view.build_embed()

    assert len(embed.fields) == 1
    field_value = embed.fields[0].value
    assert "tag:" in field_value
    assert "label:" not in field_value
    assert "keywords:" not in field_value


@pytest.mark.asyncio
async def test_table_embed_tag_is_greek_upper() -> None:
    """The auto-derived tag uses greek_upper (ALL CAPS, no τόνος)."""
    channel_id = "12345"
    cog = _make_fake_cog(
        [_make_channel(channel_id)],
        channel_id=channel_id,
        channel_name="επικαιρότητα",
    )
    view = ChannelTableView(cog)
    embed = await view.build_embed()

    # greek_upper("επικαιρότητα") == "ΕΠΙΚΑΙΡΟΤΗΤΑ"
    assert "ΕΠΙΚΑΙΡΟΤΗΤΑ" in embed.fields[0].value


# ---------------------------------------------------------------------------
# Part B - bracket-tag: direct routing (no LLM)
# ---------------------------------------------------------------------------


def _make_classifier_with_bot(
    channels: list[EnabledChannel],
    bot: MagicMock,
    *,
    test_mode: bool = False,
) -> EmailClassifier:
    store = MagicMock()
    store.list = AsyncMock(return_value=channels)
    clf = EmailClassifier(channels_store=store)
    clf.set_bot(bot)
    return clf


@pytest.mark.asyncio
async def test_bracket_tag_routes_directly_without_llm() -> None:
    """Email with [ΕΠΙΚΑΙΡΟΤΗΤΑ] routes directly - _get_client() never called."""
    channel_id = "111"
    channel_name = "επικαιρότητα"
    channels = [_make_channel(channel_id)]
    bot = _make_fake_bot(channel_id, channel_name)
    clf = _make_classifier_with_bot(channels, bot)

    with patch.object(clf, "_get_client", return_value=None) as mock_client:
        result = await clf.classify(
            subject="[ΕΠΙΚΑΙΡΟΤΗΤΑ] Νέα δραστηριότητα",
            body_preview="κάποιο body",
            test_mode=False,
        )

    # Must NOT have called the LLM client
    mock_client.assert_not_called()
    assert result.channel_id == channel_id
    assert result.confidence == 1.0
    assert result.fell_back is False
    assert result.raw_response.startswith("bracket_tag:")


@pytest.mark.asyncio
async def test_bracket_tag_mismatch_falls_back_to_llm() -> None:
    """Email with [UNKNOWN_TAG] doesn't match any channel → LLM path attempted."""
    channel_id = "111"
    channels = [_make_channel(channel_id)]
    bot = _make_fake_bot(channel_id, "επικαιρότητα")
    clf = _make_classifier_with_bot(channels, bot)

    # LLM client returns None so _classify_inner falls back to uncertain
    with patch.object(clf, "_get_client", return_value=None) as mock_client:
        result = await clf.classify(
            subject="[UNKNOWN_TAG] irrelevant subject",
            body_preview="body",
            test_mode=False,
        )

    # get_client IS called because bracket tag didn't match
    mock_client.assert_called()
    assert result.fell_back is True


# ---------------------------------------------------------------------------
# Part B - bracket-tag: case- and τόνος-insensitive matching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bracket_tag_case_and_tonos_insensitive() -> None:
    """[Επικαιρότητα] (mixed case, with τόνος) matches channel #επικαιρότητα."""
    channel_id = "111"
    channel_name = "επικαιρότητα"
    channels = [_make_channel(channel_id)]
    bot = _make_fake_bot(channel_id, channel_name)
    clf = _make_classifier_with_bot(channels, bot)

    with patch.object(clf, "_get_client", return_value=None):
        result = await clf.classify(
            subject="[Επικαιρότητα] Ενημέρωση",
            body_preview="body text",
            test_mode=False,
        )

    assert result.channel_id == channel_id
    assert result.fell_back is False


@pytest.mark.asyncio
async def test_bracket_tag_lowercase_subject_matches() -> None:
    """[επικαιρότητα] (already lowercase) still matches channel #επικαιρότητα."""
    channel_id = "111"
    channel_name = "επικαιρότητα"
    channels = [_make_channel(channel_id)]
    bot = _make_fake_bot(channel_id, channel_name)
    clf = _make_classifier_with_bot(channels, bot)

    with patch.object(clf, "_get_client", return_value=None):
        result = await clf.classify(
            subject="[επικαιρότητα] Ανακοίνωση",
            body_preview="body",
            test_mode=False,
        )

    assert result.channel_id == channel_id
    assert result.fell_back is False


# ---------------------------------------------------------------------------
# Part B - bracket-tag: no bot injected → falls through silently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bracket_tag_no_bot_falls_through() -> None:
    """When no bot is set, the pre-matcher is skipped entirely."""
    channels = [_make_channel("111")]
    store = MagicMock()
    store.list = AsyncMock(return_value=channels)
    clf = EmailClassifier(channels_store=store)
    # Do NOT call set_bot()

    with patch.object(clf, "_get_client", return_value=None) as mock_client:
        result = await clf.classify(
            subject="[ΕΠΙΚΑΙΡΟΤΗΤΑ] test",
            body_preview="body",
            test_mode=False,
        )

    # Falls through to LLM path (which returns UNCERTAIN due to no client)
    mock_client.assert_called()
    assert result.fell_back is True
