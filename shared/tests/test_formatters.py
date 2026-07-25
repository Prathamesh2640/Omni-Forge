"""Unit tests for shared.formatters."""
from __future__ import annotations

from pathlib import Path

import pytest

from shared.formatters import format_bytes, format_duration, format_impact, truncate_path

_KB = 1024
_MB = _KB * 1024
_GB = _MB * 1024
_TB = _GB * 1024


class TestFormatBytes:
    @pytest.mark.parametrize(
        ("num_bytes", "expected"),
        [
            (0, "0 B"),
            (1, "1 B"),
            (512, "512 B"),
            (1023, "1023 B"),
            (_KB, "1.0 KB"),
            (1536, "1.5 KB"),
            (_MB, "1.0 MB"),
            (_GB, "1.0 GB"),
            (_TB, "1.0 TB"),
        ],
    )
    def test_scales_to_the_right_unit(self, num_bytes: int, expected: str) -> None:
        assert format_bytes(num_bytes) == expected

    def test_bytes_never_show_decimals(self) -> None:
        assert format_bytes(999) == "999 B"

    def test_precision_is_configurable(self) -> None:
        assert format_bytes(1536, precision=2) == "1.50 KB"
        assert format_bytes(1536, precision=0) == "2 KB"

    def test_negative_values_keep_their_sign(self) -> None:
        """Used for "space freed" deltas."""
        assert format_bytes(-_GB) == "-1.0 GB"

    def test_petabyte_is_the_top_unit(self) -> None:
        assert format_bytes(_TB * 1024 * 5).endswith(" PB")

    def test_beyond_the_top_unit_does_not_overflow(self) -> None:
        assert format_bytes(_TB * 1024 * 1024 * 99).endswith(" PB")


class TestFormatDuration:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0.0, "0.0s"),
            (0.44, "0.4s"),
            (12.34, "12.3s"),
            (59.9, "59.9s"),
            (60.0, "1m 00s"),
            (63.0, "1m 03s"),
            (3599.0, "59m 59s"),
            (3600.0, "1h 00m"),
            (8100.0, "2h 15m"),
        ],
    )
    def test_picks_the_right_granularity(self, seconds: float, expected: str) -> None:
        assert format_duration(seconds) == expected


class TestFormatImpact:
    def test_renders_size_and_count(self) -> None:
        assert format_impact(4_509_715_660, 1247) == "4.2 GB across 1,247 files"

    def test_uses_the_singular_noun_for_one_file(self) -> None:
        assert format_impact(_KB, 1) == "1.0 KB across 1 file"

    def test_handles_an_empty_selection(self) -> None:
        assert format_impact(0, 0) == "0 B across 0 files"


class TestTruncatePath:
    def test_short_paths_pass_through_unchanged(self) -> None:
        path = Path("exports/out.txt")
        assert truncate_path(path, 40) == str(path)

    def test_long_paths_are_shortened_from_the_left(self) -> None:
        result = truncate_path("/very/deeply/nested/directory/tree/report.pdf", 20)
        assert len(result) == 20
        assert result.startswith("…")
        assert result.endswith("report.pdf")

    def test_result_never_exceeds_the_limit(self) -> None:
        assert len(truncate_path("x" * 500, 32)) == 32

    def test_degenerate_limits_do_not_raise(self) -> None:
        assert truncate_path("some/path/file.txt", 0) == "…"
