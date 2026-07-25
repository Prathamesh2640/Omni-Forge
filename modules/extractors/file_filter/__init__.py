"""File Filter module package.

Exposes the ``create()`` factory called by the Registry. Logic and UI are
wired together exclusively through the EventBus (rule A-03).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from core.base_module import BaseModule
from core.logger import get_logger
from core.models import ProgressEvent
from modules.extractors.file_filter import logic as _logic_mod
from modules.extractors.file_filter import ui as _ui_mod

logger = get_logger(__name__)


class FileFilterModule(BaseModule):
    """BaseModule implementation for the File Filter."""

    def __init__(self) -> None:
        self._logic = _logic_mod.FileFilterLogic()

    # ─── Identity ─────────────────────────────────────────────────────────────

    @property
    def module_id(self) -> str:
        """Return ``"extractors.file_filter"``."""
        return "extractors.file_filter"

    @property
    def name(self) -> str:
        """Return ``"File Filter"``."""
        return "File Filter"

    @property
    def pillar(self) -> str:
        """Return ``"extractors"``."""
        return "extractors"

    @property
    def icon(self) -> str:
        """Return the path to the module's SVG icon."""
        return "assets/icons/file_filter.svg"

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def on_load(self) -> None:
        """Register EventBus handlers for the logic and UI layers."""
        await self._logic.register()
        logger.info("file_filter.on_load — OK")

    async def on_unload(self) -> None:
        """Deregister EventBus handlers."""
        self.detach_ui()
        await self._logic.unregister()
        logger.info("file_filter.on_unload — OK")

    # ─── Execution ────────────────────────────────────────────────────────────

    async def execute(self, params: Any) -> AsyncIterator[ProgressEvent]:
        """Delegate to the logic layer.

        Args:
            params: FilterParams, or a dict coerced into one.

        Yields:
            ProgressEvents from the logic layer.
        """
        from modules.extractors.file_filter.models import FilterParams

        if isinstance(params, dict):
            safe_params = FilterParams(**params)
        elif isinstance(params, FilterParams):
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
        """Render the File Filter panel into *container*.

        Args:
            container: NiceGUI parent element.
        """
        # A fresh controller per client — see BaseModule.attach_ui and RFC 0003.
        self.attach_ui(
            lambda: _ui_mod.FileFilterUI(module_id=self.module_id)
        ).render(container)


def create() -> FileFilterModule:
    """Factory function invoked by the Registry.

    Returns:
        A new FileFilterModule instance.
    """
    return FileFilterModule()
