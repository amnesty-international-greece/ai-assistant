"""Integration clients for external services.

The Discord bot lives in the ``discord/`` sub-package — import from there,
not from this top-level namespace.
"""

from src.integrations.onedrive import OneDriveClient
from src.integrations.google_drive import GoogleClient
from src.integrations.gmail import GmailClient
from src.integrations.zoom import ZoomClient
from src.integrations.brevo import BrevoClient

__all__ = [
    "OneDriveClient",
    "GoogleClient",
    "GmailClient",
    "ZoomClient",
    "BrevoClient",
]
