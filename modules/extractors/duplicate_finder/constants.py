"""Constants for the duplicate_finder module (rule D-05 — no magic numbers)."""
from __future__ import annotations

# ─── EventBus topics ──────────────────────────────────────────────────────────
EVENT_SCAN: str = "extractors.duplicate_finder.scan"
EVENT_SCANNED: str = "extractors.duplicate_finder.scanned"
EVENT_EXECUTE: str = "extractors.duplicate_finder.execute"
EVENT_PROGRESS: str = "extractors.duplicate_finder.progress"
EVENT_DONE: str = "extractors.duplicate_finder.done"
EVENT_CANCEL: str = "extractors.duplicate_finder.cancel"
EVENT_CANCELLED: str = "extractors.duplicate_finder.cancelled"
EVENT_ERROR: str = "extractors.duplicate_finder.error"

# ─── Storage ──────────────────────────────────────────────────────────────────
STORAGE_TABLE: str = "extractors.duplicate_finder"
STORAGE_KEY_LAST_DIR: str = "last_source_dir"
STORAGE_KEY_LAST_EXCLUDES: str = "last_excludes"

# ─── Scanning ─────────────────────────────────────────────────────────────────
# Patterns excluded unless the user replaces them — same rebuildable trees
# file_filter skips by default.
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

# Zero-byte files are trivially "identical" to every other empty file and
# reclaim nothing when deleted, so they are excluded by default.
DEFAULT_MIN_SIZE_BYTES: int = 1

# Below this many same-size candidates, sequential hashing in the calling
# thread beats paying inter-process communication overhead for a pool.
MIN_FILES_FOR_MULTIPROCESSING: int = 64

# Files handed to each worker per round-trip when the pool is used.
HASH_POOL_CHUNKSIZE: int = 8

# ─── Progress checkpoints (resolve/delete only — scan is request/response,
# matching the file_filter and llm_packager scan convention) ──────────────────
PROGRESS_START: int = 0
PROGRESS_KEEPERS_CHOSEN: int = 40
PROGRESS_COMPLETE: int = 100

# ─── UI ───────────────────────────────────────────────────────────────────────
GROUP_LIST_HEIGHT_PX: int = 420
# Groups rendered per page. A scan can turn up thousands of duplicate groups;
# paging keeps the DOM bounded (responsive) while every group stays reachable
# and actionable — a hard cap used to hide the remainder outright.
GROUPS_PER_PAGE: int = 50

# How often a scan reports its running count. Frequent enough that the UI shows
# real movement, sparse enough not to flood the event bus on a large tree.
SCAN_REPORT_EVERY: int = 200
