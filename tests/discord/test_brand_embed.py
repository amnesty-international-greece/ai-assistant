"""Tests for ``src.integrations.discord.brand``.

Covers the v2 (2026-05-27) additions: thumbnail / image / flame_bars
parameters on ``brand_embed()`` plus the three convenience builders
(``status_embed``, ``event_live_embed``, ``stats_embed``).  Also pins
backwards compatibility - old call-sites that pass only the legacy kwargs
must keep producing identical embeds.
"""
from __future__ import annotations

from src.integrations.discord.brand import (
    AMNESTY_YELLOW,
    CANDLE_THUMBNAIL_URL,
    brand_embed,
    event_live_embed,
    flame_bar,
    stats_embed,
    status_embed,
)


# ── Backwards compatibility ──────────────────────────────────────────────────


def test_brand_embed_default_keeps_legacy_shape():
    """Existing call-sites that pass only title/description/color must work."""
    e = brand_embed(title="Hello", description="World")
    assert e.title == "Hello"
    assert e.description == "World"
    assert e.color == AMNESTY_YELLOW
    assert e.footer.text == "Διεθνής Αμνηστία - Ελληνικό Τμήμα"
    # No thumbnail / image set
    assert e.thumbnail.url is None
    assert e.image.url is None


def test_brand_embed_footer_none_suppresses_footer():
    e = brand_embed(title="x", footer=None)
    assert e.footer.text is None


# ── v2 additions: thumbnail / image / author ────────────────────────────────


def test_brand_embed_candle_thumbnail_sentinel():
    """thumbnail_url='candle' must expand to the canonical candle URL."""
    e = brand_embed(title="x", thumbnail_url="candle")
    assert e.thumbnail.url == CANDLE_THUMBNAIL_URL


def test_brand_embed_explicit_thumbnail_url_passthrough():
    e = brand_embed(title="x", thumbnail_url="https://example.com/t.png")
    assert e.thumbnail.url == "https://example.com/t.png"


def test_brand_embed_image_url_sets_full_width_image():
    e = brand_embed(title="x", image_url="https://example.com/hero.jpg")
    assert e.image.url == "https://example.com/hero.jpg"


def test_brand_embed_author_and_icon():
    e = brand_embed(
        title="x",
        author="Κατάσταση πλατφόρμας",
        author_icon="https://example.com/icon.png",
    )
    assert e.author.name == "Κατάσταση πλατφόρμας"
    assert e.author.icon_url == "https://example.com/icon.png"


# ── Flame bars ──────────────────────────────────────────────────────────────


def test_flame_bar_basic_layout():
    out = flame_bar({"#α": 100, "#β": 50, "#γ": 0})
    assert out.startswith("```\n")
    assert out.endswith("\n```")
    lines = out.split("\n")[1:-1]
    assert len(lines) == 3
    # Leader is full-width
    assert "█" * 22 in lines[0]
    # Last bar is fully empty (value=0)
    assert "░" * 22 in lines[2]


def test_flame_bar_preserves_dict_insertion_order():
    """Bars render in the order the caller supplied - never re-sorted."""
    out = flame_bar([("zzz", 10), ("aaa", 100), ("mmm", 50)])
    lines = [ln for ln in out.split("\n") if ln and not ln.startswith("```")]
    assert lines[0].startswith("zzz")
    assert lines[1].startswith("aaa")
    assert lines[2].startswith("mmm")


def test_flame_bar_empty_input_returns_empty_string():
    assert flame_bar({}) == ""
    assert flame_bar([]) == ""


def test_brand_embed_with_flame_bars_appends_to_description():
    e = brand_embed(
        title="x",
        description="Top channels",
        flame_bars={"#α": 100, "#β": 30},
    )
    assert e.description is not None
    assert "Top channels" in e.description
    assert "```" in e.description
    assert "█" in e.description


def test_brand_embed_flame_bars_without_existing_description():
    e = brand_embed(title="x", flame_bars={"#α": 5})
    assert e.description is not None
    assert e.description.startswith("```")


# ── Convenience builders ─────────────────────────────────────────────────────


def test_status_embed_has_candle_thumbnail():
    e = status_embed(description="Bot ενεργό")
    assert e.thumbnail.url == CANDLE_THUMBNAIL_URL
    assert e.title == "ΟΛΑ ΣΕ ΛΕΙΤΟΥΡΓΙΑ"
    assert e.author.name == "Κατάσταση πλατφόρμας"


def test_status_embed_accepts_fields():
    e = status_embed(
        title="ΕΛΕΓΧΟΣ",
        fields=[("Channels", "14", True), ("Users", "9", True)],
    )
    assert len(e.fields) == 2
    assert e.fields[0].name == "Channels"
    assert e.fields[1].value == "9"


def test_event_live_embed_sets_image_and_author():
    e = event_live_embed(
        title="Συγκέντρωση",
        description="Πλατεία Συντάγματος",
        image_url="https://example.com/hero.jpg",
        url="https://example.com/event",
    )
    assert e.image.url == "https://example.com/hero.jpg"
    assert e.url == "https://example.com/event"
    assert e.author.name.startswith("📣")


def test_stats_embed_renders_bars_in_description():
    e = stats_embed(
        title="2.418 ΣΥΜΒΑΝΤΑ",
        range_label="Last 30 days",
        bars={"#γενικά": 812, "#πρωτόκολλο": 521},
    )
    assert e.title == "2.418 ΣΥΜΒΑΝΤΑ"
    assert "Last 30 days" in e.author.name
    assert "█" in (e.description or "")
    assert "#γενικά" in (e.description or "")
