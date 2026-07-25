"""Unit tests for the bulk_renamer logic layer."""
from __future__ import annotations

import asyncio
import datetime
import errno
import os
from pathlib import Path

import pytest

from core.event_bus import event_bus
from core.models import ProgressEvent
from modules.extractors.bulk_renamer.constants import (
    EVENT_ERROR,
    EVENT_EXECUTE,
    EVENT_PREVIEW,
    EVENT_PREVIEWED,
    PROGRESS_COMPLETE,
    PROGRESS_START,
)
from modules.extractors.bulk_renamer.logic import BulkRenamerLogic
from modules.extractors.bulk_renamer.models import (
    PreviewResult,
    RenamedPair,
    RenameParams,
    RenamePreviewEntry,
    RenameResult,
    UndoParams,
)


@pytest.fixture()
def logic() -> BulkRenamerLogic:
    """Fresh logic instance with no EventBus registrations."""
    return BulkRenamerLogic()


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A small tree with matchable, unmatched, nested and excluded files."""
    root = tmp_path / "project"
    (root / "sub").mkdir(parents=True)
    (root / "node_modules").mkdir()

    (root / "report_v1.txt").write_bytes(b"one")
    (root / "report_v2.txt").write_bytes(b"two")
    (root / "notes.md").write_bytes(b"not matched")
    (root / "sub" / "nested_v1.txt").write_bytes(b"nested")
    (root / "node_modules" / "skip_v1.txt").write_bytes(b"skip")
    return root


def _params(source: Path, **kw: object) -> RenameParams:
    """Build RenameParams with the common fields filled in."""
    kw.setdefault("pattern", r"report_v(\d+)")
    kw.setdefault("replacement", r"summary_\1")
    return RenameParams(source_dir=source, **kw)  # type: ignore[arg-type]


def run(
    logic: BulkRenamerLogic, params: RenameParams | UndoParams
) -> tuple[list[ProgressEvent], RenameResult]:
    """Execute a rename or undo to completion."""

    async def drive() -> list[ProgressEvent]:
        return [event async for event in logic.execute(params)]

    events = asyncio.run(drive())
    assert logic._last_result is not None
    return events, logic._last_result


# ─── Preview ──────────────────────────────────────────────────────────────────


class TestPreview:
    def test_matches_are_renamed_with_a_backreference(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        result = logic.preview(_params(project))
        by_name = {e.original_name: e for e in result.entries}

        assert by_name["report_v1.txt"].proposed_name == "summary_1.txt"
        assert by_name["report_v2.txt"].proposed_name == "summary_2.txt"
        assert by_name["report_v1.txt"].would_rename
        assert result.matched_count == 2
        assert result.renameable_count == 2

    def test_non_matching_files_are_left_untouched(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        result = logic.preview(_params(project))
        notes = next(e for e in result.entries if e.original_name == "notes.md")

        assert notes.matched is False
        assert notes.proposed_name == "notes.md"
        assert not notes.would_rename

    def test_extension_is_always_preserved(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        result = logic.preview(
            _params(project, pattern=r"report_v1", replacement="totally_new_stem")
        )
        entry = next(e for e in result.entries if e.original_name == "report_v1.txt")
        assert entry.proposed_name == "totally_new_stem.txt"

    def test_non_recursive_by_default_ignores_subfolders(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        result = logic.preview(_params(project, pattern=r".*", replacement="x"))
        assert all("nested" not in e.original_name for e in result.entries)
        assert result.total_files == 3  # report_v1, report_v2, notes (node_modules excluded n/a)

    def test_recursive_includes_subfolders_but_excludes_defaults(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        result = logic.preview(_params(project, pattern=r".*", replacement="x", recursive=True))
        names = {e.original_name for e in result.entries}
        assert "nested_v1.txt" in names
        assert "skip_v1.txt" not in names

    def test_recursive_with_empty_excludes_includes_everything(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        result = logic.preview(
            _params(
                project, pattern=r".*", replacement="x", recursive=True, exclude_patterns=[]
            )
        )
        names = {e.original_name for e in result.entries}
        assert "skip_v1.txt" in names

    def test_counter_placeholder_increments_per_match_in_scan_order(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        result = logic.preview(
            _params(project, pattern=r"report_v\d+", replacement="file_{n:03d}")
        )
        by_name = {e.original_name: e.proposed_name for e in result.entries}
        assert by_name["report_v1.txt"] == "file_001.txt"
        assert by_name["report_v2.txt"] == "file_002.txt"

    def test_date_placeholder_substitutes_todays_date(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        result = logic.preview(
            _params(project, pattern=r"report_v1", replacement="renamed_{date}")
        )
        entry = next(e for e in result.entries if e.original_name == "report_v1.txt")
        today = datetime.date.today().isoformat()
        assert entry.proposed_name == f"renamed_{today}.txt"

    def test_an_invalid_replacement_template_raises_immediately(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        with pytest.raises(ValueError, match="Invalid replacement template"):
            logic.preview(_params(project, replacement="{bogus_placeholder}"))

    def test_a_replacement_producing_an_invalid_filename_is_flagged_unsafe(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        result = logic.preview(
            _params(project, pattern=r"report_v1", replacement="bad:name")
        )
        entry = next(e for e in result.entries if e.original_name == "report_v1.txt")
        assert entry.unsafe is True
        assert entry.reason is not None and "valid filename" in entry.reason
        assert not entry.would_rename

    def test_two_files_resolving_to_the_same_target_are_flagged_conflict(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        result = logic.preview(
            _params(project, pattern=r"report_v\d+", replacement="collision")
        )
        by_name = {e.original_name: e for e in result.entries}
        assert by_name["report_v1.txt"].conflict is False
        assert by_name["report_v2.txt"].conflict is True
        assert "report_v1.txt" in (by_name["report_v2.txt"].reason or "")

    def test_a_replacement_with_an_invalid_backreference_is_flagged_unsafe(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        """The pattern compiles fine, but ``\\2`` has no matching group — the
        error only surfaces from ``re.sub`` at substitution time."""
        result = logic.preview(
            _params(project, pattern=r"report_v(\d+)", replacement=r"\2_bad")
        )
        entry = next(e for e in result.entries if e.original_name == "report_v1.txt")
        assert entry.unsafe is True
        assert entry.reason is not None and "Invalid replacement" in entry.reason
        assert not entry.would_rename

    def test_a_target_colliding_with_an_untouched_existing_file_is_flagged_conflict(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        (project / "already_named.txt").write_bytes(b"existing")
        result = logic.preview(
            _params(project, pattern=r"report_v1", replacement="already_named")
        )
        entry = next(e for e in result.entries if e.original_name == "report_v1.txt")
        assert entry.conflict is True
        assert not entry.would_rename

    def test_renaming_to_a_different_case_of_the_same_name_is_not_a_conflict(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        """Whether or not the filesystem is case-insensitive, a case-only
        rename of a file onto itself must never be treated as a collision."""
        result = logic.preview(
            _params(project, pattern=r"^report_v1$", replacement="REPORT_V1")
        )
        entry = next(e for e in result.entries if e.original_name == "report_v1.txt")
        assert entry.conflict is False
        assert entry.would_rename

    def test_a_no_op_match_is_reported_but_not_renameable(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        result = logic.preview(
            _params(project, pattern=r"report_v1", replacement="report_v1")
        )
        entry = next(e for e in result.entries if e.original_name == "report_v1.txt")
        assert entry.matched is True
        assert entry.proposed_name == entry.original_name
        assert entry.reason == "No change"
        assert not entry.would_rename
        assert result.renameable_count == 0

    def test_a_missing_directory_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Not a directory"):
            _params(tmp_path / "absent")

    def test_an_invalid_pattern_is_rejected_at_construction(self, project: Path) -> None:
        with pytest.raises(ValueError):
            _params(project, pattern="(unclosed")

    def test_walk_skips_entries_that_raise_on_is_file(
        self, logic: BulkRenamerLogic, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_is_file = Path.is_file

        def flaky(self: Path) -> bool:
            if self.name == "report_v1.txt":
                raise OSError("permission denied")
            return real_is_file(self)

        monkeypatch.setattr(Path, "is_file", flaky)
        matched = logic._walk(project, False, [])
        assert all(p.name != "report_v1.txt" for p in matched)


# ─── Renaming ─────────────────────────────────────────────────────────────────


class TestRename:
    def test_renames_matched_clean_files_on_disk(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        result = logic.rename(_params(project))

        assert result.files_renamed == 2
        assert result.skipped_count == 0
        assert (project / "summary_1.txt").is_file()
        assert (project / "summary_2.txt").is_file()
        assert not (project / "report_v1.txt").exists()
        pairs = {p.old_path.name: p.new_path.name for p in result.renamed}
        assert pairs == {"report_v1.txt": "summary_1.txt", "report_v2.txt": "summary_2.txt"}

    def test_conflicts_are_skipped_and_reported(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        result = logic.rename(
            _params(project, pattern=r"report_v\d+", replacement="collision")
        )

        assert result.files_renamed == 1
        assert result.skipped_count == 1
        assert any("report_v2.txt" in w for w in result.warnings)
        assert (project / "collision.txt").is_file()
        assert (project / "report_v2.txt").is_file()  # left alone, not overwritten

    def test_raises_when_nothing_would_change(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        with pytest.raises(ValueError, match="Nothing to rename"):
            logic.rename(_params(project, pattern=r"nonexistent_pattern_xyz"))

    def test_raises_with_details_when_everything_conflicts(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        (project / "collision.txt").write_bytes(b"already here")
        with pytest.raises(ValueError, match="Nothing could be renamed"):
            logic.rename(
                _params(project, pattern=r"report_v\d+", replacement="collision")
            )

    def test_a_filesystem_error_during_rename_is_skipped_not_fatal(
        self, logic: BulkRenamerLogic, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_rename = Path.rename

        def flaky(self: Path, target: object) -> object:
            if self.name == "report_v1.txt":
                raise OSError("locked by another process")
            return real_rename(self, target)

        monkeypatch.setattr(Path, "rename", flaky)
        result = logic.rename(_params(project))

        assert result.files_renamed == 1
        assert result.skipped_count == 1
        assert any("report_v1.txt" in w for w in result.warnings)
        assert (project / "report_v1.txt").is_file()  # untouched
        assert (project / "summary_2.txt").is_file()


# ─── Undo ─────────────────────────────────────────────────────────────────────


class TestUndo:
    def test_reverses_a_completed_rename(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        renamed = logic.rename(_params(project))
        undone = logic.undo(UndoParams(pairs=renamed.renamed))

        assert undone.files_renamed == 2
        assert (project / "report_v1.txt").is_file()
        assert (project / "report_v2.txt").is_file()
        assert not (project / "summary_1.txt").exists()

    def test_skips_when_the_new_path_no_longer_exists(
        self, logic: BulkRenamerLogic, tmp_path: Path
    ) -> None:
        pair = RenamedPair(old_path=tmp_path / "old.txt", new_path=tmp_path / "gone.txt")
        with pytest.raises(ValueError, match="Nothing could be undone"):
            logic.undo(UndoParams(pairs=[pair]))

    def test_skips_when_a_different_file_now_occupies_the_old_name(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        renamed = logic.rename(_params(project, pattern=r"report_v1", replacement="moved"))
        pair = renamed.renamed[0]
        pair.old_path.write_bytes(b"someone recreated this")

        with pytest.raises(ValueError, match="Nothing could be undone"):
            logic.undo(UndoParams(pairs=[pair]))
        assert pair.new_path.is_file()  # the renamed file itself is untouched

    def test_no_pairs_raises(self, logic: BulkRenamerLogic) -> None:
        with pytest.raises(ValueError, match="Nothing to undo"):
            logic.undo(UndoParams(pairs=[]))

    def test_a_filesystem_error_is_skipped_not_fatal(
        self, logic: BulkRenamerLogic, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        renamed = logic.rename(_params(project))
        real_rename = Path.rename

        def flaky(self: Path, target: object) -> object:
            if self.name == "summary_1.txt":
                raise OSError("locked")
            return real_rename(self, target)

        monkeypatch.setattr(Path, "rename", flaky)
        result = logic.undo(UndoParams(pairs=renamed.renamed))

        assert result.files_renamed == 1
        assert result.skipped_count == 1
        assert (project / "summary_1.txt").is_file()
        assert (project / "report_v2.txt").is_file()


class TestSameFile:
    def test_true_for_the_identical_path(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        target = project / "report_v1.txt"
        assert logic._same_file(target, target) is True

    def test_false_when_either_path_is_missing(
        self, logic: BulkRenamerLogic, project: Path, tmp_path: Path
    ) -> None:
        assert logic._same_file(project / "report_v1.txt", tmp_path / "absent.txt") is False

    def test_false_on_os_error(
        self, logic: BulkRenamerLogic, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = project / "report_v1.txt"

        def flaky(self: Path, _other: object) -> bool:
            raise OSError("denied")

        monkeypatch.setattr(Path, "samefile", flaky)
        assert logic._same_file(target, target) is False


# ─── Execute contract ─────────────────────────────────────────────────────────


class TestExecute:
    def test_rename_params_dispatches_to_rename(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        events, result = run(logic, _params(project))
        assert result.files_renamed == 2
        assert events[0].percent == PROGRESS_START
        assert events[-1].percent == PROGRESS_COMPLETE

    def test_undo_params_dispatches_to_undo(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        renamed = logic.rename(_params(project))
        events, result = run(logic, UndoParams(pairs=renamed.renamed))
        assert result.files_renamed == 2
        assert events[0].message == "Undoing the last rename…"


# ─── EventBus wiring ──────────────────────────────────────────────────────────


async def test_register_and_unregister_round_trip(logic: BulkRenamerLogic) -> None:
    await logic.register()
    assert logic._on_preview in event_bus._subscribers[EVENT_PREVIEW]
    assert logic._on_execute in event_bus._subscribers[EVENT_EXECUTE]

    await logic.unregister()
    assert logic._on_preview not in event_bus._subscribers[EVENT_PREVIEW]
    assert logic._on_execute not in event_bus._subscribers[EVENT_EXECUTE]


async def test_bad_preview_payload_is_ignored(logic: BulkRenamerLogic) -> None:
    await logic._on_preview(object())


async def test_bad_execute_payload_is_ignored(logic: BulkRenamerLogic) -> None:
    await logic._on_execute(object())


async def test_preview_publishes_previewed_event(
    logic: BulkRenamerLogic, project: Path
) -> None:
    received: list[object] = []

    async def capture(payload: object) -> None:
        received.append(payload)

    event_bus.subscribe(EVENT_PREVIEWED, capture)
    try:
        await logic._on_preview(_params(project))
    finally:
        event_bus.unsubscribe(EVENT_PREVIEWED, capture)

    assert len(received) == 1
    assert isinstance(received[0], PreviewResult)


async def test_preview_failure_publishes_error_event(
    logic: BulkRenamerLogic, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[str] = []

    async def capture(payload: object) -> None:
        received.append(str(payload))

    def boom(_params: RenameParams) -> PreviewResult:
        raise RuntimeError("preview exploded")

    monkeypatch.setattr(logic, "preview", boom)
    event_bus.subscribe(EVENT_ERROR, capture)
    try:
        await logic._on_preview(_params(project))
    finally:
        event_bus.unsubscribe(EVENT_ERROR, capture)

    assert received == ["preview exploded"]


async def test_execute_publishes_done_event(
    logic: BulkRenamerLogic, project: Path
) -> None:
    from modules.extractors.bulk_renamer.constants import EVENT_DONE

    received: list[object] = []

    async def capture(payload: object) -> None:
        received.append(payload)

    event_bus.subscribe(EVENT_DONE, capture)
    try:
        await logic._on_execute(_params(project))
    finally:
        event_bus.unsubscribe(EVENT_DONE, capture)

    assert len(received) == 1
    assert isinstance(received[0], RenameResult)


async def test_execute_failure_publishes_error_event(
    logic: BulkRenamerLogic, project: Path
) -> None:
    received: list[str] = []

    async def capture(payload: object) -> None:
        received.append(str(payload))

    event_bus.subscribe(EVENT_ERROR, capture)
    try:
        await logic._on_execute(_params(project, pattern=r"nonexistent_pattern_xyz"))
    finally:
        event_bus.unsubscribe(EVENT_ERROR, capture)

    assert len(received) == 1
    assert "Nothing to rename" in received[0]


# ─── Executing the approved plan (P0) ─────────────────────────────────────────


class TestApprovedPlan:
    """A run applies the preview the user saw, not a fresh recomputation.

    Recomputing at run time meant anything that changed between Preview and Run
    was acted on without ever being shown — and the ``{n}`` counter silently
    renumbered every later match.
    """

    def test_a_file_created_after_the_preview_is_not_renamed(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        approved = logic.preview(_params(project))
        # The user is now looking at that preview. A build drops a new file in.
        (project / "report_v9.txt").write_bytes(b"appeared later")

        result = logic.rename(_params(project, plan=approved.entries))

        assert (project / "report_v9.txt").is_file()  # never shown, never touched
        assert not (project / "summary_9.txt").exists()
        assert result.files_renamed == 2

    def test_the_counter_matches_what_the_preview_showed(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        params = _params(project, pattern=r"report_v\d+", replacement="doc_{n}")
        approved = logic.preview(params)
        shown = {e.original_name: e.proposed_name for e in approved.entries if e.matched}
        # A new first-sorting match would shift every counter if recomputed.
        (project / "aaa_report_v0.txt").write_bytes(b"sorts first")

        logic.rename(_params(project, pattern=r"report_v\d+",
                             replacement="doc_{n}", plan=approved.entries))

        for proposed in shown.values():
            assert (project / proposed).is_file()

    def test_an_entry_deleted_after_the_preview_is_reported_not_crashed(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        approved = logic.preview(_params(project))
        (project / "report_v1.txt").unlink()

        result = logic.rename(_params(project, plan=approved.entries))

        assert result.files_renamed == 1
        assert any("no longer exists" in note for note in result.warnings)

    def test_a_target_claimed_after_the_preview_is_not_overwritten(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        approved = logic.preview(_params(project))
        # Something else takes the name the plan intended to create.
        (project / "summary_1.txt").write_bytes(b"do not lose me")

        result = logic.rename(_params(project, plan=approved.entries))

        assert (project / "summary_1.txt").read_bytes() == b"do not lose me"
        assert result.files_renamed == 1
        assert any("different file now has this name" in n for n in result.warnings)

    def test_without_a_plan_the_preview_is_computed_as_before(
        self, logic: BulkRenamerLogic, project: Path
    ) -> None:
        result = logic.rename(_params(project))

        assert result.files_renamed == 2
        assert (project / "summary_1.txt").is_file()

    def test_two_plan_entries_targeting_one_name_only_apply_once(
        self, logic: BulkRenamerLogic, tmp_path: Path
    ) -> None:
        root = tmp_path / "collide"
        root.mkdir()
        (root / "a_v1.txt").write_bytes(b"first")
        (root / "b_v1.txt").write_bytes(b"second")
        # A hand-built plan aiming both files at the same destination — the
        # preview would flag this as a conflict, so this is the belt-and-braces
        # check that the rename step refuses it too.
        plan = [
            RenamePreviewEntry(
                original_path=root / name, original_name=name,
                proposed_name="merged.txt", matched=True,
            )
            for name in ("a_v1.txt", "b_v1.txt")
        ]

        result = logic.rename(
            RenameParams(source_dir=root, pattern=r"_v\d+", replacement="", plan=plan)
        )

        assert result.files_renamed == 1
        assert (root / "merged.txt").is_file()
        assert any("already took that name" in n for n in result.warnings)


class TestNonClobberingRename:
    """``Path.rename`` replaces an existing destination silently on POSIX.

    Windows raises, so the pre-flight existence check was the only thing
    standing between a race and a destroyed file on Linux/macOS — and it is
    exactly the kind of bug that never shows up on a Windows dev box. These
    force the POSIX branch regardless of the host running the suite.
    """

    @pytest.fixture()
    def posix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Take the POSIX (hard-link) branch on any host."""
        monkeypatch.setattr(
            "modules.extractors.bulk_renamer.logic.is_windows", lambda: False
        )

    def test_the_rename_still_happens(
        self, logic: BulkRenamerLogic, project: Path, posix: None
    ) -> None:
        result = logic.rename(_params(project))

        assert result.files_renamed == 2
        assert (project / "summary_1.txt").read_bytes() == b"one"
        assert not (project / "report_v1.txt").exists()

    def test_an_existing_destination_is_never_replaced(
        self, logic: BulkRenamerLogic, tmp_path: Path, posix: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The claim must fail atomically, not merely fail on this host.

        Asserting the outcome alone would pass on Windows whatever the code
        does, because ``rename`` refuses an existing destination there — the
        very reason this bug was invisible in development. So the *mechanism*
        is asserted too: the destination has to be claimed with ``os.link``,
        which fails atomically when the name is taken, rather than with a
        ``rename`` that would replace it on POSIX.
        """
        root = tmp_path / "race"
        root.mkdir()
        source = root / "a.txt"
        source.write_bytes(b"source")
        victim = root / "b.txt"
        victim.write_bytes(b"must survive")

        linked: list[tuple[Path, Path]] = []
        real_link = os.link

        def spy(src: Path, dst: Path) -> None:
            linked.append((Path(src), Path(dst)))
            real_link(src, dst)

        monkeypatch.setattr("modules.extractors.bulk_renamer.logic.os.link", spy)

        with pytest.raises(FileExistsError):
            logic._rename_without_clobbering(source, victim)

        assert linked == [(source, victim)]  # claimed via link, not rename
        assert victim.read_bytes() == b"must survive"
        assert source.read_bytes() == b"source"

    def test_a_filesystem_without_hard_links_falls_back(
        self, logic: BulkRenamerLogic, tmp_path: Path, posix: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "nolinks"
        root.mkdir()
        source = root / "a.txt"
        source.write_bytes(b"content")

        def no_links(_src: Path, _dst: Path) -> None:
            raise OSError(errno.EPERM, "hard links not supported")

        monkeypatch.setattr("modules.extractors.bulk_renamer.logic.os.link", no_links)
        logic._rename_without_clobbering(source, root / "b.txt")

        assert (root / "b.txt").read_bytes() == b"content"
        assert not source.exists()

    def test_an_unexpected_link_error_propagates(
        self, logic: BulkRenamerLogic, tmp_path: Path, posix: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "broken"
        root.mkdir()
        source = root / "a.txt"
        source.write_bytes(b"content")

        def disk_error(_src: Path, _dst: Path) -> None:
            raise OSError(errno.EIO, "I/O error")

        monkeypatch.setattr("modules.extractors.bulk_renamer.logic.os.link", disk_error)
        with pytest.raises(OSError, match="I/O error"):
            logic._rename_without_clobbering(source, root / "b.txt")

        assert source.is_file()  # nothing lost when the cause is unknown
