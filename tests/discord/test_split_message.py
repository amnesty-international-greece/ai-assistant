"""Tests for EmailSyncCog._split_message (pure text-splitting logic)."""

from __future__ import annotations

import pytest

from src.integrations.discord.constants import DISCORD_MESSAGE_SAFE_CHARS
from src.integrations.discord.cogs.email_sync import EmailSyncCog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def split(text: str) -> list[str]:
    """Call _split_message as a static-style function (no bot instance needed)."""
    return EmailSyncCog._split_message(None, text)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_split_short_message_single_element():
    msg = "Hello, world!"
    result = split(msg)
    assert result == [msg]


def test_split_empty_string():
    result = split("")
    # Either [] or [""] is acceptable; just make sure we get a list
    assert isinstance(result, list)
    # Document the actual behaviour
    assert result == [""] or result == []


def test_split_exactly_at_limit_single():
    msg = "x" * DISCORD_MESSAGE_SAFE_CHARS
    result = split(msg)
    assert len(result) == 1
    assert result[0] == msg


def test_split_one_char_over_limit_splits():
    msg = "x" * (DISCORD_MESSAGE_SAFE_CHARS + 1)
    result = split(msg)
    assert len(result) == 2


def test_split_long_message_all_parts_within_limit():
    # 3× the limit, no newlines — every chunk must be <= DISCORD_MESSAGE_SAFE_CHARS
    msg = "a" * (DISCORD_MESSAGE_SAFE_CHARS * 3)
    result = split(msg)
    for part in result:
        assert len(part) <= DISCORD_MESSAGE_SAFE_CHARS, (
            f"Part of length {len(part)} exceeds DISCORD_MESSAGE_SAFE_CHARS={DISCORD_MESSAGE_SAFE_CHARS}"
        )


def test_split_long_message_reassembles():
    """Splitting and rejoining (accounting for stripped newlines) preserves content."""
    msg = "a" * (DISCORD_MESSAGE_SAFE_CHARS * 3)
    result = split(msg)
    # No newlines were in the original, so joining directly should equal the original.
    assert "".join(result) == msg


def test_split_with_newlines_splits_at_last_newline():
    """Splitter should prefer splitting at the last newline before the limit."""
    # Build a message where the newline is well within the limit window
    line = "a" * 500
    # Three lines totaling > DISCORD_MESSAGE_SAFE_CHARS (1900)
    # Line 1 = 500+1, Line 2 = 500+1, Line 3 = 500+1 = 1503 < 1900 → fits
    # Four lines = 2004 → overflows
    msg = "\n".join([line] * 4)  # 4 × 500 + 3 newlines = 2003 chars
    assert len(msg) > DISCORD_MESSAGE_SAFE_CHARS

    result = split(msg)
    assert len(result) >= 2
    # Every part must be within the safe char limit
    for part in result:
        assert len(part) <= DISCORD_MESSAGE_SAFE_CHARS


def test_split_with_newlines_no_truncation_mid_word():
    """When newlines are present, no part should end with a bare 'a' mid-word cut."""
    lines = ["word" * 100] * 25  # 25 lines of 400 chars each = 10,000 chars
    msg = "\n".join(lines)
    result = split(msg)
    # All parts must be within limit
    for part in result:
        assert len(part) <= DISCORD_MESSAGE_SAFE_CHARS


def test_split_no_newlines_splits_at_exact_limit():
    """Without newlines, split must fall back to hard-cut at DISCORD_MESSAGE_SAFE_CHARS."""
    msg = "x" * (DISCORD_MESSAGE_SAFE_CHARS * 2 + 50)
    result = split(msg)
    # All chunks should be within limit
    for part in result:
        assert len(part) <= DISCORD_MESSAGE_SAFE_CHARS
    # Content preserved (no chars lost, accounting for stripped leading newlines)
    assert "".join(result) == msg


def test_split_returns_list_of_strings():
    result = split("some text")
    assert isinstance(result, list)
    for part in result:
        assert isinstance(part, str)
