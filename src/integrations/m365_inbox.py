"""DEPRECATED — moved to :mod:`src.integrations.m365.inbox`.

This shim exists so existing ``from src.integrations.m365_inbox import ...``
statements keep working during the migration.  New code should import from
``src.integrations.m365.inbox`` directly.

Scheduled for removal after all callers are updated (see
``docs/code_structure_review.md``).
"""
from src.integrations.m365.inbox import (  # noqa: F401
    M365InboxClient,
    default_sender_allow_list,
    normalize_subject,
    sender_allowed,
    subject_matches,
)
