"""Unit tests for core.sandbox (timeout, cancellation, thread offload)."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from core import sandbox
from core.models import ProgressEvent
from core.sandbox import (
    SandboxTask,
    get_process_pool,
    run_in_thread,
    run_reporting_progress,
    shutdown_process_pool,
)


def _add(a: int, b: int) -> int:
    """Module-level so it can be pickled across the process boundary."""
    return a + b


def _boom() -> None:
    raise RuntimeError("process failure")


async def _three_steps() -> AsyncIterator[ProgressEvent]:
    """Yield three progress events with no delay."""
    for percent in (0, 50, 100):
        yield ProgressEvent(percent=percent, message=f"step {percent}")


async def _never_finishes() -> AsyncIterator[ProgressEvent]:
    """Yield once, then hang forever — used to trigger the watchdog."""
    yield ProgressEvent(percent=0, message="starting")
    await asyncio.sleep(3600)


async def _raises() -> AsyncIterator[ProgressEvent]:
    """Yield once, then fail."""
    yield ProgressEvent(percent=0, message="starting")
    raise ValueError("logic exploded")


# ─── SandboxTask ──────────────────────────────────────────────────────────────


async def test_task_reports_not_running_before_start() -> None:
    assert SandboxTask().is_running is False


# ─── run_in_thread ────────────────────────────────────────────────────────────


async def test_run_in_thread_returns_the_callables_result() -> None:
    assert await run_in_thread(lambda a, b: a + b, 2, 3) == 5


async def test_run_in_thread_propagates_exceptions() -> None:
    def boom() -> None:
        raise RuntimeError("thread failure")

    with pytest.raises(RuntimeError, match="thread failure"):
        await run_in_thread(boom)


# ─── get_process_pool ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _cleanup_process_pool() -> None:
    """Ensure no worker process outlives its test."""
    yield
    shutdown_process_pool()


def test_the_pool_runs_a_picklable_function() -> None:
    pool = get_process_pool()
    assert pool.submit(_add, 2, 3).result() == 5


def test_the_pool_propagates_exceptions() -> None:
    pool = get_process_pool()
    with pytest.raises(RuntimeError, match="process failure"):
        pool.submit(_boom).result()


def test_the_pool_is_created_lazily_and_reused() -> None:
    assert sandbox._process_pool is None
    first_pool = get_process_pool()
    assert sandbox._process_pool is first_pool
    assert get_process_pool() is first_pool


def test_shutdown_without_a_pool_is_safe() -> None:
    shutdown_process_pool()
    assert sandbox._process_pool is None


def test_shutdown_clears_the_pool_for_the_next_call() -> None:
    get_process_pool()
    assert sandbox._process_pool is not None
    shutdown_process_pool()
    assert sandbox._process_pool is None


# ─── SandboxTask.consume — the path modules actually execute through ──────────


class TestConsume:
    """Rule B-02: a module's execute() must be bounded and cancellable.

    Before RFC 0003 every module iterated its own generator directly, so the
    sandbox's timeout and cancellation were never applied to any real work.
    """

    async def _events(self, count: int, delay: float = 0.0) -> AsyncIterator[ProgressEvent]:
        """Yield *count* progress events, optionally pausing between them."""
        for index in range(count):
            if delay:
                await asyncio.sleep(delay)
            yield ProgressEvent(percent=index, message=f"step {index}")

    async def test_every_event_is_forwarded(self) -> None:
        seen: list[ProgressEvent] = []
        task = SandboxTask()

        async def collect(event: ProgressEvent) -> None:
            seen.append(event)

        await task.consume(self._events(5), collect)

        assert [event.percent for event in seen] == [0, 1, 2, 3, 4]

    async def test_a_slow_operation_times_out(self) -> None:
        task = SandboxTask()

        async def collect(_event: ProgressEvent) -> None:
            return None

        with pytest.raises(TimeoutError):
            await task.consume(self._events(50, delay=0.05), collect, timeout=0.1)

    async def test_cancellation_stops_the_generator(self) -> None:
        seen: list[ProgressEvent] = []
        task = SandboxTask()

        async def collect(event: ProgressEvent) -> None:
            seen.append(event)

        async def driver() -> None:
            await task.consume(self._events(100, delay=0.02), collect)

        running = asyncio.create_task(driver())
        await asyncio.sleep(0.05)
        assert task.request_cancel() is True

        with pytest.raises(asyncio.CancelledError):
            await running

        # It stopped early rather than running all 100 steps.
        assert 0 < len(seen) < 100

    async def test_cancel_reports_false_when_idle(self) -> None:
        assert SandboxTask().request_cancel() is False

    async def test_is_running_tracks_the_consumption(self) -> None:
        task = SandboxTask()
        states: list[bool] = []

        async def collect(_event: ProgressEvent) -> None:
            states.append(task.is_running)

        assert task.is_running is False
        await task.consume(self._events(2), collect)
        assert states == [True, True]
        assert task.is_running is False


class TestRunReportingProgress:
    """Blocking work must be able to report while it is still running.

    Before this, a scan handed the UI a single result at the end — the user
    watched a spinner with no sign of movement (audit §3.11f).
    """

    async def test_reports_arrive_before_the_work_finishes(self) -> None:
        seen: list[int] = []

        async def publish(count: int) -> None:
            seen.append(count)

        def work(report: Any) -> str:
            for index in range(1, 4):
                report(index)
                time.sleep(0.05)
            return "finished"

        result = await run_reporting_progress(work, publish, poll_seconds=0.01)

        assert result == "finished"
        assert seen == [1, 2, 3]

    async def test_work_that_reports_nothing_still_returns(self) -> None:
        async def publish(*_a: Any) -> None:
            raise AssertionError("should not be called")

        result = await run_reporting_progress(lambda _r: 42, publish, poll_seconds=0.01)

        assert result == 42

    async def test_an_exception_in_the_work_propagates(self) -> None:
        async def publish(*_a: Any) -> None:
            return None

        def boom(_report: Any) -> None:
            raise RuntimeError("scan exploded")

        with pytest.raises(RuntimeError, match="scan exploded"):
            await run_reporting_progress(boom, publish, poll_seconds=0.01)


class TestReportingProgressDelivery:
    """Every report must arrive, including the ones made just before the end.

    The drain used to run ``while not task.done()`` and discard whatever was
    still queued when the worker returned — which is exactly the final "N of N"
    update the user waits for (RFC 0005).
    """

    async def test_the_final_report_is_not_dropped(self) -> None:
        published: list[tuple[Any, ...]] = []

        async def publish(*payload: Any) -> None:
            published.append(payload)

        def work(report: Any) -> str:
            for index in range(1, 6):
                report(index, f"step {index}")
            return "finished"

        result = await run_reporting_progress(work, publish)

        assert result == "finished"
        assert published == [(index, f"step {index}") for index in range(1, 6)]

    async def test_a_report_made_immediately_before_returning_arrives(self) -> None:
        published: list[tuple[Any, ...]] = []

        async def publish(*payload: Any) -> None:
            published.append(payload)

        def work(report: Any) -> None:
            report("only", "report")

        await run_reporting_progress(work, publish)

        assert published == [("only", "report")]

    async def test_a_worker_that_reports_nothing_still_returns(self) -> None:
        async def publish(*_payload: Any) -> None:
            raise AssertionError("nothing should be published")

        assert await run_reporting_progress(lambda _r: 7, publish) == 7


class TestStallWatchdog:
    """Rule B-02's stall detection — the constant existed but nothing used it.

    A run wedged ten seconds in still occupied the UI for the full five-minute
    timeout before anything was reported. The watchdog says so while it is
    happening.
    """

    async def _one_slow_event(self, delay: float) -> AsyncIterator[ProgressEvent]:
        """Yield once, go quiet for *delay*, then finish."""
        yield ProgressEvent(percent=0, message="starting")
        await asyncio.sleep(delay)
        yield ProgressEvent(percent=100, message="done")

    async def test_a_silent_run_is_reported(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sandbox, "WATCHDOG_STALL_SECONDS", 0.05)
        task = SandboxTask()

        async def collect(_event: ProgressEvent) -> None:
            return None

        with caplog.at_level(logging.WARNING, logger="omniforge.core.sandbox"):
            await task.consume(self._one_slow_event(0.25), collect, timeout=5)

        assert any("sandbox.stalled" in r.message for r in caplog.records)

    async def test_a_healthy_run_is_not_reported(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sandbox, "WATCHDOG_STALL_SECONDS", 5.0)
        task = SandboxTask()

        async def collect(_event: ProgressEvent) -> None:
            return None

        with caplog.at_level(logging.WARNING, logger="omniforge.core.sandbox"):
            await task.consume(self._one_slow_event(0.0), collect, timeout=5)

        assert not [r for r in caplog.records if "sandbox.stalled" in r.message]

    async def test_the_watchdog_stops_with_the_operation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It must not outlive the run it was watching."""
        monkeypatch.setattr(sandbox, "WATCHDOG_STALL_SECONDS", 0.05)
        task = SandboxTask()

        async def collect(_event: ProgressEvent) -> None:
            return None

        before = len(asyncio.all_tasks())
        await task.consume(self._one_slow_event(0.0), collect, timeout=5)
        await asyncio.sleep(0.15)

        assert len(asyncio.all_tasks()) <= before
