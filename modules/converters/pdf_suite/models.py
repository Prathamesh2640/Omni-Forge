"""Pydantic v2 models for the pdf_suite module."""
from __future__ import annotations

import enum
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, model_validator

from modules.converters.pdf_suite.constants import (
    EDITABLE_METADATA_FIELDS,
    MIN_PAGES_PER_CHUNK,
    VALID_ROTATIONS,
)


class PdfOperation(enum.StrEnum):
    """The operations pdf_suite can perform."""

    MERGE = "merge"
    SPLIT = "split"
    COMPRESS = "compress"
    REMOVE_PASSWORD = "remove_password"
    EDIT_METADATA = "edit_metadata"
    ROTATE = "rotate"
    EXTRACT_TEXT = "extract_text"
    EXTRACT_IMAGES = "extract_images"
    TO_DOCX = "to_docx"


class CompressPreset(enum.StrEnum):
    """Image resampling targets for the compressor."""

    SCREEN = "screen"
    EBOOK = "ebook"
    PRINT = "print"


class SplitMode(enum.StrEnum):
    """How a document should be divided."""

    PAGE_RANGE = "page_range"
    EVERY_N = "every_n"
    SINGLE_PAGES = "single_pages"


class PdfMetadata(BaseModel):
    """Editable PDF document metadata.

    Every field is optional; only those set are written, so an edit never
    blanks a value the user did not touch.
    """

    title: str | None = None
    author: str | None = None
    subject: str | None = None
    keywords: str | None = None
    creator: str | None = None
    producer: str | None = None

    def as_updates(self) -> dict[str, str]:
        """Return only the fields the user actually supplied.

        Returns:
            Mapping of PDF metadata key to its new value.
        """
        return {
            field: value
            for field in EDITABLE_METADATA_FIELDS
            if (value := getattr(self, field)) is not None
        }


class PdfParams(BaseModel):
    """Input parameters for a pdf_suite execution.

    Attributes:
        operation: Which operation to run.
        input_paths: Source PDFs. Only merge accepts more than one.
        output_dir: Directory the results are written to.
        split_mode: How to divide the document (split only).
        page_range: 1-based inclusive ``(first, last)`` page range.
        every_n: Pages per chunk when splitting by size.
        preset: Image resampling target (compress only).
        password: Password used to unlock an encrypted document. ``SecretStr``
            so it cannot leak: Pydantic renders the whole input dict into
            ``str(ValidationError)``, and the EventBus handler publishes that
            string to the UI and logs it with ``exc_info`` — a plain ``str``
            therefore put the document password on screen and into
            ``data/logs/`` (rules C-03, C-04).
        metadata: New metadata values (edit_metadata only).
        rotation: Clockwise degrees to rotate (rotate only).
    """

    operation: PdfOperation
    input_paths: list[Path] = Field(min_length=1)
    output_dir: Path

    split_mode: SplitMode = SplitMode.EVERY_N
    page_range: tuple[int, int] | None = None
    every_n: int = Field(default=1, ge=MIN_PAGES_PER_CHUNK)

    preset: CompressPreset = CompressPreset.EBOOK
    password: SecretStr = SecretStr("")
    metadata: PdfMetadata = Field(default_factory=PdfMetadata)
    rotation: int = 90

    @model_validator(mode="after")
    def check_operation_requirements(self) -> PdfParams:
        """Validate the fields the chosen operation depends on.

        Returns:
            The validated model.

        Raises:
            ValueError: When the parameters cannot satisfy the operation.
        """
        if self.operation is PdfOperation.MERGE and len(self.input_paths) < 2:
            raise ValueError("Merging needs at least two PDF files.")

        if self.operation is not PdfOperation.MERGE and len(self.input_paths) != 1:
            raise ValueError(f"{self.operation.value} accepts exactly one PDF file.")

        if self.operation is PdfOperation.ROTATE and self.rotation not in VALID_ROTATIONS:
            allowed = ", ".join(str(r) for r in VALID_ROTATIONS)
            raise ValueError(f"Rotation must be one of: {allowed}.")

        if (
            self.operation is PdfOperation.SPLIT
            and self.split_mode is SplitMode.PAGE_RANGE
        ):
            if self.page_range is None:
                raise ValueError("Splitting by page range needs a first and last page.")
            first, last = self.page_range
            if first < 1:
                raise ValueError("The first page must be 1 or greater.")
            if last < first:
                raise ValueError("The last page must not be before the first page.")

        if self.operation is PdfOperation.EDIT_METADATA and not self.metadata.as_updates():
            raise ValueError("Provide at least one metadata field to update.")

        return self


class PdfResult(BaseModel):
    """Outcome of a completed pdf_suite operation.

    Attributes:
        operation: The operation that ran.
        output_paths: Files or directories produced.
        pages_processed: Number of pages handled.
        input_bytes: Combined size of the inputs.
        output_bytes: Combined size of the outputs.
        detail: Short human-readable summary of what happened.
    """

    operation: PdfOperation
    output_paths: list[Path]
    pages_processed: int = Field(ge=0)
    input_bytes: int = Field(ge=0)
    output_bytes: int = Field(ge=0)
    # Counted once in the worker thread. The UI used to walk the output folder
    # with rglob() while rendering, on the event loop — an extract-images run
    # over a large document made the result card visibly stall.
    output_file_count: int = Field(default=0, ge=0)
    detail: str = ""

    @property
    def bytes_saved(self) -> int:
        """Reduction in size. Negative when the output grew."""
        return self.input_bytes - self.output_bytes

    @property
    def size_change_percent(self) -> float:
        """Percentage change in size; 0.0 when the input was empty."""
        if self.input_bytes == 0:
            return 0.0
        return (self.bytes_saved / self.input_bytes) * 100.0
