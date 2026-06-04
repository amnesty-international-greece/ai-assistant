"""Board-decision drafter — turns meeting discussion into a canonical Greek decision.

A board decision in the official minutes follows a fixed shape::

    Το Διοικητικό Συμβούλιο, έχοντας υπόψη:
    1. <consideration>
    2. <consideration>
    ΑΠΟΦΑΣΗ ΔΣ01-05-2026
    <operative decision text, 3rd-person present>

This module owns:
  * the *deterministic* decision-ref computation (NOT the model's job),
  * a pure article-retrieval step over the pre-ingested governance corpus
    (``assets/governance/articles.json``),
  * a ``DecisionDrafter`` Protocol seam so the orchestrator + ref logic can be
    unit-tested with a fake drafter, and
  * a thin :class:`LLMDecisionDrafter` that reuses the project's existing
    :class:`src.core.claude.ClaudeClient` (no new SDK path).

The module imports without any LLM SDK installed/configured: the heavy client
is lazy-imported inside :meth:`LLMDecisionDrafter.draft`.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Protocol

# Default location of the pre-ingested governance corpus (see
# scripts/ingest_governance_docs.py — do NOT modify it here).
_DEFAULT_ARTICLES_PATH = "assets/governance/articles.json"

_REF_CORE_RE = re.compile(r"^\d+-\d+$")
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


# --------------------------------------------------------------------------- #
# Deterministic decision ref
# --------------------------------------------------------------------------- #
def compute_decision_ref(meeting_ref: str, sequence: int) -> str:
    """Compute the canonical decision ref ``ΔΣ{NN}-{MM}-{YYYY}``.

    ``meeting_ref`` like ``"ΔΣ05-2026"`` → core ``"05-2026"``; the decision ref
    for the *sequence*-th decision of that meeting (1-based) is
    ``"ΔΣ{sequence:02d}-05-2026"`` (e.g. ``"ΔΣ01-05-2026"``).

    Args:
        meeting_ref: Meeting identifier, e.g. ``"ΔΣ05-2026"``.
        sequence:    1-based sequence number of the decision within the meeting.

    Raises:
        ValueError: if *meeting_ref* lacks a ``\\d+-\\d+`` core, or *sequence* < 1.
    """
    core = meeting_ref.replace("ΔΣ", "", 1).strip() if meeting_ref.startswith("ΔΣ") else meeting_ref.strip()
    core = core.strip()
    if not _REF_CORE_RE.match(core):
        raise ValueError(
            f"Invalid meeting_ref {meeting_ref!r}: expected leading 'ΔΣ' "
            f"then a 'MM-YYYY' core matching \\d+-\\d+, got core {core!r}."
        )
    if sequence < 1:
        raise ValueError(
            f"Invalid sequence {sequence!r}: must be >= 1 (1-based)."
        )
    return f"ΔΣ{sequence:02d}-{core}"


# --------------------------------------------------------------------------- #
# Article retrieval (pure)
# --------------------------------------------------------------------------- #
def load_articles(path: str | None = None) -> list[dict]:
    """Load the governance article corpus; return ``[]`` if the file is missing.

    Reads the ``"articles"`` list out of the JSON produced by the ingestion
    script. A missing path returns ``[]`` without raising.
    """
    p = Path(path) if path is not None else Path(_DEFAULT_ARTICLES_PATH)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    articles = data.get("articles", []) if isinstance(data, dict) else []
    return articles if isinstance(articles, list) else []


def _normalise(text: str) -> str:
    """Lowercase + strip Greek τόνους for token matching (deterministic)."""
    decomposed = unicodedata.normalize("NFD", text)
    out = []
    for ch in decomposed:
        if ch in ("́", "̈́"):  # combining acute (tonos) / acute+diaeresis
            continue
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out)).lower()


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(_normalise(text)))


def select_relevant_articles(
    query_text: str, articles: list[dict], *, k: int = 5
) -> list[dict]:
    """Return the top-*k* articles by token overlap with *query_text*.

    Scoring: number of distinct normalised tokens shared between *query_text*
    and each article's ``title`` + ``text``. Zero-score articles are dropped.
    Stable sort (descending by score, ties keep original corpus order) so the
    result is pure and deterministic.
    """
    query_tokens = _tokens(query_text)
    if not query_tokens:
        return []
    scored: list[tuple[int, int, dict]] = []
    for idx, art in enumerate(articles):
        haystack = f"{art.get('title', '')} {art.get('text', '')}"
        score = len(query_tokens & _tokens(haystack))
        if score > 0:
            scored.append((score, idx, art))
    # Sort by score desc, then original index asc (stable).
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [art for _score, _idx, art in scored[:k]]


# --------------------------------------------------------------------------- #
# The seam
# --------------------------------------------------------------------------- #
class DecisionDrafter(Protocol):
    """Pluggable drafter — the LLM-dependent part, isolated for testability."""

    def draft(
        self,
        *,
        transcript_snippet: str,
        agenda_item: str,
        decision_ref: str,
        candidate_articles: list[dict],
        prior_decisions: list[dict],
    ) -> dict:
        """Returns {"considerations": list[str], "decision_text": str}."""
        ...


# --------------------------------------------------------------------------- #
# Orchestrator (pure given a drafter)
# --------------------------------------------------------------------------- #
def propose_decision(
    *,
    meeting_ref: str,
    sequence: int,
    transcript_snippet: str,
    drafter: DecisionDrafter,
    agenda_item: str = "",
    articles: list[dict] | None = None,
    prior_decisions: list[dict] | None = None,
) -> dict:
    """Assemble a decision proposal: ref + grounded considerations + operative text.

    Deterministic except for the *drafter.draft* call. Computes the canonical
    ref, retrieves candidate articles via token overlap, hands them (plus the
    prior decisions and transcript) to the drafter, and returns:

        {"ref": str, "considerations": list[str], "decision_text": str,
         "candidate_articles": list[dict]}
    """
    ref = compute_decision_ref(meeting_ref, sequence)
    articles = articles if articles is not None else load_articles()
    candidates = select_relevant_articles(
        transcript_snippet + " " + agenda_item, articles
    )
    result = drafter.draft(
        transcript_snippet=transcript_snippet,
        agenda_item=agenda_item,
        decision_ref=ref,
        candidate_articles=candidates,
        prior_decisions=prior_decisions or [],
    )
    return {
        "ref": ref,
        "considerations": result["considerations"],
        "decision_text": result["decision_text"],
        "candidate_articles": candidates,
    }


# --------------------------------------------------------------------------- #
# Render to canonical Greek
# --------------------------------------------------------------------------- #
def render_decision(proposal: dict) -> str:
    """Render *proposal* to the canonical Greek decision block.

    With considerations::

        Το Διοικητικό Συμβούλιο, έχοντας υπόψη:
        1. <consideration[0]>
        2. <consideration[1]>
        ΑΠΟΦΑΣΗ <ref>
        <decision_text>

    With no considerations the «έχοντας υπόψη» header is omitted and only the
    ``ΑΠΟΦΑΣΗ`` block is emitted.
    """
    considerations = proposal.get("considerations") or []
    ref = proposal["ref"]
    decision_text = proposal["decision_text"]

    lines: list[str] = []
    if considerations:
        lines.append("Το Διοικητικό Συμβούλιο, έχοντας υπόψη:")
        for i, cons in enumerate(considerations, start=1):
            lines.append(f"{i}. {cons}")
    lines.append(f"ΑΠΟΦΑΣΗ {ref}")
    lines.append(decision_text)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Lazy real drafter (thin; not unit-tested against a real LLM)
# --------------------------------------------------------------------------- #
_SYSTEM_PROMPT = """\
Είσαι βοηθός σύνταξης αποφάσεων του Διοικητικού Συμβουλίου του Ελληνικού \
Τμήματος της Διεθνούς Αμνηστίας. Συντάσσεις ΜΙΑ απόφαση σε επίσημη ελληνική \
γλώσσα, ακολουθώντας πιστά τη δομή των πρακτικών.

