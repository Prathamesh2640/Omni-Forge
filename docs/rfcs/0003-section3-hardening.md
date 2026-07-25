# RFC 0003 — Section 3 remediation: core hardening

**Status:** Accepted
**Rule affected:** A-06 (`core/` is immutable without an RFC)

## Problem

The full-project scan (`docs/audit-2026-07-23.md`) found several defects whose fixes
land in `core/`. Grouped here because they share the audit's remediation and each
touches an immutable-by-default core file:

1. **The recycle store is write-only.** `purge_expired()`, `restore_batch()` and
   `list_batches()` have no production callers — only tests. Nothing is ever freed
   (a "purge" just moves bytes into `data/recycle/` on the same disk), the 24-hour
   undow window (B-04) is unreachable from the UI, and the store grows without bound.
2. **Recycling across volumes silently copies gigabytes.** The store lives under
   `APP_ROOT/data/recycle`. When a source is on another volume (app on `D:`, caches
   on `C:`), `shutil.move` degrades to a full copy-then-delete — multi-GB, no
   progress, and it can fill the app's own drive.
3. **The sandbox is never used.** `SandboxTask`, `run_with_timeout` and
   `BaseModule.execute()` have no production call sites; every module iterates its
   own `execute()` generator directly. So B-02's 300 s timeout never applies and
   cooperative cancellation is unreachable (no module UI has a Cancel control).
4. **`registry` mis-parses `min_python_version`.** A value like `"3.11.0"` produces a
   3-tuple compared against `sys.version_info[:2]` (a 2-tuple) and always fails; a
   non-numeric value raises `ValueError` → the module is degraded. The module
   docstring also advertises a pip-import check the code never performs, and
   `_peek_module_id` swallows every error silently.
5. **`logger` binds one date at startup.** A session running past midnight keeps
   writing to the previous day's file.
6. **`sandbox.SandboxTask.cancel` uses `except (CancelledError, Exception)`** — a
   bare-except in disguise that logs without a traceback and cannot distinguish a
   normal cancel from a real failure.
7. **`permission_manager` two defects.** `WaitForSingleObject`'s default `restype`
   is signed `c_int`, so a failure returns `-1` and never matches the `WAIT_FAILED`
   (`0xFFFFFFFF`) branch (verified empirically). And elevation failures discard the
   command's captured `stderr`, so the user sees only "exited with code 1".

## Decision

### 1. Wire up the recycle store + a shell-level Recycle Bin

- `app._on_startup` calls `recycle_store.purge_expired()` so expired batches are
  actually deleted on every launch.
- Add a public `recycle_store.delete_batch(batch_id)` (thin wrapper over the existing
  private `_remove_batch_dir`) so a UI can drop a batch immediately.
- Add a **Recycle Bin** dialog to `ui/shell.py`, opened from a header button next to
  the command palette. It lists batches via `list_batches()`, shows each batch's size
  and age, and offers **Restore** (`restore_batch`) and **Delete now** (`delete_batch`).
  This is presentation over a core service — the same shape as the theme switcher and
  command palette that already live in the shell — so no feature logic leaves
  `modules/` (rule A-01 is about pillar features, not the cross-cutting undo store).

### 2. Make the recycle store volume-aware

`recycle_store.recycle_root_for(path)` returns a store root on the *same* volume as
`path` (compared by `os.stat().st_dev`): the central `DATA_DIR/recycle` when the
source shares that volume, otherwise `<source-anchor>/.omniforge-recycle`. `recycle_paths`
moves each entry into a payload directory on its own volume, so the move stays a rename.
`RecycledEntry` gains an absolute `stored_path` (superseding the batch-relative
`stored_name`); `restore_batch`, `list_batches` and `purge_expired` read it. The batch's
`manifest.json` continues to live under the central `DATA_DIR/recycle/<batch>/` and is
the index of record, so a batch is still discoverable and survives a restart. When a
cross-volume move is genuinely unavoidable, the caller's confirmation dialog surfaces the
real byte cost first (`shared.ui_components.confirm_destructive`).

