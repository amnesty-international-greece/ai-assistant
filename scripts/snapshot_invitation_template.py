"""Snapshot the Google Docs invitation template to a local .docx file.

One-off utility per ROADMAP corrections list (item #13).  The runtime
invitation workflow continues to use the live Google Doc via
``batchUpdate`` (which preserves formatting perfectly), but having a
local DOCX snapshot in ``data/templates/`` gives us:

  - A reviewable copy under git history (file is small enough to commit)
  - A disaster-recovery fallback if the Google Doc is ever deleted
  - A reference point when we want to update the template's layout

Usage:
    python -m scripts.snapshot_invitation_template

Reads ``settings.google.invitation_template_id`` and writes
``data/templates/[Πρότυπο] Πρόσκληση.docx``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from src.config import settings
from src.integrations.google_drive import GoogleClient

_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def main() -> int:
    template_id = settings.google.invitation_template_id
    if not template_id:
        print("ERROR: google.invitation_template_id not set in config.yaml", file=sys.stderr)
        return 1

    out_dir = Path("data/templates")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "[Πρότυπο] Πρόσκληση.docx"

    g = GoogleClient()
    g.authenticate()

    print(f"Exporting Google Doc {template_id} → {out_path}")
    content = g._drive_service.files().export(
        fileId=template_id, mimeType=_DOCX_MIME
    ).execute()
    out_path.write_bytes(content)
    print(f"OK  {len(content):,} bytes written")
    print()
    print("Inspect the formatting:  open the .docx in Word / LibreOffice")
    print("If it looks acceptable, we can later switch the runtime path")
    print("from Google Docs to local-DOCX manipulation.  Until then, the")
    print("snapshot is a backup only - runtime keeps using Google.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
