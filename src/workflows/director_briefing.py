"""Director's briefing auto-archive.

Fires when ``director@amnesty.org.gr`` replies on a board-meeting email
thread with attachments.

**Classification is filename-based**.  The Director simply hits "Reply"
on the thread — the email subject stays whatever Outlook prepends to
``Συνεδρίαση ΔΣXX``.  What identifies the briefing is the PDF's filename:
a non-image attachment whose name contains ``Εισηγητικό`` or
``Ενημερωτικό`` (case + τόνος insensitive) is the briefing.  Εισηγητικό
wins when both kinds appear among attachments — it's the more specific
kind (adds the ``Εισηγήσεις`` label on top of Ενημερωτικά).

Behaviour split:
  - The **main briefing** is archived with **pre-filled metadata** (no LLM
    extraction): canonical title, canonical Κύρια Σημεία, kind-specific
    Ετικέτες.  A local copy lands at
    ``data/director_briefings/{meeting_ref}/{filename}`` regardless of
    later /board cancel — the Γενική Εγκύκλιος workflow uses these.
  - **Other attachments** ride the standard ``ArchiveWorkflow`` with full
    LLM extraction — the bot treats them like any other attachment the
    Director happened to send along.

No filename matches the briefing keyword → no briefing at all; every
attachment goes through the standard archive flow.

User decisions baked in (2026-05-29):
  - Title: ``"{Kind} Διευθυντή - Συνεδρίαση {meeting_ref}"``
  - Κύρια Σημεία: the canonical 5-line bucket list (see ``_BRIEFING_KURIA_SIMEIA``)
  - Ετικέτες:
      * Εισηγητικά: ["Εισηγήσεις", "Αναφορές", "Γραφείο"]
      * Ενημερωτικά: ["Αναφορές", "Γραφείο"]
  - ``/board cancel`` does **NOT** roll the briefing back — the Director
    likely won't re-send for a short postponement.  SecGen handles edge
    cases manually.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from src.core.email_templates import greek_upper

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────


DIRECTOR_EMAIL = "director@amnesty.org.gr"

# Used as both subject markers AND filename markers.  greek_upper() handles
# τόνος/case before comparison so the actual subject text can be "εισηγητικό",
# "Εισηγητικό", "ΕΙΣΗΓΗΤΙΚΟ", "εισηγητικο" — they all match.
KIND_EISIGITIKO = "ΕΙΣΗΓΗΤΙΚΟ"
KIND_ENIMEROTIKO = "ΕΝΗΜΕΡΩΤΙΚΟ"

# Display labels (proper case, with τόνους) used in titles + Discord messages.
_KIND_DISPLAY = {
    KIND_EISIGITIKO: "Εισηγητικό",
    KIND_ENIMEROTIKO: "Ενημερωτικό",
}

# Ετικέτες (taxonomy labels) per kind.  Confirmed by SecGen 2026-05-29.
_LABELS_BY_KIND = {
    KIND_EISIGITIKO: ["Εισηγήσεις", "Αναφορές", "Γραφείο"],
    KIND_ENIMEROTIKO: ["Αναφορές", "Γραφείο"],
}

# Image extensions never count as briefing candidates — phone screenshots,
# signature blocks, scanned letterheads, etc.
_IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp",
    ".tiff", ".tif", ".heic", ".heif", ".webp", ".svg",
}

LOCAL_BRIEFING_DIR = Path("data") / "director_briefings"


# ── Detection helpers ────────────────────────────────────────────────────────


def is_director(sender_email: str) -> bool:
    """True if the email is from ``director@amnesty.org.gr`` (case-insensitive)."""
    return (sender_email or "").strip().lower() == DIRECTOR_EMAIL.lower()


# board@ identity — same constant as the workflow uses for outbound sends.
BOARD_EMAIL = "board@amnesty.org.gr"


def board_in_recipients(message: dict) -> bool:
    """True iff ``board@amnesty.org.gr`` appears in TO / CC / BCC of *message*.

    When board@ is on the recipient list, every board member sees the
    Director's email directly in their inbox — so the bot can mirror it to
    Discord verbatim, same as any other board-thread reply.

    When the only recipient is ``members@amnesty.org.gr`` (the bot itself),
    the email is private to the bot and we publish a bot-composed
    announcement instead of leaking the Director's private reply.

    Args:
        message: Graph message dict (must have ``toRecipients`` / ``ccRecipients`` /
            ``bccRecipients`` — Graph's ``$select`` typically includes them).

    Returns:
        ``True`` if board@ found in any recipient field, else ``False``.
    """
    if not message:
        return False
    needle = BOARD_EMAIL.lower()
    for field in ("toRecipients", "ccRecipients", "bccRecipients"):
        for entry in message.get(field) or []:
            addr = ((entry.get("emailAddress") or {}).get("address") or "").strip().lower()
            if addr == needle:
                return True
    return False


def _is_image(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in _IMAGE_EXTENSIONS


def classify_filename(filename: str) -> str | None:
    """Return ``KIND_EISIGITIKO``, ``KIND_ENIMEROTIKO``, or ``None``.

    Filename match is τόνος + case insensitive via :func:`greek_upper`.
    Εισηγητικό wins when both keywords appear in the same filename
    (it's the more specific kind — adds the ``Εισηγήσεις`` label).
    """
    if not filename:
        return None
    upper = greek_upper(filename)
    if KIND_EISIGITIKO in upper:
        return KIND_EISIGITIKO
    if KIND_ENIMEROTIKO in upper:
        return KIND_ENIMEROTIKO
    return None


def find_briefing_attachment(
    attachments: list[dict[str, Any]],
) -> tuple[dict[str, Any], str] | None:
    """Scan attachments for THE main briefing.

    Returns ``(attachment, kind)`` or ``None`` if no non-image attachment
    has a filename matching either keyword.

    When multiple attachments match, **Εισηγητικά win over Ενημερωτικά**
    so the labels reflect the more specific kind.  Within the same kind,
    the first-seen attachment wins.
    """
    if not attachments:
        return None
    eisigitiko: dict[str, Any] | None = None
    enimerotiko: dict[str, Any] | None = None
    for att in attachments:
        name = att.get("name") or ""
        if _is_image(name):
            continue
        kind = classify_filename(name)
        if kind == KIND_EISIGITIKO and eisigitiko is None:
            eisigitiko = att
        elif kind == KIND_ENIMEROTIKO and enimerotiko is None:
            enimerotiko = att
    if eisigitiko is not None:
        return eisigitiko, KIND_EISIGITIKO
    if enimerotiko is not None:
        return enimerotiko, KIND_ENIMEROTIKO
    return None


# ── Metadata builders ────────────────────────────────────────────────────────


def briefing_title(meeting_ref: str, kind: str) -> str:
    """Canonical archive title for a Director briefing."""
    display = _KIND_DISPLAY.get(kind, kind.title())
    return f"{display} Διευθυντή - Συνεδρίαση {meeting_ref}"


def briefing_labels(kind: str) -> list[str]:
    """Canonical Ετικέτες for a Director briefing."""
    return list(_LABELS_BY_KIND.get(kind, ["Αναφορές", "Γραφείο"]))


def local_copy_path(meeting_ref: str, filename: str) -> Path:
    """Compute the target path for the persistent local copy.

    The local copy survives ``/board cancel`` — it's what the future
    Γενική Εγκύκλιος workflow will scan to compile content.  Path
    components are sanitised against directory traversal.
    """
    safe_meeting = re.sub(r"[/\\]+", "_", (meeting_ref or "unknown").strip(". "))
    safe_filename = re.sub(r"[/\\]+", "_", (filename or "briefing").strip(". "))
    return LOCAL_BRIEFING_DIR / safe_meeting / safe_filename


def prefill_archive_context(*, meeting_ref: str, kind: str) -> dict[str, Any]:
    """Build the ``ArchiveWorkflow`` initial-data overlay for a briefing.

    Sets ``_skip_llm=True`` and pre-fills ``llm_result`` so the workflow
    uses our metadata verbatim instead of asking Gemini what the document
    is about.  ``kuria_simeia`` is **deliberately left empty** — the
    Director's actual key points vary per cycle, so the SecGen fills them
    in by hand during πρωτόκολλο review rather than the bot baking in a
    rigid template.
    """
    return {
        "_skip_llm": True,
        "llm_result": {
            "title": briefing_title(meeting_ref, kind),
            "labels": briefing_labels(kind),
            "kuria_simeia": "",   # left blank on purpose — SecGen fills in
            # Existing protocol number left blank — archive workflow assigns
            # the next available number from the πρωτόκολλο xlsx.
            "existing_protocol": "",
        },
        # Sender hints that downstream notify/email-reply steps use.
        "sender_email": DIRECTOR_EMAIL,
        "sender_name": "Διευθυντής",
        "email_subject": briefing_title(meeting_ref, kind),
        "_source": "director_briefing_intake",
    }
