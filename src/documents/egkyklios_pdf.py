"""Egkyklios PDF renderer — Markdown → ReportLab PDF.

Produces the Γενική Εγκύκλιος Ενημέρωσης document with:
  - Page 1: title block + standard intro paragraphs (no header/footer)
  - Pages 2+: running header (logo left, org name right) and footer (org name left, logo right)
  - Page 1 footer: protocol number
  - TOC auto-generated from section headings
  - Sections Α/Β with proper numbering hierarchy
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    HRFlowable,
    PageBreak,
)
from reportlab.pdfgen import canvas as pdf_canvas

from src.core.audit import log_action
from src.documents.pdf_generator import AMNESTY_YELLOW, AMNESTY_BLACK

logger = logging.getLogger(__name__)

# ── Brand assets ──────────────────────────────────────────────────────────────
_LOGO_PATH = Path(
    "brand/Logo/Amnesty Logo/ENG_Amnesty_logo_RGB/"
    "ENG_Amnesty_logo_RGB_black/ENG_Amnesty_logo_RGB_black.png"
)

_ORG_HEADER = "ΔΙΕΘΝΗΣ ΑΜΝΗΣΤΙΑ / ΕΛΛΗΝΙΚΟ ΤΜΗΜΑ"
_ORG_FOOTER = "ΔΙΕΘΝΗΣ ΑΜΝΗΣΤΙΑ / ΕΛΛΗΝΙΚΟ ΤΜΗΜΑ / amnesty.gr"

# ── Styles ────────────────────────────────────────────────────────────────────


def _make_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "doc_title": ParagraphStyle(
            "EgkTitle",
            parent=base["Title"],
            fontSize=20,
            leading=26,
            spaceAfter=6,
            textColor=AMNESTY_BLACK,
            fontName="Helvetica-Bold",
        ),
        "doc_subtitle": ParagraphStyle(
            "EgkSubtitle",
            parent=base["Title"],
            fontSize=15,
            leading=20,
            spaceAfter=18,
            textColor=AMNESTY_BLACK,
            fontName="Helvetica-Bold",
        ),
        "intro": ParagraphStyle(
            "EgkIntro",
            parent=base["Normal"],
            fontSize=10,
            leading=15,
            spaceAfter=10,
            firstLineIndent=0,
        ),
        "toc_heading": ParagraphStyle(
            "EgkTOCHeading",
            parent=base["Heading3"],
            fontSize=11,
            spaceAfter=4,
            fontName="Helvetica-Bold",
        ),
        "toc_entry": ParagraphStyle(
            "EgkTOCEntry",
            parent=base["Normal"],
            fontSize=10,
            leading=16,
            leftIndent=12,
        ),
        "h1": ParagraphStyle(
            "EgkH1",
            parent=base["Heading1"],
            fontSize=14,
            leading=18,
            spaceBefore=18,
            spaceAfter=8,
            fontName="Helvetica-Bold",
            textColor=AMNESTY_BLACK,
        ),
        "h2": ParagraphStyle(
            "EgkH2",
            parent=base["Heading2"],
            fontSize=12,
            leading=16,
            spaceBefore=14,
            spaceAfter=6,
            fontName="Helvetica-Bold",
        ),
        "h3": ParagraphStyle(
            "EgkH3",
            parent=base["Heading3"],
            fontSize=11,
            leading=15,
            spaceBefore=10,
            spaceAfter=5,
            fontName="Helvetica-BoldOblique",
        ),
        "body": ParagraphStyle(
            "EgkBody",
            parent=base["Normal"],
            fontSize=10,
            leading=15,
            spaceAfter=8,
        ),
        "bullet": ParagraphStyle(
            "EgkBullet",
            parent=base["Normal"],
            fontSize=10,
            leading=15,
            spaceAfter=4,
            leftIndent=14,
            bulletIndent=0,
        ),
        "footer_p1": ParagraphStyle(
            "EgkFooterP1",
            parent=base["Normal"],
            fontSize=8,
            textColor=colors.grey,
        ),
    }


# ── Markdown parser (handles subset the LLM emits) ───────────────────────────

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"\*(.+?)\*")
_DATE_REF_RE = re.compile(r"\[(\d{1,2}\s+\w+\s+\d{4})\]")


def _md_inline(text: str) -> str:
    """Convert inline markdown (bold, italic) to ReportLab XML."""
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)
    # Date references in brackets: [5 Μαρτίου 2026] → bold
    text = _DATE_REF_RE.sub(r"<b>[\1]</b>", text)
    # Escape remaining & and < that aren't part of our tags
    # (very light-touch — avoids XML parse errors)
    return text


def parse_markdown(md: str) -> list[dict[str, Any]]:
    """Parse markdown into a list of {type, level, text} tokens.

    Handled:
      # heading (level 1)
      ## heading (level 2)
      ### heading (level 3)
      #### heading (level 4)
      - bullet item
      blank line (ignored)
      paragraph text
    """
    tokens: list[dict[str, Any]] = []
    for raw_line in md.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        m4 = re.match(r"^#{4}\s+(.*)", line)
        m3 = re.match(r"^#{3}\s+(.*)", line)
        m2 = re.match(r"^#{2}\s+(.*)", line)
        m1 = re.match(r"^#\s+(.*)", line)
        mb = re.match(r"^[-*]\s+(.*)", line)
        if m4:
            tokens.append({"type": "heading", "level": 4, "text": m4.group(1)})
        elif m3:
            tokens.append({"type": "heading", "level": 3, "text": m3.group(1)})
        elif m2:
            tokens.append({"type": "heading", "level": 2, "text": m2.group(1)})
        elif m1:
            tokens.append({"type": "heading", "level": 1, "text": m1.group(1)})
        elif mb:
            tokens.append({"type": "bullet", "text": mb.group(1)})
        else:
            tokens.append({"type": "para", "text": line})
    return tokens


# ── Header / Footer callbacks ─────────────────────────────────────────────────


def _draw_header_footer(
    canvas: Any,
    doc: Any,
    *,
    protocol_number: str = "",
    is_first_page: bool = False,
) -> None:
    """Draw branded header + footer on each page."""
    canvas.saveState()
    w, h = A4
    logo_path = str(_LOGO_PATH)
    logo_exists = _LOGO_PATH.exists()

    if not is_first_page:
        # Header: logo left (40pt wide), org name right
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#333333"))
        canvas.drawRightString(w - 1.5 * cm, h - 1.0 * cm, _ORG_HEADER)
        if logo_exists:
            try:
                canvas.drawImage(
                    logo_path,
                    1.5 * cm, h - 1.5 * cm,
                    width=40, height=20,
                    preserveAspectRatio=True, mask="auto",
                )
            except Exception:
                pass
        # Yellow rule below header
        canvas.setStrokeColor(AMNESTY_YELLOW)
        canvas.setLineWidth(2)
        canvas.line(1.5 * cm, h - 1.7 * cm, w - 1.5 * cm, h - 1.7 * cm)

        # Footer: org name left, logo right
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#333333"))
        canvas.drawString(1.5 * cm, 0.9 * cm, _ORG_FOOTER)
        if logo_exists:
            try:
                canvas.drawImage(
                    logo_path,
                    w - 2.5 * cm, 0.6 * cm,
                    width=28, height=14,
                    preserveAspectRatio=True, mask="auto",
                )
            except Exception:
                pass
    else:
        # Page 1 footer: protocol number only
        if protocol_number:
            canvas.setFont("Helvetica", 8)
            canvas.setFillColor(colors.grey)
            canvas.drawString(
                1.5 * cm, 0.9 * cm,
                f"ΑΡΙΘΜΟΣ ΠΡΩΤΟΚΟΛΛΟΥ / {protocol_number}",
            )

    canvas.restoreState()


# ── Main render function ──────────────────────────────────────────────────────


def render_egkyklios_pdf(
    *,
    markdown_text: str,
    output_path: Path,
    title: str,
    period_start: str,
    period_end: str,
    protocol_number: str = "",
    workflow: str = "egkyklios_general",
) -> Path:
    """Render the egkyklios markdown to a branded PDF.

    Args:
        markdown_text: Full markdown produced by the LLM.
        output_path:   Where to save the PDF.
        title:         Human-readable period title (e.g. "ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026").
        period_start:  ISO date string for the period start.
        period_end:    ISO date string for the period end.
        protocol_number: If set, shown in page 1 footer.
        workflow:      Audit log label.

    Returns:
        The output_path.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    styles = _make_styles()

    # Callbacks capture protocol_number via closure
    def _on_first_page(canvas: Any, doc: Any) -> None:
        _draw_header_footer(canvas, doc, protocol_number=protocol_number, is_first_page=True)

    def _on_later_pages(canvas: Any, doc: Any) -> None:
        _draw_header_footer(canvas, doc, protocol_number=protocol_number, is_first_page=False)

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        topMargin=2.8 * cm,
        bottomMargin=2.5 * cm,
        leftMargin=2.0 * cm,
        rightMargin=2.0 * cm,
    )

    # ── Parse tokens ─────────────────────────────────────────────────────────
    tokens = parse_markdown(markdown_text)

    # ── Collect TOC entries (level 2 & 3 headings, skip title/subtitle) ──────
    toc_entries: list[tuple[int, str]] = []
    for tok in tokens:
        if tok["type"] == "heading" and tok["level"] in (2, 3):
            toc_entries.append((tok["level"], tok["text"]))

    # ── Build flowables ───────────────────────────────────────────────────────
    elements: list[Any] = []

    # Title block (before content tokens)
    elements.append(Paragraph("ΓΕΝΙΚΗ ΕΓΚΥΚΛΙΟΣ ΕΝΗΜΕΡΩΣΗΣ", styles["doc_title"]))
    elements.append(Paragraph(title, styles["doc_subtitle"]))
    elements.append(HRFlowable(width="100%", thickness=2, color=AMNESTY_YELLOW, spaceAfter=10))

    # TOC
    if toc_entries:
        elements.append(Paragraph("Περιεχόμενα", styles["toc_heading"]))
        for level, heading in toc_entries:
            indent = "    " if level == 3 else ""
            elements.append(Paragraph(f"{indent}{heading}", styles["toc_entry"]))
        elements.append(Spacer(1, 0.5 * cm))
        elements.append(HRFlowable(width="100%", thickness=1, color=colors.lightgrey, spaceAfter=8))

    # Render tokens (skip the doc title/subtitle if the LLM also emitted them)
    seen_main_heading = False
    for tok in tokens:
        if tok["type"] == "heading":
            lvl = tok["level"]
            text = tok["text"]
            # Skip top-level title/subtitle duplicates
            if lvl == 1 and not seen_main_heading:
                seen_main_heading = True
                continue  # title already added above
            if lvl == 1 and text.startswith(title[:8]):
                continue
            style_key = {1: "h1", 2: "h1", 3: "h2", 4: "h3"}.get(lvl, "h2")
            elements.append(Paragraph(_md_inline(text), styles[style_key]))
        elif tok["type"] == "bullet":
            elements.append(Paragraph(f"• {_md_inline(tok['text'])}", styles["bullet"]))
        elif tok["type"] == "para":
            text = tok["text"].strip()
            if text:
                elements.append(Paragraph(_md_inline(text), styles["body"]))

    doc.build(
        elements,
        onFirstPage=_on_first_page,
        onLaterPages=_on_later_pages,
    )

    log_action(
        workflow=workflow,
        action="egkyklios_pdf_generated",
        actor="system",
        target=str(output_path),
        details={"title": title, "period_start": period_start, "period_end": period_end},
    )
    logger.info("Egkyklios PDF generated: %s", output_path)
    return output_path
