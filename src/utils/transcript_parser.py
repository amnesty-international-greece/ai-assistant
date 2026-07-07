"""Parse meeting transcript files into plain text for LLM consumption.

Supports WebVTT (.vtt), plain text (.txt), Word (.docx), and legacy
Word (.doc) formats.  Designed for board-meeting transcripts produced
by Zoom, Google Meet, or manual note-taking.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = {".vtt", ".txt", ".docx", ".doc"}


def parse_transcript(file_path: str | Path) -> str:
    """Read a transcript file and return clean plain text.

    Parameters
    ----------
    file_path : str | Path
        Path to the transcript file.

    Returns
    -------
    str
        Plain-text transcript suitable for LLM consumption.

    Raises
    ------
    FileNotFoundError
        If *file_path* does not exist.
    ValueError
        If the file extension is not supported.
    """
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"Transcript file not found: {path}")

    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported transcript format '{ext}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    if ext == ".vtt":
        return _parse_vtt(path)
    if ext == ".txt":
        return _read_text(path)
    if ext == ".docx":
        return _parse_docx(path)
    if ext == ".doc":
        return _parse_doc(path)

    # Should be unreachable, but keeps type-checkers happy.
    raise ValueError(f"Unhandled extension: {ext}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_TIMESTAMP_RE = re.compile(r"\d{2}:\d{2}:\d{2}")
_SEQUENCE_RE = re.compile(r"^\d+$")
_SPEAKER_RE = re.compile(r"^(.+?):\s*(.*)$")


def _read_file_text(path: Path) -> str:
    """Read a text file trying several encodings."""
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    # latin-1 never raises UnicodeDecodeError, so we should never land here,
    # but just in case:
    return path.read_text(encoding="latin-1", errors="replace")


def _read_text(path: Path) -> str:
    """Return plain-text content as-is."""
    return _read_file_text(path)


def _parse_vtt(path: Path) -> str:
    """Parse a WebVTT file and collapse consecutive same-speaker lines."""
    raw = _read_file_text(path)
    lines = raw.splitlines()

    text_lines: list[str] = []
    for line in lines:
        stripped = line.strip()

        # Skip empty lines
        if not stripped:
            continue
        # Skip WEBVTT header (and optional metadata lines at the top)
        if stripped.startswith("WEBVTT"):
            continue
        # Skip sequence numbers (bare digits)
        if _SEQUENCE_RE.match(stripped):
            continue
        # Skip timestamp lines
        if _TIMESTAMP_RE.match(stripped) and "-->" in stripped:
            continue

        text_lines.append(stripped)

    # Collapse consecutive lines from the same speaker
    collapsed: list[str] = []
    current_speaker: str | None = None
    current_parts: list[str] = []

    for line in text_lines:
        match = _SPEAKER_RE.match(line)
        if match:
            speaker, text = match.group(1), match.group(2)
            if speaker == current_speaker:
                # Same speaker continues
                if text:
                    current_parts.append(text)
            else:
                # New speaker - flush previous
                if current_speaker is not None:
                    collapsed.append(f"{current_speaker}: {' '.join(current_parts)}")
                current_speaker = speaker
                current_parts = [text] if text else []
        else:
            # Continuation line without a speaker label - append to current
            if current_speaker is not None:
                current_parts.append(line)
            else:
                # Orphan text before any speaker label
                collapsed.append(line)

    # Flush last speaker
    if current_speaker is not None:
        collapsed.append(f"{current_speaker}: {' '.join(current_parts)}")

    return "\n".join(collapsed)


def _parse_docx(path: Path) -> str:
    """Extract text from a .docx file using python-docx."""
    try:
        from docx import Document  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ImportError(
            "python-docx is required to parse .docx files. "
            "Install it with: pip install python-docx"
        ) from exc

    doc = Document(str(path))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n".join(paragraphs)


def _parse_doc(path: Path) -> str:
    """Extract text from a legacy .doc file.

    Tries ``antiword`` first (common on Linux), then falls back to
    ``textract`` via subprocess, and finally to a best-effort binary
    decode.
    """
    # Try antiword
    try:
        result = subprocess.run(
            ["antiword", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, OSError):
        pass

    # Try textract CLI
    try:
        result = subprocess.run(
            ["textract", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, OSError):
        pass

    # Best-effort: read binary and decode
    raw = path.read_bytes()
    for encoding in ("utf-8", "latin-1"):
        try:
            text = raw.decode(encoding, errors="replace")
            # Strip non-printable characters but keep newlines/tabs
            cleaned = re.sub(r"[^\x20-\x7E\n\r\t\u0080-\uFFFF]", "", text)
            if cleaned.strip():
                return cleaned.strip()
        except Exception:
            continue

    return raw.decode("latin-1", errors="replace").strip()
