"""Tests for converting Zoom poll results into board vote tallies."""
from __future__ import annotations

from src.workflows.minutes_votes import (
    classify_answer,
    tally_from_poll,
    votes_from_past_meeting_polls,
)


def _participant(name, question, answer):
    return {"name": name, "email": f"{name}@x.gr",
            "question_details": [{"question": question, "answer": answer}]}


def test_classify_answer_accepts_greek_english_and_accents():
    assert classify_answer("Υπέρ") == "υπέρ"
    assert classify_answer("ΥΠΕΡ") == "υπέρ"
    assert classify_answer("Ναι") == "υπέρ"
    assert classify_answer("Κατά") == "κατά"
    assert classify_answer("Against") == "κατά"
    assert classify_answer("Αποχή") == "αποχή"
    assert classify_answer("Abstain") == "αποχή"
    assert classify_answer("") is None
    assert classify_answer("maybe later") is None      # unrecognised, not guessed


def test_tally_unanimous_is_flagged_as_such():
    poll = {"questions": [
        _participant("A", "Approve budget?", "Υπέρ"),
        _participant("B", "Approve budget?", "Υπέρ"),
        _participant("C", "Approve budget?", "Υπέρ"),
    ]}
    vote = tally_from_poll(poll)
    assert vote["tally"] == {"υπέρ": 3, "κατά": 0, "αποχή": 0}
    assert vote["result"] == "passed"
    assert vote["method"] == "unanimous"
    assert vote["label"] == "Approve budget?"
    assert vote["voters"] == {"A": "υπέρ", "B": "υπέρ", "C": "υπέρ"}


def test_tally_majority_and_failed_and_tied():
    q = "Q?"
    passed = tally_from_poll({"questions": [
        _participant("A", q, "Υπέρ"), _participant("B", q, "Υπέρ"),
        _participant("C", q, "Κατά"), _participant("D", q, "Αποχή"),
    ]})
    assert passed["tally"] == {"υπέρ": 2, "κατά": 1, "αποχή": 1}
    assert (passed["result"], passed["method"]) == ("passed", "majority")

    failed = tally_from_poll({"questions": [
        _participant("A", q, "Κατά"), _participant("B", q, "Κατά"),
        _participant("C", q, "Υπέρ"),
    ]})
    assert failed["result"] == "failed"

    tied = tally_from_poll({"questions": [
        _participant("A", q, "Υπέρ"), _participant("B", q, "Κατά"),
    ]})
    assert tied["result"] == "tied"


def test_unrecognised_answers_are_reported_not_silently_dropped():
    """A mis-worded poll must be visible, never quietly skew a governance record."""
    poll = {"questions": [
        _participant("A", "Q?", "Υπέρ"),
        _participant("B", "Q?", "Ισως"),      # not a valid choice
    ]}
    vote = tally_from_poll(poll)
    assert vote["tally"] == {"υπέρ": 1, "κατά": 0, "αποχή": 0}   # not counted
    assert vote["unrecognised_answers"] == ["Ισως"]              # but surfaced


def test_empty_poll_returns_none():
    assert tally_from_poll({"questions": []}) is None
    assert tally_from_poll({}) is None


def test_multiple_questions_become_separate_votes():
    """One meeting can run several polls; each question is its own vote."""
    response = {"id": 1, "uuid": "u", "questions": [
        {"name": "A", "email": "a@x.gr", "question_details": [
            {"question": "Q1", "answer": "Υπέρ"},
            {"question": "Q2", "answer": "Κατά"},
        ]},
        {"name": "B", "email": "b@x.gr", "question_details": [
            {"question": "Q1", "answer": "Υπέρ"},
            {"question": "Q2", "answer": "Κατά"},
        ]},
    ]}
    votes = votes_from_past_meeting_polls(response)
    assert len(votes) == 2
    by_label = {v["label"]: v for v in votes}
    assert by_label["Q1"]["result"] == "passed"
    assert by_label["Q2"]["result"] == "failed"


def test_real_empty_envelope_from_zoom_is_safe():
    """The exact shape ΔΣ05 returned (a meeting that ran no polls)."""
    response = {"id": 82264596638, "uuid": "ZEC...==",
                "start_time": "2026-06-09T17:00:12Z", "questions": []}
    assert votes_from_past_meeting_polls(response) == []


def test_vote_carries_earliest_answer_timestamp():
    """The vote's ts lets the skeleton attach it to the active agenda item."""
    poll = {"questions": [
        {"name": "A", "question_details": [
            {"question": "Q?", "answer": "Υπέρ", "date_time": "2026-06-09T18:30:00Z"}]},
        {"name": "B", "question_details": [
            {"question": "Q?", "answer": "Υπέρ", "date_time": "2026-06-09T18:29:00Z"}]},
    ]}
    assert tally_from_poll(poll)["ts"] == "2026-06-09T18:29:00Z"


def test_vote_without_timestamps_omits_ts():
    poll = {"questions": [{"name": "A", "question_details": [
        {"question": "Q?", "answer": "Υπέρ"}]}]}
    assert "ts" not in tally_from_poll(poll)


def test_vote_events_from_manifest_are_placeable():
    """Poll results become vote events carrying the timestamp the skeleton
    needs to attach them to the agenda item that was active."""
    from src.workflows.minutes_pipeline import vote_events_from_manifest
    manifest = {"start_time": "2026-06-09T17:00:00Z", "polls": {"questions": [
        {"name": "A", "question_details": [
            {"question": "Approve?", "answer": "Υπέρ", "date_time": "2026-06-09T18:30:00Z"}]},
        {"name": "B", "question_details": [
            {"question": "Approve?", "answer": "Κατά", "date_time": "2026-06-09T18:31:00Z"}]},
    ]}}
    events = vote_events_from_manifest(manifest)
    assert len(events) == 1
    ev = events[0]
    assert ev["event_type"] == "vote"
    assert ev["ts"] == "2026-06-09T18:30:00Z"        # earliest answer
    assert ev["payload"]["tally"] == {"υπέρ": 1, "κατά": 1, "αποχή": 0}
    assert ev["payload"]["result"] == "tied"


def test_vote_events_falls_back_to_manifest_start_time():
    from src.workflows.minutes_pipeline import vote_events_from_manifest
    manifest = {"start_time": "2026-06-09T17:00:00Z", "polls": {"questions": [
        {"name": "A", "question_details": [{"question": "Q?", "answer": "Υπέρ"}]},
    ]}}
    assert vote_events_from_manifest(manifest)[0]["ts"] == "2026-06-09T17:00:00Z"


def test_no_polls_yields_no_events():
    from src.workflows.minutes_pipeline import vote_events_from_manifest
    assert vote_events_from_manifest({}) == []
    assert vote_events_from_manifest({"polls": {"questions": []}}) == []
