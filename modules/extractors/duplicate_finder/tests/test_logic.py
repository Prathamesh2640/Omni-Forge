"""Unit tests for the duplicate_finder logic layer."""
from __future__ import annotations

import asyncio
import datetime
import os
import time
from collections.abc import Iterator
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest

from core import recycle_store
from core.event_bus import event_bus
from core.models import ProgressEvent
from core.sandbox import shutdown_process_pool
from modules.extractors.duplicate_finder import logic as dup_logic
from modules.extractors.duplicate_finder.constants import (
    EVENT_ERROR,
    EVENT_EXECUTE,
    EVENT_SCAN,
    EVENT_SCANNED,
    MIN_FILES_FOR_MULTIPROCESSING,
    PROGRESS_COMPLETE,
    PROGRESS_START,
    SCAN_REPORT_EVERY,
)
from modules.extractors.duplicate_finder.logic import DuplicateFinderLogic
from modules.extractors.duplicate_finder.logic import _hash_path as _real_hash_path
from modules.extractors.duplicate_finder.models import (
    DuplicateFile,
    DuplicateGroup,
    KeepStrategy,
    ResolveParams,
    ScanParams,
    ScanResult,
)


@pytest.fixture()
def logic() -> DuplicateFinderLogic:
    """Fresh logic instance with no EventBus registrations."""
    return DuplicateFinderLogic()


@pytest.fixture(autouse=True)
def temp_recycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Keep recycled files inside the test's own directory."""
    root = tmp_path / "recycle"
    monkeypatch.setattr(recycle_store, "recycle_root", lambda: root)
    yield root


