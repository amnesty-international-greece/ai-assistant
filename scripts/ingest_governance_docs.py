"""Ingest the Καταστατικό + Εσωτερικοί Κανονισμοί PDFs into a structured,
article-level JSON reference used to ground decision drafting (the
«Το Διοικητικό Συμβούλιο, έχοντας υπόψη: … του άρθρου N του Καταστατικού»
citations).

One-time / re-runnable. Reads the two PDFs in ``framework/`` and writes
``assets/governance/articles.json``. Pure offline text processing (PyPDF2).

Usage:
    python -m scripts.ingest_governance_docs
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import PyPDF2

_ROOT = Path(__file__).resolve().parent.parent
_FRAMEWORK = _ROOT / "framework"
_OUT = _ROOT / "assets" / "governance" / "articles.json"

# (filename without extension, short doc label used in citations)
_DOCS = [
    ("Καταστατικό Ελληνικού Τμήματος Διεθνούς Αμνηστίας", "Καταστατικό"),
    ("Εσωτερικοί Κανονισμοί Ελληνικού Τμήματος Διεθνούς Αμνηστίας", "Εσωτερικοί Κανονισμοί"),
]

# "Άρθρο 15. Τίτλος" — tolerant of the extra spaces PyPDF2 injects.
_ARTICLE_RE = re.compile(r"Άρθρο\s+(\d+)\s*\.?\s*([^\n]*)")


def _flat(text: str) -> str:
    """Flatten ALL whitespace (PyPDF2 scatters spaces and newlines between
    syllables) to single spaces — the only reliable normalisation across both
    documents' wildly different extracted layouts."""
    return re.sub(r"\s+", " ", text).strip()


def _extract_full_text(pdf_path: Path) -> str:
    reader = PyPDF2.PdfReader(str(pdf_path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _split_articles(doc_label: str, full_text: str) -> list[dict]:
    """Slice the flattened text between sequential ``Άρθρο N`` headers.

    Capital ``Ά`` matches only the nominative header form, not the lowercase
    genitive ``άρθρου`` used in cross-citations. We additionally require the
    article number to be strictly increasing, which drops any stray in-body
    ``Άρθρο N`` reference (statutes are numbered sequentially).
    """
    flat = _flat(full_text)
    raw = list(re.compile(r"Άρθρο\s+(\d+)").finditer(flat))

    # Keep only strictly-increasing article numbers (real headers, in order).
    kept: list[tuple[int, re.Match]] = []
    last = 0
    for m in raw:
        n = int(m.group(1))
        if n > last:
            kept.append((n, m))
            last = n

    articles: list[dict] = []
    for i, (number, m) in enumerate(kept):
        end = kept[i + 1][1].start() if i + 1 < len(kept) else len(flat)
        body = flat[m.end():end].strip(" .·—-")
        # Title = leading clause up to the first period (titles are short:
        # "Επωνυμία", "Σκοποί", ...); fall back to the first 60 chars.
        head = body[:120]
        title = head.split(".")[0].strip() if "." in head else head[:60].strip()
        articles.append({
            "doc": doc_label,
            "article": number,
            "title": title,
            "text": body,
        })
    return articles


def main() -> None:
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    all_articles: list[dict] = []
    source_docs: list[str] = []
    for filename, label in _DOCS:
        pdf_path = _FRAMEWORK / f"{filename}.pdf"
        if not pdf_path.exists():
            print(f"WARNING: missing {pdf_path}")
            continue
        full = _extract_full_text(pdf_path)
        arts = _split_articles(label, full)
        print(f"{label}: {len(arts)} articles")
        all_articles.extend(arts)
        source_docs.append(filename)

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "source_docs": source_docs,
        "article_count": len(all_articles),
        "articles": all_articles,
    }
    _OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(all_articles)} articles to {_OUT}")


if __name__ == "__main__":
    main()
