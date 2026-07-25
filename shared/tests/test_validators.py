"""Unit tests for shared.validators."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from shared.validators import (
    compile_regex,
    is_safe_filename,
    is_within,
    normalise_extension,
    validate_write_target,
)


class TestNormaliseExtension:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("py", ".py"),
            (".py", ".py"),
            ("PY", ".py"),
            (".PY", ".py"),
            ("  .Md  ", ".md"),
            ("tar.gz", ".tar.gz"),
        ],
    )
    def test_normalises_case_and_dot_prefix(self, raw: str, expected: str) -> None:
        assert normalise_extension(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_blank_input_yields_empty_string(self, raw: str) -> None:
        assert normalise_extension(raw) == ""


class TestCompileRegex:
    def test_returns_a_usable_compiled_pattern(self) -> None:
        assert compile_regex(r"IMG_(\d+)").match("IMG_042") is not None

    def test_invalid_pattern_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Invalid regular expression"):
            compile_regex("(unclosed")

    def test_error_chains_the_original_re_error(self) -> None:
        with pytest.raises(ValueError) as info:
            compile_regex("*bad")
        assert isinstance(info.value.__cause__, re.error)


class TestIsWithin:
    def test_direct_child_is_within(self, tmp_path: Path) -> None:
        assert is_within(tmp_path / "child.txt", tmp_path) is True

    def test_nested_descendant_is_within(self, tmp_path: Path) -> None:
        assert is_within(tmp_path / "a" / "b" / "c.txt", tmp_path) is True

    def test_the_root_itself_is_within(self, tmp_path: Path) -> None:
        assert is_within(tmp_path, tmp_path) is True

    def test_sibling_is_not_within(self, tmp_path: Path) -> None:
        assert is_within(tmp_path.parent / "elsewhere", tmp_path) is False

    def test_parent_traversal_cannot_escape(self, tmp_path: Path) -> None:
        """`..` must be resolved away before the containment check."""
        assert is_within(tmp_path / "sub" / ".." / ".." / "escaped", tmp_path) is False


class TestValidateWriteTarget:
    def test_exports_is_permitted(self) -> None:
        assert validate_write_target(Path("exports") / "out.txt").is_absolute()

    def test_temp_is_permitted(self) -> None:
        assert validate_write_target(Path("temp") / "scratch.bin").is_absolute()

    def test_nested_path_under_exports_is_permitted(self) -> None:
        validate_write_target(Path("exports") / "pdf" / "merged" / "out.pdf")

    def test_arbitrary_system_path_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Refusing to write outside"):
            validate_write_target(Path.home() / ".ssh" / "authorized_keys")

    def test_user_selected_directory_is_permitted_via_extra_roots(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "chosen" / "report.pdf"
        assert validate_write_target(target, extra_roots=(tmp_path,)) == target.resolve()

    def test_extra_roots_do_not_permit_their_siblings(self, tmp_path: Path) -> None:
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        with pytest.raises(ValueError):
            validate_write_target(tmp_path / "other" / "x.txt", extra_roots=(allowed,))

    def test_traversal_out_of_exports_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            validate_write_target(Path("exports") / ".." / ".." / "etc" / "passwd")


class TestIsSafeFilename:
    @pytest.mark.parametrize(
        "name",
        ["report.pdf", "IMG_0042.jpg", "my file (1).txt", ".gitignore", "a"],
    )
    def test_accepts_ordinary_names(self, name: str) -> None:
        assert is_safe_filename(name) is True

    @pytest.mark.parametrize("name", ["", ".", ".."])
    def test_rejects_empty_and_dot_names(self, name: str) -> None:
        assert is_safe_filename(name) is False

    @pytest.mark.parametrize("name", ["dir/file.txt", "dir\\file.txt"])
    def test_rejects_path_separators(self, name: str) -> None:
        assert is_safe_filename(name) is False

    @pytest.mark.parametrize("name", ['a"b', "a<b", "a>b", "a|b", "a?b", "a*b", "a:b"])
    def test_rejects_reserved_characters(self, name: str) -> None:
        assert is_safe_filename(name) is False

    @pytest.mark.parametrize("name", ["CON", "con.txt", "NUL", "COM1", "LPT9", "aux.log"])
    def test_rejects_windows_device_names(self, name: str) -> None:
        assert is_safe_filename(name) is False

    @pytest.mark.parametrize("name", ["trailing.", "trailing "])
    def test_rejects_trailing_dot_or_space(self, name: str) -> None:
        assert is_safe_filename(name) is False

    def test_device_name_as_a_substring_is_still_allowed(self) -> None:
        assert is_safe_filename("console.log") is True
