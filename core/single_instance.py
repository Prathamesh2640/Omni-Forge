"""Single-instance lock guarding port 8765.

A plain "does the lock file exist?" check is not enough: a crash, a force
kill, or a power loss leaves the file behind and locks the user out of their
own application permanently. The lock therefore records *which* process owns
it, and a lock whose owner is gone is reclaimed automatically.

The owner is identified by PID **and** process start time. PIDs are recycled
by the OS, so the start time is what distinguishes "our instance is still
running" from "an unrelated process now holds that number".
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import psutil

from core.logger import get_logger
from shared.constants import LOCK_READ_ATTEMPTS, LOCK_READ_RETRY_SECONDS

logger = get_logger(__name__)


def _owner_record() -> dict[str, float | int]:
    """Describe the current process for the lock file.

    Returns:
        A mapping of ``pid`` and ``started_at`` (process creation time).
    """
    process = psutil.Process()
    return {"pid": process.pid, "started_at": process.create_time()}


def _is_owner_alive(record: dict[str, float | int]) -> bool:
    """Return True when the process described by *record* is still running.

    Args:
        record: A previously written owner record.

    Returns:
        True only when a live process matches both the PID and its recorded
        start time. A recycled PID reports False.
    """
    try:
        pid = int(record["pid"])
        started_at = float(record["started_at"])
    except (KeyError, TypeError, ValueError):
        # An unreadable record cannot prove an instance is running.
        return False

    try:
        return bool(psutil.Process(pid).create_time() == started_at)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def acquire(lock_path: Path) -> bool:
    """Claim the single-instance lock.

    An existing lock owned by a live process is respected. A lock left behind
    by a dead process is reclaimed, so a crash never bars the next launch.

    Args:
        lock_path: Path to the lock file.

    Returns:
        True when the lock was claimed and the application may start;
        False when another instance is genuinely running.
    """
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("lock.acquire_failed — path=%s", lock_path, exc_info=exc)
        return False

    payload = json.dumps(_owner_record()).encode("utf-8")

    # Atomic exclusive create (O_CREAT | O_EXCL) is the whole point: only one of
    # several processes racing to launch can create the file, so the check and
    # the claim are a single indivisible step — no TOCTOU window. Two attempts:
    # the first may lose to an existing lock; if that lock proves stale we
    # remove it and try once more (preserving crash-recovery), then give up so
    # a live owner that wins the re-create is respected.
    for _attempt in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            record = _await_record(lock_path)
            if record is not None and _is_owner_alive(record):
                logger.warning("lock.held_by_running_instance — pid=%s", record["pid"])
                return False
            logger.info("lock.reclaiming_stale — path=%s", lock_path)
            try:
                lock_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.error("lock.reclaim_failed — path=%s", lock_path, exc_info=exc)
                return False
            continue
        except OSError as exc:
            logger.error("lock.acquire_failed — path=%s", lock_path, exc_info=exc)
            return False

        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return True

    logger.warning("lock.lost_reclaim_race — path=%s", lock_path)
    return False


def release(lock_path: Path) -> None:
    """Remove the lock file on clean shutdown.

    Args:
        lock_path: Path to the lock file.
    """
    try:
        lock_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.error("lock.release_failed — path=%s", lock_path, exc_info=exc)


def _await_record(lock_path: Path) -> dict[str, float | int] | None:
    """Read the owner record, tolerating a lock still being written.

    The exclusive create and the write of the record are two steps, so a lock
    file can legitimately be empty for the microseconds between them. Reading
    once and finding nothing would conclude "no live owner" and delete the
    winner's lock — letting both instances run, which is the exact scenario the
    lock exists to prevent (RFC 0005).

    Retrying distinguishes the two cases without a timestamp: a live winner
    finishes writing almost immediately, while a lock orphaned by a crash stays
    empty for the whole window and is still correctly reclaimed.

    Args:
        lock_path: Path to the lock file.

    Returns:
        The parsed record, or ``None`` when the lock is genuinely empty or
        malformed after every attempt.
    """
    for attempt in range(LOCK_READ_ATTEMPTS):
        record = _read_record(lock_path)
        if record is not None:
            return record
        if attempt + 1 < LOCK_READ_ATTEMPTS:
            time.sleep(LOCK_READ_RETRY_SECONDS)
    return None


def _read_record(lock_path: Path) -> dict[str, float | int] | None:
    """Parse the owner record from *lock_path*.

    Args:
        lock_path: Path to the lock file.

    Returns:
        The parsed record, or ``None`` when the file is empty, malformed, or
        written by an older build that stored no owner information.
    """
    try:
        content = lock_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("lock.unreadable — path=%s reason=%s", lock_path, exc)
        return None

    if not content:
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("lock.malformed — path=%s", lock_path)
        return None
    return parsed if isinstance(parsed, dict) else None


def current_pid() -> int:
    """Return the current process ID.

    Returns:
        The PID of this process.
    """
    return os.getpid()
