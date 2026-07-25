"""Pydantic v2 models for the document_suite module."""
from __future__ import annotations

import enum
from pathlib import Path

from pydantic import BaseModel, Field, model_validator


class Conversion(enum.StrEnum):
    """The conversions document_suite can perform."""

    MARKDOWN_TO_HTML = "markdown_to_html"
    HTML_TO_MARKDOWN = "html_to_markdown"
    JSON_TO_CSV = "json_to_csv"
    CSV_TO_JSON = "csv_to_json"
    JSON_TO_EXCEL = "json_to_excel"
    JSON_TO_YAML = "json_to_yaml"
    YAML_TO_JSON = "yaml_to_json"


#: Extensions each conversion accepts, used by the file picker's filter.
INPUT_EXTENSIONS: dict[Conversion, tuple[str, ...]] = {
    Conversion.MARKDOWN_TO_HTML: (".md", ".markdown"),
    Conversion.HTML_TO_MARKDOWN: (".html", ".htm"),
    Conversion.JSON_TO_CSV: (".json",),
    Conversion.CSV_TO_JSON: (".csv", ".tsv"),
    Conversion.JSON_TO_EXCEL: (".json",),
    Conversion.JSON_TO_YAML: (".json",),
    Conversion.YAML_TO_JSON: (".yaml", ".yml"),
}

#: Extension written for each conversion's output.
OUTPUT_EXTENSION: dict[Conversion, str] = {
    Conversion.MARKDOWN_TO_HTML: ".html",
    Conversion.HTML_TO_MARKDOWN: ".md",
    Conversion.JSON_TO_CSV: ".csv",
    Conversion.CSV_TO_JSON: ".json",
    Conversion.JSON_TO_EXCEL: ".xlsx",
    Conversion.JSON_TO_YAML: ".yaml",
    Conversion.YAML_TO_JSON: ".json",
}


class ConvertParams(BaseModel):
    """Input parameters for a document_suite execution.

    Attributes:
        conversion: Which conversion to run.
        input_paths: Source files; each is converted independently.
        output_dir: Directory the results are written to.
        flatten_nested: Flatten nested JSON objects into dotted columns when
            producing tabular output. With this off, nested values are
            serialised as JSON text in a single cell.
        infer_types: Convert CSV strings into numbers, booleans and nulls.
    """

    conversion: Conversion
    input_paths: list[Path] = Field(min_length=1)
    output_dir: Path
    flatten_nested: bool = True
    infer_types: bool = True

    @model_validator(mode="after")
    def check_extensions(self) -> ConvertParams:
        """Reject inputs whose extension the conversion cannot read.

        Returns:
            The validated model.

        Raises:
            ValueError: When a file's extension does not match the conversion.
        """
        accepted = INPUT_EXTENSIONS[self.conversion]
        for path in self.input_paths:
            if path.suffix.lower() not in accepted:
                allowed = ", ".join(accepted)
                raise ValueError(
                    f"{path.name} is not a {allowed} file, "
                    f"which {self.conversion.value.replace('_', ' ')} requires."
                )
        return self


class ConvertResult(BaseModel):
    """Outcome of a completed document_suite run.

    Attributes:
        conversion: The conversion that ran.
        output_paths: Files produced, one per input.
        records: Rows or top-level items converted, where meaningful.
        input_bytes: Combined size of the inputs.
        output_bytes: Combined size of the outputs.
        detail: Short human-readable summary.
    """

    conversion: Conversion
    output_paths: list[Path]
    records: int = Field(default=0, ge=0)
    input_bytes: int = Field(ge=0)
    output_bytes: int = Field(ge=0)
    detail: str = ""