Η απόφαση αποτελείται από:
1) Αριθμημένα στοιχεία «έχοντας υπόψη» (considerations). ΚΑΘΕ στοιχείο πρέπει \
να τεκμηριώνεται ΑΠΟΚΛΕΙΣΤΙΚΑ από όσα σου δίνονται:
   - τα άρθρα που σου παρέχονται στα candidate_articles — παραπέμπεις ως \
«του άρθρου N του {doc}» χρησιμοποιώντας ΜΟΝΟ τους αριθμούς άρθρων και τα \
έγγραφα που σου δόθηκαν,
   - τις προηγούμενες αποφάσεις/πρακτικά (prior_decisions) — παραπέμπεις με \
τον κωδικό αναφοράς τους (π.χ. ΔΣ11-2025),
   - όσα προκύπτουν από το απόσπασμα της συζήτησης (transcript_snippet).
2) Το διατακτικό κείμενο (decision_text) σε ΤΡΙΤΟ πρόσωπο ενεστώτα \
(π.χ. «Επικυρώνει…», «Εγκρίνει…», «Ορίζει…»).

ΑΥΣΤΗΡΟΙ ΚΑΝΟΝΕΣ ΚΑΤΑ ΤΗΣ ΠΑΡΑΠΟΙΗΣΗΣ (anti-hallucination):
- ΜΗΝ εφευρίσκεις αριθμούς άρθρων, ονόματα εγγράφων ή κωδικούς αποφάσεων.
- Παράπεμψε ΜΟΝΟ σε άρθρα που υπάρχουν στα candidate_articles και ΜΟΝΟ σε \
αποφάσεις που υπάρχουν στα prior_decisions.
- Αν δεν υπάρχει σχετικό τεκμήριο, μην το αναφέρεις καθόλου — προτίμησε \
λιγότερα αλλά αληθή στοιχεία «έχοντας υπόψη».

