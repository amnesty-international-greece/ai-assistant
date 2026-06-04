"""One-shot: add a "Λήψη Πρόσκλησης (PDF)" secondary button to Brevo template #234.

Inserts a secondary outlined button between the main CTA and the signature block.
The button href uses the [PDF_LINK] placeholder which the workflow substitutes with
the Google Drive download link.  When no Drive folder is configured, [PDF_LINK]
resolves to "#" and the button is a harmless no-op.

Run:
    python -m scripts.add_pdf_button_to_template
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from src.config import settings
from src.integrations.brevo import BrevoClient

TEMPLATE_ID = 234

# We insert just before the signature section (<!-- ── 7. SIGNATURE -->)
_SIGNATURE_COMMENT = "<!-- ── 7. SIGNATURE"

_PDF_BUTTON_HTML = """\
    <!-- ── 6b. PDF DOWNLOAD BUTTON ─────────────────────────────────── -->
    <tr><td style="padding: 0px 40px 20px; text-align: center; background-color: #ffffff;">
        <table cellpadding="0" cellspacing="0" border="0" role="presentation" align="center" style="margin:0 auto;">
          <tbody><tr><td style="border: 2px solid #000000; border-radius: 4px;">
            <a href="[PDF_LINK]" style="display:inline-block;padding:10px 36px;font-family:arial,helvetica,sans-serif;font-size:14px;font-weight:bold;color:#000000;text-decoration:none;">
              Λήψη Πρόσκλησης (PDF)
            </a>
          </td></tr></tbody>
        </table>
      </td></tr>

    """


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    async with httpx.AsyncClient() as http:
        resp = await http.get(
            f"https://api.brevo.com/v3/smtp/templates/{TEMPLATE_ID}",
            headers={"api-key": settings.brevo_api_key},
        )
        resp.raise_for_status()
    html: str = resp.json().get("htmlContent", "")
    if not html:
        raise RuntimeError(f"Template {TEMPLATE_ID} returned empty htmlContent")

    if "[PDF_LINK]" in html:
        print("PDF button already present in template — nothing to do.")
        return

    if _SIGNATURE_COMMENT not in html:
        raise RuntimeError(f"Could not find insertion point '{_SIGNATURE_COMMENT}' in template.")

    html = html.replace(_SIGNATURE_COMMENT, _PDF_BUTTON_HTML + _SIGNATURE_COMMENT, 1)

    client = BrevoClient()
    await client.update_template(
        template_id=TEMPLATE_ID,
        html_content=html,
        workflow="template_patch",
    )
    print(f"Template {TEMPLATE_ID} updated — PDF button added.")


if __name__ == "__main__":
    asyncio.run(main())
