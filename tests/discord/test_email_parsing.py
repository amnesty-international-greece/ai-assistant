"""Tests for pure-function email parsing helpers in email_gateway.py."""

from __future__ import annotations

import pytest

from src.integrations.discord.email_gateway import (
    InboundEmail,
    _decode_header_value,
    _parse_message,
    _split_references,
    _strip_angle_brackets,
)


# ---------------------------------------------------------------------------
# _strip_angle_brackets
# ---------------------------------------------------------------------------


def test_strip_angle_brackets_strips():
    assert _strip_angle_brackets("<foo@bar>") == "foo@bar"


def test_strip_angle_brackets_none_returns_none():
    assert _strip_angle_brackets(None) is None


def test_strip_angle_brackets_no_brackets():
    assert _strip_angle_brackets("no-brackets") == "no-brackets"


def test_strip_angle_brackets_empty_string():
    # Empty string is falsy — treated like None
    assert _strip_angle_brackets("") is None


def test_strip_angle_brackets_whitespace_around_brackets():
    assert _strip_angle_brackets("  <inner@example.com>  ") == "inner@example.com"


# ---------------------------------------------------------------------------
# _split_references
# ---------------------------------------------------------------------------


def test_split_references_multiple():
    result = _split_references("<a> <b>\n<c>")
    assert result == ["a", "b", "c"]


def test_split_references_empty_string():
    assert _split_references("") == []


def test_split_references_none():
    assert _split_references(None) == []


def test_split_references_single():
    assert _split_references("<abc123@example.com>") == ["abc123@example.com"]


def test_split_references_no_angle_brackets():
    # Tokens without brackets are returned verbatim (stripped)
    result = _split_references("plain-token")
    assert result == ["plain-token"]


# ---------------------------------------------------------------------------
# _decode_header_value
# ---------------------------------------------------------------------------


def test_decode_header_value_base64_utf8():
    # "Test" encoded as =?utf-8?b?VGVzdA==?=
    assert _decode_header_value("=?utf-8?b?VGVzdA==?=") == "Test"


def test_decode_header_value_plain_ascii():
    assert _decode_header_value("Hello World") == "Hello World"


def test_decode_header_value_none():
    assert _decode_header_value(None) == ""


def test_decode_header_value_empty():
    assert _decode_header_value("") == ""


def test_decode_header_value_greek_base64():
    # "Δοκιμή" = test in Greek, encoded as UTF-8 base64
    import base64

    greek_text = "Δοκιμή"
    encoded = base64.b64encode(greek_text.encode("utf-8")).decode("ascii")
    header_value = f"=?utf-8?b?{encoded}?="
    assert _decode_header_value(header_value) == greek_text


# ---------------------------------------------------------------------------
# _parse_message — simple email
# ---------------------------------------------------------------------------


def test_parse_message_returns_inbound_email(sample_email_bytes):
    result = _parse_message(sample_email_bytes)
    assert isinstance(result, InboundEmail)


def test_parse_message_subject(sample_email_bytes):
    result = _parse_message(sample_email_bytes)
    assert result.subject == "Hello World"


def test_parse_message_from_addr(sample_email_bytes):
    result = _parse_message(sample_email_bytes)
    assert result.from_addr == "alice@example.com"


def test_parse_message_from_name(sample_email_bytes):
    result = _parse_message(sample_email_bytes)
    assert result.from_name == "Alice Smith"


def test_parse_message_message_id(sample_email_bytes):
    result = _parse_message(sample_email_bytes)
    # Angle brackets should be stripped
    assert result.message_id == "abc123@example.com"


def test_parse_message_body_plain(sample_email_bytes):
    result = _parse_message(sample_email_bytes)
    assert "This is the body of the email." in result.body_plain


def test_parse_message_no_attachments(sample_email_bytes):
    result = _parse_message(sample_email_bytes)
    assert result.attachments == []


def test_parse_message_in_reply_to_absent(sample_email_bytes):
    result = _parse_message(sample_email_bytes)
    assert result.in_reply_to is None


def test_parse_message_references_absent(sample_email_bytes):
    result = _parse_message(sample_email_bytes)
    assert result.references == []


def test_parse_message_received_at_is_utc(sample_email_bytes):
    from datetime import timezone

    result = _parse_message(sample_email_bytes)
    assert result.received_at.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# _parse_message — multipart email
# ---------------------------------------------------------------------------


def test_parse_multipart_plain_part(multipart_email_bytes):
    result = _parse_message(multipart_email_bytes)
    assert "Plain text body." in result.body_plain


def test_parse_multipart_html_part(multipart_email_bytes):
    result = _parse_message(multipart_email_bytes)
    assert result.body_html is not None
    assert "HTML body." in result.body_html


def test_parse_multipart_attachment(multipart_email_bytes):
    result = _parse_message(multipart_email_bytes)
    assert len(result.attachments) == 1
    att = result.attachments[0]
    assert att.filename == "document.pdf"
    assert att.content_type == "application/pdf"
    assert len(att.data) > 0


def test_parse_multipart_from_addr(multipart_email_bytes):
    result = _parse_message(multipart_email_bytes)
    assert result.from_addr == "bob@example.com"


# ---------------------------------------------------------------------------
# _parse_message — HTML-only email (fallback to html.unescape)
# ---------------------------------------------------------------------------


def test_parse_html_only_fallback_extracts_text(html_only_email_bytes):
    result = _parse_message(html_only_email_bytes)
    # plain text extracted from HTML
    assert "Hello" in result.body_plain
    assert "welcome" in result.body_plain


def test_parse_html_only_unescapes_entities(html_only_email_bytes):
    result = _parse_message(html_only_email_bytes)
    # &amp; should have been unescaped to &
    assert "&" in result.body_plain
    assert "&amp;" not in result.body_plain
