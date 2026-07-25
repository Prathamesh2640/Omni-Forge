"""Media Suite module package.

Exposes the ``create()`` factory called by the Registry. Logic and UI are
wired together exclusively through the EventBus (rule A-03).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from core.base_module import BaseModule
from core.logger import get_logger
from core.models import ProgressEvent
from modules.converters.media_suite import logic as _logic_mod
from modules.converters.media_suite import ui as _ui_mod
from modules.converters.media_suite.constants import FFMPEG_INSTALL_HINT

logger = get_logger(__name__)


class MediaSuiteModule(BaseModule):
    """BaseModule implementation for the Media Suite."""

    def __init__(self) -> None:
        self._logic = _logic_mod.MediaSuiteLogic()

    # ─── Identity ─────────────────────────────────────────────────────────────

    @property
    def module_id(self) -> str:
        """Return ``"converters.media_suite"``."""
        return "converters.media_suite"

    @property
    def name(self) -> str:
        """Return ``"Media Suite"``."""
        return "Media Suite"

    @property
    def pillar(self) -> str:
        """Return ``"converters"``."""
        return "converters"

    @property
    def icon(self) -> str:
        """Return the path to the module's SVG icon."""
        return "assets/icons/media_suite.svg"

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def on_load(self) -> None:
        """Verify FFmpeg is present before offering any operation.

        Every operation shells out to FFmpeg, so without it the module has
        nothing to offer. Raising here makes the Registry mark it DEGRADED
        with an install guide, rather than letting each operation fail at the
        moment the user runs it (rule B-06).

        Raises:
            RuntimeError: When FFmpeg or ffprobe cannot be located.
        """
        if not _logic_mod.ffmpeg_available():
            raise RuntimeError(FFMPEG_INSTALL_HINT)

        await self._logic.register()
        logger.info(
            "media_suite.on_load — OK (ffmpeg=%s)",
            _logic_mod.find_binary("ffmpeg"),
        )

    async def on_unload(self) -> None:
        """Deregister EventBus handlers."""
        self.detach_ui()
        await self._logic.unregister()
        logger.info("media_suite.on_unload — OK")

    # ─── Execution ────────────────────────────────────────────────────────────

    async def execute(self, params: Any) -> AsyncIterator[ProgressEvent]:
        """Delegate to the logic layer.

        Args:
            params: MediaParams, or a dict coerced into one.

        Yields:
            ProgressEvents from the logic layer.
        """
        from modules.converters.media_suite.models import MediaParams

        if isinstance(params, dict):
            safe_params = MediaParams(**params)
        elif isinstance(params, MediaParams):
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
        """Render the Media Suite panel into *container*.

        Args:
            container: NiceGUI parent element.
        """
        # A fresh controller per client — see BaseModule.attach_ui and RFC 0003.
        self.attach_ui(
            lambda: _ui_mod.MediaSuiteUI(module_id=self.module_id)
        ).render(container)


def create() -> MediaSuiteModule:
    """Factory function invoked by the Registry.

    Returns:
        A new MediaSuiteModule instance.
    """
    return MediaSuiteModule()
