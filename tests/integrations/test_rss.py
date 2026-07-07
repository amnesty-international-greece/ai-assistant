"""Tests for the RSS fetch/parse module + the storage helpers it relies on.

Covers (replacing MonitoRSS):
  • parse_feed_bytes - RSS 2.0 with namespaces, missing dates, missing guid
  • strip_html / extract_first_image
  • filter_new_items - dedup behaviour with and without cursor
  • item_matches_route - substring URL + regex title combinations
  • Storage round-trips: upsert / list / delete for feeds + routes
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fresh_db(tmp_path):
    """Point the audit module at a throwaway SQLite file for one test."""
    import src.core.audit as audit
    db_path = tmp_path / "rss_test.db"
    with patch.object(audit, "_DB_PATH", db_path), \
         patch.object(audit, "_CONNECTION", None):
        audit.init_db()
        yield


# Sample RSS payload - minimum viable RSS 2.0 with the same shape amnesty.gr
# emits.  Includes namespaces, custom dc:creator, embedded image in description.
# Defined as str + .encode() so the Greek characters survive (Python bytes
# literals only accept ASCII).
_SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel>
  <title>Amnesty Greece</title>
  <link>https://www.amnesty.gr</link>
  <description>Latest content</description>
  <item>
    <title>Drag: Η Τέχνη της Ορατότητας</title>
    <link>https://www.amnesty.gr/blog/30828/drag-i-tehni-tis-oratotitas</link>
    <description>&lt;p&gt;Body text here.&lt;/p&gt;&lt;img src="https://www.amnesty.gr/sites/default/files/hero.jpg" alt="x"/&gt;&lt;p&gt;More body.&lt;/p&gt;</description>
    <pubDate>Tue, 19 May 2026 10:46:40 +0000</pubDate>
    <dc:creator>activism</dc:creator>
    <guid isPermaLink="false">30828 at https://www.amnesty.gr</guid>
  </item>
  <item>
    <title>Πρόσφατο άρθρο</title>
    <link>https://www.amnesty.gr/news/articles/article/30300/title</link>
    <description>&lt;p&gt;Article body.&lt;/p&gt;</description>
    <pubDate>Mon, 18 May 2026 09:00:00 +0000</pubDate>
    <dc:creator>activism</dc:creator>
    <guid isPermaLink="false">30300 at https://www.amnesty.gr</guid>
  </item>
  <item>
    <title>Δελτίο Τύπου: Eurovision</title>
    <link>https://www.amnesty.gr/news/press/article/30801/eurovision-prodosia</link>
    <description>&lt;p&gt;Press release body.&lt;/p&gt;</description>
    <pubDate>Sun, 17 May 2026 08:00:00 +0000</pubDate>
    <guid isPermaLink="false">30801 at https://www.amnesty.gr</guid>
  </item>
</channel>
</rss>
""".encode("utf-8")


# ── Pure parser tests (no network) ──────────────────────────────────────────


def test_parse_feed_bytes_returns_normalized_items():
    from src.integrations.rss import parse_feed_bytes
    items = parse_feed_bytes(_SAMPLE_RSS)
    assert len(items) == 3
    first = items[0]
    assert first.title == "Drag: Η Τέχνη της Ορατότητας"
    assert first.link == "https://www.amnesty.gr/blog/30828/drag-i-tehni-tis-oratotitas"
    assert "30828" in first.guid
    assert first.thumbnail_url == "https://www.amnesty.gr/sites/default/files/hero.jpg"
    assert first.published_at is not None
    assert first.published_at.year == 2026
    assert first.author == "activism"


def test_parse_feed_bytes_strips_html_for_description_plain():
    from src.integrations.rss import parse_feed_bytes
    items = parse_feed_bytes(_SAMPLE_RSS)
    # First item description contained <p>, <img> - should be stripped clean
    plain = items[0].description_plain
    assert "<" not in plain and ">" not in plain
    assert "Body text here" in plain
    assert "More body" in plain


def test_parse_feed_bytes_handles_missing_guid():
    """Items without <guid> should fall back to link as the dedup key."""
    no_guid = _SAMPLE_RSS.replace(
        b'<guid isPermaLink="false">30801 at https://www.amnesty.gr</guid>',
        b"",
    )
    # Sanity: replacement actually happened (otherwise the test is meaningless)
    assert b"30801 at" not in no_guid
    from src.integrations.rss import parse_feed_bytes
    items = parse_feed_bytes(no_guid)
    # 3rd item now has empty guid → falls back to link
    assert items[2].guid == "https://www.amnesty.gr/news/press/article/30801/eurovision-prodosia"


def test_strip_html_decodes_entities():
    from src.integrations.rss import strip_html
    assert strip_html("<p>foo &amp; bar &mdash; baz</p>") == "foo & bar - baz"


def test_extract_first_image_picks_first_src():
    from src.integrations.rss import extract_first_image
    html = '<p>x</p><img src="https://a/1.jpg"/><img src="https://a/2.jpg"/>'
    assert extract_first_image(html) == "https://a/1.jpg"


def test_extract_first_image_returns_empty_when_no_img():
    from src.integrations.rss import extract_first_image
    assert extract_first_image("<p>just text</p>") == ""
    assert extract_first_image("") == ""


# ── Dedup logic ─────────────────────────────────────────────────────────────


def test_filter_new_items_returns_all_when_cursor_missing():
    from src.integrations.rss import filter_new_items, parse_feed_bytes
    items = parse_feed_bytes(_SAMPLE_RSS)
    new = filter_new_items(items, last_seen_guid=None)
    assert len(new) == 3


