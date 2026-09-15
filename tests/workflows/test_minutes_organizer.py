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


# -- context across batches and island review ---------------------------------


def _ts(minute):
    return f"2026-06-09T17:{minute:02d}:00+00:00"


class _RoutingClient(_Client):
    """Batch calls get the batch body; island checks get island_body (or raise)."""

    def __init__(self, batch_body, island_body=None, island_error=None):
        super().__init__(batch_body)
        self.island_body = island_body
        self.island_error = island_error
        self.island_prompts = []

    def generate(self, *, user_prompt, system_prompt, workflow, max_tokens):
        if workflow == "minutes_organizer_island":
            self.island_prompts.append(user_prompt)
            if self.island_error:
                raise self.island_error
            return self.island_body
        return super().generate(user_prompt=user_prompt, system_prompt=system_prompt,
                                workflow=workflow, max_tokens=max_tokens)


def _island_skeleton():
    """Six opening turns, one minute apart."""
    sk = _skeleton()
    sk["unassigned_segments"] = [_turn("A", f"line {i}", _ts(10 + i)) for i in range(6)]
    return sk


# The model files turns 0-1 and 4-5 under Office update but 2-3 under Budget.
_ISLAND_BATCH = json.dumps([
    {"i": 0, "agenda": "Office update", "tag": "substantive"},
    {"i": 1, "agenda": "Office update", "tag": "substantive"},
    {"i": 2, "agenda": "Budget", "tag": "substantive"},
    {"i": 3, "agenda": "Budget", "tag": "substantive"},
    {"i": 4, "agenda": "Office update", "tag": "substantive"},
    {"i": 5, "agenda": "Office update", "tag": "substantive"},
])


def test_later_batches_see_the_previous_turns_as_context():
    sk = _skeleton()
    sk["unassigned_segments"] = [_turn("A", f"line {i}", _ts(10 + i)) for i in range(3)]
    client = _Client(json.dumps([{"i": 0, "agenda": "Budget", "tag": "substantive"},
                                 {"i": 1, "agenda": "Budget", "tag": "substantive"}]))
    organize_skeleton(sk, _settings(organizer_batch_turns=2), client=client)

    prompts = [c[1] for c in client.calls if c[0] == "generate"]
    assert "line 0" not in prompts[0].split("[0]")[0]  # first batch: no context block
    context_block = prompts[1].split("[0]")[0]
    assert "line 0" in context_block and "line 1" in context_block
    assert "-> Budget" in context_block  # shown with where it was filed


def test_find_islands_flags_a_short_run_inside_another_items_stretch():
    from src.workflows.minutes_organizer import _find_islands

    titles = ["A", "A", "B", "B", "A", "A"]
    placed = [(dict(_turn("S", "t", _ts(10 + i)), assigned_by="llm"), t)
              for i, t in enumerate(titles)]
    islands = _find_islands(placed, max_turns=8, max_gap_seconds=180)
    assert [(i["start"], i["end"], i["title"], i["surround"]) for i in islands] == [
        (2, 3, "B", "A")
    ]


def test_find_islands_ignores_long_runs_time_gaps_and_non_model_filing():
    from src.workflows.minutes_organizer import _find_islands

    def placed_for(titles, minutes, by="llm"):
        return [(dict(_turn("S", "t", _ts(m)), assigned_by=by), t)
                for t, m in zip(titles, minutes)]

    long_run = placed_for(["A", "B", "B", "B", "A"], [10, 11, 12, 13, 14])
    assert _find_islands(long_run, max_turns=2, max_gap_seconds=180) == []
    far_apart = placed_for(["A", "B", "A"], [10, 20, 30])  # 10-minute gaps
    assert _find_islands(far_apart, max_turns=8, max_gap_seconds=180) == []
    not_model = placed_for(["A", "B", "A"], [10, 11, 12], by="gap_fallback")
    assert _find_islands(not_model, max_turns=8, max_gap_seconds=180) == []


def test_island_review_returns_a_misfiled_island_to_the_surrounding_item():
    sk = _island_skeleton()
    client = _RoutingClient(_ISLAND_BATCH, island_body='{"agenda": "Office update"}')
    stats = organize_skeleton(sk, _settings(), client=client)

    office = sk["items"][0]["segments"]
    assert [s["text"] for s in office] == [f"line {i}" for i in range(6)]
    assert not sk["items"][1]["segments"]
    assert {s["assigned_by"] for s in office[2:4]} == {"llm_island"}
    assert stats["islands_reviewed"] == 1 and stats["islands_moved"] == 1
    assert "line 1" in client.island_prompts[0] and "line 4" in client.island_prompts[0]


def test_island_review_keeps_an_island_the_model_confirms():
    sk = _island_skeleton()
    client = _RoutingClient(_ISLAND_BATCH, island_body='{"agenda": "Budget"}')
    stats = organize_skeleton(sk, _settings(), client=client)

    assert [s["text"] for s in sk["items"][1]["segments"]] == ["line 2", "line 3"]
    assert stats["islands_reviewed"] == 1 and stats["islands_moved"] == 0


def test_island_review_failure_or_nonsense_leaves_filing_unchanged():
    for client in (
        _RoutingClient(_ISLAND_BATCH, island_error=RuntimeError("model down")),
        _RoutingClient(_ISLAND_BATCH, island_body="not json"),
        _RoutingClient(_ISLAND_BATCH, island_body='{"agenda": "Invented item"}'),
    ):
        sk = _island_skeleton()
        organize_skeleton(sk, _settings(), client=client)
        assert [s["text"] for s in sk["items"][1]["segments"]] == ["line 2", "line 3"]
        total = len(sk["unassigned_segments"]) + sum(len(i["segments"]) for i in sk["items"])
        assert total == 6  # nothing lost
