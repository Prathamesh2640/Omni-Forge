"""Bulk Regex Renamer module package.

Exposes the ``create()`` factory called by the Registry. Logic and UI are
wired together exclusively through the EventBus (rule A-03).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from core.base_module import BaseModule
from core.logger import get_logger
from core.models import ProgressEvent
from modules.extractors.bulk_renamer import logic as _logic_mod
from modules.extractors.bulk_renamer import ui as _ui_mod

logger = get_logger(__name__)


class BulkRenamerModule(BaseModule):
    """BaseModule implementation for the Bulk Regex Renamer."""

    def __init__(self) -> None:
        self._logic = _logic_mod.BulkRenamerLogic()

    # ─── Identity ─────────────────────────────────────────────────────────────

    @property
    def module_id(self) -> str:
        """Return ``"extractors.bulk_renamer"``."""
        return "extractors.bulk_renamer"

    @property
    def name(self) -> str:
        """Return ``"Bulk Regex Renamer"``."""
        return "Bulk Regex Renamer"

    @property
    def pillar(self) -> str:
        """Return ``"extractors"``."""
        return "extractors"

    @property
    def icon(self) -> str:
        """Return the path to the module's SVG icon."""
        return "assets/icons/bulk_renamer.svg"

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def on_load(self) -> None:
        """Register EventBus handlers for the logic and UI layers."""
        await self._logic.register()
        logger.info("bulk_renamer.on_load — OK")

    async def on_unload(self) -> None:
        """Deregister EventBus handlers."""
        self.detach_ui()
        await self._logic.unregister()
        logger.info("bulk_renamer.on_unload — OK")

    # ─── Execution ────────────────────────────────────────────────────────────

    async def execute(self, params: Any) -> AsyncIterator[ProgressEvent]:
        """Delegate to the logic layer.

        Args:
            params: RenameParams or UndoParams, or a dict coerced into one
                (a dict carrying a ``"pairs"`` key is treated as UndoParams).

        Yields:
            ProgressEvents from the logic layer.
        """
        from modules.extractors.bulk_renamer.models import RenameParams, UndoParams

        safe_params: RenameParams | UndoParams
        if isinstance(params, dict):
            safe_params = UndoParams(**params) if "pairs" in params else RenameParams(**params)
        elif isinstance(params, RenameParams | UndoParams):
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
        """Render the Bulk Regex Renamer panel into *container*.

        Args:
            container: NiceGUI parent element.
        """
        # A fresh controller per client — see BaseModule.attach_ui and RFC 0003.
        self.attach_ui(
            lambda: _ui_mod.BulkRenamerUI(module_id=self.module_id)
        ).render(container)


def create() -> BulkRenamerModule:
    """Factory function invoked by the Registry.

    Returns:
        A new BulkRenamerModule instance.
    """
    return BulkRenamerModule()
