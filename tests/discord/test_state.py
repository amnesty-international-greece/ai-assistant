"""Tests for SQLite-backed state stores: BotStateStore, EnabledChannelsStore, EmailThreadMap."""

from __future__ import annotations

import pytest

from src.integrations.discord.state import (
    BotStateStore,
    EmailThreadMap,
    EnabledChannelsStore,
)


# ---------------------------------------------------------------------------
# BotStateStore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_state_set_and_get_bool_true(in_memory_db):
    store = BotStateStore()
    await store.set_bool("my_flag", True)
    assert await store.get_bool("my_flag") is True


@pytest.mark.asyncio
async def test_bot_state_set_and_get_bool_false(in_memory_db):
    store = BotStateStore()
    await store.set_bool("my_flag", False)
    assert await store.get_bool("my_flag") is False


@pytest.mark.asyncio
async def test_bot_state_get_bool_missing_key_default_true(in_memory_db):
    store = BotStateStore()
    result = await store.get_bool("nonexistent_key", default=True)
    assert result is True


@pytest.mark.asyncio
async def test_bot_state_get_bool_missing_key_default_false(in_memory_db):
    store = BotStateStore()
    result = await store.get_bool("nonexistent_key", default=False)
    assert result is False


@pytest.mark.asyncio
async def test_bot_state_get_bool_bare_default_is_false(in_memory_db):
    """Regression for TODO #6: bare get_bool() should default to False, not True."""
    store = BotStateStore()
    # No explicit default — must return False on a fresh DB
    result = await store.get_bool("test_mode_active")
    assert result is False, (
        "BotStateStore.get_bool bare default should be False (see TODO #6). "
        "If this fails, the bug has NOT been fixed yet."
    )


@pytest.mark.asyncio
async def test_bot_state_overwrite(in_memory_db):
    store = BotStateStore()
    await store.set_bool("flag", True)
    await store.set_bool("flag", False)
    assert await store.get_bool("flag") is False


@pytest.mark.asyncio
async def test_bot_state_set_and_get_str(in_memory_db):
    store = BotStateStore()
    await store.set_str("test_email", "admin@example.com")
    assert await store.get_str("test_email") == "admin@example.com"


@pytest.mark.asyncio
async def test_bot_state_get_str_missing_returns_default(in_memory_db):
    store = BotStateStore()
    result = await store.get_str("nonexistent", default="fallback")
    assert result == "fallback"


@pytest.mark.asyncio
async def test_bot_state_snapshot(in_memory_db):
    store = BotStateStore()
    await store.set_bool("active", True)
    await store.set_str("email", "x@y.com")
    snap = await store.snapshot()
    assert snap["active"] == "1"
    assert snap["email"] == "x@y.com"


# ---------------------------------------------------------------------------
# EnabledChannelsStore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channels_add_and_list_production(in_memory_db):
    store = EnabledChannelsStore()
    await store.add("ch1", test_mode=False, label="news")
    channels = await store.list(test_mode=False)
    assert len(channels) == 1
    assert channels[0].channel_id == "ch1"
    assert channels[0].label == "news"
    assert channels[0].test_mode is False


@pytest.mark.asyncio
async def test_channels_add_and_list_test_mode(in_memory_db):
    store = EnabledChannelsStore()
    await store.add("ch1", test_mode=True, label="test-news")
    channels = await store.list(test_mode=True)
    assert len(channels) == 1
    assert channels[0].test_mode is True


@pytest.mark.asyncio
async def test_channels_list_filters_by_test_mode(in_memory_db):
    store = EnabledChannelsStore()
    await store.add("ch1", test_mode=False, label="prod")
    await store.add("ch2", test_mode=True, label="test")

    prod = await store.list(test_mode=False)
    test = await store.list(test_mode=True)

    assert len(prod) == 1 and prod[0].channel_id == "ch1"
    assert len(test) == 1 and test[0].channel_id == "ch2"


@pytest.mark.asyncio
async def test_channels_add_same_key_updates_label(in_memory_db):
    store = EnabledChannelsStore()
    await store.add("ch1", test_mode=False, label="original")
    await store.add("ch1", test_mode=False, label="updated")
    channels = await store.list(test_mode=False)
    assert len(channels) == 1
    assert channels[0].label == "updated"


@pytest.mark.asyncio
async def test_channels_same_id_different_test_mode_independent(in_memory_db):
    """Same channel_id can exist in both production and test rows."""
    store = EnabledChannelsStore()
    await store.add("ch1", test_mode=False, label="prod-label")
    await store.add("ch1", test_mode=True, label="test-label")

    prod = await store.list(test_mode=False)
    test = await store.list(test_mode=True)

    assert prod[0].label == "prod-label"
    assert test[0].label == "test-label"


