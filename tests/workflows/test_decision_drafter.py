"""Tests for the board-decision drafter (ref, retrieval, orchestration, render).

These cover the deterministic + pure parts. The real LLM drafter
(LLMDecisionDrafter) is exercised only against a configured model, so here we
use a FakeDrafter that returns canned output and records what it was handed.
"""

from __future__ import annotations

import pytest

from src.workflows.decision_drafter import (
    compute_decision_ref,
    load_articles,
    propose_decision,
    render_decision,
    select_relevant_articles,
)


class FakeDrafter:
    """Canned DecisionDrafter that records the candidate_articles it received."""

    def __init__(self, considerations=None, decision_text="Επικυρώνει τα πρακτικά."):
        self.considerations = considerations if considerations is not None else [
            "Τα πρακτικά της συνεδρίασης υπ’ αριθμόν ΔΣ11-2025.",
            "Το άρθρο 15 του Καταστατικού.",
        ]
        self.decision_text = decision_text
        self.seen_candidates = None
        self.seen_ref = None
        self.seen_priors = None

    def draft(self, *, transcript_snippet, agenda_item, decision_ref,
              candidate_articles, prior_decisions):
        self.seen_candidates = candidate_articles
        self.seen_ref = decision_ref
        self.seen_priors = prior_decisions
        return {
            "considerations": list(self.considerations),
            "decision_text": self.decision_text,
        }


# --------------------------------------------------------------------------- #
# compute_decision_ref
# --------------------------------------------------------------------------- #
def test_compute_decision_ref_basic():
    assert compute_decision_ref("ΔΣ05-2026", 1) == "ΔΣ01-05-2026"
    assert compute_decision_ref("ΔΣ05-2026", 12) == "ΔΣ12-05-2026"


def test_compute_decision_ref_matches_real_data():
    # Verified against real data: meeting ΔΣ01-2026 → ΔΣ01-01-2026 … ΔΣ05-01-2026.
    assert compute_decision_ref("ΔΣ01-2026", 1) == "ΔΣ01-01-2026"
    assert compute_decision_ref("ΔΣ01-2026", 5) == "ΔΣ05-01-2026"


def test_compute_decision_ref_bad_meeting_ref_raises():
    with pytest.raises(ValueError):
        compute_decision_ref("not-a-ref", 1)


def test_compute_decision_ref_sequence_zero_raises():
    with pytest.raises(ValueError):
        compute_decision_ref("ΔΣ05-2026", 0)


# --------------------------------------------------------------------------- #
# select_relevant_articles
# --------------------------------------------------------------------------- #
def _fake_articles():
    return [
        {"doc": "Καταστατικό", "article": 15,
         "title": "Αρμοδιότητες Διοικητικού Συμβουλίου",
         "text": "Το Διοικητικό Συμβούλιο επικυρώνει πρακτικά συνεδριάσεων."},
        {"doc": "Καταστατικό", "article": 2,
         "title": "Έδρα",
         "text": "Έδρα του Σωματείου ορίζεται η Αθήνα."},
        {"doc": "Εσωτερικοί Κανονισμοί", "article": 7,
         "title": "Οικονομικά",
         "text": "Προϋπολογισμός και δαπάνες αυλακιών ζέβρας."},
    ]


def test_select_relevant_articles_ranks_overlap_first():
    arts = _fake_articles()
    query = "Το Διοικητικό Συμβούλιο επικυρώνει τα πρακτικά της συνεδρίασης."
    result = select_relevant_articles(query, arts, k=5)
    assert result, "expected at least one matching article"
    assert result[0]["article"] == 15


def test_select_relevant_articles_drops_zero_overlap():
    arts = _fake_articles()
    query = "Το Διοικητικό Συμβούλιο επικυρώνει τα πρακτικά."
    result = select_relevant_articles(query, arts, k=5)
    # The "Οικονομικά / αυλακιών ζέβρας" article shares no tokens with the query.
    assert all(a["article"] != 7 for a in result)


def test_select_relevant_articles_respects_k():
    arts = _fake_articles()
    query = "Διοικητικό Συμβούλιο Σωματείου πρακτικά Αθήνα Έδρα"
    result = select_relevant_articles(query, arts, k=1)
    assert len(result) == 1


# --------------------------------------------------------------------------- #
# propose_decision end-to-end + render_decision
# --------------------------------------------------------------------------- #
def test_propose_decision_end_to_end_and_render():
    arts = _fake_articles()
    drafter = FakeDrafter()
    proposal = propose_decision(
        meeting_ref="ΔΣ05-2026",
        sequence=1,
        transcript_snippet="Το Διοικητικό Συμβούλιο επικυρώνει τα πρακτικά.",
        drafter=drafter,
        agenda_item="Επικύρωση πρακτικών",
        articles=arts,
        prior_decisions=[{"ref": "ΔΣ11-2025"}],
    )

    assert proposal["ref"] == "ΔΣ01-05-2026"
    # candidate_articles passed through to both the result and the drafter.
    assert proposal["candidate_articles"] == drafter.seen_candidates
    assert drafter.seen_ref == "ΔΣ01-05-2026"
    assert any(a["article"] == 15 for a in proposal["candidate_articles"])

    rendered = render_decision(proposal)
    # Header, numbered considerations, ΑΠΟΦΑΣΗ line + ref, decision text - in order.
    assert "Το Διοικητικό Συμβούλιο, έχοντας υπόψη:" in rendered
    assert "1. Τα πρακτικά της συνεδρίασης υπ’ αριθμόν ΔΣ11-2025." in rendered
    assert "2. Το άρθρο 15 του Καταστατικού." in rendered
    assert "ΑΠΟΦΑΣΗ ΔΣ01-05-2026" in rendered
    assert "Επικυρώνει τα πρακτικά." in rendered

    i_header = rendered.index("έχοντας υπόψη:")
    i_cons1 = rendered.index("1. Τα πρακτικά")
    i_apofasi = rendered.index("ΑΠΟΦΑΣΗ ΔΣ01-05-2026")
    i_text = rendered.index("Επικυρώνει τα πρακτικά.")
    assert i_header < i_cons1 < i_apofasi < i_text


def test_render_decision_empty_considerations_omits_header():
    proposal = {
        "ref": "ΔΣ02-05-2026",
        "considerations": [],
        "decision_text": "Ορίζει νέο ταμία τον κ. Παπαδόπουλο.",
        "candidate_articles": [],
    }
    rendered = render_decision(proposal)
    assert "έχοντας υπόψη" not in rendered
    assert rendered.startswith("ΑΠΟΦΑΣΗ ΔΣ02-05-2026")
    assert "Ορίζει νέο ταμία τον κ. Παπαδόπουλο." in rendered


# --------------------------------------------------------------------------- #
# load_articles
# --------------------------------------------------------------------------- #
def test_load_articles_missing_path_returns_empty():
    assert load_articles("does/not/exist/articles.json") == []
