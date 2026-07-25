"""Document Suite module package.

Exposes the ``create()`` factory called by the Registry. Logic and UI are
wired together exclusively through the EventBus (rule A-03).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from core.base_module import BaseModule
from core.logger import get_logger
from core.models import ProgressEvent
from modules.converters.document_suite import logic as _logic_mod
from modules.converters.document_suite import ui as _ui_mod

logger = get_logger(__name__)

#: Import names of the libraries each conversion needs, checked at load time.
_REQUIRED_LIBRARIES: tuple[tuple[str, str], ...] = (
    ("mistune", "Markdown rendering"),
    ("pygments", "code highlighting"),
    ("markdownify", "HTML to Markdown"),
    ("openpyxl", "Excel output"),
    ("yaml", "YAML conversion"),
)


class DocumentSuiteModule(BaseModule):
    """BaseModule implementation for the Document Suite."""

    def __init__(self) -> None:
        self._logic = _logic_mod.DocumentSuiteLogic()

    # ─── Identity ─────────────────────────────────────────────────────────────

    @property
    def module_id(self) -> str:
        """Return ``"converters.document_suite"``."""
        return "converters.document_suite"

    @property
    def name(self) -> str:
        """Return ``"Document Suite"``."""
        return "Document Suite"

    @property
    def pillar(self) -> str:
        """Return ``"converters"``."""
        return "converters"

    @property
    def icon(self) -> str:
        """Return the path to the module's SVG icon."""
        return "assets/icons/document_suite.svg"

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def on_load(self) -> None:
        """Verify every conversion library is importable, then subscribe.

        Raises:
            RuntimeError: When a library is missing, so the Registry marks the
                module DEGRADED rather than letting a conversion fail midway.
        """
        import importlib.util

        missing = [
            f"{name} ({purpose})"
            for name, purpose in _REQUIRED_LIBRARIES
            if importlib.util.find_spec(name) is None
        ]
        if missing:
            raise RuntimeError(
                "Document Suite needs these packages: "
                + ", ".join(missing)
                + ". Install them with:  pip install -r requirements.txt"
            )

        await self._logic.register()
        logger.info("document_suite.on_load — OK")

    async def on_unload(self) -> None:
        """Deregister EventBus handlers."""
        self.detach_ui()
        await self._logic.unregister()
        logger.info("document_suite.on_unload — OK")

    # ─── Execution ────────────────────────────────────────────────────────────

    async def execute(self, params: Any) -> AsyncIterator[ProgressEvent]:
        """Delegate to the logic layer.

        Args:
            params: ConvertParams, or a dict coerced into one.

        Yields:
            ProgressEvents from the logic layer.
        """
        from modules.converters.document_suite.models import ConvertParams

        if isinstance(params, dict):
            safe_params = ConvertParams(**params)
        elif isinstance(params, ConvertParams):
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
        """Render the Document Suite panel into *container*.

        Args:
            container: NiceGUI parent element.
        """
        # A fresh controller per client — see BaseModule.attach_ui and RFC 0003.
        self.attach_ui(
            lambda: _ui_mod.DocumentSuiteUI(module_id=self.module_id)
        ).render(container)


def create() -> DocumentSuiteModule:
    """Factory function invoked by the Registry.

    Returns:
        A new DocumentSuiteModule instance.
    """
    return DocumentSuiteModule()
