"""Tests for deterministic document retrieval feeding the minutes drafter."""
from __future__ import annotations

from types import SimpleNamespace

from src.workflows.minutes_documents import (
    document_context_for_skeleton,
    extract_protocol_refs,
    is_office_update_item,
)


def _settings(tmp_path):
    return SimpleNamespace(
        minutes_pipeline=SimpleNamespace(
            director_briefings_dir=str(tmp_path / "briefings")
        )
    )


def test_extract_protocol_refs_normalises_and_dedupes():
    refs = extract_protocol_refs("αρ. πρωτ. 2026_031, επίσης 2026/22 και ξανά 2026_031")
    assert refs == ["2026_031", "2026_022"]
    assert extract_protocol_refs("") == []
    assert extract_protocol_refs("no refs here 12_3") == []


def test_is_office_update_item_accent_insensitive():
    assert is_office_update_item("Ενημέρωση Γραφείου")
    assert is_office_update_item("ενημερωση γραφειου")
    assert is_office_update_item("Ενημέρωση Διευθυντή")
    assert not is_office_update_item("Προγραμματισμός συνεδριάσεων")


def test_briefings_attach_only_to_office_update_item(tmp_path):
    """The Director's briefings go to the office-update item, not to every item."""
    d = tmp_path / "briefings" / "ΔΣ05-2026"
    d.mkdir(parents=True)
    (d / "Εισηγητικό.txt").write_text("Στοιχεία γραφείου: 13 εργαζόμενοι.", encoding="utf-8")

    skeleton = {
        "meeting_ref": "ΔΣ05-2026",
        "items": [
            {"title": "Ενημέρωση Γραφείου", "segments": []},
            {"title": "Προγραμματισμός συνεδριάσεων", "segments": []},
        ],
        "decisions": [],
    }
    ctx = document_context_for_skeleton(skeleton, _settings(tmp_path))
    assert any("ενημερωση γραφειου" == k for k in ctx)
    assert "13 εργαζόμενοι" in next(iter(ctx.values()))
    assert len(ctx) == 1  # the other item gets nothing


def test_protocol_ref_in_decision_pulls_matching_document(tmp_path):
    """A protocol ref cited in a decision's «Έχοντας υπόψη» attaches that file."""
    d = tmp_path / "briefings" / "ΔΣ05-2026"
    d.mkdir(parents=True)
    (d / "[2026_031] Εισηγητικό.txt").write_text("ΠΕΡΙΕΧΟΜΕΝΟ ΕΓΓΡΑΦΟΥ", encoding="utf-8")

    skeleton = {
        "meeting_ref": "ΔΣ05-2026",
        "items": [{"title": "Προσλήψεις", "segments": []}],
        "decisions": [{
            "agenda_item": "Προσλήψεις",
            "decision_text": "Προκηρύσσει θέσεις.",
            "considerations": ["Το εισηγητικό του Διευθυντή (αρ. πρωτ. 2026_031)."],
        }],
    }
    ctx = document_context_for_skeleton(skeleton, _settings(tmp_path))
    assert "ΠΕΡΙΕΧΟΜΕΝΟ ΕΓΓΡΑΦΟΥ" in ctx["προσληψεις"]


def test_missing_dirs_degrade_to_empty(tmp_path):
    skeleton = {"meeting_ref": "ΔΣ99-2099", "items": [{"title": "Θέμα", "segments": []}]}
    assert document_context_for_skeleton(skeleton, _settings(tmp_path)) == {}