@pytest.fixture(autouse=True)
def _cleanup_process_pool() -> Iterator[None]:
    """Ensure no worker process outlives its test."""
    yield
    shutdown_process_pool()


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A small tree with a known duplicate group, a unique file, a same-size
    non-duplicate, an excluded-by-default duplicate, and two empty files."""
    root = tmp_path / "project"
    (root / "subdir").mkdir(parents=True)
    (root / "node_modules").mkdir()

    # write_bytes sidesteps Windows text-mode newline translation entirely,
    # so content (and therefore hashes) are byte-identical across platforms.
    dup_content = b"duplicate content\n"
    same_size_content = b"x" * len(dup_content)

    (root / "a.txt").write_bytes(dup_content)
    (root / "b.txt").write_bytes(dup_content)
    (root / "subdir" / "c.txt").write_bytes(dup_content)
    (root / "unique.txt").write_bytes(b"nothing else looks like this\n")
    (root / "same_size.txt").write_bytes(same_size_content)
    (root / "empty.txt").write_bytes(b"")
    (root / "empty2.txt").write_bytes(b"")
    (root / "node_modules" / "junk.txt").write_bytes(dup_content)
    (root / "big1.bin").write_bytes(b"B" * 1000)
    (root / "big2.bin").write_bytes(b"B" * 1000)
    return root


def run(
    logic: DuplicateFinderLogic, params: ResolveParams
) -> tuple[list[ProgressEvent], object]:
    """Execute a resolve to completion."""

    async def drive() -> list[ProgressEvent]:
        return [event async for event in logic.execute(params)]

    events = asyncio.run(drive())
    assert logic._last_result is not None
    return events, logic._last_result


# ─── Scanning ─────────────────────────────────────────────────────────────────


class TestScan:
    def test_finds_the_duplicate_group(
        self, logic: DuplicateFinderLogic, project: Path
    ) -> None:
        result = logic.scan(ScanParams(source_dir=project))
        dup = next(g for g in result.groups if len(g.files) == 3)
        assert {f.path.name for f in dup.files} == {"a.txt", "b.txt", "c.txt"}

    def test_same_size_different_content_is_not_grouped(
        self, logic: DuplicateFinderLogic, project: Path
    ) -> None:
        result = logic.scan(ScanParams(source_dir=project))
        for group in result.groups:
            assert "same_size.txt" not in {f.path.name for f in group.files}

    def test_node_modules_is_excluded_by_default(
        self, logic: DuplicateFinderLogic, project: Path
    ) -> None:
        result = logic.scan(ScanParams(source_dir=project))
        dup = next(g for g in result.groups if len(g.files) >= 2 and g.size_bytes < 100)
        assert "junk.txt" not in {f.path.name for f in dup.files}

    def test_custom_excludes_can_include_everything(
        self, logic: DuplicateFinderLogic, project: Path
    ) -> None:
        result = logic.scan(ScanParams(source_dir=project, exclude_patterns=[]))
        dup = next(g for g in result.groups if g.size_bytes < 100)
        assert len(dup.files) == 4

    def test_default_min_size_excludes_empty_files(
        self, logic: DuplicateFinderLogic, project: Path
    ) -> None:
        result = logic.scan(ScanParams(source_dir=project))
        names = {f.path.name for g in result.groups for f in g.files}
        assert "empty.txt" not in names
        assert "empty2.txt" not in names

    def test_min_size_zero_includes_empty_files(
        self, logic: DuplicateFinderLogic, project: Path
    ) -> None:
        result = logic.scan(ScanParams(source_dir=project, min_size_bytes=0))
        names = {f.path.name for g in result.groups for f in g.files}
        assert {"empty.txt", "empty2.txt"} <= names

    def test_wasted_bytes_reflects_extra_copies(
        self, logic: DuplicateFinderLogic, project: Path
    ) -> None:
        result = logic.scan(ScanParams(source_dir=project))
        dup = next(g for g in result.groups if len(g.files) == 3)
        assert dup.wasted_bytes == dup.size_bytes * 2

    def test_groups_are_sorted_by_wasted_bytes_descending(
        self, logic: DuplicateFinderLogic, project: Path
    ) -> None:
        result = logic.scan(ScanParams(source_dir=project))
        assert len(result.groups) == 2
        assert result.groups[0].wasted_bytes >= result.groups[1].wasted_bytes
        assert result.groups[0].size_bytes == 1000

    def test_total_files_scanned_reflects_filters(
        self, logic: DuplicateFinderLogic, project: Path
    ) -> None:
        result = logic.scan(ScanParams(source_dir=project))
        assert result.total_files_scanned == 7

    def test_a_missing_directory_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Not a directory"):
            ScanParams(source_dir=tmp_path / "absent")

    def test_a_directory_with_no_duplicates_scans_cleanly(
        self, logic: DuplicateFinderLogic, tmp_path: Path
    ) -> None:
        empty = tmp_path / "empty_dir"
        empty.mkdir()
        result = logic.scan(ScanParams(source_dir=empty))
        assert result.groups == []
        assert result.total_wasted_bytes == 0

    def test_a_vanished_file_is_skipped_not_fatal(
        self, logic: DuplicateFinderLogic, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file that vanishes between listing and sizing must not abort
        the whole scan."""
        real_walk = logic._walk

        def walk_with_a_ghost(root: Path, patterns: list[str]) -> list[Path]:
            return [*real_walk(root, patterns), project / "ghost.bin"]

        monkeypatch.setattr(logic, "_walk", walk_with_a_ghost)
        result = logic.scan(ScanParams(source_dir=project))
        assert result.total_files_scanned == 7

    def test_walk_skips_entries_that_raise_on_is_file(
        self, logic: DuplicateFinderLogic, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_is_file = Path.is_file

        def flaky(self: Path) -> bool:
            if self.name == "unique.txt":
                raise OSError("permission denied")
            return real_is_file(self)

        monkeypatch.setattr(Path, "is_file", flaky)
        matched = logic._walk(project, [])
        assert all(p.name != "unique.txt" for p in matched)

    def test_a_file_that_fails_to_hash_is_excluded_from_its_group(
        self, logic: DuplicateFinderLogic, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def flaky(path: Path) -> str | None:
            if path.name == "a.txt":
                return None
            return _real_hash_path(path)

        monkeypatch.setattr(
            "modules.extractors.duplicate_finder.logic._hash_path", flaky
        )
        result = logic.scan(ScanParams(source_dir=project))
        dup = next(g for g in result.groups if g.size_bytes < 100)
        names = {f.path.name for f in dup.files}
        assert "a.txt" not in names
        assert names == {"b.txt", "c.txt"}


class TestToDuplicateFile:
    def test_returns_none_for_a_vanished_file(
        self, logic: DuplicateFinderLogic, tmp_path: Path
    ) -> None:
        assert logic._to_duplicate_file(tmp_path / "absent.bin") is None


class TestHashPath:
    def test_returns_none_for_an_unreadable_file(self, tmp_path: Path) -> None:
        assert _real_hash_path(tmp_path / "absent.bin") is None

    def test_returns_a_digest_for_a_real_file(self, project: Path) -> None:
        digest = _real_hash_path(project / "a.txt")
        assert digest is not None
        assert digest == _real_hash_path(project / "b.txt")


class TestHashAll:
    def test_empty_input_returns_empty_dict(self, logic: DuplicateFinderLogic) -> None:
        assert logic._hash_all([]) == {}

    def test_below_threshold_uses_the_sequential_path(
        self, logic: DuplicateFinderLogic, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = {"parallel": False}

        def fake_parallel(paths: list[Path]) -> list[str | None]:
            called["parallel"] = True
            return [None] * len(paths)

        monkeypatch.setattr(logic, "_hash_in_parallel", fake_parallel)
        digests = logic._hash_all([project / "a.txt", project / "b.txt"])

        assert called["parallel"] is False
        assert len(digests) == 2

    def test_at_threshold_uses_the_parallel_path(
        self, logic: DuplicateFinderLogic, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = {"parallel": False}

        def fake_parallel(paths: list[Path]) -> list[str | None]:
            called["parallel"] = True
            return [None] * len(paths)

        monkeypatch.setattr(logic, "_hash_in_parallel", fake_parallel)
        paths = [tmp_path / f"f{i}.bin" for i in range(MIN_FILES_FOR_MULTIPROCESSING)]
        digests = logic._hash_all(paths)

        assert called["parallel"] is True
        assert digests == {}


class TestHashInParallel:
    def test_falls_back_when_the_pool_is_broken(
        self, logic: DuplicateFinderLogic, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _BrokenPool:
            def map(self, *_a: object, **_kw: object) -> object:
                raise BrokenProcessPool("simulated crash")

        monkeypatch.setattr(
            "modules.extractors.duplicate_finder.logic.get_process_pool",
            lambda: _BrokenPool(),
        )
        paths = [project / "a.txt", project / "b.txt"]
        results = logic._hash_in_parallel(paths)

        assert results == [_real_hash_path(p) for p in paths]

    def test_actually_uses_the_shared_process_pool(
        self, logic: DuplicateFinderLogic, project: Path
    ) -> None:
        """End-to-end smoke test with the real pool (no mocking) — this is
        the first ProcessPoolExecutor consumer in the codebase, so a real
        round trip through pickling and back is worth the process-spawn cost.

        Note: on Windows, running *only* this file with a narrow
        ``--cov=modules.extractors.duplicate_finder.logic`` (a single-module
        coverage target rather than a package like ``--cov=modules``, which
        is what ``make test`` actually uses) has been observed to break
        ProcessPoolExecutor spawning for the rest of that pytest session with
        a confusing ``Can't pickle _process_worker`` error — reproducible
        even with a throwaway function unrelated to this module. That is a
        coverage.py / Windows-spawn interaction, not a bug here; it does not
        occur under the project's actual ``make test`` invocation."""
        paths = [project / "a.txt", project / "b.txt", project / "unique.txt"]
        results = logic._hash_in_parallel(paths)

        assert results[0] == results[1]
        assert results[2] != results[0]


# ─── Resolving ────────────────────────────────────────────────────────────────


class TestChooseKeeper:
    def _group(self) -> DuplicateGroup:
        now = datetime.datetime.now(datetime.UTC)
        older = DuplicateFile(
            path=Path("/a"), size_bytes=10, modified_at=now - datetime.timedelta(days=1)
        )
        newer = DuplicateFile(path=Path("/b"), size_bytes=10, modified_at=now)
        return DuplicateGroup(content_hash="h", size_bytes=10, files=[older, newer])

    def test_newest_keeps_the_most_recently_modified(
        self, logic: DuplicateFinderLogic
    ) -> None:
        assert logic._choose_keeper(self._group(), KeepStrategy.NEWEST, None) == Path("/b")

    def test_oldest_keeps_the_least_recently_modified(
        self, logic: DuplicateFinderLogic
    ) -> None:
        assert logic._choose_keeper(self._group(), KeepStrategy.OLDEST, None) == Path("/a")

    def test_manual_keeps_the_named_file(self, logic: DuplicateFinderLogic) -> None:
        result = logic._choose_keeper(self._group(), KeepStrategy.MANUAL, Path("/a"))
        assert result == Path("/a")

    def test_manual_without_a_choice_is_invalid(
        self, logic: DuplicateFinderLogic
    ) -> None:
        assert logic._choose_keeper(self._group(), KeepStrategy.MANUAL, None) is None

    def test_manual_choice_outside_the_group_is_invalid(
        self, logic: DuplicateFinderLogic
    ) -> None:
        result = logic._choose_keeper(self._group(), KeepStrategy.MANUAL, Path("/nowhere"))
        assert result is None


class TestResolve:
    def test_newest_strategy_deletes_the_older_copies(
        self, logic: DuplicateFinderLogic, project: Path
    ) -> None:
        newest = project / "subdir" / "c.txt"
        future = time.time() + 100
        os.utime(newest, (future, future))

        scan = logic.scan(ScanParams(source_dir=project))
        group = next(g for g in scan.groups if len(g.files) == 3)
        events, result = run(
            logic, ResolveParams(groups=[group], strategy=KeepStrategy.NEWEST)
        )

        assert newest.is_file()
        assert not (project / "a.txt").exists()
        assert not (project / "b.txt").exists()
        assert result.files_deleted == 2
        assert result.recycle_batch_id is not None
        assert events[0].percent == PROGRESS_START
        assert events[-1].percent == PROGRESS_COMPLETE

    def test_oldest_strategy_deletes_the_newer_copies(
        self, logic: DuplicateFinderLogic, project: Path
    ) -> None:
        oldest = project / "a.txt"
        past = time.time() - 100
        os.utime(oldest, (past, past))

        scan = logic.scan(ScanParams(source_dir=project))
        group = next(g for g in scan.groups if len(g.files) == 3)
        _events, result = run(
            logic, ResolveParams(groups=[group], strategy=KeepStrategy.OLDEST)
        )

        assert oldest.is_file()
        assert not (project / "b.txt").exists()
        assert not (project / "subdir" / "c.txt").exists()
        assert result.files_deleted == 2

    def test_manual_strategy_keeps_the_chosen_file(
        self, logic: DuplicateFinderLogic, project: Path
    ) -> None:
        scan = logic.scan(ScanParams(source_dir=project))
        group = next(g for g in scan.groups if len(g.files) == 3)
        keep_path = project / "b.txt"

        _events, result = run(
            logic,
            ResolveParams(
                groups=[group],
                strategy=KeepStrategy.MANUAL,
                manual_keep={group.content_hash: keep_path},
            ),
        )

        assert keep_path.is_file()
        assert not (project / "a.txt").exists()
        assert result.files_deleted == 2

    def test_a_group_with_no_valid_manual_choice_is_skipped_with_a_warning(
        self, logic: DuplicateFinderLogic, project: Path
    ) -> None:
        scan = logic.scan(ScanParams(source_dir=project))
        dup_group = next(g for g in scan.groups if len(g.files) == 3)
        big_group = next(g for g in scan.groups if g.size_bytes == 1000)

        _events, result = run(
            logic,
            ResolveParams(
                groups=[dup_group, big_group],
                strategy=KeepStrategy.MANUAL,
                manual_keep={big_group.content_hash: big_group.files[0].path},
            ),
        )

        assert result.files_deleted == 1
        assert any("no valid file to keep" in w for w in result.warnings)

    def test_a_group_with_only_one_file_is_skipped(
        self, logic: DuplicateFinderLogic, project: Path
    ) -> None:
        lone_path = project / "unique.txt"
        lone = DuplicateFile(
            path=lone_path,
            size_bytes=lone_path.stat().st_size,
            modified_at=datetime.datetime.now(datetime.UTC),
        )
        group = DuplicateGroup(content_hash="lonely", size_bytes=lone.size_bytes, files=[lone])

        with pytest.raises(ValueError, match="Nothing to delete"):
            run(logic, ResolveParams(groups=[group], strategy=KeepStrategy.NEWEST))

    def test_no_groups_raises(self, logic: DuplicateFinderLogic) -> None:
        with pytest.raises(ValueError, match="Nothing to delete"):
            run(logic, ResolveParams(groups=[], strategy=KeepStrategy.NEWEST))


# ─── EventBus wiring ──────────────────────────────────────────────────────────


async def test_register_and_unregister_round_trip(logic: DuplicateFinderLogic) -> None:
    await logic.register()
    assert logic._on_scan in event_bus._subscribers[EVENT_SCAN]
    assert logic._on_execute in event_bus._subscribers[EVENT_EXECUTE]

    await logic.unregister()
    assert logic._on_scan not in event_bus._subscribers[EVENT_SCAN]
    assert logic._on_execute not in event_bus._subscribers[EVENT_EXECUTE]


async def test_bad_scan_payload_is_ignored(logic: DuplicateFinderLogic) -> None:
    await logic._on_scan(object())


async def test_bad_execute_payload_is_ignored(logic: DuplicateFinderLogic) -> None:
    await logic._on_execute(object())


async def test_scan_publishes_scanned_event(
    logic: DuplicateFinderLogic, project: Path
) -> None:
    received: list[object] = []

    async def capture(payload: object) -> None:
        received.append(payload)

    event_bus.subscribe(EVENT_SCANNED, capture)
    try:
        await logic._on_scan(ScanParams(source_dir=project))
    finally:
        event_bus.unsubscribe(EVENT_SCANNED, capture)

    assert len(received) == 1
    assert isinstance(received[0], ScanResult)


async def test_scan_failure_publishes_error_event(
    logic: DuplicateFinderLogic, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[str] = []

    async def capture(payload: object) -> None:
        received.append(str(payload))

    def boom(_params: ScanParams, _on_progress: object = None) -> ScanResult:
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(logic, "scan", boom)
    event_bus.subscribe(EVENT_ERROR, capture)
    try:
        await logic._on_scan(ScanParams(source_dir=project))
    finally:
        event_bus.unsubscribe(EVENT_ERROR, capture)

    assert received == ["scan exploded"]


async def test_execute_publishes_done_event(
    logic: DuplicateFinderLogic, project: Path
) -> None:
    from modules.extractors.duplicate_finder.constants import EVENT_DONE
    from modules.extractors.duplicate_finder.models import ResolveResult

    scan = logic.scan(ScanParams(source_dir=project))
    group = next(g for g in scan.groups if len(g.files) == 3)
    received: list[object] = []

    async def capture(payload: object) -> None:
        received.append(payload)

    event_bus.subscribe(EVENT_DONE, capture)
    try:
        await logic._on_execute(
            ResolveParams(groups=[group], strategy=KeepStrategy.NEWEST)
        )
    finally:
        event_bus.unsubscribe(EVENT_DONE, capture)

    assert len(received) == 1
    assert isinstance(received[0], ResolveResult)


async def test_execute_failure_publishes_error_event(
    logic: DuplicateFinderLogic,
) -> None:
    received: list[str] = []

    async def capture(payload: object) -> None:
        received.append(str(payload))

    event_bus.subscribe(EVENT_ERROR, capture)
    try:
        await logic._on_execute(ResolveParams(groups=[], strategy=KeepStrategy.NEWEST))
    finally:
        event_bus.unsubscribe(EVENT_ERROR, capture)

    assert len(received) == 1
    assert "Nothing to delete" in received[0]


# ─── Byte verification before deletion (§3.10c) ───────────────────────────────


class TestContentVerification:
    """Grouping uses a fast 128-bit hash; deletion demands certainty.

    xxh3_128 is chosen for speed, not collision resistance, and a file can also
    change between the scan and the resolve. Both cases must leave the file on
    disk rather than silently deleting something that is not a duplicate.
    """

    def test_a_file_whose_content_diverged_is_not_deleted(
        self, logic: DuplicateFinderLogic, tmp_path: Path
    ) -> None:
        keeper = tmp_path / "keep.bin"
        other = tmp_path / "other.bin"
        keeper.write_bytes(b"A" * 64)
        other.write_bytes(b"B" * 64)  # same size, different bytes

        group = DuplicateGroup(
            content_hash="deadbeef" * 4,
            size_bytes=64,
            files=[
                DuplicateFile(path=keeper, size_bytes=64, modified_at=_utc(2)),
                DuplicateFile(path=other, size_bytes=64, modified_at=_utc(1)),
            ],
        )

        to_delete, warnings = logic._plan_deletions(
            ResolveParams(groups=[group], strategy=KeepStrategy.NEWEST)
        )

        assert to_delete == []
        assert len(warnings) == 1
        assert "no longer matches" in warnings[0]

    def test_genuine_duplicates_are_still_deleted(
        self, logic: DuplicateFinderLogic, tmp_path: Path
    ) -> None:
        keeper = tmp_path / "keep.bin"
        copy = tmp_path / "copy.bin"
        keeper.write_bytes(b"identical payload")
        copy.write_bytes(b"identical payload")

        group = DuplicateGroup(
            content_hash="cafebabe" * 4,
            size_bytes=17,
            files=[
                DuplicateFile(path=keeper, size_bytes=17, modified_at=_utc(2)),
                DuplicateFile(path=copy, size_bytes=17, modified_at=_utc(1)),
            ],
        )

        to_delete, warnings = logic._plan_deletions(
            ResolveParams(groups=[group], strategy=KeepStrategy.NEWEST)
        )

        assert to_delete == [copy]
        assert warnings == []

    def test_an_unreadable_candidate_is_left_alone(
        self, logic: DuplicateFinderLogic, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        keeper = tmp_path / "keep.bin"
        copy = tmp_path / "copy.bin"
        keeper.write_bytes(b"same")
        copy.write_bytes(b"same")

        def refuse(*_a: object, **_kw: object) -> bool:
            raise PermissionError("locked by another process")

        monkeypatch.setattr(dup_logic.filecmp, "cmp", refuse)

        group = DuplicateGroup(
            content_hash="f00d" * 8,
            size_bytes=4,
            files=[
                DuplicateFile(path=keeper, size_bytes=4, modified_at=_utc(2)),
                DuplicateFile(path=copy, size_bytes=4, modified_at=_utc(1)),
            ],
        )

        to_delete, warnings = logic._plan_deletions(
            ResolveParams(groups=[group], strategy=KeepStrategy.NEWEST)
        )

        assert to_delete == []
        assert len(warnings) == 1


def _utc(day: int) -> datetime.datetime:
    """Build a distinct UTC timestamp for ordering test fixtures."""
    return datetime.datetime(2026, 1, day, tzinfo=datetime.UTC)


class TestScanProgress:
    """A long scan must show movement, not just a spinner (rule D-08, §3.11f)."""

    def test_scan_reports_its_running_count(self, tmp_path: Path) -> None:
        for index in range(SCAN_REPORT_EVERY * 2 + 5):
            (tmp_path / f"f{index}.bin").write_bytes(bytes([index % 256]) * 8)

        seen: list[int] = []
        DuplicateFinderLogic().scan(
            ScanParams(source_dir=tmp_path, min_size_bytes=0), seen.append
        )

        assert seen, "the scan finished without reporting any progress"
        assert seen == sorted(seen), "counts must only ever climb"

    def test_scan_without_a_callback_still_works(self, tmp_path: Path) -> None:
        """The reporting hook is optional — direct calls must not need it."""
        (tmp_path / "a.bin").write_bytes(b"same")
        (tmp_path / "b.bin").write_bytes(b"same")

        result = DuplicateFinderLogic().scan(
            ScanParams(source_dir=tmp_path, min_size_bytes=0)
        )

        assert len(result.groups) == 1
