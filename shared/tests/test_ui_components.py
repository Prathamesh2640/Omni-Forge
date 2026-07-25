"""Unit tests for the non-rendering logic in shared.ui_components.

Layout itself is verified by the manual GUI checks; what is tested here is
the clipboard bridge, where unescaped interpolation would be an injection
vector into the page.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared import ui_components
from shared.constants import (
    CLIPBOARD_MAX_CHARS,
    FILE_PREVIEW_MAX_CHARS,
    FILE_PREVIEW_TRUNCATION_NOTICE,
)
from shared.ui_components import (
    copy_to_clipboard,
    is_probably_binary,
    pagination_controls,
    read_text_preview,
)


@pytest.fixture
def emitted_js(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture the JavaScript that ui_components hands to NiceGUI."""
    calls: list[str] = []
    monkeypatch.setattr(ui_components.ui, "run_javascript", calls.append)
    return calls


def test_plain_text_is_passed_to_the_clipboard_api(emitted_js: list[str]) -> None:
    copy_to_clipboard("exports/report.pdf")
    assert emitted_js == ['navigator.clipboard.writeText("exports/report.pdf")']


@pytest.mark.parametrize(
    "payload",
    [
        "it's a file.txt",
        'say "hello".txt',
        "back\\slash.txt",
        "line1\nline2",
        "tab\there",
        "</script><script>alert(1)</script>",
        "'); alert('pwned'); //",
    ],
    ids=["apostrophe", "quotes", "backslash", "newline", "tab", "script-tag", "js-break-out"],
)
def test_hostile_text_cannot_break_out_of_the_js_string(
    emitted_js: list[str], payload: str
) -> None:
    copy_to_clipboard(payload)

    script = emitted_js[0]
    argument = script[len('navigator.clipboard.writeText(') : -1]
    # Round-tripping through the JSON decoder proves the argument is a single
    # well-formed string literal carrying exactly the original text.
    assert json.loads(argument) == payload


def test_windows_paths_survive_the_round_trip(emitted_js: list[str]) -> None:
    copy_to_clipboard(r"D:\Projects\Omni-Forge\exports\out.txt")
    argument = emitted_js[0][len("navigator.clipboard.writeText(") : -1]
    assert json.loads(argument) == r"D:\Projects\Omni-Forge\exports\out.txt"


# ─── File preview ─────────────────────────────────────────────────────────────


