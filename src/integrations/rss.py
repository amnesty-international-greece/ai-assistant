"""RSS / Atom feed fetcher + item normalizer (replaces MonitoRSS).

Used by :mod:`src.integrations.discord.cogs.rss_feeds` to poll configured
feeds and surface new items into Discord channels.

Why a separate module from the Discord cog
==========================================
Keeps the parser pure: it knows nothing about Discord, channels, or
routing.  The cog handles those.  This file is unit-testable without any
async/Discord imports - fetch a feed, get a list of :class:`FeedItem`
dataclasses back.

Why feedparser
==============
Most battle-tested RSS/Atom parser in Python (used by everything from
podcatchers to Plone).  Handles:
- RSS 0.91 / 0.92 / 1.0 / 2.0, Atom 0.3 / 1.0
- Missing or malformed timestamps (silently - we get a partial result
  rather than an exception)
- Custom namespaces (``dc:creator``, ``media:thumbnail``, ...)
- Sanitizer fixups for common HTML quirks

Dedup
=====
We rely on the per-item ``guid`` (or ``link`` as a fallback for feeds
that omit guid).  The poll loop in the Discord cog stores the
NEWEST-GUID-EVER-POSTED per feed in ``rss_feeds.last_seen_guid``; on the
next poll it walks down the new items in chronological order and stops
at that guid.  That gives us "post everything new since last time" with
constant DB writes regardless of feed size.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable

import feedparser
import httpx

logger = logging.getLogger(__name__)


# ── Data class ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FeedItem:
    """Normalized representation of one feed item.

    Frozen + slotted so it's hashable + small.  All fields are populated
    even if the source feed leaves them empty - defaults make downstream
    code safe to dereference without None-checks.
    """
    guid: str
    title: str
    link: str
    description_html: str
    description_plain: str   # HTML stripped, whitespace collapsed
    published_at: datetime | None    # None if the feed omits or malforms the date
    thumbnail_url: str       # first <img src=...> from description, or empty
    author: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


# ── Public API ──────────────────────────────────────────────────────────────


async def fetch_feed(
    feed_url: str,
    *,
    timeout: float = 15.0,
    max_items: int = 50,
) -> list[FeedItem]:
    """Fetch and parse a feed.  Returns items NEWEST-FIRST.

    Uses ``httpx`` for the actual HTTP fetch (so we can set custom headers
    and run from inside async contexts cleanly), then hands the bytes to
    ``feedparser`` for parsing.  Failures are logged and surface as an
    empty list rather than raising - the poll loop should never crash on
    a transient feed outage.

    Args:
        feed_url: Absolute URL of the feed.
        timeout: HTTP timeout in seconds (default 15s).
        max_items: Cap on returned items (avoids loading 1000-item archive
                   dumps into memory if a feed is misbehaving).

    Returns:
        List of :class:`FeedItem`, newest first.  Empty list on failure.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(
                feed_url,
                headers={"User-Agent": "AI-Assistant-Bot/1.0 (Amnesty International Greece)"},
            )
            resp.raise_for_status()
            raw = resp.content
    except Exception as exc:
        logger.warning("RSS fetch failed for %s: %s", feed_url, exc)
        return []

    return parse_feed_bytes(raw, max_items=max_items)


def parse_feed_bytes(raw: bytes, *, max_items: int = 50) -> list[FeedItem]:
    """Parse already-fetched feed bytes.  Exposed for unit tests."""
    parsed = feedparser.parse(raw)
    if parsed.bozo and not parsed.entries:
        # bozo=1 = parser hit a malformed input; if there are still entries,
        # feedparser recovered enough to use them.  Empty entries + bozo
        # means the feed is unusable.
        logger.warning("RSS parser bozo with no recoverable entries: %s",
                       getattr(parsed, "bozo_exception", ""))
        return []

    items: list[FeedItem] = []
    for entry in parsed.entries[:max_items]:
        try:
            items.append(_normalize_entry(entry))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Skipping malformed feed entry: %s", exc)
    return items


# ── Normalization helpers ────────────────────────────────────────────────────


_TAG_STRIPPER = re.compile(r"<[^>]+>")
_FIRST_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")
# House style: no em/en/figure dashes or middot in reposted content - flatten
# them (and decoded &mdash;/&ndash; entities) to a plain ASCII hyphen. Keyed by
# code point so this source file stays free of the characters it strips.
_DASH_FLATTEN = {cp: "-" for cp in (0x2010, 0x2011, 0x2012, 0x2013, 0x2014,
                                    0x2015, 0x2212, 0x00B7, 0x0387)}


