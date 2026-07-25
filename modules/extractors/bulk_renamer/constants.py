"""Constants for the bulk_renamer module (rule D-05 — no magic numbers)."""
from __future__ import annotations

# ─── EventBus topics ──────────────────────────────────────────────────────────
EVENT_PREVIEW: str = "extractors.bulk_renamer.preview"
EVENT_PREVIEWED: str = "extractors.bulk_renamer.previewed"
EVENT_EXECUTE: str = "extractors.bulk_renamer.execute"
EVENT_PROGRESS: str = "extractors.bulk_renamer.progress"
EVENT_DONE: str = "extractors.bulk_renamer.done"
EVENT_CANCEL: str = "extractors.bulk_renamer.cancel"
EVENT_CANCELLED: str = "extractors.bulk_renamer.cancelled"
EVENT_ERROR: str = "extractors.bulk_renamer.error"

# ─── Storage ──────────────────────────────────────────────────────────────────
STORAGE_TABLE: str = "extractors.bulk_renamer"
STORAGE_KEY_LAST_DIR: str = "last_source_dir"
STORAGE_KEY_LAST_EXCLUDES: str = "last_excludes"

# ─── Scanning ─────────────────────────────────────────────────────────────────
# Only relevant when recursive is enabled — same rebuildable trees file_filter
# and duplicate_finder skip by default.
DEFAULT_EXCLUDE_PATTERNS: tuple[str, ...] = (
    "**/.git/**",
    "**/node_modules/**",
    "**/__pycache__/**",
    "**/.venv/**",
    "**/venv/**",
    "**/dist/**",
    "**/build/**",
    "**/.mypy_cache/**",
    "**/.ruff_cache/**",
    "**/.pytest_cache/**",
)

# ─── Replacement templates ────────────────────────────────────────────────────
# {n} / {n:03d} is the per-match counter, starting here; {date} is today's date.
COUNTER_START: int = 1
DATE_PLACEHOLDER_FORMAT: str = "%Y-%m-%d"

# ─── Progress checkpoints (rename/undo only — preview is request/response,
# matching the scan convention used by file_filter and duplicate_finder) ──────
PROGRESS_START: int = 0
PROGRESS_COMPLETE: int = 100

# ─── UI ───────────────────────────────────────────────────────────────────────
PREVIEW_TABLE_ROW_HEIGHT_PX: int = 32
PREVIEW_TABLE_HEADER_HEIGHT_PX: int = 32
PREVIEW_TABLE_HEIGHT_PX: int = 360
MAX_PREVIEW_ROWS: int = 2000
