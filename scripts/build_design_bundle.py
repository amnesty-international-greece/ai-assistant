"""Build design_bundle.zip - everything Claude Design needs to restyle the
platform's visual surfaces (HTML emails, Discord embeds, the Zoom sidebar).

Run:  python scripts/build_design_bundle.py
Output: design_bundle.zip in the repo root.

Includes the DESIGN_BRIEF.md, the source of every visual surface, and freshly
rendered email previews (so the designer sees real output, not just templates).
"""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "design_bundle.zip"

# Source surfaces (relative to repo root) → kept at their real paths in the zip
SOURCES = [
    "DESIGN_BRIEF.md",
    # HTML emails
    "assets/email_templates/_shell.html",
    "assets/email_templates/invitation_board.html",
    "assets/email_templates/scheduling_with_poll.html",
    "assets/email_templates/scheduling_no_poll.html",
    "assets/email_templates/minutes_share.html",
    "assets/email_templates/archive_confirmation.html",
    "assets/email_templates/archive_failure.html",
    "assets/email_templates/egkyklios_cover.html",
    # Discord embeds + brand tokens
    "src/integrations/discord/brand.py",
    "src/integrations/discord/embeds/board_meeting.py",
    "src/integrations/discord/embeds/egkyklios.py",
    # Zoom in-meeting sidebar
    "src/api/zoom_app.py",
    # Preview tooling
    "scripts/preview_email.py",
]


def main() -> None:
    # 1. Render fresh email previews into data/preview/
    print("Rendering email previews…")
    subprocess.run([sys.executable, "scripts/preview_email.py"], cwd=ROOT, check=False)

    # 2. Zip sources + rendered previews
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in SOURCES:
            p = ROOT / rel
            if p.exists():
                z.write(p, rel)
                print(f"  + {rel}")
            else:
                print(f"  ! missing: {rel}")
        preview_dir = ROOT / "data" / "preview"
        for p in sorted(preview_dir.glob("*.html")):
            arc = f"rendered_previews/{p.name}"
            z.write(p, arc)
            print(f"  + {arc}")

    print(f"\nWrote {OUT}  ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
