"""DEPRECATED - moved to :mod:`src.integrations.m365.subscriptions`.

Re-exports the public surface so existing
``from src.integrations.graph_subscriptions import ...`` statements keep
working.  New code should import from ``src.integrations.m365.subscriptions``.
"""
from src.integrations.m365.subscriptions import (  # noqa: F401
    DEFAULT_RESOURCE,
    GraphSubscriptionError,
    GraphSubscriptionsClient,
)
