"""In-process async event bus — publishers don't know who's listening.

Used to decouple platform workflows from the Discord bot. Workflows publish
typed events; subscribers (currently only the Discord platform_bridge cog,
later also OneDrive archiver, newsletter, etc.) react.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

EventCallback = Callable[[Any], Awaitable[None]]
T = TypeVar("T")


class EventBus:
    """Async pub-sub. Subscribers run sequentially; one bad subscriber can't
    break the publisher or other subscribers."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventCallback]] = defaultdict(list)

    def subscribe(self, event_type: str, callback: EventCallback) -> None:
        """Register *callback* to be invoked for every publish of *event_type*."""
        self._subscribers[event_type].append(callback)
        logger.debug("EventBus: %s subscribed to %s", getattr(callback, "__qualname__", callback), event_type)

    def unsubscribe(self, event_type: str, callback: EventCallback) -> None:
        """Remove a previously-registered callback. No-op if not registered."""
        if callback in self._subscribers.get(event_type, []):
            self._subscribers[event_type].remove(callback)

    async def publish(self, event_type: str, payload: Any) -> None:
        """Dispatch *payload* to every subscriber of *event_type*.

        Subscribers are awaited sequentially in subscription order. Errors are
        logged and swallowed — they never propagate to the publisher and never
        block sibling subscribers.
        """
        subs = list(self._subscribers.get(event_type, []))
        logger.info("EventBus: publish %s to %d subscriber(s)", event_type, len(subs))
        for cb in subs:
            try:
                await cb(payload)
            except Exception as exc:
                logger.exception(
                    "EventBus: subscriber %s for %s raised: %s",
                    getattr(cb, "__qualname__", cb), event_type, exc,
                )

    def clear(self) -> None:
        """Drop all subscribers — useful for tests."""
        self._subscribers.clear()


# Module-level singleton — import this where you need it.
bus = EventBus()
