"""LLM helpers for the archive workflow.

Three entry points, all async:

* ``classify_document`` - first-pass extraction.  Pulls live taxonomy +
  categories from the πρωτόκολλο xlsx and asks the LLM to propose a
  ``{title, labels, key_points, existing_protocol, category_matched,
  confidence, reasoning_brief}`` dict.
* ``refine_against_recent`` - second pass for low-confidence / ad-hoc
  classifications.  Anchors the choices to the archive's recent style.
* ``parse_user_feedback`` - used by ``ai-assistant archive review`` to convert
  free-text feedback into structured amendments
  (``intent``: acknowledge / amend / cancel / unrelated).

All three reuse ``ClaudeClient`` (which routes to Gemini per ``llm.provider``
in config.yaml - Phase 1 default is Gemini).

The prompts live in this module as plain Python strings, deliberately kept
short and editable.  They're not in ``assets/email_templates/`` because they're
not user-facing copy - they're internal instructions to the model.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from src.core.claude import ClaudeClient

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Prompt templates - kept inline for editability; no f-string in the bodies so
# the literal curly braces in the JSON skeleton don't trip on placeholders.
# ──────────────────────────────────────────────────────────────────────────────

_PROMPT_CLASSIFY = """\
You are the archival assistant for the Greek section of Amnesty International.
Decide how to file the attached document in the institutional archive of the
Board of Directors (Διοικητικό Συμβούλιο).

DOCUMENT
========
Filename:        {filename}
Sender:          {sender_name} <{sender_email}>
Email subject:   {subject}
Email body:      {body}
PDF text (first 5000 chars):
{pdf_text}

{pdf_warning}

TAXONOMY (live from Ετικέτες tab of [Πρωτόκολλο] Αρχείο ΔΣ.xlsx)
================================================================
{tag_descriptions_block}

CANONICAL PATTERNS (live from Κατηγορίες tab)
==============================================
{categories_block}

GENERAL RULES
=============
- Pick 1 to 3 tags typically (4 only for truly cross-cutting documents).
- Aim for consistency with the rest of the archive - match the style of
  titles, tag combinations, and Κύρια Σημεία used in recent entries.
- For documents originally in English, keep their original-language title.
- Tag-specific usage rules are in each tag's description (TAXONOMY section
  above) - follow them.
- **Title selection (CRITICAL - read carefully):**
  • If the filename has the format ``[YYYY_NNN] <Title>.pdf``, use **the
    exact text between the bracket and the extension** as the title.
    Don't paraphrase, don't substitute names, don't translate - copy it
    verbatim.
  • The Sender field is who SUBMITTED the document - they're rarely the
    *subject* of the document.  Do NOT put the sender's name into the
    title unless the PDF text itself confirms they're the subject (e.g.
    a candidacy paper named "Υποψηφιότητα - <Name>" where <Name> matches
    the sender).
  • When in doubt, prefer the filename's title over any inference from
    sender or PDF text.

PROTOCOL NUMBER DETECTION
=========================
Look for an αρ.πρωτ. in the filename (e.g. "[2026_017] ..."), the PDF text
("Αρ. Πρωτ.: 2026_017"), or the email body.  Report it as `existing_protocol`.
If you only see a year or a fragment, leave it null.

OUTPUT
======
Strict JSON, no preamble, no markdown fences:

{{
  "title": "...",
  "labels": ["...", "..."],
  "key_points": "...",
  "existing_protocol": "YYYY_NNN" | null,
  "category_matched": "...",
  "confidence": 0.0,
  "reasoning_brief": "one sentence why these choices"
}}
"""


_PROMPT_REFINE = """\
You are the archival assistant for the Greek section of Amnesty International.
A first pass already produced a draft classification for the document below.
Your job is to refine it so it matches the archive's existing conventions -
the recent entries shown.  Adjust title casing/abbreviations, tag combinations,
and Κύρια Σημεία verbosity to match the style of the RECENT EXAMPLES.  Keep the
same fields as the first pass.

DOCUMENT
========
Filename:        {filename}
Sender:          {sender_name} <{sender_email}>
Email subject:   {subject}

FIRST-PASS RESULT
=================
{first_pass_json}

RECENT EXAMPLES (last {recent_count} archived entries, oldest first)
====================================================================
{recent_entries_block}

INSTRUCTIONS
============
- Keep the original-language title for foreign documents.
- Do NOT introduce tags that don't appear in the recent examples or in the
  first-pass result.
- If the recent examples consistently use a verbose Κύρια Σημεία style for
  this kind of document, expand the draft to match; if they're terse, trim it.
- Bump `confidence` only if you genuinely have higher confidence after the
  comparison.

