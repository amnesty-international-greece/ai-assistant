"""Tests for ``src.integrations.discord.stats.StatsStore``.

The 2026-05-27 production crash that motivated these:
    AttributeError: 'NoneType' object has no attribute 'isoformat'
    at stats.py:97 _build_time_filter

…happened when the /stats dashboard's range Select was set to "All time",
which passes ``since=None`` into ``store.summary()``.  The three regression
tests below pin the behaviour: ``since=None`` is a valid input that means
"no lower bound", and the two ``per_*`` queries that append additional
``AND`` clauses must still build valid SQL when no time/test filter is set.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.integrations.discord.stats import StatsStore


@pytest.mark.asyncio
async def test_summary_accepts_since_none(in_memory_db):
    """``since=None`` must NOT crash - it's the 'All time' branch."""
    store = StatsStore()
    await store.record(
        channel_id="c1", thread_id=None, direction="discord_post",
        classification=None, confidence=None, test_mode=False,
    )
    # Before the fix this raised AttributeError on None.isoformat()
    summary = await store.summary(since=None)
    assert summary.total == 1
    assert summary.discord_posts == 1


@pytest.mark.asyncio
async def test_per_channel_accepts_since_none(in_memory_db):
    """The per_channel query appends ``AND channel_id IS NOT NULL`` after
    the time-filter WHERE - must still produce valid SQL with no filter."""
    store = StatsStore()
    await store.record(
        channel_id="c1", thread_id=None, direction="discord_post",
        classification=None, confidence=None, test_mode=False,
    )
    rows = await store.per_channel(since=None)
    assert len(rows) == 1
    assert rows[0].channel_id == "c1"
    assert rows[0].count == 1


@pytest.mark.asyncio
async def test_per_classification_accepts_since_none(in_memory_db):
    """Same shape as per_channel - extra AND clause must compose."""
    store = StatsStore()
    await store.record(
        channel_id="c1", thread_id=None, direction="inbound_email",
        classification="admin", confidence=0.92, test_mode=False,
    )
    rows = await store.per_classification(since=None)
    assert len(rows) == 1
    assert rows[0].classification == "admin"
    assert rows[0].count == 1


@pytest.mark.asyncio
async def test_summary_with_since_still_filters(in_memory_db):
    """The lower-bound filter still works when ``since`` is provided."""
    store = StatsStore()
    await store.record(
        channel_id="c1", thread_id=None, direction="discord_post",
        classification=None, confidence=None, test_mode=False,
    )
    # since=now+1day → row was inserted before, should be excluded
    from datetime import timedelta
    future = datetime.now(timezone.utc) + timedelta(days=1)
    summary = await store.summary(since=future)
    assert summary.total == 0
