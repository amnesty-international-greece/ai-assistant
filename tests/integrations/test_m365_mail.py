"""Tests for the M365MailClient (Microsoft Graph mail).

Full integration tests require a live token; we stub the static helpers and
syntax-check the module here.  Future work mirrors the OneDrive checklist:
mock httpx, mock MSAL, and assert URL/payload shapes.

TODO when mocking infra lands:
  - send_email creates draft, then POSTs to /send, returns message id
  - send_reply uses /createReply → PATCH body → /send
  - _get_token raises OneDriveAuthRequired when cache is empty
  - The same MSAL cache is loaded that OneDriveClient uses (unified store)
"""

import ast
from pathlib import Path

from src.integrations.m365_mail import M365MailClient


def test_m365_mail_module_parses() -> None:
    """Verify m365_mail.py has no syntax errors."""
    # parents[2] = project root (test file is at tests/integrations/foo.py)
    src = Path(__file__).resolve().parents[2] / "src" / "integrations" / "m365_mail.py"
    ast.parse(src.read_text(encoding="utf-8"))


def test_recipients_accepts_string() -> None:
    """Single-string address → one-element recipient list."""
    result = M365MailClient._recipients("foo@bar.com")
    assert result == [{"emailAddress": {"address": "foo@bar.com"}}]


def test_recipients_accepts_list() -> None:
    """List of addresses → list of recipient dicts (preserves order)."""
    result = M365MailClient._recipients(["a@x.gr", "b@y.gr"])
    assert result == [
        {"emailAddress": {"address": "a@x.gr"}},
        {"emailAddress": {"address": "b@y.gr"}},
    ]


def test_recipients_none_returns_empty() -> None:
    """None or empty list → empty recipient list (Graph accepts this)."""
    assert M365MailClient._recipients(None) == []
    assert M365MailClient._recipients([]) == []
    assert M365MailClient._recipients("") == []


def test_body_block_plain_text() -> None:
    """html=False → contentType 'Text'."""
    assert M365MailClient._body_block("hello", html=False) == {
        "contentType": "Text",
        "content": "hello",
    }


def test_body_block_html() -> None:
    """html=True → contentType 'HTML'."""
    assert M365MailClient._body_block("<p>hi</p>", html=True) == {
        "contentType": "HTML",
        "content": "<p>hi</p>",
    }


def test_attachment_blocks_none_or_empty() -> None:
    """None / empty list → empty block list (no attachments key in message)."""
    assert M365MailClient._attachment_blocks(None) == []
    assert M365MailClient._attachment_blocks([]) == []


def test_attachment_blocks_encodes_file(tmp_path) -> None:
    """A real file gets base64-encoded into a fileAttachment block."""
    import base64

    f = tmp_path / "hello.pdf"
    f.write_bytes(b"%PDF-1.4 fake")

    blocks = M365MailClient._attachment_blocks([f])
    assert len(blocks) == 1
    b = blocks[0]
    assert b["@odata.type"] == "#microsoft.graph.fileAttachment"
    assert b["name"] == "hello.pdf"
    assert b["contentType"] == "application/pdf"
    assert base64.b64decode(b["contentBytes"]) == b"%PDF-1.4 fake"


def test_attachment_blocks_unknown_extension_falls_back(tmp_path) -> None:
    """Files with unknown MIME types default to application/octet-stream."""
    f = tmp_path / "data.weird"
    f.write_bytes(b"x")
    blocks = M365MailClient._attachment_blocks([f])
    assert blocks[0]["contentType"] == "application/octet-stream"


def test_attachment_blocks_accepts_string_paths(tmp_path) -> None:
    """str inputs (not just Path) are accepted."""
    f = tmp_path / "doc.txt"
    f.write_text("hi")
    blocks = M365MailClient._attachment_blocks([str(f)])
    assert blocks[0]["name"] == "doc.txt"