Απάντησε ΑΠΟΚΛΕΙΣΤΙΚΑ με έγκυρο JSON αυτής της μορφής, χωρίς άλλο κείμενο:
{"considerations": ["...", "..."], "decision_text": "..."}
"""


class LLMDecisionDrafter:
    """:class:`DecisionDrafter` backed by the project's :class:`ClaudeClient`.

    Exercised only with a configured LLM (gemini/claude per config.yaml); the
    unit tests cover the orchestration + ref logic via a fake drafter, not this
    class. The heavy client is lazy-imported inside :meth:`draft`, so importing
    this module never requires an LLM SDK.
    """

    def __init__(self, client=None, *, max_tokens: int = 2000) -> None:
        self._client = client
        self._max_tokens = max_tokens

    def draft(
        self,
        *,
        transcript_snippet: str,
        agenda_item: str,
        decision_ref: str,
        candidate_articles: list[dict],
        prior_decisions: list[dict],
    ) -> dict:
        """Call the configured LLM and parse its JSON decision response.

        Returns ``{"considerations": list[str], "decision_text": str}``.
        Parses the model output defensively (tolerates code fences / stray
        prose); on parse failure falls back to a single consideration carrying
        the raw text so the caller never crashes.
        """
        # Lazy-import the existing project LLM helper (no new SDK path).
        client = self._client
        if client is None:
            from src.core.claude import ClaudeClient

            client = ClaudeClient()

        articles_block = "\n".join(
            f"- άρθρο {a.get('article')} του {a.get('doc')}: {a.get('title', '')}"
            for a in candidate_articles
        ) or "(κανένα διαθέσιμο άρθρο)"

        priors_block = "\n".join(
            f"- {d.get('ref', d)}" for d in prior_decisions
        ) or "(καμία προηγούμενη απόφαση)"

        user_prompt = (
            f"## Κωδικός απόφασης (decision_ref) — μην τον αλλάξεις\n{decision_ref}\n\n"
            f"## Θέμα ημερήσιας διάταξης (agenda_item)\n{agenda_item or '(δεν δόθηκε)'}\n\n"
            f"## Απόσπασμα συζήτησης (transcript_snippet)\n{transcript_snippet}\n\n"
            f"## Διαθέσιμα άρθρα (candidate_articles) — μόνο αυτά επιτρέπονται\n"
            f"{articles_block}\n\n"
            f"## Προηγούμενες αποφάσεις/πρακτικά (prior_decisions)\n{priors_block}\n"
        )

        raw = client.generate(
            user_prompt=user_prompt,
            system_prompt=_SYSTEM_PROMPT,
            workflow="decision_drafter",
            max_tokens=self._max_tokens,
        )

        return self._parse(raw)

    @staticmethod
    def _parse(raw: str) -> dict:
        """Defensively parse the model's JSON into the expected shape."""
        text = (raw or "").strip()
        # Strip markdown code fences the model may add around JSON.
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Last resort: grab the first {...} block.
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    data = json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    data = None
            else:
                data = None
        if not isinstance(data, dict):
            return {"considerations": [], "decision_text": (raw or "").strip()}

        considerations = data.get("considerations") or []
        if not isinstance(considerations, list):
            considerations = [str(considerations)]
        considerations = [str(c) for c in considerations]
        decision_text = str(data.get("decision_text", "")).strip()
        return {"considerations": considerations, "decision_text": decision_text}
