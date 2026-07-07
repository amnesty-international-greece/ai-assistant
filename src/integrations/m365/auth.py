"""Shared MSAL plumbing for every Microsoft 365 / Graph integration.

Before this module existed, the same 30-line block (cache deserialize +
ConfidentialClientApplication + acquire_token_silent + persist) was duplicated
across :class:`OneDriveClient`, :class:`M365MailClient`,
:class:`M365InboxClient`, and :class:`GraphSubscriptionsClient`.  Any change
to the auth flow had to be made in all four files (and was a frequent source
of drift - slightly different error messages, slightly different scope sets).

The :class:`M365GraphAuthMixin` collapses that into one definition.  Each
client subclasses the mixin, sets its own ``_SCOPES`` class attribute, and
gets ``_get_token()`` / ``_headers()`` / ``_persist_cache()`` for free.

The shared token cache (``data/tokens.json``) is established once via
``ai-assistant auth microsoft`` and serves every scope set the mixin
declares - Files.ReadWrite.All, Mail.ReadWrite, etc.  No per-client
re-authentication required.
"""
from __future__ import annotations

import json
import logging
from typing import ClassVar

import msal

from src.config import settings
from src.core.tokens import get_section, set_section

logger = logging.getLogger(__name__)


# Defined here (not in :mod:`m365.onedrive`) so the mixin can raise it
# without an import cycle.  The legacy alias ``OneDriveAuthRequired`` is
# re-exported from :mod:`m365.onedrive` for backwards compatibility.
class M365AuthRequired(RuntimeError):
    """Raised when no Microsoft account is cached or refresh fails.

    Caller should suggest the user run ``ai-assistant auth microsoft`` and
    sign in interactively.
    """


class M365GraphAuthMixin:
    """Drop-in mixin providing MSAL token acquisition for Graph clients.

    Subclasses MUST set:
        ``_SCOPES``: list[str] - the OAuth scopes this client needs (e.g.
                      ``["Files.ReadWrite.All"]`` or ``["Mail.ReadWrite"]``).

    Subclasses gain:
        ``_get_token()`` → cached or freshly-acquired access token (string).
        ``_headers()``   → dict ready to splat into ``httpx`` calls.

    The mixin's ``__init__`` initialises ``self._cache`` and ``self._app``;
    subclasses can call ``super().__init__()`` from their own ``__init__``,
    or - if they have no other init logic - skip overriding ``__init__``
    entirely.
    """

    _SCOPES: ClassVar[list[str]] = []

    def __init__(self) -> None:
        self._cache = msal.SerializableTokenCache()
        cached = get_section("microsoft")
        if cached:
            self._cache.deserialize(json.dumps(cached))

        self._app = msal.ConfidentialClientApplication(
            client_id=settings.ms_client_id,
            client_credential=settings.ms_client_secret,
            authority=f"https://login.microsoftonline.com/{settings.ms_tenant_id}",
            token_cache=self._cache,
        )

    # ── Token management ────────────────────────────────────────────────────

    def _persist_cache(self) -> None:
        """Write the MSAL cache back to ``data/tokens.json`` if dirty."""
        if self._cache.has_state_changed:
            set_section("microsoft", json.loads(self._cache.serialize()))

    def _get_token(self) -> str:
        """Return a fresh access token for ``self._SCOPES``.

        Raises:
            M365AuthRequired: if no Microsoft account is cached or refresh
                fails.  Caller should propagate to the user with a hint to
                run ``ai-assistant auth microsoft``.
        """
        if not self._SCOPES:
            raise RuntimeError(
                f"{type(self).__name__} declares no _SCOPES - set it on the class."
            )

        accounts = self._app.get_accounts()
        if not accounts:
            raise M365AuthRequired(
                "No Microsoft account cached.  Run: ai-assistant auth microsoft"
            )

        result = self._app.acquire_token_silent(
            scopes=self._SCOPES,
            account=accounts[0],
        )
        if result and "access_token" in result:
            self._persist_cache()
            return result["access_token"]

        raise M365AuthRequired(
            "Microsoft token refresh failed.  Run: ai-assistant auth microsoft"
        )

    def _headers(self) -> dict[str, str]:
        """Build the standard Authorization + JSON-content headers."""
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }
