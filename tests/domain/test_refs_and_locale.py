"""One meeting reference, one decision reference, one set of month names.

These identifiers appear in the agenda sheet, the Zoom topic, the invitation,
the minutes, the register and every Discord thread. They used to be rebuilt
from parts at a dozen call sites, each writing the format out again, so an
unusual meeting number or a missing date produced a different reference in one
place than another.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.domain.locale_el import (
    format_date,
    meeting_type_adjective,
    meeting_type_genitive,
    month_genitive,
    month_title,
)
from src.domain.refs import (
    decision_ref,
    meeting_id,
    meeting_ref,
    meeting_ref_from_date,
    parse_meeting_ref,
)

SRC = Path(__file__).resolve().parents[2] / "src"


def test_meeting_ref_pads_the_number():
    assert meeting_ref(7, 2026) == "ΔΣ07-2026"
    assert meeting_ref("7", "2026") == "ΔΣ07-2026"
    assert meeting_ref(12, 2026) == "ΔΣ12-2026"


def test_the_sheets_own_reference_always_wins():
    """D5 is the source of truth, even when it is not what we would build."""
    assert meeting_ref(7, 2026, raw="ΔΣ7-2026") == "ΔΣ7-2026"
    assert meeting_ref_from_date(7, "2026-09-18", raw="ΔΣ07Β-2026") == "ΔΣ07Β-2026"


def test_a_missing_date_does_not_invent_a_year():
    assert meeting_ref_from_date(7, "") == "ΔΣ07-ΧΧΧΧ"
    assert meeting_ref(7, None) == "ΔΣ07-ΧΧΧΧ"


def test_no_number_means_no_reference():
    assert meeting_ref("", "2026") == ""
    assert meeting_id("") == ""


def test_parse_round_trips():
    assert parse_meeting_ref("ΔΣ07-2026") == (7, 2026)
    assert parse_meeting_ref(meeting_ref(3, 2026)) == (3, 2026)
    assert parse_meeting_ref("Συνεδρίαση ΔΣ07-2026") is None


def test_meeting_id_is_the_bus_and_discord_identifier():
    assert meeting_id("ΔΣ07-2026") == "board_meeting:ΔΣ07-2026"


def test_decision_ref_numbers_within_the_meeting():
    assert decision_ref("ΔΣ05-2026", 1) == "ΔΣ01-05-2026"
    assert decision_ref("ΔΣ05-2026", 12) == "ΔΣ12-05-2026"


@pytest.mark.parametrize("bad", ["", "ΔΣ2026", "Συνεδρίαση"])
def test_decision_ref_refuses_a_meeting_it_cannot_parse(bad):
    with pytest.raises(ValueError):
        decision_ref(bad, 1)


def test_decision_ref_refuses_a_zero_sequence():
    with pytest.raises(ValueError):
        decision_ref("ΔΣ05-2026", 0)


def test_the_drafter_and_the_minutes_agree_on_a_decision_ref():
    from src.workflows.decision_drafter import compute_decision_ref

    assert compute_decision_ref("ΔΣ05-2026", 2) == decision_ref("ΔΣ05-2026", 2)


def test_dates_and_months_read_as_Greek_prose():
    assert format_date("2026-06-09") == "9 Ιουνίου 2026"
    assert format_date("not a date") == "not a date"
    assert month_genitive(5) == "Μαΐου"
    assert month_title(5) == "ΜΑΪΟΣ"


def test_meeting_type_wording():
    assert meeting_type_genitive("ΤΑΚΤΙΚΗ") == "ΤΑΚΤΙΚΗΣ"
    assert meeting_type_genitive("ΕΚΤΑΚΤΗ") == "ΕΚΤΑΚΤΗΣ"
    assert meeting_type_adjective("ΕΚΤΑΚΤΗ") == "έκτακτη"
    assert meeting_type_adjective("") == "τακτική"


def test_nobody_builds_a_meeting_reference_by_hand_any_more():
    """A new f-string of the ΔΣNN-YYYY shape is the drift this prevents."""
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name == "refs.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            # "ΔΣ" immediately followed by a substituted value is the ref
            # being assembled; "Συνεδρίαση ΔΣ07-2026" as display text is not.
            for left, right in zip(node.values, node.values[1:]):
                if (isinstance(left, ast.Constant) and isinstance(left.value, str)
                        and left.value.endswith("ΔΣ")   # f"...ΔΣ{...}", no space
                        and isinstance(right, ast.FormattedValue)):
                    offenders.append(f"{path.relative_to(SRC.parent)}:{node.lineno}")
    assert not offenders, (
        "Meeting/decision references are built in src/domain/refs.py:\n  "
        + "\n  ".join(offenders)
    )


def test_month_names_are_written_once():
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name == "locale_el.py":
            continue
        if "Ιανουαρίου" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(SRC.parent)))
    assert not offenders, f"Month tables belong in the locale module: {offenders}"
