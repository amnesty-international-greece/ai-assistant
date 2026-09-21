"""The platform reads the section from its profile, and carries none of it.

`sections/test-en/` is a fictional second section in English, with a different
name, board prefix and quorum. Anything the platform still produces in Greek
when that profile is loaded is a value it should not have been holding.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.profile.loader import DEFAULT_SECTION, SECTIONS_DIR, Section, load_section

SRC = Path(__file__).resolve().parents[2] / "src"


@pytest.fixture
def greek() -> Section:
    return load_section("amnesty-gr")


@pytest.fixture
def english() -> Section:
    return load_section("test-en")


def test_the_greek_section_describes_itself(greek):
    assert greek.name == "Διεθνής Αμνηστία - Ελληνικό Τμήμα"
    assert greek.profile.board.ref_prefix == "ΔΣ"
    assert greek.profile.locale == "el"
    assert greek.profile.roles["board"].endswith("@amnesty.org.gr")


def test_the_statutes_rules_are_data_with_their_article(greek):
    assert greek.rules.board.quorum == 5
    assert greek.rules.board.quorum_article == "16.2"       # καταστατικό 16.2
    assert greek.rules.assembly.min_notice_days == 30       # καταστατικό 13.5
    assert greek.rules.assembly.quorum_required is False    # καταστατικό 13.6


def test_a_second_section_differs_in_every_such_value(english):
    assert english.name == "Rights Watch - Test Section"
    assert english.profile.locale == "en"
    assert english.profile.board.ref_prefix == "BT"
    assert english.rules.board.quorum == 4
    assert english.profile.roles["secgen"] == "secretary@example.org"


def test_an_unknown_section_assumes_nothing():
    """Better an empty profile than the Greek one silently standing in."""
    blank = load_section("no-such-section")
    assert blank.profile.identity.section == ""
    assert blank.rules.board.quorum == 0          # nothing is enforced
    assert blank.profile.roles == {}


def test_role_mailboxes_reach_the_settings_from_the_profile():
    from src.config import settings

    assert settings.roles.board == load_section(settings.section).profile.roles["board"]


def test_quorum_is_judged_by_the_sections_own_number(monkeypatch):
    from src.workflows import minutes_skeleton

    presence = {"present": [{"name": "A"}, {"name": "B"}, {"name": "C"},
                            {"name": "D"}, {"name": "E"}], "absent": []}

    greek = minutes_skeleton._quorum(presence)
    assert (greek["required"], greek["present"], greek["met"]) == (5, 5, True)

    monkeypatch.setattr(minutes_skeleton, "section", load_section("test-en"))
    english = minutes_skeleton._quorum({"present": presence["present"][:3], "absent": []})
    assert english["required"] == 4 and english["met"] is False


def test_quorum_is_not_judged_when_the_section_states_none(monkeypatch):
    from src.workflows import minutes_skeleton

    monkeypatch.setattr(minutes_skeleton, "section", load_section("no-such-section"))
    result = minutes_skeleton._quorum({"present": [{"name": "A"}], "absent": []})
    assert result["required"] == 0 and result["met"] is None


def test_a_sections_own_words_live_in_its_folder(greek):
    """Prompts, templates and the corpus are the section's, not the platform's."""
    for key in ("prompts", "email_templates", "governance_corpus"):
        path = greek.asset_path(key)
        assert path.exists(), f"{key} missing at {path}"
        assert "sections" in path.parts, f"{key} still lives outside the section"

    assert (greek.asset_path("prompts") / "board_minutes.md").exists()
    assert (greek.asset_path("email_templates") / "invitation_board.html").exists()


def test_an_asset_a_section_never_declared_is_an_error(english):
    with pytest.raises(KeyError):
        english.asset_path("letterhead")


def test_the_platform_keeps_no_prompts_or_templates_of_its_own():
    stray = [
        str(p.relative_to(SRC.parent))
        for p in list(SRC.rglob("*.md")) + list(SRC.rglob("*.html"))
        if "prompts" in p.parts or "templates" in p.parts
    ]
    assert not stray, f"Section wording still under src/: {stray}"


def test_every_section_folder_carries_both_files():
    for folder in SECTIONS_DIR.iterdir():
        if not folder.is_dir():
            continue
        assert (folder / "profile.yaml").exists(), f"{folder.name} has no profile.yaml"
        assert (folder / "rules.yaml").exists(), f"{folder.name} has no rules.yaml"


def test_the_platform_does_not_name_the_greek_section():
    """The one thing this whole slice exists to guarantee."""
    needles = ("Διεθνής Αμνηστία", "Διεθνούς Αμνηστίας", "Ελληνικό Τμήμα", "ΔΙΕΘΝΗΣ ΑΜΝΗΣΤΙΑ")
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if any(n in node.value for n in needles):
                    offenders.append(f"{path.relative_to(SRC.parent)}:{node.lineno}")
    assert not offenders, (
        "The section's name belongs in sections/<slug>/profile.yaml:\n  "
        + "\n  ".join(sorted(set(offenders)))
    )
