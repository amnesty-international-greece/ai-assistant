"""Render email templates in isolation - no Zoom, no Brevo, no workflow.

Usage:
    python scripts/preview_email.py                 # render ALL templates
    python scripts/preview_email.py invitation_board   # render one, open in browser

Each template is filled with sample data (auto-detected placeholders get a
visible [PLACEHOLDER] marker if not in the sample set), wrapped in the brand
shell exactly as production does, written to data/preview/, and opened.
"""
from __future__ import annotations

import re
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.email_templates import render_email, _TEMPLATE_DIR

OUT_DIR = Path("data/preview")

# Sample values for every placeholder any inner template might reference.
SAMPLE: dict[str, str] = {
    "greek_date": "9 Ιουνίου 2026",
    "meeting_time": "20:00",
    "meeting_ref": "ΔΣ05-2026",
    "zoom_url": "https://us06web.zoom.us/j/82264596638",
    "zoom_meeting_id": "822 6459 6638",
    "zoom_passcode": "amnesty",
    "share_link": "https://amnestygr.sharepoint.com/example",
    "agenda_sheet_url": "https://docs.google.com/spreadsheets/d/example",
    "sheet_url": "https://docs.google.com/spreadsheets/d/example",
    "poll_url": "https://doodle.com/group-poll/example",
    "deadline": "13 Ιουνίου",
    "deadline_with_hint": "13 Ιουνίου (σε 4 ημέρες)",
    "doc_url": "https://docs.google.com/document/d/example",
    "draft_doc_url": "https://docs.google.com/document/d/example",
    "protocol_number": "2026_029",
    "agenda_html": "<ol><li>Επικύρωση πρακτικών</li><li>Ενημέρωση Γραφείου</li></ol>",
    "subject": "Πρακτικά ΔΣ05-2026 για αρχείο",
    "reason": "Δεν εντοπίστηκε συνημμένο PDF στο μήνυμα.",
}

# Per-template kicker/title so the preview matches production look.
SHELL = {
    "invitation_board": dict(
        kicker="ΠΡΟΣΚΛΗΣΗ ΔΙΟΙΚΗΤΙΚΟΥ ΣΥΜΒΟΥΛΙΟΥ",
        title="ΣΥΝΕΔΡΙΑΣΗ ΔΣ05-2026",
        header_ref="ΔΣ - ΠΡΟΣΚΛΗΣΗ",
        footer_note="Πρόσκληση ΔΣ - Εσωτερική επικοινωνία ΔΣ",
    ),
    "scheduling_with_poll": dict(
        kicker="ΠΡΟΓΡΑΜΜΑΤΙΣΜΟΣ - ΗΜΕΡΗΣΙΑ ΔΙΑΤΑΞΗ",
        title="ΣΥΝΕΔΡΙΑΣΗ ΔΣ05-2026",
        header_ref="ΔΣ - ΠΡΟΓΡΑΜΜΑΤΙΣΜΟΣ",
        footer_note="Εσωτερική επικοινωνία ΔΣ",
    ),
    "scheduling_no_poll": dict(
        kicker="ΠΡΟΓΡΑΜΜΑΤΙΣΜΟΣ - ΗΜΕΡΗΣΙΑ ΔΙΑΤΑΞΗ",
        title="ΣΥΝΕΔΡΙΑΣΗ ΔΣ05-2026",
        header_ref="ΔΣ - ΠΡΟΓΡΑΜΜΑΤΙΣΜΟΣ",
        footer_note="Εσωτερική επικοινωνία ΔΣ",
    ),
    "archive_confirmation": dict(
        kicker="ΑΡΧΕΙΟΘΕΤΗΣΗ", title="ΕΠΙΒΕΒΑΙΩΣΗ", header_ref="ΔΣ - ΑΡΧΕΙΟ",
    ),
    "minutes_share": dict(
        kicker="ΠΡΑΚΤΙΚΑ ΣΥΝΕΔΡΙΑΣΗΣ",
        title="ΠΡΟΣΧΕΔΙΟ ΠΡΑΚΤΙΚΩΝ",
        header_ref="ΔΣ - ΠΡΑΚΤΙΚΑ",
        footer_note="Εσωτερική επικοινωνία ΔΣ",
    ),
    "archive_failure": dict(
        kicker="ΑΡΧΕΙΟΘΕΤΗΣΗ",
        title="ΑΠΟΤΥΧΙΑ ΑΡΧΕΙΟΘΕΤΗΣΗΣ",
        header_ref="ΔΣ - ΑΡΧΕΙΟ",
    ),
}


def _placeholders(template_text: str) -> set[str]:
    """Single-brace {name} tokens (ignores {{ }} CSS escapes)."""
    no_escapes = template_text.replace("{{", "").replace("}}", "")
    return set(re.findall(r"\{(\w+)\}", no_escapes))


def render_one(name: str) -> Path:
    inner = (_TEMPLATE_DIR / f"{name}.html").read_text(encoding="utf-8")
    kwargs = {}
    for ph in _placeholders(inner):
        kwargs[ph] = SAMPLE.get(ph, f"[{ph.upper()}]")
    html = render_email(name, **SHELL.get(name, {}), **kwargs)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{name}.html"
    out.write_text(html, encoding="utf-8")
    return out


def main() -> None:
    names = sys.argv[1:] or [p.stem for p in _TEMPLATE_DIR.glob("*.html") if p.stem != "_shell"]
    for name in names:
        try:
            out = render_one(name)
            print(f"  {name:24} -> {out}")
            if len(names) == 1:
                webbrowser.open(out.resolve().as_uri())
        except Exception as e:
            print(f"  {name:24} -> ERROR: {e}")


if __name__ == "__main__":
    main()
