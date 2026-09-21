"""Meeting and decision identifiers: one implementation each.

``ΔΣ07-2026`` is how a meeting is referred to in the agenda sheet, the Zoom
topic, the invitation, the minutes, the register and every Discord thread.
``ΔΣ01-07-2026`` is its first decision. These were rebuilt from parts at a
dozen call sites with the format string written out each time, so a meeting
number that was not a plain integer, or a missing date, produced a slightly
different reference in one place than another.

The ``ΔΣ`` prefix is the Greek section's; the profile will carry it in Phase 1
so another section can use its own. Everything else here is structural.
"""

from __future__ import annotations

import re
from datetime import date

BOARD_PREFIX = "ΔΣ"
UNKNOWN_YEAR = "ΧΧΧΧ"

_MEETING_RE = re.compile(rf"^{BOARD_PREFIX}?(\d{{1,2}})-(\d{{4}})$")
_DECISION_CORE_RE = re.compile(r"^\d+-\d+$")

__all__ = [
    "BOARD_PREFIX",
    "meeting_ref",
    "parse_meeting_ref",
    "meeting_id",
    "decision_ref",
    "sequence_of",
]


def sequence_of(value: object) -> str:
    """``7`` or ``"7"`` -> ``"07"``; anything else is passed through as text."""
    text = str(value if value is not None else "").strip()
    return text.zfill(2) if text.isdigit() else text


def meeting_ref(number: object, year: object, *, raw: str = "") -> str:
    """The canonical ``ΔΣNN-YYYY`` for a meeting.

    ``raw`` wins when present: the agenda sheet's own cell is the source of
    truth, and it may carry a form this function would not produce.
    Returns "" when there is nothing to build from.
    """
    raw = (raw or "").strip()
    if raw:
        return raw
    seq = sequence_of(number)
    year_text = str(year or "").strip()[:4] or UNKNOWN_YEAR
    return f"{BOARD_PREFIX}{seq}-{year_text}" if seq else ""


def meeting_ref_from_date(number: object, iso_date: str, *, raw: str = "") -> str:
    """As :func:`meeting_ref`, taking the year from an ISO date."""
    iso_date = (iso_date or "").strip()
    year = iso_date[:4] if len(iso_date) >= 4 else UNKNOWN_YEAR
    return meeting_ref(number, year, raw=raw)


def parse_meeting_ref(value: str) -> tuple[int, int] | None:
    """``"ΔΣ07-2026"`` -> ``(7, 2026)``; anything else -> ``None``."""
    match = _MEETING_RE.match(str(value or "").strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def meeting_id(ref: str) -> str:
    """The bus/Discord identifier for a meeting, e.g. ``board_meeting:ΔΣ07-2026``."""
    ref = (ref or "").strip()
    return f"board_meeting:{ref}" if ref else ""


def decision_ref(meeting: str, sequence: int) -> str:
    """The canonical decision reference ``ΔΣ{NN}-{MM}-{YYYY}``.

    ``decision_ref("ΔΣ05-2026", 1)`` -> ``"ΔΣ01-05-2026"``.

    Raises:
        ValueError: if *meeting* has no ``MM-YYYY`` core, or *sequence* < 1.
    """
    text = str(meeting or "").strip()
    core = text[len(BOARD_PREFIX):].strip() if text.startswith(BOARD_PREFIX) else text
    if not _DECISION_CORE_RE.match(core):
        raise ValueError(
            f"Invalid meeting ref {meeting!r}: expected '{BOARD_PREFIX}MM-YYYY', "
            f"got core {core!r}."
        )
    if sequence < 1:
        raise ValueError(f"Invalid sequence {sequence!r}: must be >= 1 (1-based).")
    return f"{BOARD_PREFIX}{sequence:02d}-{core}"


def year_of(iso_date: str, fallback: str = UNKNOWN_YEAR) -> str:
    """The year part of an ISO date, or *fallback* when it is unusable."""
    text = (iso_date or "").strip()
    return text[:4] if len(text) >= 4 else fallback


def today_year() -> int:
    return date.today().year