OUTPUT
======
Strict JSON, same shape as the first pass, no preamble, no markdown fences.
"""


_PROMPT_REVIEW = """\
You are parsing free-text feedback from the Secretary General of Amnesty
International Greece about an archived document.  Decide what the SecGen wants
done and return strict JSON.

ORIGINAL ARCHIVE ENTRY
======================
{original_json}

SECGEN'S MESSAGE
================
{user_text}

POSSIBLE INTENTS
================
- "acknowledge": SecGen is confirming the entry is correct (e.g. "ok", "thanks",
  "looks good") - no changes needed.
- "amend": SecGen wants a field changed (title, labels, key_points, protocol_id).
- "cancel": SecGen wants the archive entry removed entirely.
- "unrelated": the message isn't about this archive entry at all.

OUTPUT
======
Strict JSON, no preamble, no markdown fences:

{{
  "intent": "acknowledge" | "amend" | "cancel" | "unrelated",
  "amendments": {{
    "title": "..." | null,
    "labels": ["...", "..."] | null,
    "key_points": "..." | null,
    "protocol_id": "YYYY_NNN" | null
  }},
  "confidence": 0.0,
  "summary_for_human": "one sentence in Greek describing what you understood"
}}

For `acknowledge`, `cancel`, and `unrelated` intents, set every field inside
`amendments` to null.  For `amend`, only fill the fields the SecGen actually
mentioned; leave the rest null.
"""


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _strip_json_fences(text: str) -> str:
    """Remove ```json / ``` fences that some models still emit despite instructions."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
    if t.endswith("```"):
        t = t.rsplit("```", 1)[0]
    return t.strip()


def _safe_json_loads(raw: str, *, default: dict | None = None) -> dict:
    """Parse JSON, returning *default* (or an empty dict) on any failure."""
    try:
        return json.loads(_strip_json_fences(raw))
    except (ValueError, TypeError) as exc:
        logger.warning("archive_llm: JSON parse failed (%s); raw=%r", exc, raw[:200])
        return dict(default) if default is not None else {}


def _format_tag_block(tags: list[dict[str, str]]) -> str:
    if not tags:
        return "(no taxonomy rows found in Ετικέτες tab)"
    return "\n".join(f"  - {t.get('tag', '')}: {t.get('description', '')}" for t in tags)


def _format_categories_block(cats: list[dict[str, str]]) -> str:
    if not cats:
        return "(no canonical patterns found in Κατηγορίες tab)"
    lines: list[str] = []
    for c in cats:
        lines.append(f"  - Pattern: {c.get('pattern', '')}")
        lines.append(f"    Tags:    {c.get('tags', '')}")
        lines.append(f"    Σημεία:  {c.get('kuria_simeia', '')}")
    return "\n".join(lines)


