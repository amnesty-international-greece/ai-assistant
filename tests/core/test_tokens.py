"""Tests for src.core.tokens — unified token store."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_token_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect token paths to a temporary directory for test isolation."""
    import src.core.tokens as tokens_mod

    token_file = tmp_path / "tokens.json"

    monkeypatch.setattr(tokens_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(tokens_mod, "_TOKEN_FILE", token_file)

    return token_file


# ---------------------------------------------------------------------------
# Basic get/set round-trip
# ---------------------------------------------------------------------------


def test_set_then_get_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """set_section then get_section must return exactly what was stored."""
    _patch_token_paths(tmp_path, monkeypatch)
    from src.core.tokens import get_section, set_section

    data = {"refresh_token": "abc123", "scopes": ["Files.ReadWrite.All"]}
    set_section("microsoft", data)
    assert get_section("microsoft") == data


def test_get_missing_section_returns_empty_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_section on a key that does not exist must return {}."""
    _patch_token_paths(tmp_path, monkeypatch)
    from src.core.tokens import get_section

    assert get_section("microsoft") == {}
    assert get_section("google") == {}


def test_multiple_sections_coexist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Writing two sections must not overwrite each other."""
    _patch_token_paths(tmp_path, monkeypatch)
    from src.core.tokens import get_section, set_section

    set_section("google", {"token": "google_tok"})
    set_section("microsoft", {"token": "ms_tok"})

    assert get_section("google") == {"token": "google_tok"}
    assert get_section("microsoft") == {"token": "ms_tok"}


def test_set_overwrites_existing_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second set_section call must replace the first value."""
    _patch_token_paths(tmp_path, monkeypatch)
    from src.core.tokens import get_section, set_section

    set_section("google", {"token": "old"})
    set_section("google", {"token": "new"})
    assert get_section("google") == {"token": "new"}


def test_tokens_file_is_valid_json_with_greek(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The written file must be valid, human-readable JSON (Greek not escaped)."""
    token_file = _patch_token_paths(tmp_path, monkeypatch)
    from src.core.tokens import set_section

    set_section("misc", {"note": "Αρχείο ΔΣ"})

    raw = token_file.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert parsed["misc"]["note"] == "Αρχείο ΔΣ"
    # ensure_ascii=False: Greek characters must appear as-is, not escaped
    assert "\\u" not in raw or "Αρχείο" in raw


# ---------------------------------------------------------------------------
# Atomic write / concurrency
# ---------------------------------------------------------------------------


def test_concurrent_writes_do_not_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent set_section calls must produce a valid tokens.json at the end.

    The write-to-temp-then-rename pattern means any reader always sees a
    complete file, never a partial write.  This test fires 20 threads writing
    different sections simultaneously and then verifies the file is parseable.
    """
    _patch_token_paths(tmp_path, monkeypatch)
    from src.core.tokens import set_section

    errors: list[Exception] = []

    def _write(i: int) -> None:
        try:
            set_section(f"svc_{i}", {"value": i})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Threads raised exceptions: {errors}"

    # The file must be valid JSON after all concurrent writes
    token_file = tmp_path / "tokens.json"
    parsed = json.loads(token_file.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
