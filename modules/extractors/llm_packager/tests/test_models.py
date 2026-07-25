"""Unit tests for the llm_packager Pydantic models."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from modules.extractors.llm_packager.models import PackageParams, PackageResult


class TestPackageParams:
    def test_accepts_an_existing_directory(self, tmp_path: Path) -> None:
        assert PackageParams(source_dir=tmp_path).source_dir == tmp_path

    def test_rejects_a_missing_directory(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="does not exist"):
            PackageParams(source_dir=tmp_path / "absent")

    def test_rejects_a_file_as_the_source(self, tmp_path: Path) -> None:
        target = tmp_path / "a_file.txt"
        target.write_text("x", encoding="utf-8")
        with pytest.raises(ValidationError, match="not a directory"):
            PackageParams(source_dir=target)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (["py"], [".py"]),
            ([".py"], [".py"]),
            (["PY"], [".py"]),
            (["  Md  "], [".md"]),
            (["py", "ts"], [".py", ".ts"]),
        ],
    )
    def test_normalises_extensions(
        self, tmp_path: Path, raw: list[str], expected: list[str]
    ) -> None:
        assert PackageParams(source_dir=tmp_path, extensions=raw).extensions == expected

    def test_blank_extensions_are_dropped(self, tmp_path: Path) -> None:
        params = PackageParams(source_dir=tmp_path, extensions=["py", "", "  "])
        assert params.extensions == [".py"]

    def test_a_tuple_of_extensions_is_accepted(self, tmp_path: Path) -> None:
        params = PackageParams(source_dir=tmp_path, extensions=("py", "ts"))
        assert params.extensions == [".py", ".ts"]

    def test_a_non_sequence_of_extensions_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="must be a list"):
            PackageParams(source_dir=tmp_path, extensions="py")

    def test_defaults_are_applied(self, tmp_path: Path) -> None:
        params = PackageParams(source_dir=tmp_path)
        assert params.extensions
        assert params.exclude_patterns
        # Anchored to the app root, so it is absolute and CWD-independent.
        assert params.output_dir.is_absolute()
        assert params.output_dir.name == "exports"


class TestPackageResult:
    def _result(self, **overrides: object) -> PackageResult:
        defaults: dict[str, object] = {
            "output_path": Path("exports/out.txt"),
            "file_count": 3,
            "total_chars": 120,
            "token_count": 42,
            "token_model": "o200k_base",
            "skipped_count": 1,
        }
        return PackageResult(**{**defaults, **overrides})  # type: ignore[arg-type]

    def test_accepts_valid_statistics(self) -> None:
        assert self._result().token_count == 42

    def test_zero_counts_are_valid(self) -> None:
        """An empty directory legitimately produces an all-zero result."""
        assert self._result(file_count=0, total_chars=0, token_count=0).file_count == 0

    @pytest.mark.parametrize(
        "field", ["file_count", "total_chars", "token_count", "skipped_count"]
    )
    def test_negative_counts_are_rejected(self, field: str) -> None:
        with pytest.raises(ValidationError):
            self._result(**{field: -1})