### 3. Route module execution through the sandbox, add cancellation

Each module's EventBus `_on_execute` handler drives its `execute()` generator through
`core.sandbox.run_with_timeout(...)` (the existing helper) with
`DEFAULT_EXECUTION_TIMEOUT_SECONDS` and an `on_progress` callback that republishes
`EVENT_PROGRESS`. The module holds a `SandboxTask` and subscribes a new per-module
`EVENT_CANCEL` topic that calls `SandboxTask.cancel()`; each module UI gains a **Cancel**
button in its progress section. This makes B-02 (timeout) and cooperative cancellation
real without changing the EventBus contract. `BaseModule.execute()` remains the typed
contract the conformance tests assert.

### 4. `registry` parsing and honesty

Parse `min_python_version` as major.minor only (`tuple(int(x) …)[:2]`), tolerate a patch
component (`"3.11.0"`), and degrade with a clear reason on a non-numeric value instead of
raising. Correct the module docstring to describe what the code does (the pip-import check
stays an explicitly documented Phase-0 deferral, not a false promise). `_peek_module_id`
logs at debug and narrows its `except` rather than swallowing everything silently.

### 5. `logger` date rollover

Replace the plain `FileHandler` with `logging.handlers.TimedRotatingFileHandler`
(midnight rotation), so a long session rolls into the new day's file. The JSON formatter,
field allow-list and stderr fallback are unchanged.

### 6. `sandbox.SandboxTask.cancel` exception split

Handle `asyncio.CancelledError` on its own (the expected outcome, logged quietly) and any
other `Exception` separately with `exc_info` so a real teardown failure carries a traceback.

### 7. `permission_manager`

Set `kernel32.WaitForSingleObject.restype = wintypes.DWORD` so the `WAIT_FAILED` branch is
reachable. Capture and include the tail of the elevated command's `stderr` in
`ElevationResult.message` on a non-zero exit (still no secrets — this is the command's own
diagnostic output, subject to the same C-04 logging discipline: surfaced to the user, not
written to the log).

### 8. Per-client UI controllers on `BaseModule`

The Registry keeps **one module instance for the whole application**, and each
module UI stores references to the elements it built. A single shared controller
therefore meant the *last* client to render owned those references: a second
browser tab silently took over the first tab's handlers, and every re-render
(navigating away and back) subscribed another set of EventBus handlers that were
never released.

`BaseModule` gains `attach_ui(factory)` / `detach_ui(key=None)`. `build_ui()`
asks for a controller keyed by the rendering client
(`shared.ui_components.client_key`); the previous controller for that same client
is unsubscribed first, and `on_client_disconnect` releases it when the tab
closes. Modules therefore construct their UI in `build_ui()` rather than
`__init__`, and `on_load`/`on_unload` register only the logic layer.

**Boundary — deliberately not addressed.** Progress topics are global, so a run
started in one client still repaints every connected client. Scoping events per
client is an EventBus redesign, and OmniForge's target is a single native window;
what this change fixes is the reference clobbering and the subscription leak,
both of which are real in single-window use too (every re-render leaked).

## Alternatives considered

- **A dedicated `system_matrix.recycle_bin` module** instead of a shell dialog: rejected —
  the store is a core cross-cutting service, not a pillar feature; a module would have to
  reach into `core.recycle_store` anyway, and the shell already hosts equivalent
  cross-cutting UI (theme, palette).
- **Per-volume manifests** (moving the index onto each volume too): rejected — keeping the
  single central index under `DATA_DIR` preserves one discovery path and one expiry scan;
  only the payload needs to be volume-local to avoid the copy.
- **True streaming AES-GCM** for large-file crypto (a related audit item): rejected as
  over-engineering — GCM's single authentication tag makes chunked framing a format change;
  a bounded size refusal is the pragmatic fix and lives in the module, not core.
- **Routing every module through `SandboxTask` at the registry level** (a generic executor):
  rejected — modules already own their EventBus handlers and result publishing; wrapping the
  generator in each handler is a smaller, clearer change than a central dispatcher.
