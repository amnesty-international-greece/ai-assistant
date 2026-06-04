"""Shared pytest fixtures for the Discord bot test suite."""

from __future__ import annotations

import sqlite3
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import patch

import pytest

import src.core.audit as audit_mod


@pytest.fixture
def in_memory_db(monkeypatch):
    """
    Patch ``src.core.audit._get_connection`` to return a fresh in-memory SQLite
    connection with the full schema applied.  Each test gets an isolated DB.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Patch module-level globals so _get_connection() returns our in-memory conn.
    monkeypatch.setattr(audit_mod, "_CONNECTION", conn)
    monkeypatch.setattr(audit_mod, "_DB_PATH", None)

    # Apply the full schema (Discord tables included).
    conn.executescript(audit_mod._SCHEMA)
    conn.commit()

    yield conn

    conn.close()


@pytest.fixture
def sample_email_bytes() -> bytes:
    """Raw RFC 822 bytes for a minimal plain-text email."""
    raw = (
        "From: Alice Smith <alice@example.com>\r\n"
        "To: group@lists.example.com\r\n"
        "Subject: Hello World\r\n"
        "Message-ID: <abc123@example.com>\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "This is the body of the email.\r\n"
    )
    return raw.encode("utf-8")


@pytest.fixture
def multipart_email_bytes() -> bytes:
    """Raw RFC 822 bytes for a multipart email with plain text, HTML, and a PDF attachment."""
    msg = MIMEMultipart("mixed")
    msg["From"] = "Bob Jones <bob@example.com>"
    msg["To"] = "group@lists.example.com"
    msg["Subject"] = "Multipart Test"
    msg["Message-ID"] = "<multi456@example.com>"

    plain_part = MIMEText("Plain text body.", "plain", "utf-8")
    html_part = MIMEText("<html><body><p>HTML body.</p></body></html>", "html", "utf-8")

    pdf_part = MIMEApplication(b"%PDF-1.4 fake pdf content", _subtype="pdf")
    pdf_part.add_header("Content-Disposition", "attachment", filename="document.pdf")

    msg.attach(plain_part)
    msg.attach(html_part)
    msg.attach(pdf_part)

    return msg.as_bytes()


@pytest.fixture
def html_only_email_bytes() -> bytes:
    """Raw RFC 822 bytes for an HTML-only email (no plain text part)."""
    msg = MIMEMultipart("alternative")
    msg["From"] = "Carol <carol@example.com>"
    msg["To"] = "group@lists.example.com"
    msg["Subject"] = "HTML Only"
    msg["Message-ID"] = "<html789@example.com>"

    # Only an HTML part — no plain text part.
    html_part = MIMEText(
        "<html><body><p>Hello &amp; welcome!</p></body></html>", "html", "utf-8"
    )
    msg.attach(html_part)

    return msg.as_bytes()
