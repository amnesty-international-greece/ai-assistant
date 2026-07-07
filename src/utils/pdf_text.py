"""PDF text extraction helper for the archive workflow.

Uses PyPDF2 (already pulled in via ``src.documents.pdf_generator``).  Returns
both the extracted text and a small metadata dict the workflow can pass to the
LLM (encryption + scan-heuristic flags).

Encrypted PDFs raise a clear Greek error message - Phase 1 doesn't decrypt
anything.  Scanned PDFs still proceed, but the metadata flag lets the LLM
prompt say "text extraction limited" so the model down-weights body content
and leans on filename/sender/subject instead.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class EncryptedPDFError(RuntimeError):
    """Raised when a PDF is encrypted and cannot be read."""


def extract_pdf_text(
    pdf_path: Path,
    max_chars: int = 5000,
) -> tuple[str, dict]:
    """Extract text + metadata from a PDF file.

    Args:
        pdf_path:  Path to the PDF on disk.
        max_chars: Truncate the returned text to this many characters
                   (default 5000 - matches the LLM prompt budget in the
                   archive design doc).

    Returns:
        ``(text, metadata)`` where ``metadata`` always contains
        ``page_count``, ``is_encrypted``, ``is_scan``, ``char_count``.

    Raises:
        EncryptedPDFError: If the PDF is encrypted.
        FileNotFoundError: If ``pdf_path`` does not exist.
    """
    from PyPDF2 import PdfReader

    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    reader = PdfReader(str(pdf_path))
    metadata: dict = {
        "page_count": len(reader.pages),
        "is_encrypted": bool(reader.is_encrypted),
        "is_scan": False,
        "char_count": 0,
    }

    if reader.is_encrypted:
        raise EncryptedPDFError(
            "Το PDF είναι κρυπτογραφημένο και δεν μπορεί να αναγνωστεί αυτόματα. "
            "Παρακαλώ αφαιρέστε την προστασία και ξανατρέξτε την εντολή."
        )

    # Extract page text, swallowing per-page failures (corrupt streams, etc.).
    parts: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            t = page.extract_text() or ""
        except Exception as exc:
            logger.warning("PDF page %d text extraction failed: %s", i, exc)
            t = ""
        if t:
            parts.append(t)

    text = "\n".join(parts).strip()
    metadata["char_count"] = len(text)

    # Scan heuristic: any PDF with at least one page but barely any extracted
    # text is almost certainly a scan / image-only doc.  100 chars across the
    # whole document is the threshold used in the design doc.
    if metadata["page_count"] > 0 and len(text) < 100:
        metadata["is_scan"] = True

    if max_chars and len(text) > max_chars:
        text = text[:max_chars]

    return text, metadata
