"""Convert non-PDF inputs to PDF via LibreOffice headless.

Used by the archive workflow's intake step so board members can email a DOCX,
ODT, or image and have it archived as a PDF without manual conversion.

Supported source types (mapped to LibreOffice's native conversion):

| Extension | Source format             | LibreOffice can convert? |
|-----------|---------------------------|--------------------------|
| .docx     | Microsoft Word            | Yes                      |
| .doc      | Legacy Word               | Yes                      |
| .odt      | OpenDocument Text         | Yes                      |
| .rtf      | Rich Text Format          | Yes                      |
| .xlsx     | Microsoft Excel           | Yes                      |
| .ods      | OpenDocument Spreadsheet  | Yes                      |
| .pptx     | Microsoft PowerPoint      | Yes                      |
| .odp      | OpenDocument Presentation | Yes                      |
| .jpg/.jpeg/.png/.bmp/.tiff | Images       | Yes (single-page PDF)    |
| .heic/.heif | iPhone photos           | NO — requires pre-step   |

HEIC handling: LibreOffice does NOT understand HEIC.  If the input is HEIC
and pillow-heif is available, we convert HEIC → PNG first, then PNG → PDF.

LibreOffice binary discovery follows the same logic as ``scripts/office/soffice.py``:
checks ``$SOFFICE_BIN``, then ``soffice`` on PATH, then common install dirs
(``C:/Program Files/LibreOffice/program/soffice.exe`` on Windows; ``/usr/bin/soffice``
or ``/Applications/LibreOffice.app/Contents/MacOS/soffice`` elsewhere).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


# Extensions LibreOffice can convert directly to PDF.  Lower-case.
LIBREOFFICE_SOURCE_EXTS: frozenset[str] = frozenset({
    ".docx", ".doc", ".odt", ".rtf",
    ".xlsx", ".xls", ".ods", ".csv",
    ".pptx", ".ppt", ".odp",
    ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".gif",
})

# Extensions that need a pre-conversion step before LibreOffice can touch them.
PRE_CONVERT_EXTS: frozenset[str] = frozenset({".heic", ".heif"})


class ConversionError(RuntimeError):
    """Raised when a non-PDF input cannot be converted (binary missing, etc.)."""


def is_pdf(path: Path) -> bool:
    """Cheap check: filename ends in .pdf (case-insensitive)."""
    return path.suffix.lower() == ".pdf"


def needs_conversion(path: Path) -> bool:
    """True if this file is not a PDF and we have a conversion path for it."""
    ext = path.suffix.lower()
    return (not is_pdf(path)) and (ext in LIBREOFFICE_SOURCE_EXTS or ext in PRE_CONVERT_EXTS)


def _find_soffice() -> str | None:
    """Locate the LibreOffice binary or return None."""
    # 1. Explicit env override
    env_bin = os.environ.get("SOFFICE_BIN") or os.environ.get("LIBREOFFICE_BIN")
    if env_bin and Path(env_bin).exists():
        return env_bin

    # 2. PATH lookup (Linux/macOS + Windows if user added it)
    for candidate in ("soffice", "soffice.exe", "libreoffice"):
        located = shutil.which(candidate)
        if located:
            return located

    # 3. Common install locations
    candidates: list[str] = []
    if sys.platform == "win32":
        candidates += [
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]
    elif sys.platform == "darwin":
        candidates += ["/Applications/LibreOffice.app/Contents/MacOS/soffice"]
    else:
        candidates += [
            "/usr/bin/soffice",
            "/usr/local/bin/soffice",
            "/opt/libreoffice/program/soffice",
        ]
    for path in candidates:
        if Path(path).exists():
            return path
    return None


def _heic_to_png(heic_path: Path, dest_dir: Path) -> Path:
    """Convert a HEIC image to PNG so LibreOffice can ingest it.

    Uses pillow-heif if installed; otherwise raises ConversionError with a
    pointer to ``pip install pillow-heif``.
    """
    try:
        from PIL import Image
        import pillow_heif  # type: ignore[import-not-found]
        pillow_heif.register_heif_opener()
    except ImportError as e:  # pragma: no cover — depends on environment
        raise ConversionError(
            f"HEIC support requires pillow-heif (got: {e}).  "
            "Install with: pip install pillow-heif"
        )
    out = dest_dir / (heic_path.stem + ".png")
    img = Image.open(heic_path)
    img.save(out, format="PNG")
    return out


def convert_to_pdf(
    src_path: Path,
    *,
    dest_dir: Path | None = None,
    timeout_seconds: int = 60,
) -> Path:
    """Convert ``src_path`` to PDF and return the path to the produced file.

    Args:
        src_path:       The non-PDF input.  Must exist.
        dest_dir:       Directory to write the PDF into.  Defaults to a temp
                        directory under the system tempdir; caller is
                        responsible for cleanup.
        timeout_seconds: Max seconds to allow LibreOffice to run before we
                        give up and raise.

    Returns:
        Path to the produced ``.pdf`` file.

    Raises:
        ConversionError: if LibreOffice isn't installed, or the conversion
        process fails / times out, or the source format isn't supported.
    """
    if not src_path.exists():
        raise ConversionError(f"Source file not found: {src_path}")
    if is_pdf(src_path):
        raise ConversionError(f"Source is already a PDF: {src_path}")

    ext = src_path.suffix.lower()
    if ext not in LIBREOFFICE_SOURCE_EXTS and ext not in PRE_CONVERT_EXTS:
        raise ConversionError(
            f"Unsupported source extension {ext!r}.  "
            f"Supported: PDF (direct), {', '.join(sorted(LIBREOFFICE_SOURCE_EXTS | PRE_CONVERT_EXTS))}."
        )

    if dest_dir is None:
        dest_dir = Path(tempfile.mkdtemp(prefix="ai_assistant_pdf_"))
    dest_dir.mkdir(parents=True, exist_ok=True)

    # HEIC pre-conversion
    work_input = src_path
    if ext in PRE_CONVERT_EXTS:
        work_input = _heic_to_png(src_path, dest_dir)

    soffice = _find_soffice()
    if not soffice:
        raise ConversionError(
            "LibreOffice (soffice) not found.  Install it or set $SOFFICE_BIN.\n"
            "  Windows: https://www.libreoffice.org/download/download/\n"
            "  macOS:   brew install --cask libreoffice\n"
            "  Linux:   apt install libreoffice  (or your distro's equivalent)"
        )

    cmd = [
        soffice,
        "--headless",
        "--convert-to", "pdf",
        "--outdir", str(dest_dir),
        str(work_input),
    ]
    logger.info("Converting to PDF: %s → %s/", work_input.name, dest_dir)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ConversionError(
            f"LibreOffice timed out after {timeout_seconds}s converting {src_path.name}."
        )
    except FileNotFoundError as e:
        raise ConversionError(f"Could not invoke soffice: {e}")

    if result.returncode != 0:
        raise ConversionError(
            f"LibreOffice conversion failed (exit {result.returncode}): "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    # soffice names the output ``{stem}.pdf`` in the output dir
    produced = dest_dir / (work_input.stem + ".pdf")
    if not produced.exists():
        # Some LibreOffice builds name the output after the original stem if
        # the input was renamed during conversion — scan the dir as a fallback.
        for f in dest_dir.glob("*.pdf"):
            if f.stat().st_size > 0:
                produced = f
                break
    if not produced.exists():
        raise ConversionError(
            f"LibreOffice ran cleanly but produced no PDF in {dest_dir}"
        )

    logger.info("PDF produced: %s (%d bytes)", produced, produced.stat().st_size)
    return produced
