"""Amnesty brand constants + helpers used across every Discord-side embed.

Single source of truth for the 3-colour Amnesty palette (yellow / black / white)
and for resolving brand assets at runtime.  Every embed in every cog should
import its color from here so a future palette change is one edit.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import discord

logger = logging.getLogger(__name__)

# ── Palette ──────────────────────────────────────────────────────────────────

AMNESTY_YELLOW = discord.Color.from_str("#FFFF00")
AMNESTY_BLACK = discord.Color.from_str("#000000")
AMNESTY_WHITE = discord.Color.from_str("#FFFFFF")

# Flame-orange accent — the one place we break the unified yellow.
# Reserved for triage embeds ("needs human attention now") and data-viz
# gradient endpoints.  Never use for routine chrome.
AMNESTY_FLAME = discord.Color.from_str("#E63B11")

# Convenience aliases — meaning-based; both map to the same yellow per the
# user's "unified Amnesty-yellow across all embeds" preference.
BRAND_PRIMARY = AMNESTY_YELLOW
BRAND_NEUTRAL = AMNESTY_BLACK


# ── Asset paths ──────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
BRAND_DIR = _PROJECT_ROOT / "brand"

# 1284×1284 yellow candle on transparent/white background — used for the bot
# avatar and as the source for the :amnesty: app emoji.
CANDLE_YELLOW_PNG = (
    BRAND_DIR
    / "Logo" / "Amnesty Candle" / "Amnesty_candle_RGB_Yellow"
    / "Amnesty_candle_RGB_Yellow.png"
)


# ── App-owned emojis (B2 phase) ──────────────────────────────────────────────
#
# Discord app-owned emojis don't require Nitro on either side and are usable
# anywhere the bot can send messages.  We upload them once on first boot via
# ``ensure_app_emojis()`` and then reference them by name from embeds.

APP_EMOJI_DEFINITIONS = {
    # name → source PNG path (must exist on disk)
    "amnesty": CANDLE_YELLOW_PNG,
}


async def ensure_app_emojis(bot: discord.Client) -> dict[str, discord.Emoji]:
    """Upload any missing app emojis defined in APP_EMOJI_DEFINITIONS.

    Idempotent: if an emoji with the target name already exists, we reuse it.
    Returns a dict ``{name: Emoji}`` for all successfully resolved emojis.

    Best-effort — failures are logged but never propagated.  The embeds we
    render fall back to plain text when an emoji is missing.
    """
    resolved: dict[str, discord.Emoji] = {}
    try:
        existing = {e.name: e for e in await bot.fetch_application_emojis()}
    except Exception as exc:  # pragma: no cover — Discord API hiccup
        logger.warning("ensure_app_emojis: could not list app emojis: %s", exc)
        return resolved

    for name, png_path in APP_EMOJI_DEFINITIONS.items():
        if name in existing:
            resolved[name] = existing[name]
            continue
        if not png_path.exists():
            logger.warning("ensure_app_emojis: source missing for %s: %s", name, png_path)
            continue
        try:
            with open(png_path, "rb") as f:
                emoji = await bot.create_application_emoji(name=name, image=f.read())
            resolved[name] = emoji
            logger.info("Uploaded app emoji :%s: (id=%s)", name, emoji.id)
        except Exception as exc:  # pragma: no cover — Discord API hiccup
            logger.warning("ensure_app_emojis: upload failed for %s: %s", name, exc)
    return resolved


def emoji_mention(emojis: dict[str, discord.Emoji], name: str, fallback: str = "") -> str:
    """Render an app emoji as ``<:name:id>`` for inline use in messages/embeds.

    Returns *fallback* (defaults to empty string) if the emoji isn't loaded.
    """
    e = emojis.get(name)
    return f"<:{e.name}:{e.id}>" if e is not None else fallback


# ── Discord timestamp helpers (<t:UNIX:STYLE>) ───────────────────────────────
#
# Discord auto-renders these to the viewer's locale & timezone.  Styles:
#   d  = short date         "26/05/2026"
#   D  = long date          "26 May 2026"
#   t  = short time         "14:30"
#   T  = long time          "14:30:00"
#   f  = short date+time    "26 May 2026 14:30"
#   F  = long date+time     "Tuesday, 26 May 2026 14:30"
#   R  = relative           "in 3 hours" (auto-updates per viewer)


def fmt_ts(dt: datetime, style: str = "F") -> str:
    """Render a datetime as a client-rendered ``<t:UNIX:STYLE>`` token."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:{style}>"


