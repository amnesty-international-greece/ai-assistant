"""Remove the PDF download button added to Brevo template #234."""
from __future__ import annotations
import asyncio, logging, httpx
from src.config import settings
from src.integrations.brevo import BrevoClient

TEMPLATE_ID = 234
_PDF_BUTTON_BLOCK = """\
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
        resp = await http.get(f"https://api.brevo.com/v3/smtp/templates/{TEMPLATE_ID}",
                              headers={"api-key": settings.brevo_api_key})
        resp.raise_for_status()
    html: str = resp.json().get("htmlContent", "")
    if _PDF_BUTTON_BLOCK not in html:
        print("PDF button not found - nothing to remove.")
        return
    html = html.replace(_PDF_BUTTON_BLOCK, "", 1)
    await BrevoClient().update_template(TEMPLATE_ID, html_content=html, workflow="template_patch")
    print("PDF button removed from template.")

asyncio.run(main())
