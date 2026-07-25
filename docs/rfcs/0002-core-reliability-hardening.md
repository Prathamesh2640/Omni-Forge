# RFC 0002 — Core reliability hardening

**Status:** Accepted
**Rule affected:** A-06 (`core/` is immutable without an RFC)

## Problem

An audit of the phase 0–3 code surfaced three correctness gaps in `core/`, all
touching the project's "works without functional error on any OS, no freeze,
no leak" bar:

1. **Real-time modules never pause on navigation.** A module's `on_load()` and
   `on_unload()` bracket the *application* lifetime — `on_unload()` only fires
   at shutdown (`registry.unload_all()`). The shell (`ui/shell.py`) switches
   modules by clearing the content column and building the next module's UI; it
   has no way to tell the outgoing module it is no longer visible. So
   `live_monitor`, once started, keeps its 1 s / 2 s psutil polling tasks
   running forever after the user navigates elsewhere, publishing snapshots to
   an EventBus handler whose UI elements have already been deleted (the errors
   are swallowed by EventBus handler-isolation, but the CPU cost and log noise
   are real, and every future real-time module — port monitor, log streamer —
   would inherit the same leak).

2. **Single-instance lock is not acquired atomically.**
   `core/single_instance.py.acquire()` does an `exists()` check, then a
   separate `write_text()`. Two processes launched at nearly the same moment
   (a double-click, a shortcut fired twice) can both observe "no live owner"
   and both write the lock — defeating the single-instance guarantee.

3. **Windows elevation can report a stale error code.**
   `core/permission_manager.py._elevate_windows()` calls
   `ctypes.get_last_error()` after `ShellExecuteExW`, but `ctypes.windll` handles
   are not created with `use_last_error=True`, so the value read back is the
   thread's last error from *some other* call, not this one. A dismissed UAC
   prompt can therefore be misclassified as `FAILED` instead of `CANCELLED`.

## Decision

### 1. Add a concrete `on_deactivate()` lifecycle hook to `BaseModule`

```python
async def on_deactivate(self) -> None:
    """Pause real-time work when the user navigates away. Default: no-op."""
```

It is **concrete, not abstract** — existing modules need no change, so this is
backwards-compatible with every module already shipped. Only modules that own
background polling override it. The module stays loaded (subscriptions intact);
`on_deactivate()` stops *work*, `on_unload()` remains shutdown teardown.

The shell calls it on the *outgoing* module during navigation. `_activate_module`
is synchronous and is reached from a sync palette callback
(`command_palette.on_activate: Callable[[str], None]`) and from sync click
lambdas; converting the whole navigation path to `async` would cascade through
`core/command_palette.py` and its call sites. Instead the shell schedules the
coroutine on the running loop (`asyncio.create_task`) with an error guard. The
hook is designed to return quickly, so fire-and-forget is sufficient: it shrinks
the leak from "unbounded" to "at most one in-flight poll iteration" while
keeping the change surface minimal.

`live_monitor` overrides it to cancel its polling tasks (a new public
`LiveMonitorLogic.pause()` that reuses the existing `_cancel_task()`). Returning
to the module re-renders in the idle state; the user presses Start again.

### 2. Acquire the single-instance lock atomically

Replace the check-then-write with an atomic create (`open(..., "x")` /
`O_CREAT | O_EXCL`). On `FileExistsError`, read the existing record: reclaim and
retry once if the owner is dead (stale lock from a crash), refuse if it is
alive. This preserves the existing stale-lock reclamation while closing the
race — only one process can win the exclusive create.

### 2b. Anchor the TinyDB path to the app root

`core/storage.py` hard-coded `_DB_PATH = Path("data") / "omniforge.db"` — the
same CWD-relative footgun the write-root constants had. Point it at the
absolute `DATA_DIR` (`shared/constants.py`) so the database lives with the rest
of the app data regardless of launch directory. (`core/` may import `shared/`,
rule A-07.)

### 3. Create the Win32 handles with `use_last_error=True`

Use `ctypes.WinDLL("shell32", use_last_error=True)` (and `kernel32`) inside
`_elevate_windows` so `ctypes.get_last_error()` reflects `ShellExecuteExW`'s own
failure code, making the `ERROR_CANCELLED` (UAC dismissed) branch reliable.

## Alternatives considered

- **Make the whole navigation path async** (await `on_deactivate()` before
  clearing): rejected for now — it forces `command_palette`'s callback type and
  every call site to become async for a one-iteration improvement over the
  scheduled approach.
- **Publish a global "module hidden" EventBus topic** instead of a method:
  rejected — the shell would have to know a module-specific event to stop it,
  and every module would need a subscriber; a base-class method is simpler and
  typed.
- **OS advisory locks (`msvcrt.locking` / `fcntl.flock`) for single-instance**:
  rejected — an exclusive-create lock file is portable across all three targets
  with no per-OS branch and keeps the existing readable PID/start-time record.
