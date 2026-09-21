"""Brevo (formerly Sendinblue) integration - newsletter distribution."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.config import settings
from src.core.audit import log_action
from src.profile import section

logger = logging.getLogger(__name__)

_BREVO_API_BASE = "https://api.brevo.com/v3"
AUTHORISED_IPS_URL = "https://app.brevo.com/security/authorised_ips"


def _error_detail(response: httpx.Response) -> str:
    """Brevo's own error message, falling back to the raw body."""
    try:
        data = response.json()
    except ValueError:
        return (response.text or "").strip()[:200]
    if isinstance(data, dict):
        return str(data.get("message") or data.get("code") or data)[:200]
    return str(data)[:200]


def build_recipients(list_ids, segment_ids) -> dict[str, list[int]]:
    """The campaign ``recipients`` object.

    Brevo keeps lists and segments in separate ID spaces (list 1 and segment 1
    are unrelated). A campaign may target either or both, but needs at least one.
    """
    recipients: dict[str, list[int]] = {}
    if list_ids:
        recipients["listIds"] = [int(x) for x in list_ids]
    if segment_ids:
        recipients["segmentIds"] = [int(x) for x in segment_ids]
    if not recipients:
        raise ValueError("A Brevo campaign needs at least one list or segment")
    return recipients


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
        segment_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """Create a campaign from a Brevo template and, optionally, send a TEST.

        Fetches the template HTML, performs plain-string replacement on all
        ``params`` entries, and creates an emailCampaign addressed to
        ``list_ids`` and/or ``segment_ids``. If ``test_emails`` is given, a test
        render goes to those addresses.

        This method never sends to the audience. A live send is always a
        separate, explicit :meth:`send_campaign_now` call. (It used to send live
        whenever ``test_emails`` was empty, so a test run without a configured
        test address would have reached the members.)

        Args:
            template_id: Brevo template ID to use as the design base.
            list_ids: Contact list IDs the campaign is addressed to.
            subject: Email subject line for the campaign.
            params: Mapping of placeholder strings → replacement values to apply
                    to the template HTML.  Example::

                        {
                            "[ΗΜΕΡΟΜΗΝΙΑ]": "14 Απριλίου 2026",
                            "[ΩΡΑ]": "20:30",
                            "[ΤΥΠΟΣ]": "τακτική",
                            "https://zoom.us/register/OLD": "https://zoom.us/j/NEW",
                        }

            campaign_name: Display name for the campaign in the Brevo dashboard
                           (defaults to ``subject``).
            preview_text: Inbox preview line.
            test_emails: If provided, send a test render to these addresses.
            workflow: Workflow name for audit logging.
            segment_ids: Contact segment IDs the campaign is addressed to.

        Returns:
            Dict with ``campaign_id`` (int) and ``test`` (bool: a test was sent).

        Raises:
            ValueError: if neither lists nor segments are given (before any request).
        """
        recipients = build_recipients(list_ids, segment_ids)

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
                "name":  settings.brevo.sender_name or section.name,
            },
            "htmlContent": html,
            "recipients": recipients,
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
            details={"name": name, "template_id": template_id, "recipients": recipients},
        )
        logger.info("Created Brevo campaign %d: %s (%s)", campaign_id, name, recipients)

        # ── 4. Optional test send (never a live send) ────────────────────────
        if test_emails:
            async with httpx.AsyncClient() as client:
                send_resp = await client.post(
                    f"{_BREVO_API_BASE}/emailCampaigns/{campaign_id}/sendTest",
                    headers=self._headers(),
                    json={"emailTo": test_emails},
                )
                if not send_resp.is_success:
                    logger.error(
                        "Failed to send Brevo test for campaign %d (%s): %s",
                        campaign_id, send_resp.status_code, send_resp.text,
                    )
                send_resp.raise_for_status()
            log_action(
                workflow=workflow,
                action="campaign_test_sent",
                actor="system",
                target=str(campaign_id),
                details={"test_emails": test_emails},
            )
            logger.info("Brevo campaign %d test sent to %s", campaign_id, test_emails)

        return {"campaign_id": campaign_id, "test": bool(test_emails)}

    async def preflight(
        self,
        *,
        template_id: int | None,
        list_ids: list[int],
        segment_ids: list[int],
        sender_email: str,
    ) -> dict[str, Any]:
        """Check, read-only, that a newsletter send would work.

        Verifies the API key and this machine's IP, the template, the sender,
        and that every configured list and segment exists. Makes no changes.

        Returns:
            ``{"ok": bool, "problems": [str], "audience": [str]}`` where
            ``audience`` describes the resolved lists/segments by name.
        """
        problems: list[str] = []
        audience: list[str] = []
        base = _BREVO_API_BASE
        async with httpx.AsyncClient(timeout=20) as client:
            account = await client.get(f"{base}/account", headers=self._headers())
            if account.status_code == 401:
                problems.append(
                    f"Brevo rejected the request: {_error_detail(account)}. If this "
                    f"mentions an IP address, authorise it at {AUTHORISED_IPS_URL}."
                )
                return {"ok": False, "problems": problems, "audience": audience}
            if not account.is_success:
                problems.append(
                    f"Brevo account check failed ({account.status_code}): {_error_detail(account)}"
                )
                return {"ok": False, "problems": problems, "audience": audience}

            if template_id:
                tmpl = await client.get(
                    f"{base}/smtp/templates/{template_id}", headers=self._headers()
                )
                if tmpl.status_code == 404:
                    problems.append(
                        f"Template {template_id} does not exist (brevo.newsletter_template_id)."
                    )
                elif not tmpl.is_success:
                    problems.append(
                        f"Template {template_id} could not be read ({tmpl.status_code}): "
                        f"{_error_detail(tmpl)}"
                    )
            else:
                problems.append(
                    "No newsletter template configured (brevo.newsletter_template_id)."
                )

            senders = await client.get(f"{base}/senders", headers=self._headers())
            if senders.is_success:
                wanted = (sender_email or "").strip().lower()
                match = [
                    s for s in (senders.json() or {}).get("senders") or []
                    if (s.get("email") or "").strip().lower() == wanted
                ]
                if not match:
                    problems.append(
                        f"Sender {sender_email} is not registered in Brevo (brevo.sender_email)."
                    )
                elif not match[0].get("active", True):
                    problems.append(
                        f"Sender {sender_email} exists in Brevo but is not verified/active."
                    )
            else:
                problems.append(
                    f"Could not list Brevo senders ({senders.status_code}): {_error_detail(senders)}"
                )

            for list_id in list_ids:
                resp = await client.get(
                    f"{base}/contacts/lists/{list_id}", headers=self._headers()
                )
                if resp.status_code == 404:
                    problems.append(
                        f"List {list_id} does not exist. Lists and segments have separate "
                        f"IDs; if {list_id} is a segment, put it under "
                        "brevo.newsletter_segment_ids instead."
                    )
                elif resp.is_success:
                    data = resp.json() or {}
                    count = data.get("uniqueSubscribers", data.get("totalSubscribers", "?"))
                    audience.append(f"list {list_id} '{data.get('name')}' ({count} contacts)")
                else:
                    problems.append(
                        f"List {list_id} could not be read ({resp.status_code}): "
                        f"{_error_detail(resp)}"
                    )

            if segment_ids:
                found: dict[int, str] = {}
                offset, page = 0, 50
                while True:
                    resp = await client.get(
                        f"{base}/contacts/segments",
                        headers=self._headers(),
                        params={"limit": page, "offset": offset},
                    )
                    if not resp.is_success:
                        problems.append(
                            f"Could not list Brevo segments ({resp.status_code}): "
                            f"{_error_detail(resp)}"
                        )
                        break
                    batch = (resp.json() or {}).get("segments") or []
                    for seg in batch:
                        found[int(seg.get("id"))] = seg.get("segmentName") or seg.get("name") or ""
                    if len(batch) < page:
                        break
                    offset += page
                for segment_id in segment_ids:
                    if segment_id in found:
                        audience.append(f"segment {segment_id} '{found[segment_id]}'")
                    elif not any(p.startswith("Could not list Brevo segments") for p in problems):
                        problems.append(
                            f"Segment {segment_id} does not exist. If {segment_id} is a list, "
                            "put it under brevo.newsletter_list_ids instead."
                        )

        if not list_ids and not segment_ids:
            problems.append(
                "No newsletter audience configured (brevo.newsletter_segment_ids / "
                "brevo.newsletter_list_ids); a live send would be refused."
            )
        return {"ok": not problems, "problems": problems, "audience": audience}

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
        logger.info("Brevo API key verified - account active")
        return response.json()

    async def send_campaign_now(
        self,
        campaign_id: int,
        workflow: str = "brevo",
    ) -> None:
        """Trigger an immediate live send for an already-created campaign.

        The only method that sends to the audience. Called after the user
        confirms they're happy with the test send. The campaign must be in
        'draft' or 'queued' state.

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
        logger.info("Brevo campaign %d sent live to its audience", campaign_id)

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
