# RFC 0006 — Stall watchdog, recycle progress, and dead-API removal

**Status:** Accepted
**Rule affected:** A-06 (`core/` is immutable without an RFC)

## Problem

The P2/P3 tier of the audit left four things in `core/`.

### 1. The stall watchdog rule B-02 describes was never implemented

`shared/constants.WATCHDOG_STALL_SECONDS` existed and was referenced by nothing.
Only the 300-second total timeout applied, so an operation wedged ten seconds in
occupied the UI for the remaining 4m50s with no indication that anything was
wrong — and left no trace afterwards explaining the delay.

### 2. `recycle_paths` reported nothing while it worked

It is the slowest operation in the application: a cache purge or a duplicate
resolve can move thousands of files, and on a cross-volume move each one is a
full copy. Both callers wrapped it in a single ``run_in_thread`` and emitted one
ProgressEvent before and one after, so the bar sat frozen for the whole move
(rule D-08). Measured on `bulk_renamer` for comparison: 40 files produced
exactly **two** events.

### 3. Half of `core/sandbox.py` was unreachable

`run_with_timeout`, `SandboxTask.start`, `SandboxTask.cancel` and the `_task`
bookkeeping they shared had no production caller — every module drives
`consume()` / `request_cancel()` instead. They were exercised only by their own
tests, which made the suite report confidence in code the application never
runs, and left two ways to do the same thing for the next person to choose
between.

`core/models.ExecuteRequest` and `CancelRequest` were dead in the same way:
never constructed anywhere. Each module publishes its own typed params to its
own topic, which is strictly better — the handler gets a real model to validate
rather than a `dict`-carrying envelope.

### 4. Module discovery did not survive a PyInstaller freeze

`registry._MODULES_ROOT` was `Path(__file__).parent.parent / "modules"`. Under a
freeze `__file__` points inside the read-only `_MEI` bundle, whose layout does
not match the source tree, so discovery would find nothing and the application
would start with **no modules at all** — while `shared/constants.APP_ROOT` had
already solved exactly this problem for every write location (RFC 0002).

## Decision

### 1. Add a stall watchdog to `SandboxTask.consume`

A task started alongside the run logs `sandbox.stalled` whenever
`WATCHDOG_STALL_SECONDS` passes with no ProgressEvent, and is cancelled when the
run ends.

It **warns rather than cancels**, deliberately. A long silent step can be
perfectly healthy — hashing one very large file, a single slow FFmpeg pass — and
killing it would turn a working operation into a failure. The timeout stays the
mechanism with teeth; the watchdog exists so the log says *"nothing has moved
for 30 seconds"* while it is happening rather than only in hindsight.

### 2. Give `recycle_paths` an optional progress callback

`recycle_paths(paths, on_progress=None)` reports `(done, total)` per entry.
`paths` is materialised into a list first so `total` is a real denominator.
`cache_purger` and `duplicate_finder` drive it through
`run_reporting_progress`, so the move now reports continuously.

### 3. Delete the unreachable API

`run_with_timeout`, `SandboxTask.start`, `SandboxTask.cancel`, the `_task`
field, `ExecuteRequest` and `CancelRequest` are removed, along with the tests
that only covered them. `is_running` now reflects the one execution path that
exists. `core/models.py` documents why there is no generic execute envelope, so
the absence reads as a decision rather than an oversight.

### 4. Anchor `_MODULES_ROOT` to `APP_ROOT`

One line, and it makes discovery frozen-build-correct for the same reason and by
the same mechanism as every write path.

## Alternatives considered

- **Have the watchdog cancel the operation.** Rejected — it cannot distinguish a
  hang from a legitimately long step, and a false positive destroys work the
  user was waiting for. Logging is the honest amount of confidence to act on.
- **Push progress into `recycle_paths` unconditionally** (always publish to the
  EventBus from inside `core`). Rejected — `core/` must not know which topic a
  module publishes on (rule A-03); an optional callback keeps that inversion.
- **Keep the dead sandbox API "in case it is useful".** Rejected — it is
  recoverable from git, and leaving two execution paths in the file it lives in
  is how the timeout ended up applying to nothing in the first place (RFC 0003).
- **Keep `BaseModule.execute()`**, which is also never called by the registry.
  *Kept deliberately*: unlike the removals above it is the abstract contract
  every module implements and the documented shape of module work (rules A-02,
  B-01, D-08). Removing it would delete the interface rather than dead code.
