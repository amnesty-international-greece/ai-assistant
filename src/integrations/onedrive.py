"""DEPRECATED — moved to :mod:`src.integrations.m365.onedrive`.

Re-exports the public surface so existing
``from src.integrations.onedrive import ...`` statements keep working.
New code should import from ``src.integrations.m365.onedrive``.
"""
from src.integrations.m365.onedrive import (  # noqa: F401
    OneDriveAuthRequired,
    OneDriveClient,
)
