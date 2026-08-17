"""Deterministic retrieval of background documents for minutes drafting.

The per-item drafting LLM writes better minutes when it can see the documents the
Board actually had in front of it - a briefing note's exact figures, the title of
a referenced decision, a protocol number. This module decides, WITHOUT a model,
which documents are relevant to which agenda item and returns bounded text
extracts to attach to that item's drafting call.

Two sources, both deterministic:

* **Protocol references** - decisions carry «Έχοντας υπόψη» considerations that
  cite archive documents by protocol number (``2026_031``). Any protocol number
  appearing in an item's decisions (or its transcript) is looked up in the local
  archive/briefing folders.
* **Director's briefings** - the director-briefing workflow saves εισηγητικά /
  ενημερωτικά under ``data/director_briefings/{meeting_ref}/``. These are always
  attached to the office-update item (``Ενημέρωση Γραφείου`` and friends), which
  is the item they are written for.

Design rules: relevance is decided by rule, not by a model; extracts are bounded
so a long PDF can never crowd out the transcript; and every failure is
non-fatal - a missing or unreadable document degrades to "no context", never to
a broken draft.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Archive protocol number as written in decisions/άρ. πρωτ.: "2026_031", "2026/031".
_PROTOCOL_RE = re.compile(r"\b(20\d{2})[_/](\d{1,3})\b")

# Agenda titles that always receive the Director's briefings. Matched
# accent-insensitively on the normalised title (see :func:`_norm`).
_OFFICE_UPDATE_MARKERS = (
    ("ενημερωση", "γραφειου"),
    ("ενημερωση", "διευθυντη"),
    ("εισηγηση", "διευθυντη"),
    ("πορεια", "τμηματος"),
)

_ACCENTS = str.maketrans("άέήίόύώϊϋΐΰᾶ", "αεηιουωιυιυα")

# Per-document and per-item extract ceilings: reference material must never
# crowd out the actual transcript in the drafting call.
_MAX_CHARS_PER_DOC = 4000
_MAX_CHARS_PER_ITEM = 12000


def _norm(title: str) -> str:
    """Normalise an agenda title for matching: lowercase, de-accented, collapsed."""
    return " ".join((title or "").split()).strip().lower().translate(_ACCENTS)


def extract_protocol_refs(text: str) -> list[str]:
    """Return the ``YYYY_NNN`` protocol references found in *text*, de-duplicated.

    Accepts ``2026_031`` and ``2026/031`` and normalises both to ``2026_031``
    with the sequence zero-padded to three digits, which is how the archive
    names files.
    """
    seen: list[str] = []
    for year, seq in _PROTOCOL_RE.findall(text or ""):
        ref = f"{year}_{int(seq):03d}"
        if ref not in seen:
            seen.append(ref)
    return seen


def is_office_update_item(title: str) -> bool:
    """True if this agenda item is the Director's office-update item."""
    norm = _norm(title)
    return any(all(word in norm for word in markers) for markers in _OFFICE_UPDATE_MARKERS)


def _read_document(path: Path, *, limit: int = _MAX_CHARS_PER_DOC) -> str:
    """Extract bounded text from a PDF (or plain-text) file; "" on any failure."""
    try:
        if path.suffix.lower() == ".pdf":
            from src.utils.pdf_text import extract_pdf_text

            text, _meta = extract_pdf_text(path, max_chars=limit)
            text = text or ""
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - context is best-effort, never fatal
        logger.warning("Could not read document %s: %s", path, exc)
        return ""
    text = " ".join(text.split())
    return text[:limit]


def _search_dirs(settings, meeting_ref: str) -> list[Path]:
    """Local folders searched for documents, most specific first."""
    mp = getattr(settings, "minutes_pipeline", None)
    briefings = Path(
        getattr(mp, "director_briefings_dir", "data/director_briefings")
        or "data/director_briefings"
    )
    dirs = [briefings / _safe(meeting_ref), briefings]
    out_dir = Path("data/output")
    if out_dir.exists():
        dirs.append(out_dir)
    return [d for d in dirs if d.exists()]


