# RFC 0001 — ProcessPoolExecutor helper in core/sandbox.py

**Status:** Accepted
**Rule affected:** A-06 (`core/` is immutable without an RFC)

## Problem

`core/sandbox.py` offloads blocking work via `asyncio.to_thread` (`run_in_thread`),
which is correct for I/O-bound work but does nothing for CPU-bound work — the
GIL means N threads hashing files in parallel run no faster than one. Phase 0
tracked this as a deferred item: *"ProcessPoolExecutor for CPU-heavy tasks
(deferred — no CPU-bound module yet)"*.

The Duplicate Detective module (`modules/extractors/duplicate_finder/`) is
that module: its SLA is 1,000,000 files hashed in under 60 seconds, which
requires real parallelism across CPU cores.

## Decision

Add `get_process_pool()` alongside `run_in_thread`:

```python
def get_process_pool() -> ProcessPoolExecutor:
    global _process_pool
    if _process_pool is None:
        _process_pool = ProcessPoolExecutor()
    return _process_pool
```

A single module-level `ProcessPoolExecutor` is created lazily and reused
across calls (creating one per call would pay process-spawn cost every time).
`shutdown_process_pool()` is called from `app.py`'s existing shutdown hook so
worker processes do not outlive the main process.

Unlike `run_in_thread`, this is a plain synchronous accessor, not an
`async def` wrapper. Every current caller is a synchronous `scan()`-style
method that is itself already off the event loop by the time it runs (invoked
via `run_in_thread` from an async EventBus handler, matching the existing
convention in `file_filter.scan()` and `llm_packager`'s scanner). Those
callers use the executor's own synchronous `.map()`/`.submit()` directly —
adding an `async` wrapper on top would only add an unused layer with no
caller that needs it.

## Constraints this places on callers

- The target `func` must be a module-level function (or otherwise picklable) —
  bound methods, closures and lambdas cannot cross the process boundary.
- `app.py` already guards its entry point with `if __name__ == "__main__":`,
  which is required for `spawn`-based platforms (Windows, macOS) so a child
  process does not re-execute the whole application. `multiprocessing.freeze_support()`
  is added to the same guard for PyInstaller-frozen builds (Phase 8).

## Alternatives considered

- **`multiprocessing.Pool` owned per-module**: rejected — every CPU-bound
  module would pay process-spawn startup cost on every run instead of sharing
  one warm pool, and cleanup would be duplicated per module instead of once
  in `core`.
- **Leave it thread-only and accept the SLA miss**: rejected — hashing is pure
  CPU work; threads cannot parallelize it under the GIL.
