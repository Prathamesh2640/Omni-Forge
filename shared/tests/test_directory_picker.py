"""Unit tests for the filesystem-walking half of the directory picker.

The dialog itself is NiceGUI markup verified by the manual GUI check; what is
tested here is the listing logic, which must never raise on an unreadable
directory.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from shared import ui_components
from shared.constants import DIRECTORY_PICKER_MAX_ENTRIES, FILE_TYPE_ALL
from shared.ui_components import (
    build_file_types,
    drive_roots,
    matching_files,
    native_window,
    subdirectories,
)


class TestSubdirectories:
    def test_lists_immediate_child_directories(self, tmp_path: Path) -> None:
        (tmp_path / "alpha").mkdir()
        (tmp_path / "beta").mkdir()

        assert [p.name for p in subdirectories(tmp_path)] == ["alpha", "beta"]

    def test_files_are_excluded(self, tmp_path: Path) -> None:
        (tmp_path / "a_dir").mkdir()
        (tmp_path / "a_file.txt").write_text("x", encoding="utf-8")

        assert [p.name for p in subdirectories(tmp_path)] == ["a_dir"]

    def test_nested_grandchildren_are_not_listed(self, tmp_path: Path) -> None:
        (tmp_path / "parent" / "child").mkdir(parents=True)
        assert [p.name for p in subdirectories(tmp_path)] == ["parent"]

    def test_sorting_is_case_insensitive(self, tmp_path: Path) -> None:
        for name in ("Zebra", "apple", "Mango"):
            (tmp_path / name).mkdir()

        assert [p.name for p in subdirectories(tmp_path)] == ["apple", "Mango", "Zebra"]

    def test_an_empty_directory_lists_nothing(self, tmp_path: Path) -> None:
        assert subdirectories(tmp_path) == []

    def test_a_missing_directory_returns_empty(self, tmp_path: Path) -> None:
        assert subdirectories(tmp_path / "absent") == []

    def test_an_unreadable_directory_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(_self: Path) -> object:
            raise PermissionError("access denied")

        monkeypatch.setattr(Path, "iterdir", refuse)

        assert subdirectories(tmp_path) == []

    def test_one_unreadable_child_does_not_empty_the_listing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A single protected entry must not hide its siblings."""
        (tmp_path / "readable").mkdir()
        (tmp_path / "protected").mkdir()

        real_is_dir = Path.is_dir

        def selective(self: Path) -> bool:
            if self.name == "protected":
                raise PermissionError("access denied")
            return real_is_dir(self)

        monkeypatch.setattr(Path, "is_dir", selective)

        assert [p.name for p in subdirectories(tmp_path)] == ["readable"]

    def test_listing_is_capped(self, tmp_path: Path) -> None:
        """Guards the dialog against directories with huge child counts."""
        for index in range(DIRECTORY_PICKER_MAX_ENTRIES + 25):
            (tmp_path / f"dir_{index:04d}").mkdir()

        assert len(subdirectories(tmp_path)) == DIRECTORY_PICKER_MAX_ENTRIES


class TestMatchingFiles:
    def test_lists_files_with_the_requested_extension(self, tmp_path: Path) -> None:
        (tmp_path / "a.pdf").touch()
        (tmp_path / "b.pdf").touch()
        (tmp_path / "c.txt").touch()

        assert [p.name for p in matching_files(tmp_path, (".pdf",))] == [
            "a.pdf",
            "b.pdf",
        ]

    def test_an_empty_filter_accepts_every_file(self, tmp_path: Path) -> None:
        (tmp_path / "a.pdf").touch()
        (tmp_path / "c.txt").touch()
        assert len(matching_files(tmp_path, ())) == 2

    def test_extension_matching_is_case_insensitive(self, tmp_path: Path) -> None:
        (tmp_path / "SCAN.PDF").touch()
        assert [p.name for p in matching_files(tmp_path, (".pdf",))] == ["SCAN.PDF"]

    def test_several_extensions_are_accepted(self, tmp_path: Path) -> None:
        for name in ("a.png", "b.jpg", "c.txt"):
            (tmp_path / name).touch()
        assert len(matching_files(tmp_path, (".png", ".jpg"))) == 2

    def test_directories_are_excluded(self, tmp_path: Path) -> None:
        (tmp_path / "folder.pdf").mkdir()
        (tmp_path / "real.pdf").touch()
        assert [p.name for p in matching_files(tmp_path, (".pdf",))] == ["real.pdf"]

    def test_sorting_is_case_insensitive(self, tmp_path: Path) -> None:
        for name in ("Zebra.pdf", "apple.pdf"):
            (tmp_path / name).touch()
        assert [p.name for p in matching_files(tmp_path, (".pdf",))] == [
            "apple.pdf",
            "Zebra.pdf",
        ]

    def test_an_unreadable_directory_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(_self: Path) -> object:
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "iterdir", refuse)
        assert matching_files(tmp_path, (".pdf",)) == []

    def test_listing_is_capped(self, tmp_path: Path) -> None:
        for index in range(DIRECTORY_PICKER_MAX_ENTRIES + 20):
            (tmp_path / f"f{index:04d}.pdf").touch()
        assert len(matching_files(tmp_path, (".pdf",))) == DIRECTORY_PICKER_MAX_ENTRIES


