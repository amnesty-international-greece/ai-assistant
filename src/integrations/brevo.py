"""Brevo (formerly Sendinblue) integration — newsletter distribution."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.config import settings
from src.core.audit import log_action

logger = logging.getLogger(__name__)

_BREVO_API_BASE = "https://api.brevo.com/v3"


class BrevoClient:
    """Client for Brevo email marketing API."""

    def __init__(self) -> None:
        self._api_key = settings.brevo_api_key

    def _headers(self) -> dict[str, str]:
        return {"api-key": self._api_key, "Content-Type": "application/json"}

    async def send_campaign(
        self,
        template_id: int,
        list_ids: list[int],
        subject: str,
        params: dict[str, str] | None = None,
        campaign_name: str | None = None,
        preview_text: str | None = None,
        test_emails: list[str] | None = None,
        workflow: str = "brevo",
    ) -> dict[str, Any]:
        """Create and send an email campaign using a Brevo template with placeholder substitution.

        Fetches the template HTML, performs plain-string replacement on all ``params``
        entries, creates an emailCampaign with the rendered HTML, then either sends a
        test (``test_emails`` provided) or sends immediately to the contact lists.

        Args:
            template_id: Brevo template ID to use as the design base.
            list_ids: Contact list IDs for production sends (ignored for test sends).
            subject: Email subject line for the campaign.
            params: Mapping of placeholder strings → replacement values to apply
                    to the template HTML before sending.  Example::

                        {
                            "[ΗΜΕΡΟΜΗΝΙΑ]": "14 Απριλίου 2026",
                            "[ΩΡΑ]": "20:30",
                            "[ΤΥΠΟΣ]": "τακτική",
                            "https://zoom.us/register/OLD": "https://zoom.us/j/NEW",
                        }

            campaign_name: Display name for the campaign in the Brevo dashboard
                           (defaults to ``subject``).
            test_emails: If provided, send a test render to these addresses instead
                         of doing a real send to the contact lists.
            workflow: Workflow name for audit logging.

        Returns:
            Dict with ``campaign_id`` (int) and ``test`` (bool).
        """
        # ── 1. Fetch template HTML ────────────────────────────────────────────
        async with httpx.AsyncClient() as client:
            tmpl_resp = await client.get(
                f"{_BREVO_API_BASE}/smtp/templates/{template_id}",
                headers=self._headers(),
            )
            if not tmpl_resp.is_success:
                logger.error(
                    "Failed to fetch Brevo template %d (%s): %s",
                    template_id, tmpl_resp.status_code, tmpl_resp.text,
                )
            tmpl_resp.raise_for_status()

        html: str = tmpl_resp.json().get("htmlContent", "")

        # ── 2. Render: replace placeholders ──────────────────────────────────
        if params:
            for placeholder, value in params.items():
                html = html.replace(placeholder, str(value))

        # ── 3. Create campaign ───────────────────────────────────────────────
        name = campaign_name or subject
        create_payload: dict[str, Any] = {
            "name": name,
            "subject": subject,
            "sender": {
                "email": settings.brevo.sender_email,
                "name":  settings.brevo.sender_name,
            },
            "htmlContent": html,
            "recipients": {"listIds": list_ids},
        }
        if preview_text:
            create_payload["previewText"] = preview_text

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{_BREVO_API_BASE}/emailCampaigns",
                headers=self._headers(),
                json=create_payload,
            )
            if not resp.is_success:
                logger.error(
                    "Failed to create Brevo campaign (%s): %s",
                    resp.status_code, resp.text,
                )
            resp.raise_for_status()

        campaign_id: int = resp.json()["id"]
        log_action(
            workflow=workflow,
            action="campaign_created",
            actor="system",
            target=str(campaign_id),
            details={"name": name, "template_id": template_id},
        )
        logger.info("Created Brevo campaign %d: %s", campaign_id, name)

        # ── 4. Send (test or production) ─────────────────────────────────────
        async with httpx.AsyncClient() as client:
            if test_emails:
                send_resp = await client.post(
                    f"{_BREVO_API_BASE}/emailCampaigns/{campaign_id}/sendTest",
                    headers=self._headers(),
                    json={"emailTo": test_emails},
                )
            else:
                send_resp = await client.post(
                    f"{_BREVO_API_BASE}/emailCampaigns/{campaign_id}/sendNow",
                    headers=self._headers(),
                )
            if not send_resp.is_success:
                logger.error(
                    "Failed to send Brevo campaign %d (%s): %s",
                    campaign_id, send_resp.status_code, send_resp.text,
                )
            send_resp.raise_for_status()

        action = "campaign_test_sent" if test_emails else "campaign_sent"
        log_action(
            workflow=workflow,
            action=action,
            actor="system",
            target=str(campaign_id),
            details={"list_ids": list_ids, "test_emails": test_emails},
        )
        logger.info(
            "Brevo campaign %d %s",
            campaign_id,
            f"test sent to {test_emails}" if test_emails else f"sent to lists {list_ids}",
        )
        return {"campaign_id": campaign_id, "test": bool(test_emails)}

    async def update_template(
        self,
        template_id: int,
        html_content: str,
        subject: str | None = None,
        template_name: str | None = None,
        workflow: str = "brevo",
    ) -> None:
        """Upload new HTML to an existing Brevo template (PUT /smtp/templates/{id}).

        Args:
            template_id: Brevo template ID to overwrite.
            html_content: Full HTML string for the new template body.
            subject: Optional default subject line stored on the template.
            template_name: Optional display name in the Brevo dashboard.
            workflow: Workflow name for audit logging.
        """
        payload: dict[str, Any] = {"htmlContent": html_content}
        if subject:
            payload["subject"] = subject
        if template_name:
            payload["templateName"] = template_name

        async with httpx.AsyncClient() as client:
            response = await client.put(
                f"{_BREVO_API_BASE}/smtp/templates/{template_id}",
                headers=self._headers(),
                json=payload,
            )
            if not response.is_success:
                logger.error(
                    "Failed to update Brevo template %d (%s): %s",
                    template_id, response.status_code, response.text,
                )
            response.raise_for_status()

        log_action(
            workflow=workflow,
            action="template_updated",
            actor="system",
            target=str(template_id),
            details={"template_name": template_name},
        )
        logger.info("Brevo template %d updated successfully", template_id)

    async def verify_api_key(self) -> dict[str, Any]:
        """Verify the Brevo API key by calling GET /account.

        Returns:
            Account info dict on success.

        Raises:
            httpx.HTTPStatusError: If the key is invalid or IP is not authorized.
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{_BREVO_API_BASE}/account",
                headers=self._headers(),
            )
            if not response.is_success:
                logger.error("Brevo API key verification failed (%s): %s", response.status_code, response.text)
            response.raise_for_status()
        logger.info("Brevo API key verified — account active")
        return response.json()

    async def send_campaign_now(
        self,
        campaign_id: int,
        workflow: str = "brevo",
    ) -> None:
        """Trigger an immediate live send for an already-created campaign.

        Called after the user confirms they're happy with the test send.
        The campaign must be in 'draft' or 'queued' state.

        Args:
            campaign_id: Brevo campaign ID (returned by send_campaign).
            workflow: Workflow name for audit logging.
        """
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{_BREVO_API_BASE}/emailCampaigns/{campaign_id}/sendNow",
                headers=self._headers(),
            )
            if not resp.is_success:
                logger.error(
                    "Failed to live-send Brevo campaign %d (%s): %s",
                    campaign_id, resp.status_code, resp.text,
                )
            resp.raise_for_status()

        log_action(
            workflow=workflow,
            action="campaign_sent",
            actor="system",
            target=str(campaign_id),
        )
        logger.info("Brevo campaign %d sent live to contact lists", campaign_id)

    async def delete_campaign(
        self,
        campaign_id: int,
        workflow: str = "brevo",
    ) -> None:
        """Delete a draft campaign (DELETE /emailCampaigns/{campaignId}).

        Used in test mode to clean up after sending the test email so no
        orphaned draft campaigns accumulate in the Brevo dashboard.

        Args:
            campaign_id: Brevo campaign ID to delete.
            workflow: Workflow name for audit logging.
        """
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{_BREVO_API_BASE}/emailCampaigns/{campaign_id}",
                headers=self._headers(),
            )
            if not resp.is_success:
                logger.error(
                    "Failed to delete Brevo campaign %d (%s): %s",
                    campaign_id, resp.status_code, resp.text,
                )
            resp.raise_for_status()

        log_action(
            workflow=workflow,
            action="campaign_deleted",
            actor="system",
            target=str(campaign_id),
        )
        logger.info("Brevo campaign %d deleted (test mode cleanup)", campaign_id)

    async def get_contacts(self, list_id: int, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Retrieve contacts from a Brevo contact list.

        Args:
            list_id: Brevo contact list ID.
            limit: Maximum number of contacts to return (max 500).
            offset: Pagination offset.

        Returns:
            List of contact dicts with email, attributes, etc.
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{_BREVO_API_BASE}/contacts/lists/{list_id}/contacts",
                headers=self._headers(),
                params={"limit": limit, "offset": offset},
            )
            response.raise_for_status()
            return response.json().get("contacts", [])