def _safe(ref: str) -> str:
    return re.sub(r"[^\w\-.]+", "_", ref or "", flags=re.UNICODE).strip("_") or "meeting"


def find_documents_by_protocol(refs: list[str], dirs: list[Path]) -> list[Path]:
    """Files whose name contains any of the given protocol refs (e.g. ``[2026_031]``)."""
    hits: list[Path] = []
    for directory in dirs:
        try:
            for path in sorted(directory.iterdir()):
                if not path.is_file():
                    continue
                if any(ref in path.name for ref in refs) and path not in hits:
                    hits.append(path)
        except OSError:  # pragma: no cover - unreadable dir
            continue
    return hits


def find_director_briefings(meeting_ref: str, settings) -> list[Path]:
    """The Director's εισηγητικά/ενημερωτικά saved for this meeting."""
    mp = getattr(settings, "minutes_pipeline", None)
    root = Path(
        getattr(mp, "director_briefings_dir", "data/director_briefings")
        or "data/director_briefings"
    ) / _safe(meeting_ref)
    if not root.exists():
        return []
    return [p for p in sorted(root.iterdir()) if p.is_file()]


def document_context_for_skeleton(skeleton: dict, settings) -> dict[str, str]:
    """Map ``normalised agenda title -> bounded document context`` for drafting.

    An item receives context when either rule fires:

    * a protocol reference appears in its decisions («Έχοντας υπόψη») or its
      transcript, and a matching file exists locally; or
    * it is the office-update item, which always receives the Director's
      briefings for this meeting.

    Items with no relevant documents are simply absent from the mapping, so the
    drafting call stays clean. Never raises.
    """
    try:
        meeting_ref = skeleton.get("meeting_ref") or ""
        dirs = _search_dirs(settings, meeting_ref)
        briefings = find_director_briefings(meeting_ref, settings)

        decisions_by_title: dict[str, list[dict]] = {}
        for decision in skeleton.get("decisions") or []:
            decisions_by_title.setdefault(
                _norm(decision.get("agenda_item") or ""), []
            ).append(decision)

        context: dict[str, str] = {}
        for item in skeleton.get("items") or []:
            title = item.get("title") or ""
            key = _norm(title)
            paths: list[Path] = []

            # Rule 1: protocol refs cited by this item's decisions, then its talk.
            haystack = " ".join(
                json_bits(d) for d in decisions_by_title.get(key, [])
            ) + " " + " ".join(
                (s.get("text") or "") for s in (item.get("segments") or [])
            )
            refs = extract_protocol_refs(haystack)
            if refs:
                paths.extend(find_documents_by_protocol(refs, dirs))

            # Rule 2: the office-update item always gets the Director's briefings.
            if is_office_update_item(title):
                paths.extend(p for p in briefings if p not in paths)

            if not paths:
                continue

            chunks: list[str] = []
            used = 0
            for path in paths:
                text = _read_document(path)
                if not text:
                    continue
                chunk = f"### {path.name}\n{text}"
                if used + len(chunk) > _MAX_CHARS_PER_ITEM:
                    break
                chunks.append(chunk)
                used += len(chunk)
            if chunks:
                context[key] = "\n\n".join(chunks)
                logger.info(
                    "Attached %d document(s) to agenda item %r", len(chunks), title
                )
        return context
    except Exception as exc:  # noqa: BLE001 - context is best-effort
        logger.warning("Document context unavailable: %s", exc)
        return {}


def json_bits(decision: dict) -> str:
    """Flatten a decision's text fields (incl. «Έχοντας υπόψη») for ref scanning."""
    parts = [
        str(decision.get("decision_text") or ""),
        str(decision.get("ref") or ""),
        str(decision.get("outcome") or ""),
    ]
    parts.extend(str(c) for c in (decision.get("considerations") or []))
    return " ".join(parts)
