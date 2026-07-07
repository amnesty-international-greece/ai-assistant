"""DEPRECATED - moved to :mod:`src.integrations.m365.mail`.

Re-exports the public surface so existing
``from src.integrations.m365_mail import ...`` statements keep working.
New code should import from ``src.integrations.m365.mail``.
"""
from src.integrations.m365.mail import M365MailClient  # noqa: F401
