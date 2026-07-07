"""Email subject/body classifier - routes inbound emails to Discord channels.

Uses Google Gemini (``google.generativeai``) via ``asyncio.to_thread`` because
the ``google-generativeai`` SDK exposes a synchronous ``generate_content`` API.
The newer ``google-genai`` (v1) package offers native async but is a different
import; we stay with the package already used across the platform.

The Gemini client is constructed lazily on the first ``classify`` call so that
importing this module never triggers network access.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.config import settings
from src.core.audit import log_action
from src.core.email_templates import greek_upper
from src.integrations.discord.constants import (
    CLASSIFIER_CONFIDENCE_THRESHOLD,
    CLASSIFIER_MAX_OUTPUT_TOKENS,
    CLASSIFIER_MODEL,
    CLASSIFIER_TEMPERATURE,
    CLASSIFIER_UNCERTAIN_LABEL,
    WORKFLOW_NAME,
)
from src.integrations.discord.state import EnabledChannelsStore

if TYPE_CHECKING:
    import discord as _discord  # only used for type hints

# Regex that captures any text inside square brackets in an email subject.
_BRACKET_TAG_RE = re.compile(r"\[([^\]]+)\]")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Alternate:
    """A ranked alternate routing candidate returned by the classifier.

    Attributes:
        label:      Channel label (must be a key in the ``label_to_id`` map).
        channel_id: Discord channel ID for this candidate.
        confidence: Classifier confidence in ``[0.0, 1.0]``.
    """

    label: str
    channel_id: str
    confidence: float


@dataclass(slots=True)
class ClassificationResult:
    """Outcome of a single classifier run.

    Attributes:
        label:        Chosen channel label, or ``CLASSIFIER_UNCERTAIN_LABEL``
                      when confidence is below the threshold.
        channel_id:   Discord channel ID that owns ``label``, or ``None``
                      when the result is uncertain.
        confidence:   Normalised float in ``[0.0, 1.0]``.
        raw_response: Full Gemini response text (useful for debugging).
        fell_back:    ``True`` when confidence was below the threshold and the
                      result was downgraded to ``CLASSIFIER_UNCERTAIN_LABEL``.
        alternates:   Up to 2 ranked alternate candidates (excluding the
                      primary choice).  Empty list when the classifier returned
                      only a single line or when the result fell back to
                      UNCERTAIN.
    """

    label: str
    channel_id: str | None
    confidence: float
    raw_response: str
    fell_back: bool
    alternates: list[Alternate] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_uncertain(raw: str = "") -> ClassificationResult:
    """Return a fully-uncertain result."""
    return ClassificationResult(
        label=CLASSIFIER_UNCERTAIN_LABEL,
        channel_id=None,
        confidence=0.0,
        raw_response=raw,
        fell_back=True,
    )


def _build_prompt(
    subject: str,
    body_preview: str,
    labels_with_keywords: dict[str, list[str]],
) -> str:
    """Build the English-language classification prompt sent to Gemini.

    ``labels_with_keywords`` maps each candidate label to its keyword list
    (may be empty).  Keywords are included as hints when present.

    The prompt now asks for **up to 3 ranked candidates** so the triage card
    can surface alternates.  First line is the primary choice; subsequent
    lines are alternates.  Old single-line responses remain valid - the parser
    treats them as "no alternates".
    """
    lines: list[str] = []
    for label, keywords in labels_with_keywords.items():
        if keywords:
            kw_str = ", ".join(keywords)
            lines.append(f"  Channel: {label} - {kw_str}")
        else:
            lines.append(f"  Channel: {label} - {label}")
    labels_block = "\n".join(lines)

    return (
        "You are an email classifier for an Amnesty International Greece activist group.\n"
        "Given the email below, rank the TOP 3 most likely Discord channels.\n\n"
        f"{labels_block}\n\n"
        f"Subject: {subject}\n"
        f"Body preview: {body_preview}\n\n"
        "Reply STRICTLY with 1-3 lines, best match first:\n"
        "LABEL|CONFIDENCE\n"
        "LABEL2|CONFIDENCE2\n"
        "LABEL3|CONFIDENCE3\n"
        "where each LABEL is EXACTLY one of the channel names above, "
        "CONFIDENCE is a number 0.0-1.0, and each label appears at most once."
    )


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


class EmailClassifier:
    """Classifies email subject+body to a Discord channel label using Gemini.

    Returns ``CLASSIFIER_UNCERTAIN_LABEL`` when confidence is below *threshold*
    (default 70 %).

    Reads enabled channels (and their ``classifier_keywords``) from
    ``EnabledChannelsStore`` to build the candidate label set dynamically.
    Channels without keywords are still candidates; the prompt simply omits the
    keyword hint for them.

    The Gemini client is constructed lazily on the first ``classify`` call so
    that importing this module never triggers network access or requires a
    valid API key at import time.
    """

    def __init__(self, channels_store: EnabledChannelsStore | None = None) -> None:
        self._store: EnabledChannelsStore = channels_store or EnabledChannelsStore()
        self._client: object | None = None   # google.generativeai.GenerativeModel, lazily set
        self._client_error: str | None = None  # cached init-failure message
        self._bot: "_discord.Client | None" = None  # injected via set_bot()

    # -- private helpers -----------------------------------------------------

    def _get_client(self) -> object | None:
        """Return (or lazily initialise) the Gemini GenerativeModel.

        Returns ``None`` if the API key is absent or the SDK fails to
        configure - in that case ``_client_error`` is set.
        """
        if self._client is not None:
            return self._client
        if self._client_error is not None:
            return None  # already failed; don't retry on every call

        api_key: str = settings.gemini_api_key
        if not api_key:
            self._client_error = "GEMINI_API_KEY is not configured"
            logger.warning("EmailClassifier: %s", self._client_error)
            return None

        try:
            import google.generativeai as genai  # type: ignore[import-untyped]

            genai.configure(api_key=api_key)
            self._client = genai.GenerativeModel(
                model_name=CLASSIFIER_MODEL,
                generation_config={
                    "temperature": CLASSIFIER_TEMPERATURE,
                    "max_output_tokens": CLASSIFIER_MAX_OUTPUT_TOKENS,
                },
            )
            logger.info("EmailClassifier: Gemini client initialised (model=%s)", CLASSIFIER_MODEL)
        except Exception as exc:  # noqa: BLE001
            self._client_error = str(exc)
            logger.warning("EmailClassifier: failed to initialise Gemini client: %s", exc)
            return None

        return self._client

    # -- public API ----------------------------------------------------------

    def is_enabled(self) -> bool:
        """Return ``True`` when the classifier is enabled in config and an API key exists."""
        return settings.discord.classifier.enabled and bool(settings.gemini_api_key)

    def set_channels_store(self, store: EnabledChannelsStore) -> None:
        """Replace the channels store (useful for testing or late injection)."""
        self._store = store

    def set_bot(self, bot: "_discord.Client") -> None:
        """Inject the Discord bot instance (needed for bracket-tag channel name lookups)."""
        self._bot = bot

    async def _try_bracket_tag(
        self,
        subject: str,
        *,
        test_mode: bool,
        request_id: str,
    ) -> "ClassificationResult | None":
        """Pre-classifier pass: match ``[TAG]`` in subject against enabled channel names.

        Returns a high-confidence ``ClassificationResult`` when a bracket tag in
        *subject* matches ``greek_upper(channel.name)`` for any enabled channel,
        or ``None`` to fall through to the LLM.

        Requires ``self._bot`` to be set; if not, returns ``None`` immediately.
        """
        if self._bot is None:
            return None

        # Collect bracket-tag candidates from the subject.
        bracket_matches = _BRACKET_TAG_RE.findall(subject)
        if not bracket_matches:
            return None

        normalised_tags = [greek_upper(m.strip()) for m in bracket_matches]

        channels = await self._store.list(test_mode=test_mode)
        for ch in channels:
            try:
                discord_ch = self._bot.get_channel(int(ch.channel_id))
                if discord_ch is None:
                    continue
                channel_tag = greek_upper(discord_ch.name)
            except Exception:
                continue

            if channel_tag in normalised_tags:
                result = ClassificationResult(
                    label=discord_ch.name,
                    channel_id=ch.channel_id,
                    confidence=1.0,
                    raw_response=f"bracket_tag:{channel_tag}",
                    fell_back=False,
                )
                self._audit(result, subject=subject, request_id=request_id)
                logger.info(
                    "EmailClassifier: bracket-tag match [%s] → channel %s",
                    channel_tag,
                    ch.channel_id,
                )
                return result

        return None

    async def classify(
        self,
        *,
        subject: str,
        body_preview: str,
        test_mode: bool = False,
        request_id: str = "",
    ) -> ClassificationResult:
        """Classify *subject* + *body_preview* to a Discord channel label.

        The caller is responsible for truncating *body_preview* to
        ``EMAIL_BODY_CLASSIFY_PREVIEW_CHARS`` characters before calling this
        method.

        Args:
            subject:      Email subject line.
            body_preview: Truncated email body (plain text).
            test_mode:    When ``True``, only test-mode channels are considered.
            request_id:   Correlation token shared across audit rows for this email.

        Returns:
            A :class:`ClassificationResult`.  Never raises - exceptions are
            caught and returned as an uncertain result with
            ``confidence=0.0``.
        """
        try:
            return await self._classify_inner(
                subject=subject,
                body_preview=body_preview,
                test_mode=test_mode,
                request_id=request_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("EmailClassifier.classify: unexpected error: %s", exc)
            result = _build_uncertain(raw=str(exc))
            self._audit(result, subject=subject, request_id=request_id)
            return result

    async def _classify_inner(
        self,
        *,
        subject: str,
        body_preview: str,
        test_mode: bool,
        request_id: str = "",
    ) -> ClassificationResult:
        """Core classification logic - may raise; ``classify`` wraps it."""
        # -- 0. Bracket-tag pre-match (no LLM call needed) -------------------
        bracket_result = await self._try_bracket_tag(
            subject, test_mode=test_mode, request_id=request_id
        )
        if bracket_result is not None:
            return bracket_result

        # -- 1. Fetch candidate channels -------------------------------------
        channels = await self._store.list(test_mode=test_mode)
        if not channels:
            logger.warning("EmailClassifier: no enabled channels for test_mode=%s", test_mode)
            result = _build_uncertain(raw="no enabled channels")
            self._audit(result, subject=subject, request_id=request_id)
            return result

        # Build label → channel_id and label → keywords maps.
        # Use channel.label if non-empty, else fall back to channel.channel_id.
        label_to_id: dict[str, str] = {}
        label_to_keywords: dict[str, list[str]] = {}
        for ch in channels:
            lbl = ch.label if ch.label else ch.channel_id
            label_to_id[lbl] = ch.channel_id
            label_to_keywords[lbl] = ch.classifier_keywords or []

        # -- 2. Get the Gemini client ----------------------------------------
        client = self._get_client()
        if client is None:
            msg = self._client_error or "Gemini client unavailable"
            logger.warning("EmailClassifier: %s - returning UNCERTAIN", msg)
            result = _build_uncertain(raw=msg)
            self._audit(result, subject=subject, request_id=request_id)
            return result

        # -- 3. Build prompt and call Gemini ---------------------------------
        prompt = _build_prompt(
            subject=subject,
            body_preview=body_preview,
            labels_with_keywords=label_to_keywords,
        )

        try:
            # google.generativeai.GenerativeModel.generate_content is synchronous;
            # wrap in asyncio.to_thread to avoid blocking the event loop.
            response = await asyncio.to_thread(
                client.generate_content,  # type: ignore[union-attr]
                prompt,
            )
            raw_text: str = response.text.strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("EmailClassifier: Gemini API error: %s", exc)
            result = _build_uncertain(raw=str(exc))
            self._audit(result, subject=subject, request_id=request_id)
            return result

        # -- 4. Parse response -----------------------------------------------
        result = self._parse_response(
            raw_text=raw_text,
            label_to_id=label_to_id,
        )
        self._audit(result, subject=subject, request_id=request_id)
        return result

    # -- parsing & audit helpers --------------------------------------------

    def _parse_response(
        self,
        *,
        raw_text: str,
        label_to_id: dict[str, str],
    ) -> ClassificationResult:
        """Parse ranked ``LABEL|CONFIDENCE`` Gemini output.

        Accepts 1-3 lines.  The first valid, above-threshold line becomes the
        primary result; remaining valid lines become ``alternates``.  Lines
        with unknown labels or non-float confidences are silently skipped.

        Backwards compatible: a single-line response produces ``alternates=[]``.
        """
        try:
            # -- Parse all LABEL|CONFIDENCE lines, best-first ------------------
            candidates: list[tuple[str, str, float]] = []  # (matched_label, channel_id, conf)
            seen_labels: set[str] = set()
            # Track the highest-confidence well-formed-but-unknown-label line
            # so we can preserve Gemini's confidence on the fallback path -
            # the contract is "we recognised your format but not your label",
            # not "we got nothing parseable".
            unknown_label_conf: float | None = None

            for raw_line in raw_text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                parts = line.split("|", maxsplit=1)
                if len(parts) != 2:
                    continue
                label_raw = parts[0].strip()
                try:
                    conf = float(parts[1].strip())
                except ValueError:
                    continue

                # Case-sensitive then case-insensitive label match
                matched_label: str | None = None
                if label_raw in label_to_id:
                    matched_label = label_raw
                else:
                    for candidate in label_to_id:
                        if candidate.lower() == label_raw.lower():
                            matched_label = candidate
                            break

                if matched_label is None:
                    # Well-formed line, just an unrecognised label.  Remember
                    # the highest such confidence - it's the signal "Gemini
                    # was sure, just about something we don't route to".
                    if unknown_label_conf is None or conf > unknown_label_conf:
                        unknown_label_conf = conf
                    continue
                if matched_label in seen_labels:
                    continue
                seen_labels.add(matched_label)
                candidates.append((matched_label, label_to_id[matched_label], conf))

            if not candidates:
                # Nothing routable - but if Gemini gave us a parseable line
                # with an unknown label, surface that confidence instead of
                # zeroing it (existing contract; preserves UX for the admin
                # who sees the triage card).
                if unknown_label_conf is not None:
                    logger.warning(
                        "EmailClassifier: Gemini returned unknown label(s) only - "
                        "treating as UNCERTAIN (preserved confidence=%.2f)",
                        unknown_label_conf,
                    )
                    return ClassificationResult(
                        label=CLASSIFIER_UNCERTAIN_LABEL,
                        channel_id=None,
                        confidence=unknown_label_conf,
                        raw_response=raw_text,
                        fell_back=True,
                    )
                raise ValueError(f"no valid LABEL|CONFIDENCE lines in: {raw_text!r}")

            # Primary is always the first candidate (Gemini ranks best-first)
            primary_label, primary_channel_id, primary_conf = candidates[0]

            # -- Apply confidence threshold to primary -------------------------
            if primary_conf < CLASSIFIER_CONFIDENCE_THRESHOLD:
                # Primary fell back - still surface alternates for triage
                return ClassificationResult(
                    label=CLASSIFIER_UNCERTAIN_LABEL,
                    channel_id=None,
                    confidence=primary_conf,
                    raw_response=raw_text,
                    fell_back=True,
                )

            if primary_label not in label_to_id:
                logger.warning(
                    "EmailClassifier: Gemini returned unknown label %r - treating as UNCERTAIN",
                    primary_label,
                )
                return ClassificationResult(
                    label=CLASSIFIER_UNCERTAIN_LABEL,
                    channel_id=None,
                    confidence=primary_conf,
                    raw_response=raw_text,
                    fell_back=True,
                )

            # -- Build alternates (remaining valid lines) ----------------------
            alternates = [
                Alternate(label=lbl, channel_id=ch_id, confidence=conf)
                for lbl, ch_id, conf in candidates[1:]
            ]

            return ClassificationResult(
                label=primary_label,
                channel_id=primary_channel_id,
                confidence=primary_conf,
                raw_response=raw_text,
                fell_back=False,
                alternates=alternates,
            )

        except Exception as exc:  # noqa: BLE001
            logger.warning("EmailClassifier: failed to parse Gemini response %r: %s", raw_text, exc)
            return _build_uncertain(raw=raw_text)

    def _audit(self, result: ClassificationResult, subject: str = "", request_id: str = "") -> None:
        """Write a single audit-log entry for this classification."""
        try:
            log_action(
                workflow=WORKFLOW_NAME,
                action="email_classified",
                details={
                    "label": result.label,
                    "confidence": result.confidence,
                    "fell_back": result.fell_back,
                    "subject": subject[:80],
                    "request_id": request_id,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("EmailClassifier: audit log failed: %s", exc)
