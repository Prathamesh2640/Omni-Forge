"""Constants for the llm_packager module (rule D-05 — no magic numbers)."""
from __future__ import annotations

# ─── Defaults ─────────────────────────────────────────────────────────────────

# Default file extensions to include.
DEFAULT_EXTENSIONS: tuple[str, ...] = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs",
    ".java", ".kt", ".md", ".txt", ".yaml", ".yml", ".toml", ".json",
)

# Default glob patterns to exclude (passed to pathspec).
DEFAULT_EXCLUDE_PATTERNS: tuple[str, ...] = (
    "**/__pycache__/**",
    "**/.git/**",
    "**/node_modules/**",
    "**/.venv/**",
    "**/venv/**",
    "**/dist/**",
    "**/build/**",
    "**/.mypy_cache/**",
    "**/.ruff_cache/**",
    "**/*.pyc",
    "**/*.pyo",
    "**/*.egg-info/**",
)

# Tiktoken model names mapped to display names.
TOKEN_MODELS: dict[str, str] = {
    "o200k_base": "GPT-4o / o1",
    "cl100k_base": "GPT-4 / Claude 3",
    "p50k_base": "GPT-3.5 / Codex",
}

DEFAULT_TOKEN_MODEL: str = "o200k_base"

# tiktoken downloads its BPE vocabulary on first use, which would be an
# external network call (rule C-01) and a multi-second silent stall. The
# module instead checks for an already-present cache and, when it is absent,
# estimates instead of reaching out. These mirror tiktoken's own resolution
# order for locating that cache.
TIKTOKEN_CACHE_ENV_VARS: tuple[str, ...] = ("TIKTOKEN_CACHE_DIR", "DATA_GYM_CACHE_DIR")
TIKTOKEN_CACHE_DIRNAME: str = "data-gym-cache"

# Fallback ratio when no vocabulary is available. Four characters per token is
# the widely used rule of thumb for English source text.
APPROX_CHARS_PER_TOKEN: int = 4

# File separator inserted between each file in the output.
FILE_SEPARATOR_TEMPLATE: str = "\n\n{'='*80}\n# FILE: {path}\n{'='*80}\n\n"

# Maximum file size read per file in bytes (files larger than this are skipped).
MAX_FILE_SIZE_BYTES: int = 5 * 1024 * 1024  # 5 MiB

# Progress event checkpoints.
PROGRESS_SCAN_START: int = 0
PROGRESS_SCAN_DONE: int = 15
PROGRESS_READ_DONE: int = 75
PROGRESS_TOKEN_DONE: int = 90
PROGRESS_WRITE_DONE: int = 100

# Output file name template.
OUTPUT_FILE_TEMPLATE: str = "llm_context_{timestamp}.txt"

# Name used when the output is split across several files.
CHUNK_FILE_TEMPLATE: str = "llm_context_{timestamp}_part{index:02d}of{total:02d}.txt"

# ─── Table of contents ────────────────────────────────────────────────────────
TOC_HEADER: str = "TABLE OF CONTENTS"
TOC_ROW_TEMPLATE: str = "{index:>4}. {path}  ({lines} lines, {tokens} tokens)"
TOC_SEPARATOR_WIDTH: int = 80

# ─── Chunking ─────────────────────────────────────────────────────────────────
# 0 disables chunking; any positive value caps each output file's token count.
DEFAULT_MAX_TOKENS_PER_CHUNK: int = 0

# A single file larger than the chunk limit cannot be split without corrupting
# it, so it is emitted alone and the overflow is reported.
CHUNK_OVERSIZE_NOTE: str = (
    "{path} alone is {tokens:,} tokens, above the {limit:,} limit — "
    "it was written to its own file rather than being cut in half."
)

# EventBus event type strings.
EVENT_EXECUTE: str = "extractors.llm_packager.execute"
EVENT_PROGRESS: str = "extractors.llm_packager.progress"
EVENT_DONE: str = "extractors.llm_packager.done"
EVENT_CANCEL: str = "extractors.llm_packager.cancel"
EVENT_CANCELLED: str = "extractors.llm_packager.cancelled"
EVENT_ERROR: str = "extractors.llm_packager.error"

# TinyDB storage keys.
STORAGE_TABLE: str = "extractors.llm_packager"
STORAGE_KEY_LAST_DIR: str = "last_source_dir"
STORAGE_KEY_LAST_EXTS: str = "last_extensions"
STORAGE_KEY_LAST_MODEL: str = "last_model"
STORAGE_KEY_LAST_EXCLUDES: str = "last_excludes"
