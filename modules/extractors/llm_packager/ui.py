"""LLM Packager — NiceGUI presentation layer.

Responsibilities:
- Render the directory picker, extension filter, exclude patterns, model selector.
- Publish PackageParams to EventBus when the user clicks Pack & Export.
- Subscribe to progress/done/error events and update the UI.
- Persist last-used values in TinyDB (rule E-03).

Zero imports from logic.py (rule A-03).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nicegui import ui

from core.event_bus import event_bus
from core.logger import get_logger
from core.storage import store_get, store_set
from modules.extractors.llm_packager.constants import (
    DEFAULT_EXCLUDE_PATTERNS,
    DEFAULT_EXTENSIONS,
    DEFAULT_MAX_TOKENS_PER_CHUNK,
    DEFAULT_TOKEN_MODEL,
    EVENT_CANCEL,
    EVENT_CANCELLED,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_EXECUTE,
    EVENT_PROGRESS,
    STORAGE_KEY_LAST_DIR,
    STORAGE_KEY_LAST_EXCLUDES,
    STORAGE_KEY_LAST_EXTS,
    STORAGE_KEY_LAST_MODEL,
    STORAGE_TABLE,
    TOKEN_MODELS,
)
from modules.extractors.llm_packager.models import PackageParams
from shared.constants import (
    COLOR_BORDER,
    COLOR_CARD_BG,
    COLOR_OVERLAY,
    COLOR_POSITIVE,
    COLOR_PRIMARY,
    COLOR_TEXT_DIM,
    COLOR_TEXT_MUTED,
    COLOR_WARNING,
)
from shared.ui_components import (
    ErrorPanel,
    ProgressPanel,
    choose_directory,
    module_header,
    output_file_actions,
)

logger = get_logger(__name__)

_HELP_TEXT = (
    "Scans your source directory, filters by extension, counts tokens, and "
    "writes a structured .txt file ready to paste into any LLM. Nothing is "
    "uploaded and no vocabulary is downloaded — an uncached token count is "
    "estimated instead."
)


class LLMPackagerUI:
    """Renders the LLM Packager panel and handles EventBus updates.

    Args:
        module_id: The module's dot-separated ID (used for logging).
    """

    def __init__(self, module_id: str) -> None:
        self._module_id = module_id
        self._is_running: bool = False

        # Form state — loaded from TinyDB on render
        self._source_dir: str = str(
            store_get(STORAGE_TABLE, STORAGE_KEY_LAST_DIR, default="")
        )
        self._extensions_str: str = ", ".join(
            store_get(STORAGE_TABLE, STORAGE_KEY_LAST_EXTS, default=list(DEFAULT_EXTENSIONS))
        )
        self._excludes_str: str = "\n".join(
            store_get(STORAGE_TABLE, STORAGE_KEY_LAST_EXCLUDES, default=list(DEFAULT_EXCLUDE_PATTERNS))
        )
        self._token_model: str = str(
            store_get(STORAGE_TABLE, STORAGE_KEY_LAST_MODEL, default=DEFAULT_TOKEN_MODEL)
        )
        self._include_toc: bool = True
        self._max_tokens_per_chunk: int = DEFAULT_MAX_TOKENS_PER_CHUNK

        # UI element refs
        self._dir_input: ui.input | None = None
        self._ext_input: ui.input | None = None
        self._exclude_textarea: ui.textarea | None = None
        self._model_select: ui.select | None = None
        self._progress = ProgressPanel()
        self._errors = ErrorPanel()
        self._result_card: ui.card | None = None
        self._pack_btn: ui.button | None = None

    def subscribe(self) -> None:
        """Register EventBus handlers.  Call once from ``on_load()``."""
        event_bus.subscribe(EVENT_PROGRESS, self._on_progress)
        event_bus.subscribe(EVENT_DONE, self._on_done)
        event_bus.subscribe(EVENT_ERROR, self._on_error)
        event_bus.subscribe(EVENT_CANCELLED, self._on_cancelled)

    def unsubscribe(self) -> None:
        """Deregister EventBus handlers.  Call from ``on_unload()``."""
        event_bus.unsubscribe(EVENT_PROGRESS, self._on_progress)
        event_bus.unsubscribe(EVENT_DONE, self._on_done)
        event_bus.unsubscribe(EVENT_ERROR, self._on_error)
        event_bus.unsubscribe(EVENT_CANCELLED, self._on_cancelled)

    # ─── Render ───────────────────────────────────────────────────────────────

    def render(self, container: Any) -> None:
        """Build the full LLM Packager UI inside *container*.

        Args:
            container: NiceGUI parent element.
        """
        with ui.column().style("gap: 20px; width: 100%; max-width: 800px;"):
            self._render_header()
            self._render_form()
            self._render_progress_section()
            self._render_result_section()

    def _render_header(self) -> None:
        """Render module title, description, and help tooltip (rule E-02)."""
        module_header(
            "LLM Context Packager",
            "Pack a codebase into a single context file for LLM prompts.",
            _HELP_TEXT,
        )

    def _render_form(self) -> None:
        """Render the configuration form card."""
        with ui.card().style(
            f"background: {COLOR_CARD_BG}; border: 1px solid {COLOR_BORDER};"
            " border-radius: 10px; padding: 22px; width: 100%;"
        ).props("flat"):
            ui.label("Configuration").style(
                f"color: {COLOR_TEXT_MUTED}; font-size: 11px; font-weight: 700;"
                " text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 12px;"
            )

            # Source directory
            with ui.row().style("align-items: center; gap: 8px; width: 100%;"):
                self._dir_input = ui.input(
                    label="Source Directory",
                    value=self._source_dir,
                    placeholder="/path/to/your/project",
                    on_change=lambda e: setattr(self, "_source_dir", e.value),
                ).style("flex: 1;").props("outlined dense dark")

                ui.button(
                    "Browse",
                    on_click=self._on_browse_click,
                ).props("no-caps flat dense").style(
                    f"color: {COLOR_TEXT_MUTED}; border: 1px solid {COLOR_BORDER};"
                    " border-radius: 6px;"
                )

            # Extensions
            self._ext_input = ui.input(
                label="File Extensions (comma-separated)",
                value=self._extensions_str,
                placeholder=".py, .ts, .js, .md",
                on_change=lambda e: setattr(self, "_extensions_str", e.value),
            ).style("width: 100%; margin-top: 12px;").props("outlined dense dark")

            # Exclude patterns
            with ui.column().style("gap: 4px; width: 100%; margin-top: 12px;"):
                ui.label("Exclude Patterns (one per line, gitignore style)").style(
                    f"color: {COLOR_TEXT_MUTED}; font-size: 12px;"
                )
                self._exclude_textarea = ui.textarea(
                    value=self._excludes_str,
                    on_change=lambda e: setattr(self, "_excludes_str", e.value),
                ).style(
                    "width: 100%; font-family: monospace; font-size: 12px; height: 100px;"
                ).props("outlined dense dark")

            # Token model
            model_options = dict(TOKEN_MODELS.items())
            self._model_select = ui.select(
                options=model_options,
                value=self._token_model,
                label="Token Counting Model",
                on_change=lambda e: setattr(self, "_token_model", e.value),
            ).style("width: 100%; margin-top: 12px;").props("outlined dense dark")

            # Output shaping
            with ui.row().style(
                "align-items: center; gap: 16px; width: 100%; margin-top: 12px;"
            ):
                ui.checkbox(
                    "Include table of contents",
                    value=self._include_toc,
                    on_change=lambda e: setattr(self, "_include_toc", bool(e.value)),
                ).props("dense")
                ui.number(
                    label="Split every N tokens (0 = one file)",
                    value=self._max_tokens_per_chunk,
                    min=0,
                    precision=0,
                    on_change=lambda e: setattr(
                        self, "_max_tokens_per_chunk", int(e.value or 0)
                    ),
                ).props("outlined dense").style("flex: 1;")

            # Pack button
            with ui.row().style("justify-content: flex-end; margin-top: 16px;"):
                self._pack_btn = ui.button(
                    "⚡  Pack & Export",
                    on_click=self._on_pack_click,
                ).props("no-caps").style(
                    f"background: {COLOR_PRIMARY}; color: white; font-weight: 600;"
                    " padding: 8px 24px; border-radius: 6px;"
                )

    def _render_progress_section(self) -> None:
        """Render the shared progress panel (rules E-01, B-02 cancellation)."""
        self._progress.render(on_cancel=self._on_cancel_click)
        self._errors.render()

    def _render_result_section(self) -> None:
        """Render the result card (hidden until first successful run)."""
        with ui.card().style(
            f"background: {COLOR_CARD_BG}; border: 1px solid {COLOR_BORDER};"
            " border-radius: 10px; padding: 18px 22px; width: 100%;"
        ).props("flat") as self._result_card:
            ui.label("Result will appear here after packaging.").style(
                f"color: {COLOR_TEXT_DIM}; font-size: 13px;"
            )

    # ─── EventBus handlers ────────────────────────────────────────────────────

    async def _on_cancel_click(self) -> None:
        """Ask the logic layer to stop the packaging run."""
        await event_bus.publish(EVENT_CANCEL, None)

    async def _on_cancelled(self, _payload: Any) -> None:
        """Return the panel to an idle state once a cancel takes effect."""
        self._is_running = False
        self._progress.reset("Cancelled.")
        if self._pack_btn:
            self._pack_btn.set_text("⚡  Pack & Export")

    async def _on_progress(self, payload: Any) -> None:
        """Update progress bar and status label on each ProgressEvent.

        Args:
            payload: ProgressEvent instance.
        """
        from core.models import ProgressEvent

        if not isinstance(payload, ProgressEvent):
            return
        self._progress.update(payload.percent, payload.message)

    async def _on_done(self, payload: Any) -> None:
        """Render the result card when packaging completes.

        Args:
            payload: PackageResult instance.
        """
        from modules.extractors.llm_packager.models import PackageResult

        if not isinstance(payload, PackageResult):
            return

        if self._pack_btn:
            self._pack_btn.set_text("⚡  Pack & Export")
            self._pack_btn.props(add="no-caps")
        self._is_running = False

        if self._result_card is not None:
            self._result_card.clear()
            with self._result_card:
                self._render_result_content(payload)

    async def _on_error(self, payload: Any) -> None:
        """Display error details when packaging fails.

        Args:
            payload: Error string.
        """
        error_msg = str(payload)
        self._progress.reset("Failed.")
        self._errors.show("Packaging could not be completed.", error_msg)
        if self._pack_btn:
            self._pack_btn.set_text("⚡  Pack & Export")
        self._is_running = False
        logger.error("llm_packager.ui.error — %s", error_msg)

    # ─── Controls ─────────────────────────────────────────────────────────────

    async def _on_browse_click(self) -> None:
        """Open the directory picker, starting at the last-used folder (E-03)."""
        start = Path(self._source_dir) if self._source_dir.strip() else None
        chosen = await choose_directory(start)
        if chosen is None:
            return
        self._source_dir = str(chosen)
        if self._dir_input:
            self._dir_input.set_value(self._source_dir)

    async def _on_pack_click(self) -> None:
        """Validate inputs and publish ExecuteRequest to EventBus."""
        if self._is_running:
            return

        source = self._source_dir.strip()
        if not source:
            ui.notify("Source directory is required.", type="warning")
            return

        source_path = Path(source)
        if not source_path.is_dir():
            ui.notify(f"Directory not found: {source}", type="negative")
            return

        extensions = [
            e.strip() for e in self._extensions_str.split(",") if e.strip()
        ]
        if not extensions:
            ui.notify("At least one file extension is required.", type="warning")
            return

        excludes = [
            line.strip() for line in self._excludes_str.splitlines() if line.strip()
        ]

        # Persist last-used values (rule E-03)
        store_set(STORAGE_TABLE, STORAGE_KEY_LAST_DIR, source)
        store_set(STORAGE_TABLE, STORAGE_KEY_LAST_EXTS, extensions)
        store_set(STORAGE_TABLE, STORAGE_KEY_LAST_EXCLUDES, excludes)
        store_set(STORAGE_TABLE, STORAGE_KEY_LAST_MODEL, self._token_model)

        try:
            params = PackageParams(
                source_dir=source_path,
                extensions=extensions,
                exclude_patterns=excludes,
                token_model=self._token_model,
                include_toc=self._include_toc,
                max_tokens_per_chunk=self._max_tokens_per_chunk,
            )
        except ValueError as exc:
            ui.notify(str(exc), type="negative")
            return

        self._is_running = True
        if self._pack_btn:
            self._pack_btn.set_text("⏳  Packing…")
        self._errors.hide()
        self._progress.update(0, "Starting…")

        await event_bus.publish(EVENT_EXECUTE, params)

    # ─── Result rendering ─────────────────────────────────────────────────────

    def _render_result_content(self, result: Any) -> None:
        """Populate the result card after a successful run.

        Args:
            result: PackageResult instance.
        """
        from modules.extractors.llm_packager.models import PackageResult

        if not isinstance(result, PackageResult):
            return

        ui.label("Packaging Complete").style(
            f"color: {COLOR_POSITIVE}; font-size: 16px; font-weight: 700; margin-bottom: 12px;"
        )

        approximate = result.token_count_is_approximate
        with ui.grid(columns=3).style("gap: 16px; width: 100%;"):
            self._stat_chip("📄 Files", str(result.file_count))
            self._stat_chip(
                "🔢 Tokens (estimated)" if approximate else "🔢 Tokens",
                f"{'≈' if approximate else ''}{result.token_count:,}",
            )
            self._stat_chip(
                "⏭ Skipped",
                str(result.skipped_count),
                color=COLOR_WARNING if result.skipped_count > 0 else COLOR_TEXT_MUTED,
            )

        if approximate:
            ui.label(
                "Estimated from character count — no tiktoken vocabulary is cached"
                " locally, and OmniForge will not download one."
            ).style(f"color: {COLOR_WARNING}; font-size: 11px; margin-top: 8px;")

        for note in result.warnings:
            ui.label(f"⚠ {note}").style(
                f"color: {COLOR_WARNING}; font-size: 11px; margin-top: 6px;"
            )

        for path in result.output_paths or [result.output_path]:
            output_file_actions(path)

    def _stat_chip(self, label: str, value: str, color: str = COLOR_PRIMARY) -> None:
        """Render a small statistic card.

        Args:
            label: Descriptive label text.
            value: The statistic value to display.
            color: CSS colour string for the value.
        """
        with ui.card().style(
            f"background: {COLOR_OVERLAY}; border: 1px solid {COLOR_BORDER};"
            " border-radius: 8px; padding: 12px 16px; text-align: center;"
        ).props("flat"):
            ui.label(value).style(f"color: {color}; font-size: 22px; font-weight: 700;")
            ui.label(label).style(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
