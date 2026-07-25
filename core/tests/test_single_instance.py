"""Unit tests for core.single_instance (stale-lock recovery)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import psutil
import pytest

from core import single_instance
from core.single_instance import _owner_record, acquire, current_pid, release
from shared.constants import LOCK_READ_ATTEMPTS, LOCK_READ_RETRY_SECONDS


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    """Path to a lock file inside a throwaway directory."""
    return tmp_path / "data" / "omniforge.lock"


def _dead_pid() -> int:
    """Return a PID that is guaranteed not to be running."""
    for candidate in range(999_000, 1_000_000):
        if not psutil.pid_exists(candidate):
            return candidate
    raise AssertionError("no free PID available")


class TestAcquire:
    def test_claims_a_free_lock(self, lock_path: Path) -> None:
        assert acquire(lock_path) is True

    def test_creates_the_parent_directory(self, lock_path: Path) -> None:
        acquire(lock_path)
        assert lock_path.exists()

    def test_records_the_owning_process(self, lock_path: Path) -> None:
        acquire(lock_path)
        record = json.loads(lock_path.read_text(encoding="utf-8"))

        assert record["pid"] == os.getpid()
        assert record["started_at"] == psutil.Process().create_time()

    def test_refuses_when_a_live_instance_holds_the_lock(self, lock_path: Path) -> None:
        """The current process is alive, so its own lock must block a second start."""
        acquire(lock_path)
        assert acquire(lock_path) is False

    def test_reclaims_a_lock_left_by_a_dead_process(self, lock_path: Path) -> None:
        """A crash must never bar the next launch."""
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text(
            json.dumps({"pid": _dead_pid(), "started_at": 1.0}), encoding="utf-8"
        )

        assert acquire(lock_path) is True
        assert json.loads(lock_path.read_text(encoding="utf-8"))["pid"] == os.getpid()

    def test_reclaims_a_lock_whose_pid_was_recycled(self, lock_path: Path) -> None:
        """A live PID with a different start time is a different process."""
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text(
            json.dumps({"pid": os.getpid(), "started_at": 1.0}), encoding="utf-8"
        )

        assert acquire(lock_path) is True

    def test_reclaims_an_empty_lock_file(self, lock_path: Path) -> None:
        """Locks written by the pre-PID build carried no owner information."""
        lock_path.parent.mkdir(parents=True)
        lock_path.touch()

        assert acquire(lock_path) is True

    def test_reclaims_a_malformed_lock_file(self, lock_path: Path) -> None:
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text("{ not json", encoding="utf-8")

        assert acquire(lock_path) is True

    def test_reclaims_a_lock_holding_a_json_non_object(self, lock_path: Path) -> None:
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text("[1, 2, 3]", encoding="utf-8")

        assert acquire(lock_path) is True

    @pytest.mark.parametrize(
        "record",
        [{"started_at": 1.0}, {"pid": "abc", "started_at": 1.0}, {"pid": 1}],
        ids=["no-pid", "non-numeric-pid", "no-start-time"],
    )
    def test_reclaims_a_lock_with_an_incomplete_record(
        self, lock_path: Path, record: dict[str, object]
    ) -> None:
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text(json.dumps(record), encoding="utf-8")

        assert acquire(lock_path) is True

    def test_an_inaccessible_owner_is_treated_as_gone(
        self, lock_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A protected process cannot be confirmed as ours, so we reclaim."""
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text(
            json.dumps({"pid": os.getpid(), "started_at": 1.0}), encoding="utf-8"
        )

        real_process = psutil.Process

        def deny(pid: int | None = None) -> psutil.Process:
            # Only the ownership probe is denied; describing ourselves for the
            # new record must still work.
            if pid is None:
                return real_process()
            raise psutil.AccessDenied(pid=pid)

        monkeypatch.setattr(single_instance.psutil, "Process", deny)

        assert acquire(lock_path) is True

    def test_an_unwritable_location_fails_closed(
        self, lock_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Failing to write the lock must refuse the launch, not proceed blind."""

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise OSError("read-only filesystem")

        # The lock is now claimed via an atomic os.open exclusive-create.
        monkeypatch.setattr(single_instance.os, "open", refuse)

        assert acquire(lock_path) is False


class TestRelease:
    def test_removes_the_lock(self, lock_path: Path) -> None:
        acquire(lock_path)
        release(lock_path)
        assert not lock_path.exists()

    def test_releasing_an_absent_lock_is_safe(self, lock_path: Path) -> None:
        release(lock_path)

    def test_a_released_lock_can_be_reacquired(self, lock_path: Path) -> None:
        acquire(lock_path)
        release(lock_path)
        assert acquire(lock_path) is True

    def test_a_failing_unlink_is_logged_not_raised(
        self, lock_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        acquire(lock_path)

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise OSError("file is locked")

        monkeypatch.setattr(Path, "unlink", refuse)
        release(lock_path)


def test_current_pid_reports_this_process() -> None:
    assert current_pid() == os.getpid()


class TestWriteWindow:
    """The claim and the owner record are two steps, so a lock can be caught
    momentarily empty (RFC 0005).

    Reading it once and finding nothing would conclude "no live owner", delete
    the winner's lock and let a second instance start — the exact outcome the
    lock exists to prevent.
    """

    def test_a_lock_being_written_is_respected_once_the_record_lands(
        self, lock_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock_path.parent.mkdir(parents=True)
        lock_path.touch()  # created by the winner, record not yet written

        own = _owner_record()
        reads = {"count": 0}

        def racing_read(_path: Path) -> dict[str, float | int] | None:
            """Empty on the first look, then the winner's record appears."""
            reads["count"] += 1
            if reads["count"] == 1:
                return None
            return own

        monkeypatch.setattr(single_instance, "_read_record", racing_read)

        assert acquire(lock_path) is False
        assert reads["count"] > 1  # it looked again rather than assuming stale
        assert lock_path.exists()  # the winner's lock was not deleted

    def test_a_permanently_empty_lock_is_still_reclaimed(
        self, lock_path: Path
    ) -> None:
        """A crash leaves an empty lock forever; that must not bar a relaunch."""
        lock_path.parent.mkdir(parents=True)
        lock_path.touch()

        assert acquire(lock_path) is True

    def test_the_retry_window_is_bounded(self, lock_path: Path) -> None:
        """Reclaiming a stale lock must not stall startup indefinitely."""
        lock_path.parent.mkdir(parents=True)
        lock_path.touch()

        started = time.monotonic()
        acquire(lock_path)

        assert time.monotonic() - started < LOCK_READ_ATTEMPTS * LOCK_READ_RETRY_SECONDS + 1
