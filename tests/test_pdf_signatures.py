"""Tests for PDF signature embedding."""
import pytest
from pathlib import Path
from unittest.mock import patch

@pytest.fixture
def mock_db(tmp_path):
    with patch("src.core.audit._DB_PATH", tmp_path / "test.db"), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        yield

def _make_tiny_png(path):
    """Create a minimal valid PNG file for testing."""
    import struct, zlib
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr_data = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
    ihdr_crc = zlib.crc32(b'IHDR' + ihdr_data) & 0xffffffff
    ihdr = struct.pack('>I', 13) + b'IHDR' + ihdr_data + struct.pack('>I', ihdr_crc)
    raw = zlib.compress(b'\x00\xff\xff\xff')
    idat_crc = zlib.crc32(b'IDAT' + raw) & 0xffffffff
    idat = struct.pack('>I', len(raw)) + b'IDAT' + raw + struct.pack('>I', idat_crc)
    iend_crc = zlib.crc32(b'IEND') & 0xffffffff
    iend = struct.pack('>I', 0) + b'IEND' + struct.pack('>I', iend_crc)
    path.write_bytes(sig + ihdr + idat + iend)

def test_embed_signatures(mock_db, tmp_path):
    """embed_signatures should overlay signature images on a PDF."""
    from src.documents.pdf_generator import generate_pdf, embed_signatures

    # Generate a simple PDF first
    content = {"title": "Test Document", "sections": [{"heading": "Test", "body": "Content here."}]}
    pdf_path = tmp_path / "test.pdf"
    generate_pdf(content, pdf_path)
    assert pdf_path.exists()

    # Create fake signature images
    sig1 = tmp_path / "sig_president.png"
    sig2 = tmp_path / "sig_secgen.png"
    _make_tiny_png(sig1)
    _make_tiny_png(sig2)

    output_path = tmp_path / "signed.pdf"
    result = embed_signatures(
        pdf_path,
        output_path,
        signatures=[
            {"image_path": str(sig1), "x": 100, "y": 100, "width": 80, "height": 30, "label": "Ο Πρόεδρος"},
            {"image_path": str(sig2), "x": 350, "y": 100, "width": 80, "height": 30, "label": "Ο Γενικός Γραμματέας"},
        ],
    )
    assert result.exists()
    assert result.stat().st_size > 0