def test_filter_new_items_stops_at_cursor():
    """Items newer than the cursor are returned; the cursor item and older are skipped."""
    from src.integrations.rss import filter_new_items, parse_feed_bytes
    items = parse_feed_bytes(_SAMPLE_RSS)
    # Cursor = the 2nd-newest item; expect only the newest one back
    cursor = items[1].guid
    new = filter_new_items(items, last_seen_guid=cursor)
    assert len(new) == 1
    assert new[0].guid == items[0].guid


def test_filter_new_items_empty_when_cursor_matches_newest():
    from src.integrations.rss import filter_new_items, parse_feed_bytes
    items = parse_feed_bytes(_SAMPLE_RSS)
    new = filter_new_items(items, last_seen_guid=items[0].guid)
    assert new == []


# ── Routing logic ───────────────────────────────────────────────────────────


def test_item_matches_route_url_substring():
    from src.integrations.rss import item_matches_route, parse_feed_bytes
    items = parse_feed_bytes(_SAMPLE_RSS)
    # Article item - link contains /news/articles/
    article = items[1]
    assert item_matches_route(article, url_pattern="/news/articles/", title_pattern=None)
    assert not item_matches_route(article, url_pattern="/news/press/", title_pattern=None)


def test_item_matches_route_title_regex_case_insensitive():
    from src.integrations.rss import item_matches_route, parse_feed_bytes
    items = parse_feed_bytes(_SAMPLE_RSS)
    press = items[2]   # "Δελτίο Τύπου: Eurovision"
    assert item_matches_route(press, url_pattern=None, title_pattern=r"eurovision")
    assert not item_matches_route(press, url_pattern=None, title_pattern=r"unrelated")


def test_item_matches_route_wildcard_when_no_patterns():
    """When BOTH filters are empty/None, the route is a wildcard - match everything."""
    from src.integrations.rss import item_matches_route, parse_feed_bytes
    items = parse_feed_bytes(_SAMPLE_RSS)
    for item in items:
        assert item_matches_route(item, url_pattern=None, title_pattern=None)


def test_item_matches_route_url_and_title_are_AND():
    """When BOTH set, both must match."""
    from src.integrations.rss import item_matches_route, parse_feed_bytes
    items = parse_feed_bytes(_SAMPLE_RSS)
    press = items[2]
    # URL matches press, title matches eurovision → AND = True
    assert item_matches_route(press, url_pattern="/news/press/", title_pattern="eurovision")
    # URL matches press, title regex doesn't → AND = False
    assert not item_matches_route(press, url_pattern="/news/press/", title_pattern="article")


def test_item_matches_route_bad_regex_fails_closed():
    """Malformed title regex doesn't crash - just no match."""
    from src.integrations.rss import item_matches_route, parse_feed_bytes
    items = parse_feed_bytes(_SAMPLE_RSS)
    assert not item_matches_route(items[0], url_pattern=None, title_pattern="(unclosed")


# ── Storage helpers (round-trip) ────────────────────────────────────────────


def test_upsert_and_list_rss_feeds(fresh_db):
    from src.core.audit import upsert_rss_feed, list_rss_feeds
    upsert_rss_feed("https://example.com/rss.xml", label="Example")
    feeds = list_rss_feeds()
    assert len(feeds) == 1
    assert feeds[0]["label"] == "Example"
    assert feeds[0]["enabled"] == 1
    # Upsert updates without creating a duplicate
    upsert_rss_feed("https://example.com/rss.xml", label="Renamed")
    feeds = list_rss_feeds()
    assert len(feeds) == 1
    assert feeds[0]["label"] == "Renamed"


def test_delete_rss_feed_cascades_to_routes(fresh_db):
    from src.core.audit import (
        upsert_rss_feed, add_rss_route, delete_rss_feed,
        list_rss_feeds, list_rss_routes,
    )
    url = "https://example.com/rss.xml"
    upsert_rss_feed(url)
    add_rss_route(url, channel_id="111", url_pattern="/foo/")
    add_rss_route(url, channel_id="222", url_pattern="/bar/")
    assert len(list_rss_routes()) == 2
    delete_rss_feed(url)
    assert list_rss_feeds() == []
    assert list_rss_routes() == []


def test_update_rss_feed_cursor_advances_dedup_state(fresh_db):
    from src.core.audit import (
        upsert_rss_feed, update_rss_feed_cursor, list_rss_feeds,
    )
    url = "https://example.com/rss.xml"
    upsert_rss_feed(url)
    update_rss_feed_cursor(url, "guid-123")
    feeds = list_rss_feeds()
    assert feeds[0]["last_seen_guid"] == "guid-123"
    assert feeds[0]["last_polled_at"] is not None


def test_add_and_remove_rss_route(fresh_db):
    from src.core.audit import (
        upsert_rss_feed, add_rss_route, delete_rss_route, list_rss_routes,
    )
    url = "https://example.com/rss.xml"
    upsert_rss_feed(url)
    rid = add_rss_route(
        url, channel_id="333",
        url_pattern="/news/events/",
        forum_tag_name="Εκδηλώσεις",
        label="Events",
    )
    assert rid > 0
    routes = list_rss_routes(url)
    assert len(routes) == 1
    assert routes[0]["forum_tag_name"] == "Εκδηλώσεις"
    delete_rss_route(rid)
    assert list_rss_routes(url) == []


def test_list_rss_feeds_enabled_only_filter(fresh_db):
    from src.core.audit import upsert_rss_feed, list_rss_feeds
    upsert_rss_feed("https://a.com/rss", enabled=True)
    upsert_rss_feed("https://b.com/rss", enabled=False)
    all_feeds = list_rss_feeds()
    enabled = list_rss_feeds(enabled_only=True)
    assert len(all_feeds) == 2
    assert len(enabled) == 1
    assert enabled[0]["feed_url"] == "https://a.com/rss"