@pytest.mark.asyncio
async def test_channels_remove_returns_true_on_delete(in_memory_db):
    store = EnabledChannelsStore()
    await store.add("ch1", test_mode=False)
    deleted = await store.remove("ch1", test_mode=False)
    assert deleted is True


@pytest.mark.asyncio
async def test_channels_remove_returns_false_when_not_found(in_memory_db):
    store = EnabledChannelsStore()
    deleted = await store.remove("nonexistent_ch", test_mode=False)
    assert deleted is False


@pytest.mark.asyncio
async def test_channels_remove_does_not_affect_other_test_mode(in_memory_db):
    store = EnabledChannelsStore()
    await store.add("ch1", test_mode=False, label="prod")
    await store.add("ch1", test_mode=True, label="test")

    await store.remove("ch1", test_mode=False)

    prod = await store.list(test_mode=False)
    test = await store.list(test_mode=True)

    assert prod == []
    assert len(test) == 1


@pytest.mark.asyncio
async def test_channels_keywords_round_trip(in_memory_db):
    store = EnabledChannelsStore()
    keywords = ["human rights", "amnesty", "campaign"]
    await store.add("ch1", test_mode=False, label="news", classifier_keywords=keywords)
    channels = await store.list(test_mode=False)
    assert channels[0].classifier_keywords == keywords


@pytest.mark.asyncio
async def test_channels_get(in_memory_db):
    store = EnabledChannelsStore()
    await store.add("ch1", test_mode=False, label="news")
    ch = await store.get("ch1", test_mode=False)
    assert ch is not None
    assert ch.channel_id == "ch1"


@pytest.mark.asyncio
async def test_channels_get_missing_returns_none(in_memory_db):
    store = EnabledChannelsStore()
    ch = await store.get("missing", test_mode=False)
    assert ch is None


# ---------------------------------------------------------------------------
# EmailThreadMap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_thread_map_record_and_lookup_thread(in_memory_db):
    tmap = EmailThreadMap()
    await tmap.record(
        "msg-001@example.com",
        discord_thread_id="thread-111",
        discord_channel_id="channel-999",
        subject="Test Subject",
    )
    link = await tmap.lookup_thread("msg-001@example.com")
    assert link is not None
    assert link.message_id == "msg-001@example.com"
    assert link.discord_thread_id == "thread-111"
    assert link.discord_channel_id == "channel-999"
    assert link.subject == "Test Subject"


@pytest.mark.asyncio
async def test_email_thread_map_lookup_missing_returns_none(in_memory_db):
    tmap = EmailThreadMap()
    result = await tmap.lookup_thread("nonexistent@example.com")
    assert result is None


@pytest.mark.asyncio
async def test_email_thread_map_lookup_by_thread(in_memory_db):
    tmap = EmailThreadMap()
    await tmap.record(
        "msg-001@example.com",
        discord_thread_id="thread-111",
        discord_channel_id="channel-999",
    )
    await tmap.record(
        "msg-002@example.com",
        discord_thread_id="thread-111",
        discord_channel_id="channel-999",
    )
    await tmap.record(
        "msg-003@example.com",
        discord_thread_id="thread-222",
        discord_channel_id="channel-999",
    )
    links = await tmap.lookup_by_thread("thread-111")
    assert len(links) == 2
    msg_ids = {l.message_id for l in links}
    assert msg_ids == {"msg-001@example.com", "msg-002@example.com"}


@pytest.mark.asyncio
async def test_email_thread_map_lookup_by_thread_empty(in_memory_db):
    tmap = EmailThreadMap()
    links = await tmap.lookup_by_thread("nonexistent-thread")
    assert links == []


@pytest.mark.asyncio
async def test_email_thread_map_record_upsert(in_memory_db):
    """Re-recording the same message_id updates the row."""
    tmap = EmailThreadMap()
    await tmap.record(
        "msg-001@example.com",
        discord_thread_id="thread-111",
        discord_channel_id="channel-999",
        subject="Original",
    )
    await tmap.record(
        "msg-001@example.com",
        discord_thread_id="thread-222",
        discord_channel_id="channel-888",
        subject="Updated",
    )
    link = await tmap.lookup_thread("msg-001@example.com")
    assert link is not None
    assert link.discord_thread_id == "thread-222"
    assert link.subject == "Updated"