class TestReadTextPreview:
    def test_returns_short_content_verbatim(self, tmp_path: Path) -> None:
        target = tmp_path / "out.txt"
        target.write_text("hello world", encoding="utf-8")
        assert read_text_preview(target) == "hello world"

    def test_an_empty_file_previews_as_empty(self, tmp_path: Path) -> None:
        target = tmp_path / "empty.txt"
        target.touch()
        assert read_text_preview(target) == ""

    def test_long_content_is_truncated_with_a_notice(self, tmp_path: Path) -> None:
        """A multi-megabyte export must not be handed to the browser whole."""
        target = tmp_path / "big.txt"
        target.write_text("x" * 500, encoding="utf-8")

        preview = read_text_preview(target, max_chars=100)

        assert preview.startswith("x" * 100)
        assert FILE_PREVIEW_TRUNCATION_NOTICE in preview

    def test_content_exactly_at_the_limit_is_not_truncated(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "exact.txt"
        target.write_text("y" * 100, encoding="utf-8")

        preview = read_text_preview(target, max_chars=100)

        assert preview == "y" * 100
        assert FILE_PREVIEW_TRUNCATION_NOTICE not in preview

    def test_invalid_encoding_is_replaced_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "binary.bin"
        target.write_bytes(b"\xff\xfe valid tail")
        assert "valid tail" in read_text_preview(target)

    def test_a_missing_file_reports_the_error(self, tmp_path: Path) -> None:
        preview = read_text_preview(tmp_path / "absent.txt")
        assert "Could not read the file" in preview

    def test_the_default_limit_is_applied(self, tmp_path: Path) -> None:
        target = tmp_path / "huge.txt"
        target.write_text("z" * (FILE_PREVIEW_MAX_CHARS + 50), encoding="utf-8")

        preview = read_text_preview(target)

        assert len(preview) == FILE_PREVIEW_MAX_CHARS + len(
            FILE_PREVIEW_TRUNCATION_NOTICE
        )


# ─── §3.11g/h — bounded clipboard copy, resilient file rows ──────────────────


class TestCopyFileContent:
    """An export can be hundreds of megabytes; the clipboard path must bound it."""

    def test_a_large_file_is_truncated_to_the_ceiling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        copied: list[str] = []
        monkeypatch.setattr(ui_components, "copy_to_clipboard", copied.append)
        monkeypatch.setattr(ui_components.ui, "notify", lambda *a, **kw: None)
        big = tmp_path / "huge.txt"
        big.write_text("x" * (CLIPBOARD_MAX_CHARS + 5_000), encoding="utf-8")

        ui_components._copy_file_content(big)

        assert len(copied[0]) == CLIPBOARD_MAX_CHARS

    def test_a_small_file_is_copied_whole(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        copied: list[str] = []
        monkeypatch.setattr(ui_components, "copy_to_clipboard", copied.append)
        monkeypatch.setattr(ui_components.ui, "notify", lambda *a, **kw: None)
        small = tmp_path / "small.txt"
        small.write_text("hello world", encoding="utf-8")

        ui_components._copy_file_content(small)

        assert copied == ["hello world"]

    def test_truncation_is_reported_to_the_user(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        notes: list[dict[str, object]] = []
        monkeypatch.setattr(ui_components, "copy_to_clipboard", lambda _t: None)
        monkeypatch.setattr(
            ui_components.ui, "notify", lambda msg, **kw: notes.append({"msg": msg, **kw})
        )
        big = tmp_path / "huge.txt"
        big.write_text("x" * (CLIPBOARD_MAX_CHARS + 1), encoding="utf-8")

        ui_components._copy_file_content(big)

        assert notes and notes[0]["type"] == "warning"

    def test_an_unreadable_file_reports_instead_of_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        notes: list[str] = []
        monkeypatch.setattr(ui_components.ui, "notify", lambda msg, **kw: notes.append(msg))

        ui_components._copy_file_content(tmp_path / "missing.txt")

        assert notes and "Could not read" in notes[0]


class TestSafeSize:
    def test_reports_a_real_file_size(self, tmp_path: Path) -> None:
        target = tmp_path / "f.bin"
        target.write_bytes(b"0123456789")
        assert ui_components._safe_size(target) == 10

    def test_a_directory_measures_zero(self, tmp_path: Path) -> None:
        assert ui_components._safe_size(tmp_path) == 0

    def test_an_unreadable_entry_does_not_abort_the_picker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One bad row used to raise straight out of the dialog's render."""
        target = tmp_path / "locked.bin"
        target.write_bytes(b"x")

        def refuse(self: Path) -> None:
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "stat", refuse)

        assert ui_components._safe_size(target) == 0


# ─── Binary detection ─────────────────────────────────────────────────────────


class TestIsProbablyBinary:
    """A NUL byte in the head is the git/file(1) heuristic for 'not text'."""

    def test_plain_text_is_not_binary(self, tmp_path: Path) -> None:
        target = tmp_path / "notes.txt"
        target.write_text("just some text\nover two lines", encoding="utf-8")
        assert is_probably_binary(target) is False

    def test_a_nul_byte_means_binary(self, tmp_path: Path) -> None:
        target = tmp_path / "app.bin"
        target.write_bytes(b"MZ\x00\x00 header bytes")
        assert is_probably_binary(target) is True

    def test_high_bytes_without_a_nul_are_still_text(self, tmp_path: Path) -> None:
        """A UTF-8 BOM or accented text has high bytes but no NUL — it is text."""
        target = tmp_path / "accents.txt"
        target.write_bytes("café — résumé".encode())
        assert is_probably_binary(target) is False

    def test_only_the_head_is_inspected(self, tmp_path: Path) -> None:
        """A NUL past the sniff window does not make an otherwise-text file binary."""
        target = tmp_path / "late.txt"
        target.write_bytes(b"a" * 32 + b"\x00")
        assert is_probably_binary(target, sample_bytes=16) is False

    def test_an_empty_file_is_not_binary(self, tmp_path: Path) -> None:
        target = tmp_path / "empty"
        target.touch()
        assert is_probably_binary(target) is False

    def test_an_unreadable_file_is_reported_as_not_binary(self, tmp_path: Path) -> None:
        """Unreadable is left to the reader, which surfaces the real error."""
        assert is_probably_binary(tmp_path / "absent") is False


class TestReadTextPreviewRefusesBinary:
    def test_a_binary_file_is_named_not_rendered(self, tmp_path: Path) -> None:
        """Decoding a PDF as UTF-8 produced a screen of mojibake that looked
        like corruption; the file is fine, it simply is not text."""
        target = tmp_path / "scan.pdf"
        target.write_bytes(b"%PDF-1.7\x00 binary stream " + b"\x00\xff" * 100)

        preview = read_text_preview(target)

        assert "scan.pdf" in preview
        assert "binary file" in preview
        assert FILE_PREVIEW_TRUNCATION_NOTICE not in preview


# ─── Pagination controls ──────────────────────────────────────────────────────


class _FakeButton:
    """Records disable() and the on_click it was built with."""

    def __init__(self, on_click: object) -> None:
        self.on_click = on_click
        self.disabled = False

    def props(self, _p: str) -> _FakeButton:
        return self

    def style(self, _s: str) -> _FakeButton:
        return self

    def disable(self) -> None:
        self.disabled = True


class _FakeRow:
    """A no-op context manager standing in for ui.row()."""

    def style(self, _s: str) -> _FakeRow:
        return self

    def __enter__(self) -> _FakeRow:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


class _FakeLabel:
    def __init__(self, text: str) -> None:
        self.text = text

    def style(self, _s: str) -> _FakeLabel:
        return self


class _PaginationRecorder:
    """Intercepts the NiceGUI calls pagination_controls makes."""

    def __init__(self) -> None:
        self.buttons: list[_FakeButton] = []
        self.labels: list[_FakeLabel] = []

    def row(self) -> _FakeRow:
        return _FakeRow()

    def button(self, *, icon: str, on_click: object) -> _FakeButton:
        del icon
        button = _FakeButton(on_click)
        self.buttons.append(button)
        return button

    def label(self, text: str) -> _FakeLabel:
        label = _FakeLabel(text)
        self.labels.append(label)
        return label


@pytest.fixture
def pager(monkeypatch: pytest.MonkeyPatch) -> _PaginationRecorder:
    """Replace the NiceGUI factories pagination_controls uses."""
    recorder = _PaginationRecorder()
    monkeypatch.setattr(ui_components.ui, "row", recorder.row)
    monkeypatch.setattr(ui_components.ui, "button", recorder.button)
    monkeypatch.setattr(ui_components.ui, "label", recorder.label)
    return recorder


class TestPaginationControls:
    """The page arithmetic behind six modules' result lists.

    A hard row cap hides the overflow; this helper is what replaced every one
    of them, so its off-by-one-prone maths earns a test.
    """

    def test_a_single_page_renders_nothing(self, pager: _PaginationRecorder) -> None:
        """No chrome when everything already fits — no dead Prev/Next."""
        pagination_controls(0, 30, 40, lambda _p: None)
        assert pager.buttons == []
        assert pager.labels == []

    def test_exactly_one_full_page_renders_nothing(
        self, pager: _PaginationRecorder
    ) -> None:
        pagination_controls(0, 40, 40, lambda _p: None)
        assert pager.buttons == []

    def test_one_over_a_page_paginates(self, pager: _PaginationRecorder) -> None:
        pagination_controls(0, 41, 40, lambda _p: None)
        assert pager.labels[0].text == "Page 1 of 2 · 41 items"

    def test_page_count_rounds_up(self, pager: _PaginationRecorder) -> None:
        pagination_controls(0, 95, 40, lambda _p: None)  # 3 pages: 40, 40, 15
        assert "of 3" in pager.labels[0].text

    def test_prev_is_disabled_on_the_first_page(
        self, pager: _PaginationRecorder
    ) -> None:
        pagination_controls(0, 120, 40, lambda _p: None)
        prev_button, next_button = pager.buttons
        assert prev_button.disabled is True
        assert next_button.disabled is False

    def test_next_is_disabled_on_the_last_page(
        self, pager: _PaginationRecorder
    ) -> None:
        pagination_controls(2, 120, 40, lambda _p: None)  # last of 3
        prev_button, next_button = pager.buttons
        assert prev_button.disabled is False
        assert next_button.disabled is True

    def test_both_arrows_live_in_the_middle(self, pager: _PaginationRecorder) -> None:
        pagination_controls(1, 120, 40, lambda _p: None)
        prev_button, next_button = pager.buttons
        assert prev_button.disabled is False
        assert next_button.disabled is False

    def test_the_arrows_move_one_page_each_way(
        self, pager: _PaginationRecorder
    ) -> None:
        seen: list[int] = []
        pagination_controls(1, 120, 40, seen.append)
        prev_button, next_button = pager.buttons

        prev_button.on_click()  # type: ignore[operator]
        next_button.on_click()  # type: ignore[operator]

        assert seen == [0, 2]

    def test_an_out_of_range_page_is_clamped_before_rendering(
        self, pager: _PaginationRecorder
    ) -> None:
        """A stale page index (list shrank under it) must not read as page 99."""
        pagination_controls(99, 120, 40, lambda _p: None)  # only 3 pages exist
        assert "Page 3 of 3" in pager.labels[0].text
        _prev, next_button = pager.buttons
        assert next_button.disabled is True

    def test_the_noun_is_used_in_the_label(self, pager: _PaginationRecorder) -> None:
        pagination_controls(0, 120, 40, lambda _p: None, noun="file types")
        assert "file types" in pager.labels[0].text

    def test_the_total_is_thousands_separated(
        self, pager: _PaginationRecorder
    ) -> None:
        pagination_controls(0, 12345, 40, lambda _p: None)
        assert "12,345" in pager.labels[0].text
