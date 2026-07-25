"""Duplicate Detective module package.

Exposes the ``create()`` factory called by the Registry. Logic and UI are
wired together exclusively through the EventBus (rule A-03).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from core.base_module import BaseModule
from core.logger import get_logger
from core.models import ProgressEvent
from modules.extractors.duplicate_finder import logic as _logic_mod
from modules.extractors.duplicate_finder import ui as _ui_mod

logger = get_logger(__name__)


class DuplicateFinderModule(BaseModule):
    """BaseModule implementation for the Duplicate Detective."""

    def __init__(self) -> None:
        self._logic = _logic_mod.DuplicateFinderLogic()

    # ─── Identity ─────────────────────────────────────────────────────────────

    @property
    def module_id(self) -> str:
        """Return ``"extractors.duplicate_finder"``."""
        return "extractors.duplicate_finder"

    @property
    def name(self) -> str:
        """Return ``"Duplicate Detective"``."""
        return "Duplicate Detective"

    @property
    def pillar(self) -> str:
        """Return ``"extractors"``."""
        return "extractors"

    @property
    def icon(self) -> str:
        """Return the path to the module's SVG icon."""
        return "assets/icons/duplicate_finder.svg"

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def on_load(self) -> None:
        """Register EventBus handlers for the logic and UI layers."""
        await self._logic.register()
        logger.info("duplicate_finder.on_load — OK")

    async def on_unload(self) -> None:
        """Deregister EventBus handlers."""
        self.detach_ui()
        await self._logic.unregister()
        logger.info("duplicate_finder.on_unload — OK")

    # ─── Execution ────────────────────────────────────────────────────────────

    async def execute(self, params: Any) -> AsyncIterator[ProgressEvent]:
        """Delegate to the logic layer.

        Args:
            params: ResolveParams, or a dict coerced into one.

        Yields:
            ProgressEvents from the logic layer.
        """
        from modules.extractors.duplicate_finder.models import ResolveParams

        if isinstance(params, dict):
            safe_params = ResolveParams(**params)
        elif isinstance(params, ResolveParams):
            safe_params = params
        else:
            yield ProgressEvent(
                percent=100,
                message="Invalid parameters.",
                error=f"Invalid params type: {type(params).__name__}",
            )
            return

        async for event in self._logic.execute(safe_params):
            yield event

    # ─── UI ───────────────────────────────────────────────────────────────────

    def build_ui(self, container: Any) -> None:
        """Render the Duplicate Detective panel into *container*.

        Args:
            container: NiceGUI parent element.
        """
        # A fresh controller per client — see BaseModule.attach_ui and RFC 0003.
        self.attach_ui(
            lambda: _ui_mod.DuplicateFinderUI(module_id=self.module_id)
        ).render(container)


def create() -> DuplicateFinderModule:
    """Factory function invoked by the Registry.

    Returns:
        A new DuplicateFinderModule instance.
    """
    return DuplicateFinderModule()
