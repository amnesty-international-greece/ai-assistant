"""Microsoft Graph webhook subscription lifecycle.

A subscription is the bridge between an Exchange mailbox and our FastAPI
``/webhooks/m365/inbox`` route - Graph POSTs a notification each time a
new message lands.  Two-pass coverage:

1.  **Webhook**       → near-real-time (sub-second) delivery
2.  **Safety poll**   → backstop at 12:00 Europe/Athens daily (see
                        :mod:`src.core.scheduler`); catches anything the
                        webhook missed during a renewal gap or downtime.

Renewal policy
--------------
Outlook resources max out at 4230 minutes (~70.5h).  We pick a lifetime
from config (``settings.m365_inbox.subscription_lifetime_minutes``) and
renew when remaining time < ``renew_threshold_hours`` (default 24h),
giving us 2-3 attempts before any real outage.

clientState
-----------
A random opaque token persisted per subscription.  Every notification
includes it; we compare against the DB row and drop notifications whose
clientState doesn't match - defends against forged/replayed deliveries.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.config import settings
from src.core.audit import (
    delete_graph_subscription,
    get_active_graph_subscriptions,
    log_action,
    upsert_graph_subscription,
)
from src.integrations.m365.auth import M365GraphAuthMixin

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Default resource path - watches the signed-in user's Inbox.
DEFAULT_RESOURCE = "/me/mailFolders('Inbox')/messages"


class GraphSubscriptionError(RuntimeError):
    """Raised on any non-2xx response from the /subscriptions endpoint."""


class GraphSubscriptionsClient(M365GraphAuthMixin):
    """Thin wrapper over the Graph ``/subscriptions`` collection.

    Reuses the same MSAL token cache as every other M365 client via
    :class:`M365GraphAuthMixin` - one ``ai-assistant auth microsoft`` run
    covers all four (OneDrive, mail, inbox, subscriptions).
    """

    # Mail.ReadWrite covers /subscriptions for /me/mailFolders/inbox/messages.
    _SCOPES = ["Mail.ReadWrite"]

    # ── Public API ───────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        notification_url: str | None = None,
        resource: str = DEFAULT_RESOURCE,
        change_type: str = "created",
        lifetime_minutes: int | None = None,
    ) -> dict[str, Any]:
        """Create a new webhook subscription.

        Args:
            notification_url: Public HTTPS endpoint Graph will POST to.
                Defaults to ``settings.m365_inbox.webhook_url +
                "/webhooks/m365/inbox"``.
            resource: Graph resource to watch.  Defaults to the signed-in
                user's Inbox.
            change_type: One of ``"created"``, ``"updated"``, ``"deleted"``
                or a comma list.  Default ``"created"`` - we only care
                about new messages.
            lifetime_minutes: Subscription lifetime; falls back to
                ``settings.m365_inbox.subscription_lifetime_minutes``.

        Returns:
            The parsed JSON body of the create response, including the
            assigned ``id`` and ``expirationDateTime``.
        """
        cfg = settings.m365_inbox
        base = (notification_url or cfg.webhook_url).rstrip("/")
        if not base:
            raise GraphSubscriptionError(
                "No webhook URL configured.  Set m365_inbox.webhook_url in "
                "config.yaml or pass notification_url=... explicitly."
            )
        full_url = base if base.endswith("/webhooks/m365/inbox") else f"{base}/webhooks/m365/inbox"

        lifetime = lifetime_minutes or cfg.subscription_lifetime_minutes
        expiration = datetime.now(timezone.utc) + timedelta(minutes=lifetime)
        client_state = secrets.token_urlsafe(32)

        payload = {
            "changeType": change_type,
            "notificationUrl": full_url,
            "resource": resource,
            "expirationDateTime": expiration.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "clientState": client_state,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{_GRAPH_BASE}/subscriptions",
                headers=self._headers(),
                json=payload,
            )
            if resp.status_code >= 300:
                raise GraphSubscriptionError(
                    f"Graph /subscriptions returned {resp.status_code}: {resp.text}"
                )
            body = resp.json()

        upsert_graph_subscription(
            body["id"],
            resource=resource,
            client_state=client_state,
            expiration_date_time=body["expirationDateTime"],
        )
        log_action(
            workflow="m365_inbox",
            action="subscription_create",
            actor="system",
            target=full_url,
            details={
                "subscription_id": body["id"],
                "resource": resource,
                "expires": body["expirationDateTime"],
            },
        )
        logger.info(
            "Created Graph subscription %s on %s (expires %s)",
            body["id"], resource, body["expirationDateTime"],
        )
        return body

    async def renew(
        self,
        subscription_id: str,
        *,
        lifetime_minutes: int | None = None,
    ) -> dict[str, Any]:
        """Extend a subscription's expirationDateTime via PATCH.

        Args:
            subscription_id: The Graph-assigned id of the subscription.
            lifetime_minutes: New lifetime relative to *now*; defaults to
                ``settings.m365_inbox.subscription_lifetime_minutes``.

        Returns:
            The parsed JSON body of the PATCH response (new expirationDateTime).
        """
        cfg = settings.m365_inbox
        lifetime = lifetime_minutes or cfg.subscription_lifetime_minutes
        expiration = datetime.now(timezone.utc) + timedelta(minutes=lifetime)
        payload = {
            "expirationDateTime": expiration.isoformat(timespec="seconds").replace("+00:00", "Z"),
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.patch(
                f"{_GRAPH_BASE}/subscriptions/{subscription_id}",
                headers=self._headers(),
                json=payload,
            )
            if resp.status_code >= 300:
                raise GraphSubscriptionError(
                    f"Graph PATCH /subscriptions/{subscription_id} returned "
                    f"{resp.status_code}: {resp.text}"
                )
            body = resp.json()

        # Preserve the existing clientState - only expiry changes here.
        existing = get_active_graph_subscriptions()
        for row in existing:
            if row["subscription_id"] == subscription_id:
                upsert_graph_subscription(
                    subscription_id,
                    resource=row["resource"],
                    client_state=row["client_state"],
                    expiration_date_time=body["expirationDateTime"],
                )
                break

        log_action(
            workflow="m365_inbox",
            action="subscription_renew",
            actor="system",
            target=subscription_id,
            details={"new_expires": body["expirationDateTime"]},
        )
        logger.info(
            "Renewed Graph subscription %s (now expires %s)",
            subscription_id, body["expirationDateTime"],
        )
        return body

    async def delete(self, subscription_id: str) -> None:
        """Delete a subscription via Graph + remove the local DB row."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.delete(
                f"{_GRAPH_BASE}/subscriptions/{subscription_id}",
                headers=self._headers(),
            )
            # 204 = success; 404 = already gone (treat as success)
            if resp.status_code not in (204, 404):
                raise GraphSubscriptionError(
                    f"Graph DELETE /subscriptions/{subscription_id} returned "
                    f"{resp.status_code}: {resp.text}"
                )
        delete_graph_subscription(subscription_id)
        log_action(
            workflow="m365_inbox",
            action="subscription_delete",
            actor="system",
            target=subscription_id,
        )
        logger.info("Deleted Graph subscription %s", subscription_id)

    async def list_remote(self) -> list[dict[str, Any]]:
        """List all subscriptions Graph thinks we own (truth source)."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_GRAPH_BASE}/subscriptions",
                headers=self._headers(),
            )
            if resp.status_code >= 300:
                raise GraphSubscriptionError(
                    f"Graph GET /subscriptions returned {resp.status_code}: {resp.text}"
                )
            return resp.json().get("value", [])

    async def renew_expiring(
        self,
        *,
        threshold_hours: int | None = None,
    ) -> list[str]:
        """Renew every local subscription whose remaining lifetime is below
        the threshold.

        Returns:
            List of subscription ids that were actually renewed.
        """
        cfg = settings.m365_inbox
        threshold = threshold_hours if threshold_hours is not None else cfg.renew_threshold_hours
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=threshold)

        renewed: list[str] = []
        for row in get_active_graph_subscriptions():
            try:
                # Graph timestamps come back as e.g. "2026-05-29T12:00:00Z".
                exp = datetime.fromisoformat(row["expiration_date_time"].replace("Z", "+00:00"))
            except (ValueError, KeyError):
                continue
            if exp <= cutoff:
                try:
                    await self.renew(row["subscription_id"])
                    renewed.append(row["subscription_id"])
                except GraphSubscriptionError as e:
                    logger.error("Failed to renew %s: %s", row["subscription_id"], e)
        return renewed