class TestBuildFileTypes:
    def test_builds_a_filter_for_one_extension(self) -> None:
        assert build_file_types((".pdf",), "PDF documents")[0] == (
            "PDF documents (*.pdf)"
        )

    def test_combines_several_extensions(self) -> None:
        built = build_file_types((".png", ".jpg"), "Images")[0]
        assert built == "Images (*.png;*.jpg)"

    def test_always_offers_all_files_last(self) -> None:
        assert build_file_types((".pdf",), "PDF")[-1] == FILE_TYPE_ALL

    def test_no_extensions_yields_only_all_files(self) -> None:
        assert build_file_types((), "Anything") == (FILE_TYPE_ALL,)


class TestNativeDialogDetection:
    def test_no_native_window_in_browser_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NiceGUI leaves main_window unset unless running natively."""
        assert native_window() is None

    async def test_dialog_reports_unavailable_without_a_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """None means 'fall back'; an empty tuple would mean 'cancelled'."""
        monkeypatch.setattr(ui_components, "native_window", lambda: None)
        assert await ui_components._native_dialog(10) is None

    async def test_a_cancelled_dialog_is_distinct_from_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Window:
            async def create_file_dialog(self, *_a: object, **_k: object) -> None:
                return None

        monkeypatch.setattr(ui_components, "native_window", lambda: _Window())
        assert await ui_components._native_dialog(10) == ()

    async def test_selected_paths_are_returned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Window:
            async def create_file_dialog(
                self, *_a: object, **_k: object
            ) -> tuple[str, ...]:
                return ("C:\\docs\\a.pdf", "C:\\docs\\b.pdf")

        monkeypatch.setattr(ui_components, "native_window", lambda: _Window())
        result = await ui_components._native_dialog(10, allow_multiple=True)

        assert result == (Path("C:\\docs\\a.pdf"), Path("C:\\docs\\b.pdf"))

    async def test_a_bare_string_selection_is_wrapped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Save dialogs return a single string, not a sequence."""

        class _Window:
            async def create_file_dialog(self, *_a: object, **_k: object) -> str:
                return "C:\\docs\\out.pdf"

        monkeypatch.setattr(ui_components, "native_window", lambda: _Window())
        assert await ui_components._native_dialog(30) == (Path("C:\\docs\\out.pdf"),)

    async def test_a_failing_dialog_falls_back_rather_than_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Window:
            async def create_file_dialog(self, *_a: object, **_k: object) -> None:
                raise RuntimeError("dialog subsystem unavailable")

        monkeypatch.setattr(ui_components, "native_window", lambda: _Window())
        assert await ui_components._native_dialog(10) is None


class TestDriveRoots:
    def test_reports_at_least_one_root(self) -> None:
        assert drive_roots()

    def test_every_root_is_an_existing_directory(self) -> None:
        assert all(root.is_dir() for root in drive_roots())

    def test_falls_back_when_enumeration_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing partition probe must still yield a usable starting point."""

        def refuse(**_kwargs: object) -> list[object]:
            raise OSError("cannot enumerate partitions")

        monkeypatch.setattr(ui_components.psutil, "disk_partitions", refuse)

        roots = drive_roots()

        assert len(roots) == 1
        assert roots[0].is_dir()

    def test_unreadable_partitions_are_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        class _Partition:
            def __init__(self, mountpoint: str) -> None:
                self.mountpoint = mountpoint

        monkeypatch.setattr(
            ui_components.psutil,
            "disk_partitions",
            lambda **_k: [_Partition(str(tmp_path)), _Partition(str(tmp_path / "gone"))],
        )

        assert drive_roots() == [tmp_path]
