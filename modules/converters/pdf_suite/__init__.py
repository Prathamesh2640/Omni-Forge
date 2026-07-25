"""PDF Suite module package.

Exposes the ``create()`` factory called by the Registry. Logic and UI are
wired together exclusively through the EventBus (rule A-03).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from core.base_module import BaseModule
from core.logger import get_logger
from core.models import ProgressEvent
from modules.converters.pdf_suite import logic as _logic_mod
from modules.converters.pdf_suite import ui as _ui_mod

logger = get_logger(__name__)


class PdfSuiteModule(BaseModule):
    """BaseModule implementation for the PDF Suite."""

    def __init__(self) -> None:
        self._logic = _logic_mod.PdfSuiteLogic()

    # ─── Identity ─────────────────────────────────────────────────────────────

    @property
    def module_id(self) -> str:
        """Return ``"converters.pdf_suite"``."""
        return "converters.pdf_suite"

    @property
    def name(self) -> str:
        """Return ``"PDF Suite"``."""
        return "PDF Suite"

    @property
    def pillar(self) -> str:
        """Return ``"converters"``."""
        return "converters"

    @property
    def icon(self) -> str:
        """Return the path to the module's SVG icon."""
        return "assets/icons/pdf_suite.svg"

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def on_load(self) -> None:
        """Verify PyMuPDF is importable and register EventBus handlers.

        Raises:
            RuntimeError: When PyMuPDF is unavailable, so the Registry marks
                the module DEGRADED instead of letting it fail mid-operation.
        """
        try:
            import fitz
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise RuntimeError(
                "PyMuPDF is required by the PDF Suite. Install it with:"
                "  pip install PyMuPDF"
            ) from exc

        await self._logic.register()
        logger.info("pdf_suite.on_load — OK (PyMuPDF %s)", fitz.VersionBind)

    async def on_unload(self) -> None:
        """Deregister EventBus handlers."""
        self.detach_ui()
        await self._logic.unregister()
        logger.info("pdf_suite.on_unload — OK")

    # ─── Execution ────────────────────────────────────────────────────────────

    async def execute(self, params: Any) -> AsyncIterator[ProgressEvent]:
        """Delegate to the logic layer.

        Args:
            params: PdfParams, or a dict coerced into one.

        Yields:
            ProgressEvents from the logic layer.
        """
        from modules.converters.pdf_suite.models import PdfParams

        if isinstance(params, dict):
            safe_params = PdfParams(**params)
        elif isinstance(params, PdfParams):
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
        """Render the PDF Suite panel into *container*.

        Args:
            container: NiceGUI parent element.
        """
        # A fresh controller per client — see BaseModule.attach_ui and RFC 0003.
        self.attach_ui(
            lambda: _ui_mod.PdfSuiteUI(module_id=self.module_id)
        ).render(container)


def create() -> PdfSuiteModule:
    """Factory function invoked by the Registry.

    Returns:
        A new PdfSuiteModule instance.
    """
    return PdfSuiteModule()
