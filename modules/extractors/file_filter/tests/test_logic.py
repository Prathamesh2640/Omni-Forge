"""Unit tests for the file_filter logic layer."""
from __future__ import annotations

import asyncio
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from core import recycle_store
from core.models import ProgressEvent
from modules.extractors.file_filter.constants import (
    NO_EXTENSION_LABEL,
    OUTPUT_SUBDIR,
    PROGRESS_COMPLETE,
)
from modules.extractors.file_filter.logic import FileFilterLogic
from modules.extractors.file_filter.models import (
    FilterParams,
    FilterResult,
    OutputMode,
    ScanParams,
    UndoParams,
)


@pytest.fixture()
def logic() -> FileFilterLogic:
    """Fresh logic instance with no EventBus registrations."""
    return FileFilterLogic()


@pytest.fixture(autouse=True)
def temp_recycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Keep recycled files inside the test's own directory."""
    root = tmp_path / "recycle"
    monkeypatch.setattr(recycle_store, "recycle_root", lambda: root)
    yield root


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A small source tree with a mix of file types."""
    root = tmp_path / "project"
    (root / "src" / "deep").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "node_modules").mkdir()

    # newline="" prevents Windows text-mode translation of "\n" to "\r\n" so
    # fixture content is byte-identical across platforms (tests assert on it).
    (root / "main.py").write_text("print('a')\n", encoding="utf-8", newline="")
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8", newline="")
    (root / "src" / "deep" / "util.py").write_text("y = 2\n", encoding="utf-8", newline="")
    (root / "src" / "style.css").write_text("body{}\n", encoding="utf-8", newline="")
    (root / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8", newline="")
    (root / "LICENSE").write_text("MIT\n", encoding="utf-8", newline="")
    (root / "node_modules" / "junk.py").write_text("ignore\n", encoding="utf-8", newline="")
    return root


@pytest.fixture()
def out_dir(tmp_path: Path) -> Path:
    """Directory results are written to."""
    return tmp_path / "exports"


def run(
    logic: FileFilterLogic, params: FilterParams
) -> tuple[list[ProgressEvent], FilterResult]:
    """Execute a filter to completion."""

    async def drive() -> list[ProgressEvent]:
        return [event async for event in logic.execute(params)]

    events = asyncio.run(drive())
    assert logic._last_result is not None
    return events, logic._last_result


def _params(source: Path, out: Path, **kw: object) -> FilterParams:
    """Build FilterParams with the common fields filled in."""
    return FilterParams(source_dir=source, output_dir=out, **kw)  # type: ignore[arg-type]


# ─── Scanning ─────────────────────────────────────────────────────────────────


class TestScan:
    def test_counts_files_by_extension(
        self, logic: FileFilterLogic, project: Path
    ) -> None:
        result = logic.scan(ScanParams(source_dir=project))
        counts = {e.extension: e.count for e in result.extensions}

        assert counts[".py"] == 3  # node_modules is excluded by default
        assert counts[".md"] == 1
        assert counts[".css"] == 1

    def test_groups_files_without_an_extension(
        self, logic: FileFilterLogic, project: Path
    ) -> None:
        result = logic.scan(ScanParams(source_dir=project))
        labels = {e.extension for e in result.extensions}
        assert NO_EXTENSION_LABEL in labels

    def test_default_excludes_skip_rebuildable_trees(
        self, logic: FileFilterLogic, project: Path
    ) -> None:
        """node_modules would otherwise dominate every scan."""
        result = logic.scan(ScanParams(source_dir=project))
        assert result.total_files == 6

    def test_custom_excludes_are_applied(
        self, logic: FileFilterLogic, project: Path
    ) -> None:
        result = logic.scan(
            ScanParams(source_dir=project, exclude_patterns=["**/docs/**"])
        )
        assert all(e.extension != ".md" for e in result.extensions)

    def test_sizes_are_totalled(self, logic: FileFilterLogic, project: Path) -> None:
        result = logic.scan(ScanParams(source_dir=project))
        assert result.total_bytes > 0
        assert sum(e.total_bytes for e in result.extensions) == result.total_bytes

    def test_most_common_extension_is_listed_first(
        self, logic: FileFilterLogic, project: Path
    ) -> None:
        result = logic.scan(ScanParams(source_dir=project))
        assert result.extensions[0].extension == ".py"

    def test_extension_matching_is_case_insensitive(
        self, logic: FileFilterLogic, tmp_path: Path
    ) -> None:
        root = tmp_path / "mixed"
        root.mkdir()
        (root / "a.PY").write_text("x\n", encoding="utf-8")
        (root / "b.py").write_text("y\n", encoding="utf-8")

        result = logic.scan(ScanParams(source_dir=root))
        assert {e.extension: e.count for e in result.extensions} == {".py": 2}

    def test_an_empty_directory_scans_cleanly(
        self, logic: FileFilterLogic, tmp_path: Path
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        result = logic.scan(ScanParams(source_dir=empty))

        assert result.total_files == 0
        assert result.extensions == []

    def test_a_missing_directory_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Not a directory"):
            ScanParams(source_dir=tmp_path / "absent")

    def test_an_unstattable_file_is_skipped_not_fatal(
        self, logic: FileFilterLogic, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file that vanishes between listing and sizing (e.g. deleted by
        another process mid-scan) must not abort the whole scan."""
        real_walk = logic.walk

        def walk_with_a_ghost(root: Path, exclude_patterns: list[str]) -> list[Path]:
            return [*real_walk(root, exclude_patterns), project / "ghost.py"]

        monkeypatch.setattr(logic, "walk", walk_with_a_ghost)
        result = logic.scan(ScanParams(source_dir=project))
        counts = {e.extension: e.count for e in result.extensions}

        assert result.total_files == 6  # the ghost file is not counted
        assert counts[".py"] == 3

    def test_an_unlistable_entry_is_skipped_not_fatal(
        self, logic: FileFilterLogic, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``walk`` must tolerate a path that raises mid-listing (e.g. a
        broken symlink or a permission-denied stat) rather than crashing."""
        real_is_file = Path.is_file

        def flaky(self: Path) -> bool:
            if self.name == "main.py":
                raise OSError("permission denied")
            return real_is_file(self)

        monkeypatch.setattr(Path, "is_file", flaky)
        matched = logic.walk(project, [])

        assert all(p.name != "main.py" for p in matched)


# ─── Selection ────────────────────────────────────────────────────────────────


class TestSelection:
    def test_filters_to_the_chosen_extensions(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        selected = logic.select(_params(project, out_dir, extensions=[".py"]))
        assert all(p.suffix == ".py" for p in selected)
        assert len(selected) == 3

    def test_several_extensions_are_combined(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        selected = logic.select(_params(project, out_dir, extensions=[".py", ".md"]))
        assert len(selected) == 4

    def test_no_extensions_selects_everything(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        selected = logic.select(_params(project, out_dir, extensions=[]))
        assert len(selected) == 6

    def test_the_no_extension_group_is_selectable(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        selected = logic.select(
            _params(project, out_dir, extensions=[NO_EXTENSION_LABEL])
        )
        assert [p.name for p in selected] == ["LICENSE"]


# ─── Copy ─────────────────────────────────────────────────────────────────────


class TestCopyMode:
    def test_copies_matching_files(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        _events, result = run(logic, _params(project, out_dir, extensions=[".py"]))

        copied = list(result.output_paths[0].rglob("*.py"))
        assert len(copied) == 3
        assert result.files_written == 3

    def test_originals_are_left_alone(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        run(logic, _params(project, out_dir, extensions=[".py"]))
        assert (project / "main.py").is_file()

    def test_hierarchy_is_preserved_by_default(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        _events, result = run(logic, _params(project, out_dir, extensions=[".py"]))
        assert (result.output_paths[0] / "src" / "deep" / "util.py").is_file()

    def test_hierarchy_can_be_flattened(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        _events, result = run(
            logic,
            _params(project, out_dir, extensions=[".py"], preserve_hierarchy=False),
        )
        assert (result.output_paths[0] / "util.py").is_file()

    def test_flattening_does_not_overwrite_same_named_files(
        self, logic: FileFilterLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        """Two index.js files in different folders must both survive."""
        root = tmp_path / "app"
        (root / "a").mkdir(parents=True)
        (root / "b").mkdir(parents=True)
        (root / "a" / "index.js").write_text("first\n", encoding="utf-8")
        (root / "b" / "index.js").write_text("second\n", encoding="utf-8")

        _events, result = run(
            logic,
            _params(root, out_dir, extensions=[".js"], preserve_hierarchy=False),
        )

        assert result.files_written == 2
        assert len(list(result.output_paths[0].glob("*.js"))) == 2

    def test_writes_into_the_module_subdirectory(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        _events, result = run(logic, _params(project, out_dir, extensions=[".py"]))
        assert result.output_paths[0].parent.name == OUTPUT_SUBDIR


# ─── Move ─────────────────────────────────────────────────────────────────────


class TestMoveMode:
    def test_files_leave_the_source_tree(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        run(
            logic,
            _params(project, out_dir, extensions=[".py"], output_mode=OutputMode.MOVE),
        )
        assert not (project / "main.py").exists()

    def test_copies_land_in_the_destination(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        _events, result = run(
            logic,
            _params(project, out_dir, extensions=[".py"], output_mode=OutputMode.MOVE),
        )
        assert len(list(result.output_paths[0].rglob("*.py"))) == 3

    def test_move_records_a_reversible_pair_per_file(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        """A move is undone by moving back, so every relocation is recorded."""
        _events, result = run(
            logic,
            _params(project, out_dir, extensions=[".py"], output_mode=OutputMode.MOVE),
        )

        assert len(result.moved_pairs) == 3
        for pair in result.moved_pairs:
            assert not pair.source_path.exists()
            assert pair.destination_path.is_file()

    def test_move_does_not_duplicate_the_data(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        """Regression: Move used to copy *then* recycle the original.

        That needed twice the space and freed none of it — the bytes ended up
        both in the destination and in the recycle store. A relocation must
        leave exactly one copy on disk and create no recycle batch.
        """
        before = len(recycle_store.list_batches())

        _events, result = run(
            logic,
            _params(project, out_dir, extensions=[".py"], output_mode=OutputMode.MOVE),
        )

        assert len(recycle_store.list_batches()) == before
        # Exactly one copy of each file survives: the destination.
        assert not (project / "main.py").exists()
        assert len(list(result.output_paths[0].rglob("*.py"))) == 3

    def test_the_move_can_be_undone(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        _events, result = run(
            logic,
            _params(project, out_dir, extensions=[".py"], output_mode=OutputMode.MOVE),
        )

        undone = logic.undo(UndoParams(pairs=result.moved_pairs))

        assert undone.files_written == 3
        assert (project / "main.py").is_file()
        assert (project / "src" / "deep" / "util.py").is_file()

    def test_undo_skips_a_destination_that_vanished(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        _events, result = run(
            logic,
            _params(project, out_dir, extensions=[".py"], output_mode=OutputMode.MOVE),
        )
        result.moved_pairs[0].destination_path.unlink()

        undone = logic.undo(UndoParams(pairs=result.moved_pairs))

        assert undone.files_written == 2
        assert len(undone.warnings) == 1

    def test_undo_refuses_when_nothing_can_be_moved_back(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        _events, result = run(
            logic,
            _params(project, out_dir, extensions=[".py"], output_mode=OutputMode.MOVE),
        )
        for pair in result.moved_pairs:
            pair.destination_path.unlink()

        with pytest.raises(ValueError, match="Nothing could be moved back"):
            logic.undo(UndoParams(pairs=result.moved_pairs))

    def test_copy_mode_records_no_pairs(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        _events, result = run(logic, _params(project, out_dir, extensions=[".py"]))
        assert result.moved_pairs == []

    def test_only_move_is_flagged_destructive(
        self, project: Path, out_dir: Path
    ) -> None:
        assert _params(project, out_dir, output_mode=OutputMode.MOVE).is_destructive
        for mode in (OutputMode.COPY, OutputMode.ZIP, OutputMode.MANIFEST):
            assert not _params(project, out_dir, output_mode=mode).is_destructive


# ─── Zip ──────────────────────────────────────────────────────────────────────


class TestZipMode:
    def test_produces_a_readable_archive(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        _events, result = run(
            logic,
            _params(project, out_dir, extensions=[".py"], output_mode=OutputMode.ZIP),
        )

        with zipfile.ZipFile(result.output_paths[0]) as archive:
            assert len(archive.namelist()) == 3
            assert archive.testzip() is None

    def test_archive_keeps_the_hierarchy(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        _events, result = run(
            logic,
            _params(project, out_dir, extensions=[".py"], output_mode=OutputMode.ZIP),
        )

        with zipfile.ZipFile(result.output_paths[0]) as archive:
            assert "src/deep/util.py" in archive.namelist()

    def test_archived_content_matches_the_source(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        _events, result = run(
            logic,
            _params(project, out_dir, extensions=[".py"], output_mode=OutputMode.ZIP),
        )

        with zipfile.ZipFile(result.output_paths[0]) as archive:
            assert archive.read("main.py").decode() == "print('a')\n"

    def test_originals_are_untouched(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        run(
            logic,
            _params(project, out_dir, extensions=[".py"], output_mode=OutputMode.ZIP),
        )
        assert (project / "main.py").is_file()

    def test_a_vanished_file_is_skipped_not_fatal(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        """A selected file that disappears before archiving must be noted
        as a warning rather than aborting the whole archive."""
        out_dir.mkdir(parents=True)
        selected = [project / "main.py", project / "ghost.py"]
        params = _params(project, out_dir, extensions=[".py"], output_mode=OutputMode.ZIP)

        outputs, written, warnings, _batch = logic._write_archive(
            selected, params, out_dir, "project", "20260101-000000"
        )

        assert written == 1
        assert any("ghost.py" in note for note in warnings)
        with zipfile.ZipFile(outputs[0]) as archive:
            assert archive.namelist() == ["main.py"]


# ─── Manifest ─────────────────────────────────────────────────────────────────


class TestManifestMode:
    def test_lists_every_match(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        _events, result = run(
            logic,
            _params(
                project, out_dir, extensions=[".py"], output_mode=OutputMode.MANIFEST
            ),
        )

        content = result.output_paths[0].read_text(encoding="utf-8")
        assert "main.py" in content
        assert "src/deep/util.py" in content

    def test_records_sizes_and_timestamps(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        _events, result = run(
            logic,
            _params(
                project, out_dir, extensions=[".py"], output_mode=OutputMode.MANIFEST
            ),
        )

        rows = [
            line
            for line in result.output_paths[0].read_text(encoding="utf-8").splitlines()
            if not line.startswith("#")
        ]
        assert all(len(row.split("\t")) == 3 for row in rows if row)

    def test_copies_nothing(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        _events, result = run(
            logic,
            _params(
                project, out_dir, extensions=[".py"], output_mode=OutputMode.MANIFEST
            ),
        )

        assert result.output_paths[0].suffix == ".txt"
        assert (project / "main.py").is_file()

    def test_a_vanished_file_is_noted_not_fatal(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        """A selected file that disappears before it can be stat'd must be
        noted as a warning rather than aborting the whole manifest."""
        out_dir.mkdir(parents=True)
        selected = [project / "main.py", project / "ghost.py"]
        params = _params(
            project, out_dir, extensions=[".py"], output_mode=OutputMode.MANIFEST
        )

        outputs, written, warnings, _batch = logic._write_manifest(
            selected, params, out_dir, "project", "20260101-000000"
        )

        assert written == 1
        assert any("ghost.py" in note for note in warnings)
        assert "main.py" in outputs[0].read_text(encoding="utf-8")


# ─── Contract ─────────────────────────────────────────────────────────────────


class TestExecutionContract:
    def test_no_matches_is_reported_clearly(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        with pytest.raises(ValueError, match="No files matched"):
            run(logic, _params(project, out_dir, extensions=[".nonexistent"]))

    def test_an_output_inside_the_source_is_rejected(
        self, project: Path
    ) -> None:
        """Writing into the tree being scanned would feed on its own output."""
        with pytest.raises(ValueError, match="inside the folder being filtered"):
            _params(project, project / "results")

    def test_a_sibling_output_is_allowed(
        self, project: Path, out_dir: Path
    ) -> None:
        assert _params(project, out_dir).output_dir == out_dir

    def test_a_missing_source_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Not a directory"):
            _params(tmp_path / "absent", tmp_path / "out")

    def test_progress_ends_at_one_hundred(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        events, _result = run(logic, _params(project, out_dir, extensions=[".py"]))
        assert events[-1].percent == PROGRESS_COMPLETE

    def test_totals_are_reported(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        _events, result = run(logic, _params(project, out_dir, extensions=[".py"]))
        assert result.files_matched == 3
        assert result.total_bytes > 0

    def test_an_unreadable_file_is_skipped_not_fatal(
        self, logic: FileFilterLogic, project: Path, out_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real_copy = FileFilterLogic._write_files

        def selective(source: Path, destination: Path) -> Path:
            if source.name == "main.py":
                raise PermissionError("locked by another process")
            return destination

        monkeypatch.setattr(
            "modules.extractors.file_filter.logic.safe_copy", selective
        )
        _events, result = run(logic, _params(project, out_dir, extensions=[".py"]))

        assert result.files_written == 2
        assert any("main.py" in note for note in result.warnings)
        assert real_copy is FileFilterLogic._write_files

    def test_relative_target_falls_back_to_bare_name_when_outside_source(
        self, logic: FileFilterLogic, project: Path, out_dir: Path
    ) -> None:
        """A source path that isn't under ``source_dir`` (e.g. reached via a
        symlink) must still produce a usable destination name."""
        params = _params(project, out_dir)
        outside = out_dir.parent / "elsewhere" / "file.txt"
        assert logic._relative_target(outside, params) == "file.txt"

    def test_safe_size_returns_zero_for_a_vanished_file(
        self, logic: FileFilterLogic, tmp_path: Path
    ) -> None:
        assert logic._safe_size(tmp_path / "absent.txt") == 0


# ─── EventBus wiring ──────────────────────────────────────────────────────────


@pytest.mark.asyncio()
async def test_register_and_unregister_round_trip(logic: FileFilterLogic) -> None:
    from core.event_bus import event_bus
    from modules.extractors.file_filter.constants import EVENT_EXECUTE, EVENT_SCAN

    await logic.register()
    assert logic._on_scan in event_bus._subscribers[EVENT_SCAN]
    assert logic._on_execute in event_bus._subscribers[EVENT_EXECUTE]

    await logic.unregister()
    assert logic._on_scan not in event_bus._subscribers[EVENT_SCAN]
    assert logic._on_execute not in event_bus._subscribers[EVENT_EXECUTE]


@pytest.mark.asyncio()
async def test_scan_result_is_published(
    logic: FileFilterLogic, project: Path
) -> None:
    from core.event_bus import event_bus
    from modules.extractors.file_filter.constants import EVENT_SCANNED
    from modules.extractors.file_filter.models import ScanResult

    received: list[ScanResult] = []

    async def capture(payload: object) -> None:
        assert isinstance(payload, ScanResult)
        received.append(payload)

    event_bus.subscribe(EVENT_SCANNED, capture)
    try:
        await logic._on_scan(ScanParams(source_dir=project))
    finally:
        event_bus.unsubscribe(EVENT_SCANNED, capture)

    assert len(received) == 1
    assert received[0].total_files == 6


@pytest.mark.asyncio()
async def test_a_failed_scan_publishes_an_error(
    logic: FileFilterLogic, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.event_bus import event_bus
    from modules.extractors.file_filter.constants import EVENT_ERROR

    errors: list[object] = []

    async def on_error(payload: object) -> None:
        errors.append(payload)

    def explode(_self: object, _params: object) -> None:
        raise OSError("disk failure")

    monkeypatch.setattr(FileFilterLogic, "scan", explode)
    event_bus.subscribe(EVENT_ERROR, on_error)
    try:
        await logic._on_scan(ScanParams(source_dir=project))
    finally:
        event_bus.unsubscribe(EVENT_ERROR, on_error)

    assert len(errors) == 1


@pytest.mark.asyncio()
async def test_result_is_published_on_completion(
    logic: FileFilterLogic, project: Path, out_dir: Path
) -> None:
    from core.event_bus import event_bus
    from modules.extractors.file_filter.constants import EVENT_DONE

    received: list[FilterResult] = []

    async def capture(payload: object) -> None:
        assert isinstance(payload, FilterResult)
        received.append(payload)

    event_bus.subscribe(EVENT_DONE, capture)
    try:
        await logic._on_execute(_params(project, out_dir, extensions=[".py"]))
    finally:
        event_bus.unsubscribe(EVENT_DONE, capture)

    assert len(received) == 1


@pytest.mark.asyncio()
async def test_a_failure_publishes_an_error_not_a_result(
    logic: FileFilterLogic, project: Path, out_dir: Path
) -> None:
    from core.event_bus import event_bus
    from modules.extractors.file_filter.constants import EVENT_DONE, EVENT_ERROR

    done: list[object] = []
    errors: list[object] = []

    async def on_done(payload: object) -> None:
        done.append(payload)

    async def on_error(payload: object) -> None:
        errors.append(payload)

    event_bus.subscribe(EVENT_DONE, on_done)
    event_bus.subscribe(EVENT_ERROR, on_error)
    try:
        await logic._on_execute(
            _params(project, out_dir, extensions=[".nothing-matches"])
        )
    finally:
        event_bus.unsubscribe(EVENT_DONE, on_done)
        event_bus.unsubscribe(EVENT_ERROR, on_error)

    assert done == []
    assert len(errors) == 1


@pytest.mark.asyncio()
async def test_non_params_payloads_are_ignored(logic: FileFilterLogic) -> None:
    await logic._on_scan({"source_dir": "/tmp"})
    await logic._on_execute({"source_dir": "/tmp"})
    assert logic._last_result is None
