"""Pydantic v2 models for the file_filter module."""
from __future__ import annotations

import enum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from modules.extractors.file_filter.constants import DEFAULT_EXCLUDE_PATTERNS


class OutputMode(enum.StrEnum):
    """What the filter does with the files it selects."""

    COPY = "copy"
    MOVE = "move"
    ZIP = "zip"
    MANIFEST = "manifest"


class ExtensionCount(BaseModel):
    """How many files of one extension a scan found.

    Attributes:
        extension: Dot-prefixed suffix, or the no-extension label.
        count: Number of matching files.
        total_bytes: Combined size of those files.
    """

    extension: str
    count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)


class ScanParams(BaseModel):
    """Input parameters for a directory scan.

    Attributes:
        source_dir: Root directory to scan.
        exclude_patterns: Gitignore-style patterns to skip.
    """

    source_dir: Path
    exclude_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDE_PATTERNS)
    )

    @field_validator("source_dir")
    @classmethod
    def source_must_exist(cls, value: Path) -> Path:
        """Validate that the source directory exists.

        Args:
            value: Path to validate.

        Returns:
            The validated path.

        Raises:
            ValueError: When the directory is absent.
        """
        if not value.is_dir():
            raise ValueError(f"Not a directory: {value}")
        return value


class ScanResult(BaseModel):
    """What a scan found, grouped by extension.

    Attributes:
        source_dir: The directory scanned.
        extensions: Counts per extension, largest first.
        total_files: Number of files matched overall.
        total_bytes: Combined size of all matched files.
    """

    source_dir: Path
    extensions: list[ExtensionCount] = Field(default_factory=list)
    total_files: int = Field(default=0, ge=0)
    total_bytes: int = Field(default=0, ge=0)


class FilterParams(BaseModel):
    """Input parameters for a filter run.

    Attributes:
        source_dir: Root directory to pull files from.
        extensions: Extensions to include; empty means every file.
        exclude_patterns: Gitignore-style patterns to skip.
        output_mode: What to do with the selected files.
        output_dir: Directory results are written to.
        preserve_hierarchy: Keep each file's path relative to the source root,
            rather than flattening everything into one folder.
    """

    source_dir: Path
    extensions: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDE_PATTERNS)
    )
    output_mode: OutputMode = OutputMode.COPY
    output_dir: Path
    preserve_hierarchy: bool = True

    @field_validator("source_dir")
    @classmethod
    def source_must_exist(cls, value: Path) -> Path:
        """Validate that the source directory exists.

        Args:
            value: Path to validate.

        Returns:
            The validated path.

        Raises:
            ValueError: When the directory is absent.
        """
        if not value.is_dir():
            raise ValueError(f"Not a directory: {value}")
        return value

    @model_validator(mode="after")
    def check_output_location(self) -> FilterParams:
        """Reject an output directory nested inside the source.

        Writing results back into the tree being scanned would make the
        operation feed on its own output.

        Returns:
            The validated model.

        Raises:
            ValueError: When the output directory sits inside the source.
        """
        try:
            self.output_dir.resolve().relative_to(self.source_dir.resolve())
        except (ValueError, OSError):
            return self
        raise ValueError(
            "The output folder is inside the folder being filtered. "
            "Choose a destination outside it."
        )

    @property
    def is_destructive(self) -> bool:
        """True when the run removes files from their original location."""
        return self.output_mode is OutputMode.MOVE


class MovedPair(BaseModel):
    """One file relocated by a Move run, kept so the move can be reversed.

    A move is not destructive — nothing leaves the disk, the file just changes
    address — so it is undone by moving it back, exactly as ``bulk_renamer``
    reverses a rename. That is why Move does not route through the recycle
    store: doing so meant copying every file and *then* recycling the original,
    which needed twice the space and freed none of it.

    Attributes:
        source_path: Where the file was before the run.
        destination_path: Where the run put it.
    """

    source_path: Path
    destination_path: Path


class FilterResult(BaseModel):
    """Outcome of a completed filter run.

    Attributes:
        output_mode: The mode that ran.
        output_paths: Files or directories produced.
        files_matched: Number of files selected by the filter.
        files_written: Number successfully copied, moved or recorded.
        total_bytes: Combined size of the selected files.
        moved_pairs: Every ``(source, destination)`` a Move run relocated, so
            the run can be reversed. Empty for the non-destructive modes.
        detail: Short human-readable summary.
        warnings: Non-fatal notes, such as files that could not be read.
    """

    output_mode: OutputMode
    output_paths: list[Path] = Field(default_factory=list)
    files_matched: int = Field(default=0, ge=0)
    files_written: int = Field(default=0, ge=0)
    total_bytes: int = Field(default=0, ge=0)
    moved_pairs: list[MovedPair] = Field(default_factory=list)
    detail: str = ""
    warnings: list[str] = Field(default_factory=list)


class UndoParams(BaseModel):
    """Request to reverse a completed Move run.

    Attributes:
        pairs: The ``(source, destination)`` pairs to move back.
    """

    pairs: list[MovedPair] = Field(min_length=1)
