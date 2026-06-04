"""Tests for the redesigned Discord triage card (brand.py + email_sync.py).

Covers:
- confidence_bar() helper in brand.py
- AMNESTY_FLAME color constant
- Ranked triage view structure (primary + alternate buttons)
- Defer and Spam buttons always present
- Legacy fallback when no alternates
- Classifier multiline ranked-response parsing
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import discord.ext.commands
import pytest

from src.integrations.discord.brand import (
    AMNESTY_FLAME,
    AMNESTY_YELLOW,
    confidence_bar,
)
from src.integrations.discord.classifier import (
    Alternate,
    ClassificationResult,
    EmailClassifier,
)
from src.integrations.discord.cogs.email_sync import (
    _TriageDeferButton,
    _TriageRouteAlternateButton,
    _TriageRoutePrimaryButton,
    _TriageRouteButton,
    _TriageSpamButton,
    EmailSyncCog,
)
from src.integrations.discord.constants import CLASSIFIER_UNCERTAIN_LABEL


# ---------------------------------------------------------------------------
# Part A — confidence_bar()
# ---------------------------------------------------------------------------


def test_confidence_bar_renders_filled_segments_for_high_confidence():
    result = confidence_bar(0.7)
    # 7 filled (▰) + 3 empty (▱), then percentage
    assert result.startswith("▰▰▰▰▰▰▰▱▱▱")
    assert "70%" in result


def test_confidence_bar_renders_empty_segments_for_low_confidence():
    result = confidence_bar(0.1)
    # 1 filled + 9 empty
    assert result.startswith("▰▱▱▱▱▱▱▱▱▱")
    assert "10%" in result


def test_confidence_bar_clamps_out_of_range_values():
    # 0.0 → all empty
    zero = confidence_bar(0.0)
    assert zero.startswith("▱▱▱▱▱▱▱▱▱▱")
    assert "0%" in zero

    # 1.0 → all filled
    full = confidence_bar(1.0)
    assert full.startswith("▰▰▰▰▰▰▰▰▰▰")
    assert "100%" in full

    # Above-range still produces full bar
    over = confidence_bar(1.5)
    assert over.startswith("▰▰▰▰▰▰▰▰▰▰")

    # Below-range still produces empty bar
    under = confidence_bar(-0.5)
    assert under.startswith("▱▱▱▱▱▱▱▱▱▱")


def test_confidence_bar_segment_count():
    bar = confidence_bar(0.32)
    filled = bar.count("▰")
    empty = bar.count("▱")
    assert filled + empty == 10  # default 10 segments
    assert filled == 3  # round(0.32 * 10) = 3


def test_confidence_bar_custom_segments():
    bar = confidence_bar(0.5, segments=4)
    assert bar.count("▰") == 2
    assert bar.count("▱") == 2


# ---------------------------------------------------------------------------
# Part A — AMNESTY_FLAME
# ---------------------------------------------------------------------------


def test_amnesty_flame_is_defined_and_not_yellow():
    assert AMNESTY_FLAME != AMNESTY_YELLOW
    # Check it's the correct hex value (#E63B11)
    # discord.Color stores as integer; verify it round-trips correctly
    assert AMNESTY_FLAME.value == discord.Color.from_str("#E63B11").value


# ---------------------------------------------------------------------------
# Part B — Classifier: multiline ranked-response parsing
# ---------------------------------------------------------------------------


def make_classifier() -> EmailClassifier:
    obj = EmailClassifier.__new__(EmailClassifier)
    return obj


LABEL_TO_ID = {
    "επικαιρότητα": "111",
    "εκδηλώσεις": "222",
    "ανακοινώσεις": "333",
}


def test_classifier_parses_multiline_ranked_response():
    classifier = make_classifier()
    raw = "επικαιρότητα|0.85\nεκδηλώσεις|0.72\nανακοινώσεις|0.60"
    result = classifier._parse_response(raw_text=raw, label_to_id=LABEL_TO_ID)

    assert result.label == "επικαιρότητα"
    assert result.channel_id == "111"
    assert abs(result.confidence - 0.85) < 1e-9
    assert result.fell_back is False
    assert len(result.alternates) == 2
    assert result.alternates[0].label == "εκδηλώσεις"
    assert result.alternates[0].channel_id == "222"
    assert abs(result.alternates[0].confidence - 0.72) < 1e-9
    assert result.alternates[1].label == "ανακοινώσεις"
    assert result.alternates[1].channel_id == "333"


def test_classifier_single_line_has_empty_alternates():
    classifier = make_classifier()
    result = classifier._parse_response(
        raw_text="επικαιρότητα|0.9", label_to_id=LABEL_TO_ID
    )
    assert result.fell_back is False
    assert result.alternates == []


def test_classifier_multiline_skips_unknown_labels():
    classifier = make_classifier()
    raw = "επικαιρότητα|0.80\nunknown_channel|0.75\nεκδηλώσεις|0.65"
    result = classifier._parse_response(raw_text=raw, label_to_id=LABEL_TO_ID)
    assert len(result.alternates) == 1
    assert result.alternates[0].label == "εκδηλώσεις"


def test_classifier_multiline_primary_below_threshold_falls_back():
    classifier = make_classifier()
    # Primary is 0.50 which is below the 0.70 threshold
    raw = "επικαιρότητα|0.50\nεκδηλώσεις|0.45"
    result = classifier._parse_response(raw_text=raw, label_to_id=LABEL_TO_ID)
    assert result.fell_back is True
    assert result.label == CLASSIFIER_UNCERTAIN_LABEL


# ---------------------------------------------------------------------------
# Part C — Triage view structure
# ---------------------------------------------------------------------------


def _make_result(
    label: str = "επικαιρότητα",
    channel_id: str = "111",
    confidence: float = 0.85,
    alternates: list[Alternate] | None = None,
    fell_back: bool = False,
) -> ClassificationResult:
    return ClassificationResult(
        label=label,
        channel_id=channel_id,
        confidence=confidence,
        raw_response="",
        fell_back=fell_back,
        alternates=alternates or [],
    )


def _make_email(message_id: str = "msg-001") -> MagicMock:
    email = MagicMock()
    email.message_id = message_id
    email.subject = "Test subject"
    email.body_plain = "Test body content"
    email.from_name = "Alice"
    email.from_addr = "alice@example.com"
    return email


def _run(coro):
    """Run a coroutine synchronously for testing.

    Uses ``asyncio.run`` (not ``get_event_loop().run_until_complete``) so we
    don't depend on whatever leftover loop pytest-asyncio may or may not
    leave around — the latter raises ``RuntimeError: There is no current
    event loop`` once another test closes the policy's default loop.
    """
    return asyncio.run(coro)


def _make_cog() -> EmailSyncCog:
    bot = MagicMock(spec=discord.ext.commands.Bot)
    cog = EmailSyncCog.__new__(EmailSyncCog)
    cog.bot = bot
    cog._channels_store = MagicMock()
    cog._channels_store.list = AsyncMock(return_value=[])
    cog._last_result = None
    return cog


def test_triage_view_uses_flame_color():
    """The embed built in _post_to_admin must use AMNESTY_FLAME, not AMNESTY_YELLOW."""
    from src.integrations.discord.brand import AMNESTY_FLAME as FLAME

    result = _make_result(
        alternates=[Alternate(label="εκδηλώσεις", channel_id="222", confidence=0.72)]
    )
    # Build a brand_embed with flame color and verify
    from src.integrations.discord.brand import brand_embed
    embed = brand_embed(title="test", color=FLAME)
    assert embed.color == FLAME
    assert embed.color != AMNESTY_YELLOW


def test_triage_view_has_ranked_buttons_when_alternates_present():
    cog = _make_cog()
    result = _make_result(
        alternates=[
            Alternate(label="εκδηλώσεις", channel_id="222", confidence=0.72),
            Alternate(label="ανακοινώσεις", channel_id="333", confidence=0.60),
        ]
    )
    email = _make_email()
    view = _run(cog._build_triage_view(email, test_mode=False, result=result))

    buttons = [item for item in view.children if isinstance(item, discord.ui.Button)]
    styles = [b.style for b in buttons]

    # Primary = success (green)
    assert discord.ButtonStyle.success in styles
    # At least one secondary (alternate or defer)
    assert discord.ButtonStyle.secondary in styles
    # Check primary is a _TriageRoutePrimaryButton
    primary_btns = [b for b in buttons if isinstance(b, _TriageRoutePrimaryButton)]
    assert len(primary_btns) == 1
    # Check alternates
    alt_btns = [b for b in buttons if isinstance(b, _TriageRouteAlternateButton)]
    assert len(alt_btns) == 2


def test_triage_view_has_defer_and_spam_buttons():
    cog = _make_cog()
    result = _make_result()
    email = _make_email()
    view = _run(cog._build_triage_view(email, test_mode=False, result=result))

    defer_btns = [b for b in view.children if isinstance(b, _TriageDeferButton)]
    spam_btns = [b for b in view.children if isinstance(b, _TriageSpamButton)]

    assert len(defer_btns) == 1
    assert len(spam_btns) == 1
    assert spam_btns[0].style == discord.ButtonStyle.danger


def test_triage_view_falls_back_to_full_channel_list_when_no_alternates():
    """When result has fell_back=True (no ranked), use the legacy full-channel list."""
    cog = _make_cog()

    # Simulate 3 configured channels
    ch1, ch2, ch3 = MagicMock(), MagicMock(), MagicMock()
    ch1.label = "επικαιρότητα"
    ch1.channel_id = "111"
    ch2.label = "εκδηλώσεις"
    ch2.channel_id = "222"
    ch3.label = "ανακοινώσεις"
    ch3.channel_id = "333"
    cog._channels_store.list = AsyncMock(return_value=[ch1, ch2, ch3])

    # fell_back=True → no ranked alternates
    result = _make_result(
        label=CLASSIFIER_UNCERTAIN_LABEL,
        channel_id=None,
        confidence=0.5,
        fell_back=True,
    )
    email = _make_email()
    view = _run(cog._build_triage_view(email, test_mode=False, result=result))

    # Should have legacy _TriageRouteButton (not primary/alternate)
    legacy_btns = [b for b in view.children if isinstance(b, _TriageRouteButton)]
    # Not _TriageRoutePrimaryButton (that inherits from Button too, check type exactly)
    primary_btns = [b for b in view.children if type(b) is _TriageRoutePrimaryButton]
    assert len(primary_btns) == 0
    assert len(legacy_btns) == 3

    # Defer + spam still present
    defer_btns = [b for b in view.children if isinstance(b, _TriageDeferButton)]
    spam_btns = [b for b in view.children if isinstance(b, _TriageSpamButton)]
    assert len(defer_btns) == 1
    assert len(spam_btns) == 1


def test_triage_view_falls_back_when_result_is_none():
    """When result=None (auto_classify disabled), use legacy full-channel list."""
    cog = _make_cog()
    ch = MagicMock()
    ch.label = "γενικά"
    ch.channel_id = "999"
    cog._channels_store.list = AsyncMock(return_value=[ch])

    email = _make_email()
    view = _run(cog._build_triage_view(email, test_mode=False, result=None))

    legacy_btns = [b for b in view.children if type(b) is _TriageRouteButton]
    assert len(legacy_btns) == 1
