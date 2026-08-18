"""Pure conversion of Zoom poll results into board vote tallies.

The Board votes through Zoom's native poll dialog (created from the sidebar when
a decision is recorded). After the meeting, ``GET /past_meetings/{uuid}/polls``
returns each participant's answers; this module turns that into the ``vote`` event
shape the minutes skeleton already understands::

    {"label": str, "result": "passed"|"failed"|"tied",
     "tally": {"υπέρ": int, "κατά": int, "αποχή": int},
     "method": "unanimous"|"majority"}

Pure and offline: no network, no model, no I/O - so it is trivially testable and
the mapping from raw answers to a tally is auditable.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

YES, NO, ABSTAIN = "υπέρ", "κατά", "αποχή"

_ACCENTS = str.maketrans("άέήίόύώϊϋΐΰ", "αεηιουωιυιυ")

# Accepted spellings for each tally bucket (normalised: lowercase, de-accented).
_ANSWER_BUCKETS: dict[str, tuple[str, ...]] = {
    YES:     ("υπερ", "ναι", "yes", "for", "approve", "εγκριση"),
    NO:      ("κατα", "οχι", "no", "against", "reject", "απορριψη"),
    ABSTAIN: ("αποχη", "λευκο", "abstain", "abstention", "blank"),
}


def _norm(text: str) -> str:
    return " ".join((text or "").split()).strip().lower().translate(_ACCENTS)


def classify_answer(answer: str) -> str | None:
    """Map a raw poll answer to a tally bucket, or ``None`` if unrecognised.

    Unrecognised answers are deliberately NOT silently counted - the caller
    reports them so a mis-worded poll is visible rather than quietly skewing a
    governance record.
    """
    norm = _norm(answer)
    if not norm:
        return None
    for bucket, spellings in _ANSWER_BUCKETS.items():
        if any(norm == s or norm.startswith(s) for s in spellings):
            return bucket
    return None


def _participant_answers(poll: dict) -> list[tuple[str, str, str]]:
    """Flatten Zoom's per-participant results to ``(voter, question, answer, ts)``.

    Zoom nests results as ``questions -> [participant] -> question_details ->
    [{question, answer}]`` (the outer key is ``questions`` even though its
    entries are participants). Tolerates missing levels.
    """
    out: list[tuple[str, str, str, str]] = []
    for participant in poll.get("questions") or []:
        voter = (participant.get("name") or participant.get("email") or "").strip()
        for detail in participant.get("question_details") or []:
            question = (detail.get("question") or "").strip()
            answer = (detail.get("answer") or "").strip()
            when = (detail.get("date_time") or "").strip()
            if answer:
                out.append((voter, question, answer, when))
    return out


def tally_from_poll(poll: dict, *, label: str = "") -> dict | None:
    """Build one ``vote`` payload from a past-meeting poll result.

    Returns ``None`` when the poll recorded no answers at all (nothing to
    report). ``label`` defaults to the poll's own question text.
    """
    answers = _participant_answers(poll)
    if not answers:
        return None

    tally = {YES: 0, NO: 0, ABSTAIN: 0}
    voters: dict[str, str] = {}
    unrecognised: list[str] = []
    question_text = ""
    times: list[str] = []
    for voter, question, answer, when in answers:
        if when:
            times.append(when)
        question_text = question_text or question
        bucket = classify_answer(answer)
        if bucket is None:
            unrecognised.append(answer)
            continue
        tally[bucket] += 1
        if voter:
            voters[voter] = bucket

    if unrecognised:
        logger.warning(
            "Poll %r had %d unrecognised answer(s): %s - not counted in the tally",
            label or question_text, len(unrecognised), sorted(set(unrecognised)),
        )

    if tally[YES] > tally[NO]:
        result = "passed"
    elif tally[NO] > tally[YES]:
        result = "failed"
    else:
        result = "tied"

    method = (
        "unanimous"
        if tally[YES] > 0 and tally[NO] == 0 and tally[ABSTAIN] == 0
        else "majority"
    )

    payload = {
        "label": label or question_text or "Ψηφοφορία",
        "result": result,
        "tally": tally,
        "method": method,
    }
    if times:
        # Earliest answer: when the vote actually happened, so the minutes
        # skeleton can attach it to the agenda item that was active.
        payload["ts"] = min(times)
    if voters:
        payload["voters"] = voters          # who voted how (non-anonymous polls)
    if unrecognised:
        payload["unrecognised_answers"] = sorted(set(unrecognised))
    return payload


def votes_from_past_meeting_polls(response: dict) -> list[dict]:
    """Convert a whole ``GET /past_meetings/{uuid}/polls`` response to vote payloads.

    Zoom returns one object whose ``questions`` array holds every participant's
    answers across all polls run in the meeting. Answers are grouped by question
    text so each distinct question becomes its own vote.
    """
    by_question: dict[str, dict] = {}
    for participant in response.get("questions") or []:
        for detail in participant.get("question_details") or []:
            question = (detail.get("question") or "").strip()
            by_question.setdefault(question, {"questions": []})
            by_question[question]["questions"].append(
                {"name": participant.get("name"), "email": participant.get("email"),
                 "question_details": [detail]}
            )
    votes = []
    for question, grouped in by_question.items():
        vote = tally_from_poll(grouped, label=question)
        if vote:
            votes.append(vote)
    return votes
