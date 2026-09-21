"""Greek wording that documents and emails need: dates, months, case.

Month tables were written out five times across the workflows, in two cases
(genitive for prose, nominative uppercase for titles), so a fix in one place
never reached the others. Phase 1 moves this under the section's profile as
its locale; until then it lives here, in one copy.
"""

from __future__ import annotations

from datetime import date

from src.core.email_templates import greek_upper

# "9 Ιουνίου 2026" - the form used in prose and on documents.
MONTHS_GENITIVE = {
    1: "Ιανουαρίου", 2: "Φεβρουαρίου", 3: "Μαρτίου", 4: "Απριλίου",
    5: "Μαΐου", 6: "Ιουνίου", 7: "Ιουλίου", 8: "Αυγούστου",
    9: "Σεπτεμβρίου", 10: "Οκτωβρίου", 11: "Νοεμβρίου", 12: "Δεκεμβρίου",
}

# "ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026" - the form used in titles.
MONTHS_NOMINATIVE_UPPER = {
    1: "ΙΑΝΟΥΑΡΙΟΣ", 2: "ΦΕΒΡΟΥΑΡΙΟΣ", 3: "ΜΑΡΤΙΟΣ", 4: "ΑΠΡΙΛΙΟΣ",
    5: "ΜΑΪΟΣ", 6: "ΙΟΥΝΙΟΣ", 7: "ΙΟΥΛΙΟΣ", 8: "ΑΥΓΟΥΣΤΟΣ",
    9: "ΣΕΠΤΕΜΒΡΙΟΣ", 10: "ΟΚΤΩΒΡΙΟΣ", 11: "ΝΟΕΜΒΡΙΟΣ", 12: "ΔΕΚΕΜΒΡΙΟΣ",
}

# Same names indexed by month number, for callers that index a list.
MONTHS_GENITIVE_LIST = [""] + [MONTHS_GENITIVE[i] for i in range(1, 13)]

__all__ = [
    "MONTHS_GENITIVE",
    "MONTHS_GENITIVE_LIST",
    "MONTHS_NOMINATIVE_UPPER",
    "format_date",
    "month_genitive",
    "month_title",
    "meeting_type_genitive",
    "meeting_type_adjective",
    "greek_upper",
]


def month_genitive(month: int) -> str:
    return MONTHS_GENITIVE.get(int(month), "")


def month_title(month: int) -> str:
    return MONTHS_NOMINATIVE_UPPER.get(int(month), "")


def format_date(iso_date: str) -> str:
    """``"2026-06-09"`` -> ``"9 Ιουνίου 2026"``; unparseable input is returned as is."""
    try:
        parsed = date.fromisoformat(str(iso_date).strip())
    except (TypeError, ValueError):
        return str(iso_date or "")
    return f"{parsed.day} {month_genitive(parsed.month)} {parsed.year}"


def meeting_type_genitive(meeting_type: str) -> str:
    """``"ΤΑΚΤΙΚΗ"`` -> ``"ΤΑΚΤΙΚΗΣ"``, for "ΠΡΟΣΚΛΗΣΗ ΤΑΚΤΙΚΗΣ ΣΥΝΕΔΡΙΑΣΗΣ"."""
    value = (meeting_type or "ΤΑΚΤΙΚΗ").strip().upper()
    if value in ("ΤΑΚΤΙΚΗ", "ΤΑΚΤΙΚΗΣ"):
        return "ΤΑΚΤΙΚΗΣ"
    if value in ("ΕΚΤΑΚΤΗ", "ΕΚΤΑΚΤΗΣ"):
        return "ΕΚΤΑΚΤΗΣ"
    return value


def meeting_type_adjective(meeting_type: str) -> str:
    """The lowercase adjective used mid-sentence: τακτική / έκτακτη."""
    return "έκτακτη" if "ΕΚΤΑΚΤΗ" in str(meeting_type or "").upper() else "τακτική"
