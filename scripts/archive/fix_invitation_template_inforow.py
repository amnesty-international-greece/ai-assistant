"""One-shot: patch Brevo template #234 so the date / time / Zoom pairs stop
line-breaking between the emoji and its value on narrow viewports.

Strategy:
    • Fetch current htmlContent via GET /smtp/templates/234
    • Replace each "emoji &nbsp; <strong>[PLACEHOLDER]</strong>" fragment
      with a <span style="white-space:nowrap"> wrapper that keeps the pair
      glued together.  Removes the plain space that was sitting between
      &nbsp; and <strong>, which was the actual break opportunity.
    • PUT the modified html back via update_template().

Run:
    python -m scripts.fix_invitation_template_inforow
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from src.config import settings
from src.integrations.brevo import BrevoClient

TEMPLATE_ID = 234

REPLACEMENTS: list[tuple[str, str]] = [
    # First run: wrap emoji+value in nowrap span.
    (
        '📅&nbsp; <strong fr-original-style="" style="font-weight: 700;">[ΗΜΕΡΟΜΗΝΙΑ]</strong>',
        '<span style="white-space:nowrap;"><strong style="font-weight:700;">[ΗΜΕΡΟΜΗΝΙΑ]</strong></span>',
    ),
    (
        '🕐&nbsp; <strong fr-original-style="" style="font-weight: 700;">[ΩΡΑ]</strong>',
        '<span style="white-space:nowrap;"><strong style="font-weight:700;">[ΩΡΑ]</strong></span>',
    ),
    (
        '📍&nbsp; <strong fr-original-style="" style="font-weight: 700;">Zoom</strong>',
        '<span style="white-space:nowrap;"><strong style="font-weight:700;">Zoom</strong></span>',
    ),
    # Re-run (after previous patch): strip the emoji from the nowrap span form.
    (
        '<span style="white-space:nowrap;">📅&nbsp;<strong style="font-weight:700;">[ΗΜΕΡΟΜΗΝΙΑ]</strong></span>',
        '<span style="white-space:nowrap;"><strong style="font-weight:700;">[ΗΜΕΡΟΜΗΝΙΑ]</strong></span>',
    ),
    (
        '<span style="white-space:nowrap;">🕐&nbsp;<strong style="font-weight:700;">[ΩΡΑ]</strong></span>',
        '<span style="white-space:nowrap;"><strong style="font-weight:700;">[ΩΡΑ]</strong></span>',
    ),
    (
        '<span style="white-space:nowrap;">📍&nbsp;<strong style="font-weight:700;">Zoom</strong></span>',
        '<span style="white-space:nowrap;"><strong style="font-weight:700;">Zoom</strong></span>',
    ),
]


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # 1. Fetch current html
    async with httpx.AsyncClient() as http:
        resp = await http.get(
            f"https://api.brevo.com/v3/smtp/templates/{TEMPLATE_ID}",
            headers={"api-key": settings.brevo_api_key},
        )
        resp.raise_for_status()
    html: str = resp.json().get("htmlContent", "")
    if not html:
        raise RuntimeError(f"Template {TEMPLATE_ID} returned empty htmlContent")

    # 2. Apply replacements - fail loudly if any fragment is missing so we
    #    don't silently push an unchanged template.
    applied = 0
    for old, new in REPLACEMENTS:
        if old not in html:
            print(f"WARNING: fragment not found, skipping:\n    {old[:80]}…")
            continue
        html = html.replace(old, new, 1)
        applied += 1

    if applied == 0:
        raise RuntimeError("No replacements applied - template structure has drifted.")
    print(f"Applied {applied}/{len(REPLACEMENTS)} replacements.")

    # 3. Push back
    client = BrevoClient()
    await client.update_template(
        template_id=TEMPLATE_ID,
        html_content=html,
        workflow="template_patch",
    )
    print(f"Template {TEMPLATE_ID} updated successfully.")


if __name__ == "__main__":
    asyncio.run(main())
