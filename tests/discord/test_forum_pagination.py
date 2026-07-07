"""Tests for ``ChannelTableView`` pagination (forum cog).

Pinning the slicing invariants so the 'pagination TODO' replacement at
forum.py doesn't quietly regress.  The view itself is exercised by mocking
out the EnabledChannelsStore - we only care about the page-math here, not
the Discord interaction wiring.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.integrations.discord.cogs.forum import (
    ChannelTableView,
    _CHANNELS_PAGE_SIZE,
)


@dataclass
class _FakeRow:
    channel_id: str
    label: str
    classifier_keywords: list[str]
    forum_tag_ids: list[str]


def _make_rows(n: int) -> list[_FakeRow]:
    return [
        _FakeRow(
            channel_id=str(1000 + i),
            label=f"label-{i}",
            classifier_keywords=[],
            forum_tag_ids=[],
        )
        for i in range(n)
    ]


def test_page_size_is_ten() -> None:
    """Page size is fixed at 10 - under Discord's 25-field embed cap, and
    comfortably under the Select-options cap (25) when used as fallback."""
    assert _CHANNELS_PAGE_SIZE == 10


def test_single_page_when_under_page_size() -> None:
    """≤ page_size rows ⇒ one page total."""
    view = ChannelTableView(cog=object())  # cog not touched by _slice
    page_rows, total = view._slice(_make_rows(7))
    assert total == 1
    assert len(page_rows) == 7


def test_exactly_one_full_page() -> None:
    """page_size rows ⇒ still exactly one page (not two)."""
    view = ChannelTableView(cog=object())
    page_rows, total = view._slice(_make_rows(_CHANNELS_PAGE_SIZE))
    assert total == 1
    assert len(page_rows) == _CHANNELS_PAGE_SIZE


def test_two_pages_when_one_over() -> None:
    view = ChannelTableView(cog=object())
    rows = _make_rows(_CHANNELS_PAGE_SIZE + 1)
    page0, total = view._slice(rows)
    assert total == 2
    assert len(page0) == _CHANNELS_PAGE_SIZE
    # Second page
    view.page = 1
    page1, _ = view._slice(rows)
    assert len(page1) == 1
    assert page1[0].channel_id == rows[-1].channel_id


def test_page_clamps_when_overshoots() -> None:
    """If the caller is on page 5 but rows shrank to fit one page, clamp
    to the last valid page rather than returning an empty slice."""
    view = ChannelTableView(cog=object(), page=5)
    page_rows, total = view._slice(_make_rows(3))
    assert total == 1
    assert view.page == 0  # clamped
    assert len(page_rows) == 3


def test_empty_rows_clamp_to_page_zero() -> None:
    view = ChannelTableView(cog=object(), page=3)
    page_rows, total = view._slice([])
    assert total == 1
    assert view.page == 0
    assert page_rows == []
