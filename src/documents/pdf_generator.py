"""PDF generation from structured content using ReportLab."""

from __future__ import annotations

import logging
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
    Table,
    TableStyle,
    Image,
)

from src.core.audit import log_action

logger = logging.getLogger(__name__)

# Amnesty International brand colors
AMNESTY_YELLOW = colors.HexColor("#FFFF00")
AMNESTY_BLACK = colors.HexColor("#000000")


def _create_styles() -> dict[str, ParagraphStyle]:
    """Create document styles matching institutional formatting."""
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "InstitutionalTitle",
            parent=base["Title"],
            fontSize=16,
            spaceAfter=12,
            textColor=AMNESTY_BLACK,
        ),
        "heading": ParagraphStyle(
            "InstitutionalHeading",
            parent=base["Heading2"],
            fontSize=12,
            spaceAfter=6,
            textColor=AMNESTY_BLACK,
        ),
        "body": ParagraphStyle(
            "InstitutionalBody",
            parent=base["Normal"],
            fontSize=10,
            leading=14,
            spaceAfter=8,
        ),
        "footer": ParagraphStyle(
            "InstitutionalFooter",
            parent=base["Normal"],
            fontSize=8,
            textColor=colors.grey,
        ),
    }


def generate_pdf(
    content: dict[str, Any],
    output_path: Path,
    workflow: str = "pdf_generator",
) -> Path:
    """Generate a PDF document from structured content.

    Args:
        content: Dictionary with document structure:
            - title: str — Document title
            - subtitle: str — Optional subtitle (e.g., date, reference number)
            - sections: list[dict] — Each with 'heading' and 'body' keys
            - footer: str — Optional footer text
        output_path: Where to save the PDF.
        workflow: Workflow name for audit logging.

    Returns:
        Path to the generated PDF.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    styles = _create_styles()

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        topMargin=2.5 * cm,
        bottomMargin=2.5 * cm,
        leftMargin=2.5 * cm,
        rightMargin=2.5 * cm,
    )

    elements = []

    # Title
    elements.append(Paragraph(content["title"], styles["title"]))
    if content.get("subtitle"):
        elements.append(Paragraph(content["subtitle"], styles["body"]))
    elements.append(Spacer(1, 0.5 * cm))

    # Sections
    for section in content.get("sections", []):
        if section.get("heading"):
            elements.append(Paragraph(section["heading"], styles["heading"]))
        if section.get("body"):
            elements.append(Paragraph(section["body"], styles["body"]))
        elements.append(Spacer(1, 0.3 * cm))

    # Footer
    if content.get("footer"):
        elements.append(Spacer(1, 1 * cm))
        elements.append(Paragraph(content["footer"], styles["footer"]))

    doc.build(elements)

    log_action(
        workflow=workflow,
        action="pdf_generated",
        actor="system",
        target=str(output_path),
        details={"title": content["title"]},
    )
    logger.info("Generated PDF: %s", output_path)
    return output_path


def embed_signatures(
    input_pdf: Path,
    output_pdf: Path,
    signatures: list[dict[str, Any]],
    page_number: int = -1,
    workflow: str = "pdf_generator",
) -> Path:
    """Overlay signature images on a page of a PDF.

    Args:
        input_pdf: Source PDF path.
        output_pdf: Output path for signed PDF.
        signatures: List of signature configs, each with:
            - image_path: str — path to signature image (PNG/JPG)
            - x: float — x position from left edge (points)
            - y: float — y position from bottom edge (points)
            - width: float — display width (points)
            - height: float — display height (points)
            - label: str — text label below signature (e.g., "Ο Πρόεδρος")
        page_number: Page to sign (default -1 = last page).
        workflow: Workflow name for audit logging.

    Returns:
        Path to the signed PDF.
    """
    from PyPDF2 import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas as pdf_canvas
    from io import BytesIO

    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    # Create an overlay PDF with signatures
    overlay_buffer = BytesIO()
    c = pdf_canvas.Canvas(overlay_buffer, pagesize=A4)
    for sig in signatures:
        c.drawImage(
            sig["image_path"],
            sig["x"], sig["y"],
            width=sig["width"], height=sig["height"],
            preserveAspectRatio=True, mask="auto",
        )
        if sig.get("label"):
            c.setFont("Helvetica", 8)
            c.drawCentredString(
                sig["x"] + sig["width"] / 2,
                sig["y"] - 12,
                sig["label"],
            )
    c.save()
    overlay_buffer.seek(0)

    # Merge overlay onto the target page
    reader = PdfReader(str(input_pdf))
    overlay_reader = PdfReader(overlay_buffer)
    writer = PdfWriter()

    target_page = page_number if page_number >= 0 else len(reader.pages) + page_number
    for i, page in enumerate(reader.pages):
        if i == target_page:
            page.merge_page(overlay_reader.pages[0])
        writer.add_page(page)

    with open(output_pdf, "wb") as f:
        writer.write(f)

    log_action(
        workflow=workflow,
        action="signatures_embedded",
        actor="system",
        target=str(output_pdf),
        details={"signatures": len(signatures), "page": target_page},
    )
    logger.info("Embedded %d signatures on page %d → %s", len(signatures), target_page, output_pdf)
    return output_pdf
