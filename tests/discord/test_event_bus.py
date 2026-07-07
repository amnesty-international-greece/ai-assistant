"""Tests for src.core.event_bus - the in-process async pub-sub."""
from __future__ import annotations

import pytest

from src.core.event_bus import EventBus


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_bus() -> EventBus:
    """Return a fresh EventBus (not the module singleton) for each test."""
    return EventBus()


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subscribe_and_publish_calls_handler():
    """A subscribed handler is called with the published payload."""
    bus = make_bus()
    received: list = []

    async def handler(payload):
        received.append(payload)

    bus.subscribe("test.event", handler)
    await bus.publish("test.event", {"key": "value"})

    assert received == [{"key": "value"}]


@pytest.mark.asyncio
async def test_multiple_subscribers_run_in_order():
    """Multiple subscribers are called in subscription order."""
    bus = make_bus()
    order: list[str] = []

    async def first(payload):
        order.append("first")

    async def second(payload):
        order.append("second")

    async def third(payload):
        order.append("third")

    bus.subscribe("ordered.event", first)
    bus.subscribe("ordered.event", second)
    bus.subscribe("ordered.event", third)

    await bus.publish("ordered.event", None)

    assert order == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_raising_subscriber_does_not_break_publisher_or_siblings():
    """A subscriber that raises must not propagate the error or skip later subscribers."""
    bus = make_bus()
    sibling_called: list[bool] = []

    async def bad_handler(payload):
        raise RuntimeError("boom")

    async def good_handler(payload):
        sibling_called.append(True)

    bus.subscribe("fault.event", bad_handler)
    bus.subscribe("fault.event", good_handler)

    # publish must not raise
    await bus.publish("fault.event", "data")

    # sibling subscriber must have run despite the earlier failure
    assert sibling_called == [True]


@pytest.mark.asyncio
async def test_unsubscribe_removes_callback():
    """After unsubscribe, the handler is no longer called."""
    bus = make_bus()
    called: list = []

    async def handler(payload):
        called.append(payload)

    bus.subscribe("remove.event", handler)
    bus.unsubscribe("remove.event", handler)

    await bus.publish("remove.event", "should not arrive")

    assert called == []


@pytest.mark.asyncio
async def test_unsubscribe_noop_when_not_registered():
    """unsubscribe on an unknown callback is a no-op (no exception)."""
    bus = make_bus()

    async def handler(payload):
        pass

    # Should not raise
    bus.unsubscribe("ghost.event", handler)


@pytest.mark.asyncio
async def test_clear_removes_all_subscribers():
    """clear() drops every subscriber across all event types."""
    bus = make_bus()
    called: list = []

    async def handler(payload):
        called.append(payload)

    bus.subscribe("event.a", handler)
    bus.subscribe("event.b", handler)

    bus.clear()

    await bus.publish("event.a", 1)
    await bus.publish("event.b", 2)

    assert called == []


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_is_safe():
    """Publishing to an event type with zero subscribers must not raise."""
    bus = make_bus()
    await bus.publish("nobody.listening", {"x": 1})


@pytest.mark.asyncio
async def test_subscribe_different_events_are_isolated():
    """A subscriber on event A is not triggered by event B."""
    bus = make_bus()
    received_a: list = []
    received_b: list = []

    async def handler_a(payload):
        received_a.append(payload)

    async def handler_b(payload):
        received_b.append(payload)

    bus.subscribe("event.a", handler_a)
    bus.subscribe("event.b", handler_b)

    await bus.publish("event.a", "for-a")

    assert received_a == ["for-a"]
    assert received_b == []


@pytest.mark.asyncio
async def test_handler_receives_exact_payload():
    """The handler receives the exact object published (identity, not a copy)."""
    bus = make_bus()
    received: list = []

    async def handler(payload):
        received.append(payload)

    payload_obj = object()
    bus.subscribe("identity.event", handler)
    await bus.publish("identity.event", payload_obj)

    assert len(received) == 1
    assert received[0] is payload_obj
