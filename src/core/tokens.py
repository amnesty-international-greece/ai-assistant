"""Unified token store — persists OAuth tokens in data/tokens.json.

Current state (2026-05-23)
--------------------------
Only Microsoft uses this store; Google's OAuth token still lives in its
own file ``data/google_token.json`` (see ``src/integrations/google_drive.py``).
The Google migration was scoped but not executed because Google's helper
is already small, working, and gitignored — there's no value in churning
auth code for a single-file cleanup.

If/when Google moves here, the file shape becomes:
    {
      "google":    { ...google.oauth2.credentials.Credentials.to_json() fields... },
      "microsoft": { ...MSAL SerializableTokenCache JSON (parsed to dict)... }
    }

Usage:
    from src.core.tokens import get_section, set_section
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"
_TOKEN_FILE = _DATA_DIR / "tokens.json"


def _ensure_data_dir() -> None:
    """Create the data/ directory if it does not exist."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)


def _read_all() -> dict[str, Any]:
    """Read the full tokens.json, returning an empty dict if missing or corrupt."""
    if not _TOKEN_FILE.exists():
        return {}
    try:
        return json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def get_section(service: str) -> dict[str, Any]:
    """Return the token dict for *service* (e.g. "google", "microsoft").

    Returns an empty dict if the section does not exist yet.
    """
    return _read_all().get(service, {})


def set_section(service: str, data: dict[str, Any]) -> None:
    """Write *data* into tokens.json under key *service*.

    Uses write-to-temp-then-rename for atomicity — a reader always sees a
    complete, valid JSON file (never a partial write).

    On Windows, os.replace can raise PermissionError if another thread is
    simultaneously renaming a file in the same directory.  We retry a few
    times with a short backoff to ride out these transient collisions.
    """
    import time as _time

    _ensure_data_dir()
    all_tokens = _read_all()
    all_tokens[service] = data
    payload = json.dumps(all_tokens, ensure_ascii=False, indent=2)

    # Atomic write: write to a sibling temp file, then rename.
    fd, tmp_path = tempfile.mkstemp(dir=_DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)

        # Retry loop for Windows: os.replace can transiently fail with
        # PermissionError when another process/thread holds the target file.
        for attempt in range(5):
            try:
                os.replace(tmp_path, _TOKEN_FILE)
                return  # success
            except PermissionError:
                if attempt == 4:
                    raise
                _time.sleep(0.02 * (attempt + 1))  # 20 ms, 40 ms, 60 ms, 80 ms
    except Exception:
        # Clean up the temp file on failure so it does not linger.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


