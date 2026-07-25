"""Constants for the file_filter module (rule D-05 — no magic numbers)."""
from __future__ import annotations

# ─── EventBus topics ──────────────────────────────────────────────────────────
EVENT_SCAN: str = "extractors.file_filter.scan"
EVENT_SCANNED: str = "extractors.file_filter.scanned"
EVENT_EXECUTE: str = "extractors.file_filter.execute"
EVENT_UNDO: str = "extractors.file_filter.undo"
EVENT_PROGRESS: str = "extractors.file_filter.progress"
EVENT_DONE: str = "extractors.file_filter.done"
EVENT_CANCEL: str = "extractors.file_filter.cancel"
EVENT_CANCELLED: str = "extractors.file_filter.cancelled"
EVENT_ERROR: str = "extractors.file_filter.error"

# ─── Storage ──────────────────────────────────────────────────────────────────
STORAGE_TABLE: str = "extractors.file_filter"
STORAGE_KEY_LAST_DIR: str = "last_source_dir"
STORAGE_KEY_LAST_EXCLUDES: str = "last_excludes"

# ─── Output ───────────────────────────────────────────────────────────────────
OUTPUT_SUBDIR: str = "file_filter"
ARCHIVE_NAME_TEMPLATE: str = "{stem}_filtered_{timestamp}.zip"
MANIFEST_NAME_TEMPLATE: str = "{stem}_manifest_{timestamp}.txt"
COPY_DIR_TEMPLATE: str = "{stem}_filtered_{timestamp}"
TIMESTAMP_FORMAT: str = "%Y%m%d_%H%M%S"

MANIFEST_HEADER: str = "# OmniForge — file filter manifest"
MANIFEST_ROW_TEMPLATE: str = "{path}\t{size}\t{modified}"
MANIFEST_COLUMNS: str = "# relative_path\tbytes\tmodified"

# ─── Progress checkpoints ─────────────────────────────────────────────────────
PROGRESS_START: int = 0
PROGRESS_SCAN_DONE: int = 15
PROGRESS_WORK_END: int = 95
PROGRESS_COMPLETE: int = 100

# ─── Scanning ─────────────────────────────────────────────────────────────────
# Patterns excluded unless the user replaces them. These directories are
# rebuildable and would otherwise dominate every scan.
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

# Files with no suffix are grouped under this label in the extension list.
NO_EXTENSION_LABEL: str = "(no extension)"

# Extensions shown in the picker, most common first. A scan of a large tree
# can turn up hundreds of one-off suffixes; the rest stay searchable.
# Extension rows rendered per page. Bounded so a scan over a sprawling
# workspace cannot put hundreds of rows into the DOM (rule E-05), while
# pagination keeps every type reachable.
EXTENSIONS_PER_PAGE: int = 40

# ─── UI ───────────────────────────────────────────────────────────────────────
EXTENSION_LIST_HEIGHT_PX: int = 260
PREVIEW_ROW_LIMIT: int = 500

# How often a scan reports its running count. Frequent enough that the UI shows
# real movement, sparse enough not to flood the event bus on a large tree.
SCAN_REPORT_EVERY: int = 200

# Remembers where the user last sent this module's output (rule E-03).
STORAGE_KEY_OUTPUT_DIR: str = "last_output_dir"
