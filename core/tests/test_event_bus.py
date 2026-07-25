"""Unit tests for core.event_bus."""
from __future__ import annotations

from typing import Any

import pytest

from core.event_bus import _EventBus

_EVENT = "test.event"


@pytest.fixture
def bus() -> _EventBus:
    """Return a fresh, isolated EventBus (never the module singleton)."""
    return _EventBus()


async def test_publish_delivers_payload_to_subscriber(bus: _EventBus) -> None:
    received: list[Any] = []

    async def handler(payload: Any) -> None:
        received.append(payload)

    bus.subscribe(_EVENT, handler)
    await bus.publish(_EVENT, {"value": 42})

    assert received == [{"value": 42}]


async def test_publish_with_no_subscribers_is_a_noop(bus: _EventBus) -> None:
    await bus.publish("nobody.listening", "payload")


async def test_all_subscribers_receive_the_same_event(bus: _EventBus) -> None:
    calls: list[str] = []

    async def first(_payload: Any) -> None:
        calls.append("first")

    async def second(_payload: Any) -> None:
        calls.append("second")

    bus.subscribe(_EVENT, first)
    bus.subscribe(_EVENT, second)
    await bus.publish(_EVENT)

    assert sorted(calls) == ["first", "second"]


async def test_failing_handler_does_not_prevent_others(bus: _EventBus) -> None:
    """A raising handler is logged and swallowed so siblings still run."""
    survived: list[str] = []

    async def exploding(_payload: Any) -> None:
        raise RuntimeError("boom")

    async def healthy(_payload: Any) -> None:
        survived.append("ok")

    bus.subscribe(_EVENT, exploding)
    bus.subscribe(_EVENT, healthy)
    await bus.publish(_EVENT)

    assert survived == ["ok"]


async def test_unsubscribe_stops_delivery(bus: _EventBus) -> None:
    received: list[Any] = []

    async def handler(payload: Any) -> None:
        received.append(payload)

    bus.subscribe(_EVENT, handler)
    bus.unsubscribe(_EVENT, handler)
    await bus.publish(_EVENT, "ignored")

    assert received == []


async def test_unsubscribe_unknown_handler_is_tolerated(bus: _EventBus) -> None:
    """Removing a handler that was never registered warns rather than raising."""

    async def never_registered(_payload: Any) -> None:
        return None

    bus.unsubscribe(_EVENT, never_registered)


async def test_handler_added_during_publish_is_not_called(bus: _EventBus) -> None:
    """publish() snapshots the handler list, so re-entrant subscribes are deferred."""
    late_calls: list[str] = []

    async def late(_payload: Any) -> None:
        late_calls.append("late")

    async def subscriber(_payload: Any) -> None:
        bus.subscribe(_EVENT, late)

    bus.subscribe(_EVENT, subscriber)
    await bus.publish(_EVENT)

    assert late_calls == []


class TestSubscriberTableHygiene:
    """Publishing must not grow the subscriber table (RFC 0005).

    ``publish`` read ``self._subscribers[event_type]`` on a defaultdict, so
    every topic ever published with no listener — most progress/done/error
    topics, until a UI renders — left a permanent empty list behind.
    """

    async def test_publishing_to_nobody_creates_no_entry(self) -> None:
        bus = _EventBus()

        for index in range(100):
            await bus.publish(f"module.topic_{index}", index)

        assert bus._subscribers == {}

    async def test_the_last_unsubscribe_drops_the_topic(self) -> None:
        bus = _EventBus()

        async def handler(_payload: object) -> None:
            return None

        bus.subscribe("module.topic", handler)
        assert "module.topic" in bus._subscribers

        bus.unsubscribe("module.topic", handler)

        assert "module.topic" not in bus._subscribers

    async def test_a_remaining_handler_keeps_the_topic(self) -> None:
        bus = _EventBus()
        seen: list[object] = []

        async def first(_payload: object) -> None:
            return None

        async def second(payload: object) -> None:
            seen.append(payload)

        bus.subscribe("module.topic", first)
        bus.subscribe("module.topic", second)
        bus.unsubscribe("module.topic", first)
        await bus.publish("module.topic", "delivered")

        assert seen == ["delivered"]

    async def test_a_load_unload_cycle_leaves_nothing_behind(self) -> None:
        """A module reloaded repeatedly must not grow the table each time."""
        bus = _EventBus()

        async def handler(_payload: object) -> None:
            return None

        for _cycle in range(50):
            bus.subscribe("module.execute", handler)
            await bus.publish("module.execute", None)
            bus.unsubscribe("module.execute", handler)

        assert bus._subscribers == {}
