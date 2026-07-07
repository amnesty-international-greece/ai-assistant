"""Regression tests for the `debug run` CLI contract.

These tests enforce the standing rule documented in ``docs/DEBUG_CLI.md`` and
``CLAUDE.md``: *every workflow step must be invokable via*
``python -m src.cli debug run <workflow> <step>`` *without a KeyError*.  A
``WorkflowStep`` is only debuggable if the workflow's ``debug_fixture()``
supplies every ctx key the step reads.  If someone adds a step (or makes an
existing step read a new ctx key) and forgets to extend the fixture, the
``test_pure_steps_invoke_without_keyerror`` parametrisation below fails loudly.

About the PURE allow-list
-------------------------
``_PURE_STEPS`` is **intentionally conservative**.  It contains only steps that
perform NO external I/O once ``test_mode=True`` is forced - no Google / Zoom /
M365 / Brevo / SharePoint / LLM / event-bus / live-DB access.  Such steps are
deterministic in CI, so we can actually *run* them and prove the fixture covers
their inputs.

Steps that hit a network service, the LLM, the event bus, or the live SQLite
audit DB are deliberately EXCLUDED - running them in CI would be flaky or would
mutate shared state.  Their fixture coverage is therefore checked only
structurally (keys exist, keys are strings) rather than by execution.  A small
reliable allow-list beats a broad flaky one; when in doubt a step is left off.

The allow-list was built by reading each step body and confirming the path
taken under the canonical fixture short-circuits before any I/O (e.g.
``test_mode`` guards, ``_skip_*`` escape hatches, a pre-set ``override_*`` /
valid ``protocol_number``, or empty input lists).
"""

from __future__ import annotations

import asyncio

import pytest

from src.core.workflow import StepResult
from src.workflows.registry import WORKFLOWS

# The four workflow names that MUST be registered.  Hard-coded on purpose: if a
# new workflow module is added but not wired into the registry (or vice versa),
# test_registry_covers_all_workflow_modules fails.
_EXPECTED_WORKFLOWS = {
    "archive",
    "board_meeting_invitation",
    "board_meeting_minutes",
    "egkyklios_general",
}

# Curated allow-list of (workflow, step) pairs that are PURE under test_mode.
# Each entry has been verified by reading the step body - see module docstring.
# Pairs are excluded (with a one-word reason) when they touch:
#   archive:                  (all 7 steps are pure under the fixture)
#   board_meeting_invitation: send_scheduling_email (M365), schedule_zoom (Zoom),
#                             generate_pdf (Google), send_board_email (M365),
#                             send_newsletter (Brevo), confirm_newsletter (bus)
#   board_meeting_minutes:    select_sources (Google), draft_minutes (LLM),
#                             write_draft_to_doc (Google), approval_and_share
#                             (Gmail), finalize (Google), extract_decisions
#                             (Google Sheets read) - none are pure, so none listed
#   egkyklios_general:        gather_sources (live DB), draft_circular (LLM),
#                             render_pdf (writes PDF/assets), notify_board_for_review
#                             (M365), send_brevo_campaign (Brevo), publish_event (bus)
_PURE_STEPS: list[tuple[str, str]] = [
    # archive - every step short-circuits with no external I/O:
    ("archive", "intake"),               # fixture pdf_path missing → clean File-not-found
    ("archive", "extract_metadata"),     # _skip_llm=True → echoes llm_result
    ("archive", "resolve_protocol"),     # override_protocol set → CLI-override branch
    ("archive", "collision_check"),      # test_mode → skipped
    ("archive", "upload_and_register"),  # test_mode → "[TEST] would upload"
    ("archive", "notify"),               # only prints a CLI summary
    ("archive", "revision_window"),      # computes a deadline timestamp
    # board_meeting_invitation:
    ("board_meeting_invitation", "await_approval"),       # returns immediately
    ("board_meeting_invitation", "read_agenda"),          # _skip_read_agenda + agenda_items
    ("board_meeting_invitation", "init_meeting_thread"),  # derives meeting_id from ctx
    ("board_meeting_invitation", "draft_invitation"),     # valid protocol_number → no fetch
    ("board_meeting_invitation", "approval"),             # test_mode → approved
    ("board_meeting_invitation", "archive"),              # test_mode → skipped
    # egkyklios_general:
    ("egkyklios_general", "extract_briefing_texts"),     # briefings_meta=[] → empty loop
    ("egkyklios_general", "extract_meeting_summaries"),  # minutes_rows=[] → empty loop
    ("egkyklios_general", "await_approval"),             # draft_id=0 → no DB write
    ("egkyklios_general", "archive_to_sharepoint"),      # test_mode → skipped
]


def test_every_workflow_has_a_debug_fixture():
    """Each registered workflow exposes a non-empty dict debug_fixture()."""
    for name, cls in WORKFLOWS.items():
        fixture = cls.debug_fixture()
        assert isinstance(fixture, dict), f"{name}.debug_fixture() is not a dict"
        assert fixture, f"{name}.debug_fixture() is empty"


def test_fixture_keys_are_strings():
    """Every fixture key is a str (ctx keys are always string-named)."""
    for name, cls in WORKFLOWS.items():
        for key in cls.debug_fixture():
            assert isinstance(key, str), f"{name} fixture has non-str key {key!r}"


def test_registry_covers_all_workflow_modules():
    """The registry maps exactly the four expected workflow names.

    Guards against adding a workflow file but forgetting to register it (or
    leaving a stale registration after a rename).
    """
    assert set(WORKFLOWS) == _EXPECTED_WORKFLOWS


def test_fixture_does_not_set_test_mode():
    """No fixture bakes in ``test_mode`` - the runner forces it.

    Setting it in the fixture would be misleading: it implies the fixture is
    coupled to test_mode when in fact the runner always sets it to True.
    """
    for name, cls in WORKFLOWS.items():
        assert "test_mode" not in cls.debug_fixture(), (
            f"{name}.debug_fixture() must not set 'test_mode' - the runner forces it"
        )


@pytest.mark.parametrize(
    ("workflow", "step_name"),
    _PURE_STEPS,
    ids=[f"{w}:{s}" for w, s in _PURE_STEPS],
)
def test_pure_steps_invoke_without_keyerror(workflow: str, step_name: str):
    """A pure step runs against its fixture without raising.

    Builds ctx exactly like ``_run_debug_run`` does (``dict(debug_fixture())``
    then ``test_mode=True``), resolves the ``WorkflowStep`` by name, and runs
    ``execute_step``.  The contract under test is *no unhandled exception* -
    a step legitimately reporting ``success is False`` (e.g. the archive
    fixture's intake path can't find its placeholder PDF) is allowed; only a
    KeyError / AttributeError / TypeError (i.e. the fixture forgot a key the
    step reads) must fail this test.
    """
    cls = WORKFLOWS[workflow]

    ctx = dict(cls.debug_fixture())
    ctx["test_mode"] = True

    wf = cls(actor="debug")
    step_by_name = {s.name: s for s in wf.steps}
    assert step_name in step_by_name, (
        f"{workflow!r} has no step named {step_name!r}; update _PURE_STEPS"
    )
    step = step_by_name[step_name]

    try:
        result = asyncio.run(wf.execute_step(step, ctx))
    except (KeyError, AttributeError, TypeError) as exc:
        pytest.fail(
            f"{workflow}:{step_name} raised {type(exc).__name__}: {exc} - "
            f"debug_fixture() is likely missing a ctx key this step reads"
        )

    assert isinstance(result, StepResult), (
        f"{workflow}:{step_name} returned {type(result).__name__}, not StepResult"
    )
