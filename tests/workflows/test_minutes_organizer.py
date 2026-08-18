"""Tests for the LLM organiser (re-file ambiguous turns + tag them).

Every failure mode must degrade to "leave the deterministic result alone", so
most of these tests are about what happens when the model misbehaves.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from src.workflows.minutes_organizer import (
    drafting_turns,
    organize_skeleton,
)


def _settings(**over):
    base = dict(organizer_batch_turns=40, organizer_text_limit=600,
                organizer_max_tokens=8000)
    base.update(over)
    return SimpleNamespace(minutes_pipeline=SimpleNamespace(**base))


def _turn(speaker, text, start):
    return {"speaker": speaker, "text": text, "start": start, "end": start,
            "off_topic": False}


def _skeleton():
    """Two agenda items plus an opening bucket holding a misplaced turn."""
    return {
        "meeting_ref": "DS05-2026",
        "items": [
            {"index": 1, "title": "Office update", "segments": [], "votes": []},
            {"index": 2, "title": "Budget", "segments": [], "votes": []},
        ],
        "unassigned_segments": [
            _turn("A", "The office reported on the annual campaign.", "2026-06-09T17:05:00+00:00"),
            _turn("B", "Can you hear me now?", "2026-06-09T17:06:00+00:00"),
        ],
    }


class _Client:
    """Fake LLM returning a canned body; records the prompts it received."""

    def __init__(self, body):
        self.body, self.calls = body, []

    def load_prompt(self, name):
        self.calls.append(("prompt", name))
        return "SYS"

    def generate(self, *, user_prompt, system_prompt, workflow, max_tokens):
        self.calls.append(("generate", user_prompt))
        return self.body


def test_moves_turn_to_the_right_item_and_tags_it():
    sk = _skeleton()
    client = _Client(json.dumps([
        {"i": 0, "agenda": "Office update", "tag": "substantive"},
        {"i": 1, "agenda": "opening", "tag": "procedural"},
    ]))
    stats = organize_skeleton(sk, _settings(), client=client)

    assert stats["considered"] == 2 and stats["moved"] == 1
    assert stats["by_tag"] == {"substantive": 1, "procedural": 1, "off_topic": 0}
    # the substantive turn moved under its real agenda item...
    moved = sk["items"][0]["segments"]
    assert len(moved) == 1 and moved[0]["tag"] == "substantive"
    assert moved[0]["assigned_by"] == "llm"          # provenance recorded
    # ...and the procedural one stayed in the opening bucket, still present.
    assert len(sk["unassigned_segments"]) == 1
    assert sk["unassigned_segments"][0]["tag"] == "procedural"


def test_uses_the_organiser_prompt_not_a_drafting_one():
    sk = _skeleton()
    client = _Client("[]")
    organize_skeleton(sk, _settings(), client=client)
    assert ("prompt", "minutes_organizer") in client.calls


def test_nothing_is_ever_deleted():
    """Tagging must never remove a turn from the skeleton."""
    sk = _skeleton()
    before = len(sk["unassigned_segments"]) + sum(len(i["segments"]) for i in sk["items"])
    organize_skeleton(sk, _settings(), client=_Client(json.dumps([
        {"i": 0, "agenda": "Budget", "tag": "off_topic"},
        {"i": 1, "agenda": "opening", "tag": "off_topic"},
    ])))
    after = len(sk["unassigned_segments"]) + sum(len(i["segments"]) for i in sk["items"])
    assert after == before


def test_unknown_agenda_title_is_not_invented():
    """A hallucinated agenda title must leave the turn where it was."""
    sk = _skeleton()
    organize_skeleton(sk, _settings(), client=_Client(json.dumps([
        {"i": 0, "agenda": "A topic that does not exist", "tag": "substantive"},
    ])))
    assert len(sk["unassigned_segments"]) == 2      # nothing moved
    assert all(not i["segments"] for i in sk["items"])


def test_malformed_response_changes_nothing():
    for body in ["not json at all", "", "{}", "[{}]", '[{"i": 99, "tag": "substantive"}]']:
        sk = _skeleton()
        stats = organize_skeleton(sk, _settings(), client=_Client(body))
        assert stats["moved"] == 0 and stats["tagged"] == 0
        assert len(sk["unassigned_segments"]) == 2


def test_invalid_tag_is_ignored_but_move_still_applies():
    sk = _skeleton()
    organize_skeleton(sk, _settings(), client=_Client(json.dumps([
        {"i": 0, "agenda": "Budget", "tag": "totally-made-up"},
    ])))
    moved = sk["items"][1]["segments"]
    assert len(moved) == 1
    assert "tag" not in moved[0]        # unknown tag not written


def test_llm_failure_degrades_to_no_change():
    class Boom(_Client):
        def generate(self, **kw):
            raise RuntimeError("model down")

    sk = _skeleton()
    stats = organize_skeleton(sk, _settings(), client=Boom("[]"))
    assert stats["moved"] == 0 and len(sk["unassigned_segments"]) == 2


def test_json_in_code_fence_is_accepted():
    sk = _skeleton()
    body = '```json\n[{"i": 0, "agenda": "Budget", "tag": "substantive"}]\n```'
    stats = organize_skeleton(sk, _settings(), client=_Client(body))
    assert stats["moved"] == 1


def test_only_ambiguous_turns_are_considered():
    """Turns filed by a real agenda mark are left alone; inferred ones re-checked."""
    sk = _skeleton()
    sk["items"][0]["segments"] = [
        _turn("C", "Filed by a real agenda mark.", "2026-06-09T17:10:00+00:00"),
        dict(_turn("D", "Placed by inference.", "2026-06-09T17:11:00+00:00"),
             assigned_by="gap_fallback"),
    ]
    client = _Client("[]")
    organize_skeleton(sk, _settings(), client=client)
    prompt = [c for c in client.calls if c[0] == "generate"][0][1]
    assert "Placed by inference." in prompt          # inferred -> re-checked
    assert "Filed by a real agenda mark." not in prompt   # marked -> untouched


def test_batching_splits_large_inputs():
    sk = _skeleton()
    sk["unassigned_segments"] = [
        _turn("A", f"turn {i}", f"2026-06-09T17:{i:02d}:00+00:00") for i in range(10)
    ]
    client = _Client("[]")
    organize_skeleton(sk, _settings(organizer_batch_turns=4), client=client)
    assert len([c for c in client.calls if c[0] == "generate"]) == 3   # 4+4+2


def test_drafting_turns_keeps_substantive_and_untagged():
    segs = [
        {"text": "a", "tag": "substantive"},
        {"text": "b", "tag": "procedural"},
        {"text": "c", "tag": "off_topic"},
        {"text": "d"},                       # untagged -> assume substantive
    ]
    assert [s["text"] for s in drafting_turns(segs)] == ["a", "d"]
