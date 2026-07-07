"""Tests for MessageRouter.resolve using MagicMock fakes for discord.Guild."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.integrations.discord.classifier import ClassificationResult
from src.integrations.discord.constants import CLASSIFIER_UNCERTAIN_LABEL
from src.integrations.discord.routing import MessageRouter, RoutingDecision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_classification_result(
    *,
    label: str = "news",
    channel_id: str | None = "111",
    confidence: float = 0.9,
    fell_back: bool = False,
) -> ClassificationResult:
    return ClassificationResult(
        label=label,
        channel_id=channel_id,
        confidence=confidence,
        raw_response=f"{label}|{confidence}",
        fell_back=fell_back,
    )


def make_uncertain_result() -> ClassificationResult:
    return ClassificationResult(
        label=CLASSIFIER_UNCERTAIN_LABEL,
        channel_id=None,
        confidence=0.0,
        raw_response="",
        fell_back=True,
    )


def make_guild(channel_id: str | None = "111") -> MagicMock:
    """Return a minimal fake discord.Guild stub."""
    guild = MagicMock()
    if channel_id is not None:
        fake_channel = MagicMock()
        fake_channel.id = int(channel_id)
        # make get_channel return the fake channel only for the right ID
        guild.get_channel.side_effect = lambda cid: fake_channel if cid == int(channel_id) else None
    else:
        guild.get_channel.return_value = None
    guild.get_thread.return_value = None
    return guild


def make_router(guild: MagicMock) -> MessageRouter:
    channels_store = MagicMock()
    return MessageRouter(guild=guild, channels_store=channels_store)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_uncertain_result_returns_none_channel():
    guild = make_guild()
    router = make_router(guild)
    result = make_uncertain_result()

    decision = await router.resolve(result)

    assert isinstance(decision, RoutingDecision)
    assert decision.channel is None
    assert decision.thread is None
    assert "UNCERTAIN" in decision.reason or "confidence" in decision.reason.lower()


@pytest.mark.asyncio
async def test_resolve_fell_back_true_returns_none_channel():
    guild = make_guild()
    router = make_router(guild)
    result = make_classification_result(fell_back=True, channel_id=None)

    decision = await router.resolve(result)

    assert decision.channel is None


@pytest.mark.asyncio
async def test_resolve_channel_not_found_in_guild():
    # Guild has no channel with ID 999
    guild = make_guild(channel_id=None)
    router = make_router(guild)
    result = make_classification_result(channel_id="999", fell_back=False)

    decision = await router.resolve(result)

    assert decision.channel is None
    assert "999" in decision.reason


@pytest.mark.asyncio
async def test_resolve_valid_channel_returned():
    guild = make_guild(channel_id="111")
    router = make_router(guild)
    result = make_classification_result(channel_id="111", fell_back=False)

    decision = await router.resolve(result)

    assert decision.channel is not None
    assert decision.channel.id == 111


@pytest.mark.asyncio
async def test_resolve_no_existing_thread_returns_none_thread():
    guild = make_guild(channel_id="111")
    guild.get_thread.return_value = None
    router = make_router(guild)
    result = make_classification_result(channel_id="111", fell_back=False)

    decision = await router.resolve(result, existing_thread_id=None)

    assert decision.thread is None


@pytest.mark.asyncio
async def test_resolve_existing_thread_id_resolved():
    guild = make_guild(channel_id="111")
    fake_thread = MagicMock()
    fake_thread.id = 77777
    guild.get_thread.return_value = fake_thread
    router = make_router(guild)
    result = make_classification_result(channel_id="111", fell_back=False)

    decision = await router.resolve(result, existing_thread_id="77777")

    assert decision.thread is not None
    assert decision.thread.id == 77777


@pytest.mark.asyncio
async def test_resolve_thread_not_in_cache_falls_back_to_fetch():
    """When get_thread returns None, router should call fetch_channel."""
    guild = make_guild(channel_id="111")
    guild.get_thread.return_value = None  # not in cache

    fake_thread = MagicMock()
    fake_thread.id = 77777
    # fetch_channel is awaitable
    guild.fetch_channel = AsyncMock(return_value=fake_thread)

    router = make_router(guild)
    result = make_classification_result(channel_id="111", fell_back=False)

    decision = await router.resolve(result, existing_thread_id="77777")

    assert decision.thread is not None
    guild.fetch_channel.assert_awaited_once_with(77777)


@pytest.mark.asyncio
async def test_resolve_thread_fetch_exception_continues():
    """If fetching the thread raises, routing still succeeds (thread=None)."""
    guild = make_guild(channel_id="111")
    guild.get_thread.return_value = None
    guild.fetch_channel = AsyncMock(side_effect=Exception("fetch failed"))

    router = make_router(guild)
    result = make_classification_result(channel_id="111", fell_back=False)

    # Should NOT raise
    decision = await router.resolve(result, existing_thread_id="77777")

    assert decision.channel is not None  # channel still resolved
    assert decision.thread is None       # thread silently failed


@pytest.mark.asyncio
async def test_resolve_reason_contains_label_and_confidence():
    guild = make_guild(channel_id="111")
    router = make_router(guild)
    result = make_classification_result(label="news", channel_id="111", confidence=0.85, fell_back=False)

    decision = await router.resolve(result)

    assert "news" in decision.reason
    assert "85" in decision.reason  # confidence formatted as %


# ---------------------------------------------------------------------------
# MessageRouter.truncate (static method - pure, no guild needed)
# ---------------------------------------------------------------------------


def test_truncate_short_string_unchanged():
    from src.integrations.discord.constants import DISCORD_MESSAGE_SAFE_CHARS

    short = "hello"
    assert MessageRouter.truncate(short) == short


def test_truncate_long_string_trimmed():
    from src.integrations.discord.constants import DISCORD_MESSAGE_SAFE_CHARS

    long_text = "a" * (DISCORD_MESSAGE_SAFE_CHARS + 100)
    result = MessageRouter.truncate(long_text)
    assert len(result) <= DISCORD_MESSAGE_SAFE_CHARS
    assert result.endswith("…")


def test_truncate_exact_limit_unchanged():
    from src.integrations.discord.constants import DISCORD_MESSAGE_SAFE_CHARS

    exact = "b" * DISCORD_MESSAGE_SAFE_CHARS
    assert MessageRouter.truncate(exact) == exact