def strip_html(html: str) -> str:
    """Best-effort HTML → plain text.

    Not a full HTML parser - for that we'd need bs4 or lxml.  We just remove
    tags and collapse whitespace, which is plenty for embed description
    rendering.  Unescape common entities the lazy way (Python's html module).
    """
    import html as _html
    if not html:
        return ""
    no_tags = _TAG_STRIPPER.sub(" ", html)
    decoded = _html.unescape(no_tags).translate(_DASH_FLATTEN)
    return _WHITESPACE.sub(" ", decoded).strip()


def extract_first_image(html: str) -> str:
    """Pull the URL of the first ``<img>`` from an HTML fragment, or ""."""
    if not html:
        return ""
    m = _FIRST_IMG_RE.search(html)
    return m.group(1) if m else ""


def _normalize_entry(entry: dict) -> FeedItem:
    """Translate a feedparser entry dict into our FeedItem dataclass."""
    # GUID: feedparser exposes it as `id` (Atom) or `guid` (RSS) under the
    # unified `id` key.  Fall back to link if id is missing.
    guid = (entry.get("id") or entry.get("guid") or entry.get("link") or "").strip()

    title = (entry.get("title") or "").strip()
    link = (entry.get("link") or "").strip()

    # Description / content / summary - RSS uses description, Atom uses
    # content[].value or summary.  Take the first non-empty source.
    description_html = ""
    if entry.get("content"):
        try:
            description_html = entry["content"][0].get("value", "") or ""
        except (IndexError, AttributeError):
            pass
    if not description_html:
        description_html = (
            entry.get("description") or entry.get("summary") or ""
        )

    description_plain = strip_html(description_html)

    # Published date - feedparser parses to a struct_time in
    # entry.published_parsed; convert to UTC datetime.
    published_at: datetime | None = None
    raw_date = entry.get("published") or entry.get("updated") or ""
    parsed_tuple = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed_tuple:
        try:
            # struct_time is treated as UTC by feedparser convention
            published_at = datetime(*parsed_tuple[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    if published_at is None and raw_date:
        # Fallback: try parsing the raw RFC 2822 string ourselves
        try:
            published_at = parsedate_to_datetime(raw_date)
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            published_at = None

    thumbnail_url = extract_first_image(description_html)
    # feedparser also exposes media:thumbnail at entry.media_thumbnail[0]['url']
    # - prefer that when present (publisher-curated thumbnail).
    if not thumbnail_url:
        media_thumb = entry.get("media_thumbnail") or []
        if media_thumb:
            try:
                thumbnail_url = media_thumb[0].get("url", "") or ""
            except (AttributeError, IndexError):
                pass

    author = (entry.get("author") or entry.get("dc_creator") or "").strip()

    # Tags / categories - feedparser exposes them as entry.tags = list of
    # {term, scheme, label} dicts.
    tags_raw = entry.get("tags") or []
    tags = tuple(
        (t.get("term") if isinstance(t, dict) else str(t)).strip()
        for t in tags_raw
        if t
    )

    return FeedItem(
        guid=guid,
        title=title,
        link=link,
        description_html=description_html,
        description_plain=description_plain,
        published_at=published_at,
        thumbnail_url=thumbnail_url,
        author=author,
        tags=tags,
    )


# ── Routing helpers ─────────────────────────────────────────────────────────


def filter_new_items(
    items: Iterable[FeedItem],
    *,
    last_seen_guid: str | None,
) -> list[FeedItem]:
    """Return the items NEWER than ``last_seen_guid``.

    Walks the (newest-first) list and stops at the first item whose guid
    matches ``last_seen_guid``.  If the cursor is missing (first poll
    ever), returns at most the most recent N items so we don't dump the
    entire archive - a 1-item bootstrap is usually right but we leave
    that to the caller (use ``items[:1]`` after this returns).
    """
    new_items: list[FeedItem] = []
    for item in items:
        if last_seen_guid and item.guid == last_seen_guid:
            break
        new_items.append(item)
    return new_items


def item_matches_route(
    item: FeedItem,
    *,
    url_pattern: str | None,
    title_pattern: str | None,
) -> bool:
    """Evaluate a route's filters against an item.

    Both filters are optional.  When BOTH are set, both must match (AND).
    When NEITHER is set, the route is a wildcard - every item matches.

    Args:
        url_pattern: Substring that must appear in ``item.link`` (NOT regex -
            URL patterns are typically literal path fragments like
            "/news/events/"; substring match is faster and less surprising
            than regex for callers who don't know they're configuring one).
        title_pattern: Regex matched against ``item.title`` (case-insensitive).
            Use this for content-based routing when URL paths don't
            distinguish categories.
    """
    if url_pattern:
        if url_pattern not in (item.link or ""):
            return False
    if title_pattern:
        try:
            if not re.search(title_pattern, item.title or "", re.IGNORECASE):
                return False
        except re.error:
            # Bad regex - fail open (admin sees the misconfiguration in logs)
            logger.warning("Invalid title_pattern regex: %r", title_pattern)
            return False
    return True
