"""Async pub/sub EventBus — the SOLE inter-component communication channel.

All module ↔ module and UI ↔ logic communication flows through this bus.
Direct module-to-module imports are an architectural violation (rule A-03).

Usage::

    from core.event_bus import event_bus

    # Publishing
    await event_bus.publish("my_event", payload)

    # Subscribing
    async def my_handler(payload: Any) -> None:
        ...

    event_bus.subscribe("my_event", my_handler)
    event_bus.unsubscribe("my_event", my_handler)
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Coroutine
from typing import Any

from core.logger import get_logger

logger = get_logger(__name__)

EventHandler = Callable[[Any], Coroutine[Any, Any, None]]


class _EventBus:
    """Singleton async pub/sub broker.

    Handlers are coroutines executed concurrently via ``asyncio.gather``.
    Exceptions in individual handlers are logged but do not abort others.
    """

    def __init__(self) -> None:
        self._subscribers: defaultdict[str, list[EventHandler]] = defaultdict(list)

    async def publish(self, event_type: str, payload: Any = None) -> None:
        """Dispatch *payload* to every handler registered for *event_type*.

        Handlers run concurrently; exceptions are logged, not re-raised.

        Args:
            event_type: Dot-namespaced event string, e.g.
                        ``"converters.pdf_suite.progress"``.
            payload: Arbitrary data delivered to every handler.
        """
        # .get(), not [] — indexing a defaultdict *creates* an entry, so every
        # topic ever published with no listener (most progress/done/error
        # topics, until a UI renders) left a permanent empty list behind and
        # the table grew for the life of the process. See RFC 0005.
        handlers = list(self._subscribers.get(event_type, ()))
        if not handlers:
            return
        logger.debug("event.publish — %s (%d handlers)", event_type, len(handlers))
        tasks = [asyncio.create_task(h(payload)) for h in handlers]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for handler, result in zip(handlers, results, strict=False):
            if isinstance(result, BaseException):
                logger.error(
                    "event.handler_error — event=%s handler=%s",
                    event_type,
                    handler.__qualname__,
                    exc_info=result,
                )

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Register *handler* coroutine for *event_type* events.

        Args:
            event_type: Event string to listen for.
            handler: Async callable ``(payload: Any) -> None``.
        """
        self._subscribers[event_type].append(handler)
        logger.debug("event.subscribe — event=%s handler=%s", event_type, handler.__qualname__)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """Remove *handler* from the *event_type* subscription list.

        A missing handler is logged as a warning, not an error.

        Args:
            event_type: Event string the handler was registered for.
            handler: The handler to remove.
        """
        handlers = self._subscribers.get(event_type, [])
        try:
            handlers.remove(handler)
            if not handlers:
                # Drop the topic entirely once its last handler goes, so a
                # module loaded and unloaded repeatedly does not leave a
                # growing tail of empty lists behind (RFC 0005).
                self._subscribers.pop(event_type, None)
            logger.debug(
                "event.unsubscribe — event=%s handler=%s", event_type, handler.__qualname__
            )
        except ValueError:
            logger.warning(
                "event.unsubscribe_miss — event=%s handler=%s",
                event_type,
                handler.__qualname__,
            )


#: Singleton instance — import and use this everywhere.
event_bus = _EventBus()
