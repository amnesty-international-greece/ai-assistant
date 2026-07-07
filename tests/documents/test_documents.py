"""Tests for document generation."""

import pytest
from pathlib import Path


def test_generate_pdf(tmp_path):
    """PDF generation should create a valid file."""
    from src.documents.pdf_generator import generate_pdf
    from unittest.mock import patch

    with patch("src.core.audit._DB_PATH", tmp_path / "test.db"), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()

        content = {
            "title": "Test Document",
            "subtitle": "Testing PDF Generation",
            "sections": [
                {"heading": "Section 1", "body": "This is the first section."},
                {"heading": "Section 2", "body": "This is the second section."},
            ],
            "footer": "Test Footer",
        }
        output = tmp_path / "test.pdf"
        result = generate_pdf(content, output)
        assert result.exists()
        assert result.stat().st_size > 0


def test_generate_docx(tmp_path):
    """DOCX generation should create a valid file."""
    from src.documents.docx_generator import generate_docx
    from unittest.mock import patch

    with patch("src.core.audit._DB_PATH", tmp_path / "test.db"), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()

        content = {
            "title": "Πρακτικά Συνεδρίασης",
            "subtitle": "ΔΣ #42 - 2026-04-01",
            "sections": [
                {"heading": "Παρόντες", "body": "Μέλη Α, Β, Γ"},
                {"heading": "Αποφάσεις", "body": "Εγκρίθηκε η πρόταση."},
            ],
            "metadata": {"author": "Γενικός Γραμματέας"},
        }
        output = tmp_path / "test.docx"
        result = generate_docx(content, output)
        assert result.exists()
        assert result.stat().st_size > 0
