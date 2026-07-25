"""Pydantic v2 models for the image_suite module."""
from __future__ import annotations

import enum
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from modules.converters.image_suite.constants import (
    DEFAULT_MAX_DIMENSION,
    DEFAULT_QUALITY,
    DEFAULT_TARGET_KB,
    MAX_DIMENSION,
    MAX_QUALITY,
    MIN_DIMENSION,
    MIN_QUALITY,
    OUTPUT_FORMATS,
    RASTER_EXTENSIONS,
    SVG_EXTENSIONS,
)


class ImageOperation(enum.StrEnum):
    """The operations image_suite can perform."""

    CONVERT = "convert"
    COMPRESS_TO_TARGET = "compress_to_target"
    RESIZE = "resize"
    STRIP_METADATA = "strip_metadata"
    SVG_TO_DENSITIES = "svg_to_densities"
    SVG_TO_FAVICONS = "svg_to_favicons"
    SVG_TO_VECTOR_DRAWABLE = "svg_to_vector_drawable"


class OutputFormat(enum.StrEnum):
    """Raster formats image_suite can write."""

    JPEG = "jpeg"
    PNG = "png"
    WEBP = "webp"


#: Extensions each operation accepts, used by the file picker's filter.
INPUT_EXTENSIONS: dict[ImageOperation, tuple[str, ...]] = {
    ImageOperation.CONVERT: RASTER_EXTENSIONS,
    ImageOperation.COMPRESS_TO_TARGET: RASTER_EXTENSIONS,
    ImageOperation.RESIZE: RASTER_EXTENSIONS,
    ImageOperation.STRIP_METADATA: RASTER_EXTENSIONS,
    ImageOperation.SVG_TO_DENSITIES: SVG_EXTENSIONS,
    ImageOperation.SVG_TO_FAVICONS: SVG_EXTENSIONS,
    ImageOperation.SVG_TO_VECTOR_DRAWABLE: SVG_EXTENSIONS,
}


class ImageParams(BaseModel):
    """Input parameters for an image_suite execution.

    Attributes:
        operation: Which operation to run.
        input_paths: Source images; each is processed independently.
        output_dir: Directory the results are written to.
        output_format: Raster format to write, where the operation converts.
        quality: Encoder quality for lossy formats.
        target_kb: Desired file size when compressing to a target.
        max_dimension: Longest edge, in pixels, when resizing.
        base_size_dp: Logical size used as the 1x baseline for density export.
    """

    operation: ImageOperation
    input_paths: list[Path] = Field(min_length=1)
    output_dir: Path

    output_format: OutputFormat = OutputFormat.PNG
    quality: int = Field(default=DEFAULT_QUALITY, ge=MIN_QUALITY, le=MAX_QUALITY)
    target_kb: int = Field(default=DEFAULT_TARGET_KB, gt=0)
    max_dimension: int = Field(
        default=DEFAULT_MAX_DIMENSION, ge=MIN_DIMENSION, le=MAX_DIMENSION
    )
    base_size_dp: int = Field(default=24, ge=1, le=MAX_DIMENSION)

    @model_validator(mode="after")
    def check_inputs(self) -> ImageParams:
        """Reject inputs the chosen operation cannot read.

        Returns:
            The validated model.

        Raises:
            ValueError: When a file's extension does not suit the operation.
        """
        accepted = INPUT_EXTENSIONS[self.operation]
        for path in self.input_paths:
            if path.suffix.lower() not in accepted:
                allowed = ", ".join(accepted)
                raise ValueError(
                    f"{path.name} is not a {allowed} file, which "
                    f"{self.operation.value.replace('_', ' ')} requires."
                )
        return self

    @property
    def pillow_format(self) -> str:
        """Pillow's format name for the selected output format."""
        return OUTPUT_FORMATS[self.output_format.value][0]

    @property
    def output_extension(self) -> str:
        """File extension for the selected output format."""
        return OUTPUT_FORMATS[self.output_format.value][1]


class ImageResult(BaseModel):
    """Outcome of a completed image_suite run.

    Attributes:
        operation: The operation that ran.
        output_paths: Files or directories produced.
        images_processed: Number of source images handled.
        input_bytes: Combined size of the inputs.
        output_bytes: Combined size of the outputs.
        detail: Short human-readable summary.
        warnings: Non-fatal notes, e.g. a target size that could not be met.
    """

    operation: ImageOperation
    output_paths: list[Path]
    images_processed: int = Field(ge=0)
    input_bytes: int = Field(ge=0)
    output_bytes: int = Field(ge=0)
    detail: str = ""
    warnings: list[str] = Field(default_factory=list)

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
