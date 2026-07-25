"""Abstract base class that every OmniForge module must subclass.

Architectural constraints enforced here:
- Rule A-02: Every module inherits BaseModule and implements ALL abstract methods.
- Rule B-01: ``execute()`` must be an async generator (never blocks the GUI loop).
- Rule D-08: ``execute()`` must yield ProgressEvents at >= 10 checkpoints.

The Registry instantiates modules via the ``create()`` factory in each
module's ``__init__.py``. Modules are NOT imported directly by each other.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from typing import Any

from core.logger import get_logger
from core.models import ProgressEvent
from shared.ui_components import client_key, on_client_disconnect

logger = get_logger(__name__)


class BaseModule(ABC):
    """Contract every OmniForge module must satisfy.

    Subclasses provide a typed ``execute()`` accepting their specific
    Pydantic model; the base signature uses ``Any`` for Registry routing.

    Lifecycle::

        registry → on_load()
        user action → execute(params)   [async generator, yields ProgressEvent]
        app shutdown → on_unload()
    """

    @property
    @abstractmethod
    def module_id(self) -> str:
        """Dot-separated unique identifier, e.g. ``"extractors.llm_packager"``.

        Must match the ``id`` field in ``manifest.json``.
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable display name shown in the sidebar."""
        ...

    @property
    @abstractmethod
    def pillar(self) -> str:
        """Pillar key, e.g. ``"extractors"``. Must match the manifest."""
        ...

    @property
    @abstractmethod
    def icon(self) -> str:
        """Relative path to the SVG icon, e.g. ``"assets/icons/live_monitor.svg"``."""
        ...

    @abstractmethod
    async def on_load(self) -> None:
        """Validate external dependencies and initialise module state.

        Called by the Registry after import. If a required binary or library
        is absent, raise ``RuntimeError`` with a user-readable message — the
        Registry will mark the module DEGRADED and show an install guide.
        """
        ...

    @abstractmethod
    async def on_unload(self) -> None:
        """Release resources and cancel background tasks.

        Called on application shutdown or when the module is disabled.
        Must not raise.
        """
        ...

    async def on_deactivate(self) -> None:
        """Pause real-time work when the user navigates away from this module.

        Concrete (not abstract) with a no-op default, so existing modules need
        no change. The shell calls it on the *outgoing* module when another
        becomes active. Override it in modules that own background polling
        (live monitors, port watchers, log streamers) so switching away stops
        the work instead of leaving it running against a detached UI.

        The module stays loaded — subscriptions and state are untouched — so
        returning to it simply re-renders. ``on_unload`` remains the
        shutdown-time teardown. Must not raise (it is scheduled fire-and-forget
        by the shell). See ``docs/rfcs/0002-core-reliability-hardening.md``.
        """
        return None

    @abstractmethod
    async def execute(self, params: Any) -> AsyncIterator[ProgressEvent]:
        """Run the module's primary action.

        Must be an ``async def`` generator that ``yield``s at least 10
        ``ProgressEvent`` checkpoints for operations > 1 second (rule D-08).

        Args:
            params: Module-specific Pydantic model instance (typed in subclass).

        Yields:
            ProgressEvent at each checkpoint. The final event must have
            ``percent == 100`` or ``error`` set.
        """
        # Satisfy type checker — concrete implementations use `yield`.
        yield ProgressEvent(percent=100, message="Not implemented")  # pragma: no cover

    # ─── Per-client UI controllers ────────────────────────────────────────────

    def attach_ui(self, factory: Callable[[], Any]) -> Any:
        """Create and register this browser client's own UI controller.

        A module UI holds references to the elements it built. The Registry
        keeps one module instance for the whole application, so a single shared
        controller meant the *last* client to render owned those references —
        a second tab silently stole the first tab's handlers, and every
        re-render leaked another set of EventBus subscriptions.

        Keying controllers by client fixes both: each client gets its own, the
        previous controller for that same client is torn down before a
        re-render, and closing the tab releases it. See RFC 0003.

        Args:
            factory: Builds a fresh UI controller. It must expose
                ``subscribe()`` and ``unsubscribe()``.

        Returns:
            The newly created controller, ready to render.
        """
        registry: dict[str, Any] = self.__dict__.setdefault("_client_uis", {})
        key = client_key()
        self.detach_ui(key)

        controller = factory()
        controller.subscribe()
        registry[key] = controller
        on_client_disconnect(lambda: self.detach_ui(key))
        return controller

    def detach_ui(self, key: str | None = None) -> None:
        """Release UI controllers and their EventBus subscriptions.

        Args:
            key: The client to release. ``None`` releases every client's
                controller, which is what shutdown wants.
        """
        registry: dict[str, Any] = self.__dict__.setdefault("_client_uis", {})
        targets = [key] if key is not None else list(registry)
        for target in targets:
            controller = registry.pop(target, None)
            if controller is None:
                continue
            try:
                controller.unsubscribe()
            except Exception as exc:  # pragma: no cover - teardown must not raise
                # Full traceback: a controller failing to unsubscribe leaks its
                # EventBus handlers, and the reason is the only way to find out
                # why (rule D-07).
                logger.warning(
                    "module.ui_teardown_failed — client=%s", target, exc_info=exc
                )

    @abstractmethod
    def build_ui(self, container: Any) -> None:
        """Render the module's NiceGUI interface inside *container*.

        MUST NOT import or call ``logic.py`` directly — use the EventBus.
        MUST NOT block the event loop.

        Args:
            container: A NiceGUI element (column, card, etc.) used as parent.
        """
        ...
