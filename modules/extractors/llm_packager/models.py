"""Pydantic v2 models for the llm_packager module."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from modules.extractors.llm_packager.constants import (
    DEFAULT_EXCLUDE_PATTERNS,
    DEFAULT_EXTENSIONS,
    DEFAULT_MAX_TOKENS_PER_CHUNK,
    DEFAULT_TOKEN_MODEL,
)
from shared.constants import EXPORTS_DIR


class PackageParams(BaseModel):
    """Input parameters for an LLM Packager execution.

    Attributes:
        source_dir: Root directory to scan.
        extensions: File extensions to include (dot-prefixed, e.g. ``".py"``).
        exclude_patterns: Glob patterns to exclude.
        token_model: Tiktoken encoding name for token counting.
        output_dir: Directory where the packed file is written.
    """

    source_dir: Path
    extensions: list[str] = Field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    exclude_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDE_PATTERNS)
    )
    token_model: str = DEFAULT_TOKEN_MODEL
    output_dir: Path = Field(default_factory=lambda: Path(EXPORTS_DIR))
    include_toc: bool = True
    max_tokens_per_chunk: int = Field(default=DEFAULT_MAX_TOKENS_PER_CHUNK, ge=0)

    @field_validator("source_dir")
    @classmethod
    def source_must_exist(cls, v: Path) -> Path:
        """Validate that the source directory exists.

        Args:
            v: Path value to validate.

        Returns:
            The validated Path.

        Raises:
            ValueError: When the directory does not exist.
        """
        if not v.is_dir():
            raise ValueError(f"source_dir does not exist or is not a directory: {v}")
        return v

    @field_validator("extensions", mode="before")
    @classmethod
    def normalise_extensions(cls, v: object) -> list[str]:
        """Ensure each extension is lowercase and dot-prefixed.

        Args:
            v: Raw extensions input.

        Returns:
            Normalised list of extension strings.

        Raises:
            ValueError: When *v* is not a list or tuple. Pydantic only folds
                ValueError into ValidationError, so raising TypeError here
                would escape the UI's validation handler.
        """
        if not isinstance(v, (list, tuple)):
            raise ValueError("extensions must be a list")
        result: list[str] = []
        for ext in v:
            ext = str(ext).strip().lower()
            if ext and not ext.startswith("."):
                ext = f".{ext}"
            if ext:
                result.append(ext)
        return result


class PackageResult(BaseModel):
    """Output produced by a successful LLM Packager execution.

    Attributes:
        output_path: Path to the generated context file.
        file_count: Number of files included.
        total_chars: Total character count across all files.
        token_count: Token count for the selected model.
        token_model: The tiktoken encoding used.
        skipped_count: Number of files skipped (too large or unreadable).
        token_count_is_approximate: True when no local tiktoken vocabulary
            was available and the count was estimated from character length
            rather than downloading one (rule C-01).
    """

    output_path: Path
    file_count: int = Field(ge=0)
    total_chars: int = Field(ge=0)
    token_count: int = Field(ge=0)
    token_model: str
    skipped_count: int = Field(ge=0)
    token_count_is_approximate: bool = False
    output_paths: list[Path] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def chunk_count(self) -> int:
        """How many files the context was written across."""
        return len(self.output_paths) or 1


class PackagedFile(BaseModel):
    """One source file included in a packaged context.

    Attributes:
        relative_path: Path as shown in the output, relative to the scan root.
        content: The file's text.
        lines: Number of lines.
        tokens: Token count for this file alone.
    """

    relative_path: str
    content: str
    lines: int = Field(ge=0)
    tokens: int = Field(default=0, ge=0)