def _format_recent_block(entries: list[dict[str, str]]) -> str:
    if not entries:
        return "(no recent entries found)"
    lines: list[str] = []
    for e in entries:
        lines.append(
            f"  - [{e.get('proto', '')}] {e.get('date', '')} - {e.get('title', '')}"
        )
        lines.append(f"    Tags:   {e.get('tags', '')}")
        if e.get("key_points"):
            kp = e["key_points"].replace("\n", " / ")
            lines.append(f"    Σημεία: {kp[:200]}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


async def classify_document(
    *,
    filename: str,
    sender_email: str,
    sender_name: str = "",
    subject: str = "",
    body: str = "",
    pdf_text: str = "",
    pdf_metadata: dict | None = None,
    taxonomy: list[dict[str, str]] | None = None,
    categories: list[dict[str, str]] | None = None,
    llm: ClaudeClient | None = None,
    workflow: str = "archive",
) -> dict[str, Any]:
    """Run the first-pass classification.

    Pulls live taxonomy + categories via OneDriveClient if the caller doesn't
    provide them (tests pass them explicitly to avoid a real network call).
    Returns the parsed JSON dict from the prompt - missing fields are filled
    with sensible defaults so downstream callers don't need to handle absence.
    """
    if taxonomy is None or categories is None:
        # Lazy import to keep this module light when callers (tests) inject.
        from src.integrations.onedrive import OneDriveClient
        client = OneDriveClient()
        # One download covers both tabs - halves the SharePoint round-trips
        # this step makes (previously 2× ~100 KB → 1×).
        if taxonomy is None and categories is None:
            taxonomy, categories = await client.read_taxonomy_and_categories()
        elif taxonomy is None:
            taxonomy = await client.read_taxonomy()
        elif categories is None:
            categories = await client.read_categories()

    pdf_warning = ""
    if pdf_metadata and pdf_metadata.get("is_scan"):
        pdf_warning = (
            "NOTE: PDF appears to be a scan - text extraction limited. "
            "Lean on filename / sender / subject when deciding."
        )

    prompt = _PROMPT_CLASSIFY.format(
        filename=filename or "(unknown)",
        sender_name=sender_name or "",
        sender_email=sender_email or "(unknown sender)",
        subject=subject or "(no subject)",
        body=(body or "")[:1000],
        pdf_text=(pdf_text or "")[:5000] or "(no extractable text)",
        pdf_warning=pdf_warning,
        tag_descriptions_block=_format_tag_block(taxonomy),
        categories_block=_format_categories_block(categories),
    )

    llm = llm or ClaudeClient()
    raw = llm.generate(
        user_prompt=prompt,
        system_prompt=(
            "You are a careful archivist.  Output strict JSON only, "
            "no preamble, no markdown fences."
        ),
        workflow=workflow,
    )

    result = _safe_json_loads(raw)
    return {
        "title": result.get("title") or filename,
        "labels": list(result.get("labels") or []),
        "key_points": result.get("key_points") or "",
        "existing_protocol": result.get("existing_protocol"),
        "category_matched": result.get("category_matched") or "ad-hoc",
        "confidence": float(result.get("confidence") or 0.0),
        "reasoning_brief": result.get("reasoning_brief") or "",
    }


async def refine_against_recent(
    initial_result: dict[str, Any],
    recent_entries: list[dict[str, str]],
    document_context: dict[str, Any],
    *,
    llm: ClaudeClient | None = None,
    workflow: str = "archive",
) -> dict[str, Any]:
    """Run the second-pass refinement against recent archive entries.

    Args:
        initial_result:    Output of :func:`classify_document`.
        recent_entries:    Output of ``OneDriveClient.read_recent_entries``.
        document_context:  Dict with at least ``filename`` / ``sender_email`` /
                           ``subject`` keys (the same payload that was fed to
                           the first pass).
    """
    prompt = _PROMPT_REFINE.format(
        filename=document_context.get("filename", ""),
        sender_name=document_context.get("sender_name", ""),
        sender_email=document_context.get("sender_email", ""),
        subject=document_context.get("subject", ""),
        first_pass_json=json.dumps(initial_result, ensure_ascii=False, indent=2),
        recent_count=len(recent_entries),
        recent_entries_block=_format_recent_block(recent_entries),
    )

    llm = llm or ClaudeClient()
    raw = llm.generate(
        user_prompt=prompt,
        system_prompt=(
            "You are a careful archivist refining a draft classification.  "
            "Output strict JSON only, no preamble, no markdown fences."
        ),
        workflow=workflow,
    )

    refined = _safe_json_loads(raw, default=initial_result)
    return {
        "title": refined.get("title") or initial_result.get("title", ""),
        "labels": list(refined.get("labels") or initial_result.get("labels", [])),
        "key_points": refined.get("key_points")
            if refined.get("key_points") is not None
            else initial_result.get("key_points", ""),
        "existing_protocol": refined.get("existing_protocol")
            if "existing_protocol" in refined
            else initial_result.get("existing_protocol"),
        "category_matched": refined.get("category_matched")
            or initial_result.get("category_matched", "ad-hoc"),
        "confidence": float(refined.get("confidence") or initial_result.get("confidence", 0.0)),
        "reasoning_brief": refined.get("reasoning_brief") or "",
    }


async def parse_user_feedback(
    workflow_id: str,
    original: dict[str, Any],
    user_text: str,
    *,
    llm: ClaudeClient | None = None,
    workflow: str = "archive",
) -> dict[str, Any]:
    """Parse free-text SecGen feedback into a structured intent + amendments dict."""
    prompt = _PROMPT_REVIEW.format(
        original_json=json.dumps(original, ensure_ascii=False, indent=2),
        user_text=(user_text or "").strip() or "(empty)",
    )
    llm = llm or ClaudeClient()
    raw = llm.generate(
        user_prompt=prompt,
        system_prompt=(
            "You parse user feedback into strict JSON.  Output JSON only, "
            "no preamble, no markdown fences."
        ),
        workflow=workflow,
    )
    parsed = _safe_json_loads(raw)
    intent = (parsed.get("intent") or "unrelated").lower()
    if intent not in ("acknowledge", "amend", "cancel", "unrelated"):
        intent = "unrelated"
    amendments = parsed.get("amendments") or {}
    return {
        "workflow_id": workflow_id,
        "intent": intent,
        "amendments": {
            "title": amendments.get("title"),
            "labels": amendments.get("labels"),
            "key_points": amendments.get("key_points"),
            "protocol_id": amendments.get("protocol_id"),
        },
        "confidence": float(parsed.get("confidence") or 0.0),
        "summary_for_human": parsed.get("summary_for_human") or "",
    }