def fmt_ts_full_and_relative(dt: datetime) -> str:
    """Render ``<t:UNIX:F> (<t:UNIX:R>)`` — the most common pairing."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    unix = int(dt.timestamp())
    return f"<t:{unix}:F> (<t:{unix}:R>)"


# ── Flame palette (charts only — never for chrome) ──────────────────────────
#
# The style guide reserves the yellow → orange → red gradient for data viz.
# We use it exclusively in ``/stats``-style embeds via ``flame_bar=`` below.

FLAME_PALETTE_HEX = ("#FFE600", "#FFA000", "#E63B11")

# Public asset URL for the yellow candle — used as the default thumbnail on
# system / status embeds so the bot's voice carries a watermark.  Override
# by passing ``thumbnail_url=`` directly to :func:`brand_embed`.
CANDLE_THUMBNAIL_URL = (
    "https://www.amnesty.gr/sites/default/files/styles/thumbnail/public/candle.png"
)


# ── Confidence bar renderer ─────────────────────────────────────────────────


def confidence_bar(confidence: float, segments: int = 10) -> str:
    """Render a confidence value in [0.0, 1.0] as a Unicode block bar.

    Uses full-block ▰ (U+25B0) for filled segments and dashed ▱ (U+25B1)
    for empty segments.  The percentage is appended as a suffix.

    Examples::

        >>> confidence_bar(0.7)   # 7 filled, 3 empty
        '▰▰▰▰▰▰▰▱▱▱  70%'
        >>> confidence_bar(0.32)
        '▰▰▰▱▱▱▱▱▱▱  32%'

    Out-of-range values are clamped to ``[0, segments]``.
    """
    filled = max(0, min(segments, round(confidence * segments)))
    bar = "▰" * filled + "▱" * (segments - filled)
    pct = max(0.0, min(1.0, confidence))
    return f"{bar}  {pct:.0%}"


# ── Flame-gradient bar renderer ──────────────────────────────────────────────


def flame_bar(
    rows: list[tuple[str, int]] | dict[str, int],
    *,
    width: int = 22,
    max_value: int | None = None,
) -> str:
    r"""Render a flame-gradient horizontal bar chart for embed descriptions.

    Uses Unicode block characters (``█`` filled / ``░`` empty) inside a
    monospaced code block — readable in every Discord client without
    relying on ANSI rendering (which Discord-mobile still ignores in 2026).

    Args:
        rows:       Either an ordered list of ``(label, value)`` tuples, or
                    a dict (in which case insertion order is preserved).
        width:      Number of bar slots (default 22 — fits the embed's
                    natural width on mobile without wrapping).
        max_value:  Scale all bars against this max.  Defaults to the
                    largest value in ``rows`` (so the leader is full-width).

    Returns:
        A pre-formatted ``\`\`\`...\`\`\``` code block, ready to drop into
        the ``description=`` slot of a Discord embed (or assign with
        :meth:`discord.Embed.description` += flame_bar(...)).
    """
    items = list(rows.items()) if isinstance(rows, dict) else list(rows)
    if not items:
        return ""
    cap = max_value if max_value is not None else max(v for _, v in items) or 1
    label_w = max(len(str(label)) for label, _ in items)
    value_w = max(len(f"{v:,}".replace(",", ".")) for _, v in items)

    lines = []
    for label, value in items:
        filled = round(width * value / cap) if cap else 0
        filled = max(0, min(width, filled))
        bar = "█" * filled + "░" * (width - filled)
        v_str = f"{value:,}".replace(",", ".").rjust(value_w)
        lines.append(f"{str(label).ljust(label_w)}  {bar}  {v_str}")
    return "```\n" + "\n".join(lines) + "\n```"


# ── Embed base factory ───────────────────────────────────────────────────────


def brand_embed(
    *,
    title: str | None = None,
    description: str | None = None,
    color: discord.Color | None = None,
    url: str | None = None,
    timestamp: datetime | None = None,
    footer: str | None = "Διεθνής Αμνηστία — Ελληνικό Τμήμα",
    thumbnail_url: str | None = None,
    image_url: str | None = None,
    flame_bars: list[tuple[str, int]] | dict[str, int] | None = None,
    author: str | None = None,
    author_icon: str | None = None,
) -> discord.Embed:
    """Return a pre-configured Embed in the Amnesty palette.

    Defaults: AMNESTY_YELLOW color, our footer text.  Pass ``footer=None`` to
    suppress the footer entirely.  ``timestamp=None`` defaults to NOW.

    v2 additions (2026-05-27 — backwards compatible):
        thumbnail_url:  Small image top-right.  Pass the literal string
                        ``"candle"`` to use :data:`CANDLE_THUMBNAIL_URL`
                        (the yellow candle watermark) — recommended for
                        status / system embeds so the bot's voice is
                        signed visually.
        image_url:      Full-width hero image below the embed body — used
                        for event / live announcement embeds.
        flame_bars:     Either a dict ``{label: value}`` or list of
                        ``(label, value)`` tuples.  Renders a Unicode-block
                        flame-gradient bar chart and appends it to the
                        description.  Reserve for ``/stats`` and other
                        chart-style embeds — never for chrome.
        author:         Small line above the title.  ``author_icon`` sets
                        the round avatar next to it.
    """
    embed = discord.Embed(
        title=title,
        description=description,
        color=color or AMNESTY_YELLOW,
        url=url,
        timestamp=timestamp or datetime.now(timezone.utc),
    )
    if footer:
        embed.set_footer(text=footer)

    if author:
        embed.set_author(name=author, icon_url=author_icon)

    if thumbnail_url:
        resolved_thumb = (
            CANDLE_THUMBNAIL_URL if thumbnail_url == "candle" else thumbnail_url
        )
        embed.set_thumbnail(url=resolved_thumb)

    if image_url:
        embed.set_image(url=image_url)

    if flame_bars:
        bar_block = flame_bar(flame_bars)
        embed.description = (
            f"{embed.description}\n\n{bar_block}" if embed.description else bar_block
        )

    return embed


# ── Convenience builders for the three new registers ────────────────────────


def status_embed(
    *,
    title: str = "ΟΛΑ ΣΕ ΛΕΙΤΟΥΡΓΙΑ",
    description: str | None = None,
    fields: Iterable[tuple[str, str, bool]] | None = None,
) -> discord.Embed:
    """Status / health embed — candle watermark in the thumbnail slot.

    Use for ``/status``, scheduled health pings, "all-systems-go" replies.
    The candle in the top-right makes it instantly recognizable as a
    system message vs. a routine post.
    """
    embed = brand_embed(
        title=title,
        description=description,
        author="Κατάσταση πλατφόρμας",
        thumbnail_url="candle",
    )
    for name, value, inline in fields or []:
        embed.add_field(name=name, value=value, inline=inline)
    return embed


def event_live_embed(
    *,
    title: str,
    description: str,
    image_url: str,
    url: str | None = None,
    fields: Iterable[tuple[str, str, bool]] | None = None,
) -> discord.Embed:
    """Event / live-announcement embed — full-width hero image.

    Use for protest live-posts, public event announcements, anything that
    deserves a poster treatment rather than a system-message look.  The
    hero ``image_url`` must be a fully-qualified https URL (Discord can't
    render local files via :func:`set_image`).
    """
    embed = brand_embed(
        title=title,
        description=description,
        url=url,
        image_url=image_url,
        author="📣 LIVE τώρα",
    )
    for name, value, inline in fields or []:
        embed.add_field(name=name, value=value, inline=inline)
    return embed


def stats_embed(
    *,
    title: str,
    bars: list[tuple[str, int]] | dict[str, int],
    description: str | None = None,
    range_label: str | None = None,
) -> discord.Embed:
    """Stats / metrics embed — flame-gradient bar chart in the body.

    Use for ``/stats``, traffic dashboards, "top channels last 30 days"
    summaries.  The flame palette is reserved for this register — using
    it elsewhere dilutes its meaning.
    """
    return brand_embed(
        title=title,
        description=description,
        author=f"Στατιστικά γέφυρας · {range_label}" if range_label else "Στατιστικά γέφυρας",
        flame_bars=bars,
    )
