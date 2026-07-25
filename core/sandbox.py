"""Execution sandbox — wraps module logic with timeout and cancellation.

Rule B-01: Every ``execute()`` runs inside the sandbox, never blocking
the NiceGUI/asyncio event loop for more than a few milliseconds.

Rule B-02: Every execution has a configurable timeout (default 300s).
The watchdog cancels the task after timeout and publishes an error event.

For CPU-heavy work, offload the blocking portion to a thread via
``asyncio.to_thread()``. The async generator itself always runs in the
event loop so it can yield ProgressEvents directly to the UI.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from core.logger import get_logger
from core.models import ProgressEvent
from shared.constants import (
    DEFAULT_EXECUTION_TIMEOUT_SECONDS,
    PROGRESS_QUEUE_POLL_SECONDS,
    WATCHDOG_STALL_SECONDS,
)

logger = get_logger(__name__)

#: Lazily-created, process-wide pool for CPU-bound work (see docs/rfcs/0001).
#: Created on first use rather than at import time so a module that never
#: needs real parallelism never pays a single worker-process spawn cost.
_process_pool: ProcessPoolExecutor | None = None


class SandboxTask:
    """Manages a single cancellable sandbox execution.

    Usage::

        task = SandboxTask()
        await task.consume(module.execute(params), on_progress=publish)
        # from another handler, while that is still running:
        task.request_cancel()
    """

    def __init__(self) -> None:
        self._consuming: asyncio.Task[Any] | None = None
        #: When the most recent ProgressEvent arrived, for the stall watchdog.
        self._last_progress: float = 0.0

    @property
    def is_running(self) -> bool:
        """True if an execution is currently in progress."""
        return self._consuming is not None and not self._consuming.done()

    async def consume(
        self,
        generator: AsyncIterator[ProgressEvent],
        on_progress: Callable[[ProgressEvent], Awaitable[None]],
        timeout: float = DEFAULT_EXECUTION_TIMEOUT_SECONDS,
    ) -> None:
        """Drive a module's ``execute()`` to completion, bounded by *timeout*.

        This is the path a module's EventBus handler uses. ``event_bus.publish``
        already runs each handler as its own asyncio task, so the handler task
        *is* the unit of work — remembering it here is all that is needed to
        make the operation cancellable, with no extra worker plumbing.

        Rule B-02: every execution is bounded. Before this existed each module
        iterated its own generator directly, so the timeout and cancellation the
        sandbox offered were never actually applied to anything (see RFC 0003).

        A stalled operation is also reported. The *timeout* bounds the whole
        run, so an operation wedged after ten seconds still tied the UI up for
        the full five minutes before anything was said. The watchdog notices
        that no ProgressEvent has arrived for
        :data:`~shared.constants.WATCHDOG_STALL_SECONDS` and logs it, so a hang
        is visible in the log while it is happening rather than only in
        hindsight. It deliberately warns rather than cancels: a long single step
        (hashing one huge file, one slow FFmpeg pass) is silent but healthy, and
        killing it would be wrong. The timeout remains the thing with teeth.

        Args:
            generator: The module's ``execute()`` async generator.
            on_progress: Awaited for each ProgressEvent — normally an EventBus
                publish.
            timeout: Maximum seconds before the operation is abandoned.

        Raises:
            TimeoutError: When the operation exceeds *timeout*.
            asyncio.CancelledError: When :meth:`request_cancel` was called.
        """
        self._consuming = asyncio.current_task()
        self._last_progress = time.monotonic()

        async def _drive() -> None:
            async for event in generator:
                self._last_progress = time.monotonic()
                await on_progress(event)

        watchdog = asyncio.create_task(self._watch_for_stall())
        try:
            await asyncio.wait_for(_drive(), timeout=timeout)
        finally:
            watchdog.cancel()
            self._consuming = None

    async def _watch_for_stall(self, stall_seconds: float | None = None) -> None:
        """Log a warning whenever progress goes quiet for too long.

        Runs alongside :meth:`consume` and is cancelled when it finishes.

        Args:
            stall_seconds: How long without a ProgressEvent counts as a stall.
                Defaults to :data:`~shared.constants.WATCHDOG_STALL_SECONDS`,
                read at call time so it can be overridden in tests.
        """
        interval = WATCHDOG_STALL_SECONDS if stall_seconds is None else stall_seconds
        try:
            while True:
                await asyncio.sleep(interval)
                silent = time.monotonic() - self._last_progress
                if silent >= interval:
                    logger.warning(
                        "sandbox.stalled — no progress for %.0fs", silent
                    )
        except asyncio.CancelledError:
            return

    def request_cancel(self) -> bool:
        """Ask the in-flight :meth:`consume` to stop.

        Returns:
            True when there was something running to cancel.
        """
        if self._consuming is None or self._consuming.done():
            return False
        self._consuming.cancel()
        logger.info("sandbox.cancel_requested")
        return True


async def run_in_thread(func: Callable[..., Any], *args: Any) -> Any:
    """Run a blocking callable in a thread pool without blocking the event loop.

    Use this inside ``execute()`` for blocking I/O — file reads, subprocess
    calls, anything that releases the GIL while it waits. For pure CPU-bound
    work that needs real parallelism across cores, get a worker pool from
    :func:`get_process_pool` instead (see docs/rfcs/0001).

    Args:
        func: Synchronous callable to execute in a thread.
        *args: Positional arguments forwarded to *func*.

    Returns:
        The return value of *func*.
    """
    return await asyncio.to_thread(func, *args)


async def run_reporting_progress(
    work: Callable[[Callable[..., None]], Any],
    publish: Callable[..., Awaitable[None]],
    poll_seconds: float = PROGRESS_QUEUE_POLL_SECONDS,
) -> Any:
    """Run blocking *work* in a thread, relaying its progress as it goes.

    *work* is handed a plain synchronous callback it can invoke from the worker
    thread as often as it likes. Each call is handed to the event loop and
    published here, so the reports reach the UI while the work is still running
    instead of arriving in one lump at the end. Nothing blocks the event loop
    (rule B-01).

    This is the pattern ``disk_heatmap`` proved out, extracted so the scan
    phases of the other modules can report progress too rather than leaving the
    user watching a spinner with no indication of movement (rule D-08).

    Every report is delivered, including the ones made just before *work*
    returns. The previous implementation drained only ``while not task.done()``
    and discarded whatever was still queued when the worker finished — which is
    exactly the "N of N" final update the user is waiting for. It also polled a
    thread-pool slot per interval; the queue handoff needs neither (RFC 0005).

    Args:
        work: Callable taking a progress callback and returning the result.
        publish: Awaited with whatever arguments *work* passed to its callback.
        poll_seconds: Obsolete and ignored — the drain is no longer polled.
            Retained so existing call sites keep working.

    Returns:
        Whatever *work* returned.
    """
    del poll_seconds  # no longer polled; see the docstring
    loop = asyncio.get_running_loop()
    updates: asyncio.Queue[tuple[Any, ...] | None] = asyncio.Queue()

    def report(*payload: Any) -> None:
        """Hand one progress report to the event loop from the worker thread."""
        loop.call_soon_threadsafe(updates.put_nowait, payload)

    task = asyncio.create_task(run_in_thread(work, report))
    # The worker makes every report() call before it returns, so those
    # call_soon_threadsafe callbacks are already queued on the loop by the time
    # this done-callback runs. The sentinel therefore always arrives last.
    task.add_done_callback(lambda _task: updates.put_nowait(None))

    while (payload := await updates.get()) is not None:
        await publish(*payload)
    return await task


def get_process_pool() -> ProcessPoolExecutor:
    """Return the shared process pool, creating it on first use.

    A single pool is shared by every CPU-bound module rather than one per
    module, so the process-spawn cost is paid once per application run, not
    once per operation. Callers already run off the event loop by the time
    they reach this (typically from inside a function passed to
    :func:`run_in_thread`), so the pool's synchronous ``.map()``/``.submit()``
    API is used directly — no ``asyncio`` bridging needed.

    The callable submitted to the pool must be picklable — a module-level
    function, not a bound method, closure or lambda — since it crosses the
    process boundary.

    Returns:
        The shared :class:`~concurrent.futures.ProcessPoolExecutor`.
    """
    global _process_pool
    if _process_pool is None:
        _process_pool = ProcessPoolExecutor()
        logger.debug("sandbox.process_pool_created")
    return _process_pool


def shutdown_process_pool() -> None:
    """Release worker processes, if any were ever started.

    Call once from the application shutdown hook. Safe to call even when
    no CPU-bound module ever ran (the pool is created lazily).
    """
    global _process_pool
    if _process_pool is not None:
        _process_pool.shutdown(wait=False, cancel_futures=True)
        _process_pool = None
        logger.debug("sandbox.process_pool_shutdown")
