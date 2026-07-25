"""Image Suite module package.

Exposes the ``create()`` factory called by the Registry. Logic and UI are
wired together exclusively through the EventBus (rule A-03).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from core.base_module import BaseModule
from core.logger import get_logger
from core.models import ProgressEvent
from modules.converters.image_suite import logic as _logic_mod
from modules.converters.image_suite import ui as _ui_mod

logger = get_logger(__name__)


class ImageSuiteModule(BaseModule):
    """BaseModule implementation for the Image Suite."""

    def __init__(self) -> None:
        self._logic = _logic_mod.ImageSuiteLogic()

    # ─── Identity ─────────────────────────────────────────────────────────────

    @property
    def module_id(self) -> str:
        """Return ``"converters.image_suite"``."""
        return "converters.image_suite"

    @property
    def name(self) -> str:
        """Return ``"Image Suite"``."""
        return "Image Suite"

    @property
    def pillar(self) -> str:
        """Return ``"converters"``."""
        return "converters"

    @property
    def icon(self) -> str:
        """Return the path to the module's SVG icon."""
        return "assets/icons/image_suite.svg"

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def on_load(self) -> None:
        """Verify Pillow is present and report optional capabilities.

        Only Pillow is mandatory. HEIC support is optional: the module loads
        either way and the UI marks it unavailable, rather than failing at the
        moment of use (rule B-06).

        Raises:
            RuntimeError: When Pillow itself is missing.
        """
        try:
            import PIL
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise RuntimeError(
                "Pillow is required by the Image Suite. Install it with:"
                "  pip install Pillow"
            ) from exc

        heif = _logic_mod.register_heif()

        await self._logic.register()
        logger.info(
            "image_suite.on_load — OK (Pillow %s, HEIC=%s)",
            PIL.__version__,
            heif,
        )

    async def on_unload(self) -> None:
        """Deregister EventBus handlers."""
        self.detach_ui()
        await self._logic.unregister()
        logger.info("image_suite.on_unload — OK")

    # ─── Execution ────────────────────────────────────────────────────────────

    async def execute(self, params: Any) -> AsyncIterator[ProgressEvent]:
        """Delegate to the logic layer.

        Args:
            params: ImageParams, or a dict coerced into one.

        Yields:
            ProgressEvents from the logic layer.
        """
        from modules.converters.image_suite.models import ImageParams

        if isinstance(params, dict):
            safe_params = ImageParams(**params)
        elif isinstance(params, ImageParams):
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
        """Render the Image Suite panel into *container*.

        Args:
            container: NiceGUI parent element.
        """
        # A fresh controller per client — see BaseModule.attach_ui and RFC 0003.
        self.attach_ui(
            lambda: _ui_mod.ImageSuiteUI(module_id=self.module_id)
        ).render(container)


def create() -> ImageSuiteModule:
    """Factory function invoked by the Registry.

    Returns:
        A new ImageSuiteModule instance.
    """
    return ImageSuiteModule()
