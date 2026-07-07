"""Tests for EmailClassifier._parse_response (pure function, no Gemini calls)."""

from __future__ import annotations

import pytest

from src.integrations.discord.classifier import ClassificationResult, EmailClassifier
from src.integrations.discord.constants import CLASSIFIER_UNCERTAIN_LABEL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_classifier() -> EmailClassifier:
    """Construct an EmailClassifier instance bypassing __init__ side effects."""
    obj = EmailClassifier.__new__(EmailClassifier)
    # Set the minimal attributes _parse_response needs (none, it's pure)
    # but _audit is called by _classify_inner, not _parse_response, so we're fine.
    return obj


LABEL_TO_ID = {
    "επικαιρότητα": "111",
    "εκδηλώσεις": "222",
    "ανακοινώσεις": "333",
}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_parse_response_valid_label_and_confidence():
    classifier = make_classifier()
    result = classifier._parse_response(
        raw_text="επικαιρότητα|0.9",
        label_to_id=LABEL_TO_ID,
    )
    assert result.label == "επικαιρότητα"
    assert result.channel_id == "111"
    assert abs(result.confidence - 0.9) < 1e-9
    assert result.fell_back is False


def test_parse_response_second_label():
    classifier = make_classifier()
    result = classifier._parse_response(
        raw_text="εκδηλώσεις|0.85",
        label_to_id=LABEL_TO_ID,
    )
    assert result.label == "εκδηλώσεις"
    assert result.channel_id == "222"
    assert result.fell_back is False


# ---------------------------------------------------------------------------
# Confidence threshold
# ---------------------------------------------------------------------------


def test_parse_response_below_threshold_falls_back():
    """Confidence below 0.70 → UNCERTAIN, fell_back=True, confidence preserved."""
    classifier = make_classifier()
    result = classifier._parse_response(
        raw_text="επικαιρότητα|0.5",
        label_to_id=LABEL_TO_ID,
    )
    assert result.label == CLASSIFIER_UNCERTAIN_LABEL
    assert result.channel_id is None
    assert abs(result.confidence - 0.5) < 1e-9
    assert result.fell_back is True


def test_parse_response_at_threshold_passes():
    """Confidence exactly at 0.70 should NOT fall back (>=, not >)."""
    from src.integrations.discord.constants import CLASSIFIER_CONFIDENCE_THRESHOLD

    classifier = make_classifier()
    result = classifier._parse_response(
        raw_text=f"επικαιρότητα|{CLASSIFIER_CONFIDENCE_THRESHOLD}",
        label_to_id=LABEL_TO_ID,
    )
    # 0.70 is the threshold; >= means it passes
    assert result.fell_back is False
    assert result.label == "επικαιρότητα"


# ---------------------------------------------------------------------------
# Unknown label
# ---------------------------------------------------------------------------


def test_parse_response_unknown_label_falls_back():
    classifier = make_classifier()
    result = classifier._parse_response(
        raw_text="unknown_channel|0.95",
        label_to_id=LABEL_TO_ID,
    )
    assert result.label == CLASSIFIER_UNCERTAIN_LABEL
    assert result.fell_back is True
    # Confidence is preserved even on label fall-back
    assert abs(result.confidence - 0.95) < 1e-9


# ---------------------------------------------------------------------------
# Malformed responses
# ---------------------------------------------------------------------------


def test_parse_response_not_a_float_falls_back():
    classifier = make_classifier()
    result = classifier._parse_response(
        raw_text="επικαιρότητα|not-a-float",
        label_to_id=LABEL_TO_ID,
    )
    assert result.fell_back is True
    assert result.label == CLASSIFIER_UNCERTAIN_LABEL


def test_parse_response_no_separator_falls_back():
    classifier = make_classifier()
    result = classifier._parse_response(
        raw_text="επικαιρότητα",
        label_to_id=LABEL_TO_ID,
    )
    assert result.fell_back is True
    assert result.label == CLASSIFIER_UNCERTAIN_LABEL


def test_parse_response_empty_string_falls_back():
    classifier = make_classifier()
    result = classifier._parse_response(
        raw_text="",
        label_to_id=LABEL_TO_ID,
    )
    assert result.fell_back is True


def test_parse_response_extra_pipes_uses_first_split():
    """Extra '|' characters - maxsplit=1, so only the first pipe is the separator."""
    classifier = make_classifier()
    # "επικαιρότητα|0.9|extra" - second field is "0.9|extra" which can't be float.
    result = classifier._parse_response(
        raw_text="επικαιρότητα|0.9|extra",
        label_to_id=LABEL_TO_ID,
    )
    # "0.9|extra" is not a valid float → fell_back
    assert result.fell_back is True


# ---------------------------------------------------------------------------
# Case-insensitive label matching
# ---------------------------------------------------------------------------


def test_parse_response_case_insensitive_match():
    """Upper-case Greek label should match via case-insensitive fallback.

    Python 3.11's str.lower() correctly lowercases accented Greek capitals
    (e.g. 'ΕΠΙΚΑΙΡΌΤΗΤΑ' → 'επικαιρότητα'), so the classifier's
    case-insensitive candidate scan finds the match.
    """
    classifier = make_classifier()
    result = classifier._parse_response(
        raw_text="ΕΠΙΚΑΙΡΌΤΗΤΑ|0.9",
        label_to_id=LABEL_TO_ID,
    )
    assert result.fell_back is False
    assert result.label == "επικαιρότητα"
    assert result.channel_id == "111"


def test_parse_response_ascii_case_insensitive():
    """ASCII label case-insensitive matching always works."""
    classifier = make_classifier()
    label_to_id_ascii = {"events": "123", "news": "456"}
    result = classifier._parse_response(
        raw_text="EVENTS|0.9",
        label_to_id=label_to_id_ascii,
    )
    assert result.fell_back is False
    assert result.label == "events"
    assert result.channel_id == "123"


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------


def test_parse_response_returns_classification_result():
    classifier = make_classifier()
    result = classifier._parse_response(
        raw_text="επικαιρότητα|0.9",
        label_to_id=LABEL_TO_ID,
    )
    assert isinstance(result, ClassificationResult)
