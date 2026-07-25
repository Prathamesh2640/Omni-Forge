# RFC 0005 — Core resource and I/O hygiene

**Status:** Accepted
**Rule affected:** A-06 (`core/` is immutable without an RFC)

## Problem

The P1 tier of the same audit that produced RFC 0004 found six defects in
`core/` in the "leaks, blocks, or quietly loses data" class. None of them is a
crash, which is why the suite is green and none has been reported — they degrade
a long-running session rather than breaking a short one.

### 1. `event_bus` accumulates an entry per topic ever published

`publish()` reads `self._subscribers[event_type]` on a `defaultdict(list)`, so
*publishing* a topic creates a permanent empty list for it. Every module
publishes progress/done/error topics that often have no subscriber (nothing is
listening until a UI is rendered), so the dict grows for the life of the
process and never shrinks. `unsubscribe()` likewise leaves an empty list behind
after the last handler goes.

### 2. `sandbox.run_reporting_progress` drops the tail of a run's progress

The drain loop is `while not task.done()`. When the worker finishes, the loop
exits immediately — anything still sitting in the queue is discarded. The last
reports of every scan are therefore lost, which is precisely the "N of N"
moment the user is waiting to see. It also polls: each iteration schedules
`updates.get` on the default thread-pool executor, so a long scan burns an
executor slot per poll interval, shared with every other `asyncio.to_thread`
call in the app.

### 3. `storage` performs disk I/O on the event loop

`store_set` holds a `threading.Lock` across a full TinyDB write. Measured on the
development machine: **3.4 ms median, 4.8 ms worst case** per call (`store_get`
is 0.17 ms and is not a problem). It is called synchronously from the UI thread,
including from `ui.input(on_change=…)` on the output-directory picker — which
fires **per keystroke**. Typing a path therefore stutters the whole event loop,
and every module that persists a last-used directory has the same shape.

### 4. Log rotation is defeated by a dated filename

`shared/constants.LOG_FILE_TEMPLATE` is `"omniforge-{date}.log"` *and*
`core/logger` attaches a `TimedRotatingFileHandler` rotating at midnight. The
two mechanisms fight: at midnight the handler renames the current file to
`omniforge-<start-date>.log.<rollover-date>` and keeps writing to a base file
still named for the day the session *started*. Day two's records therefore live
in a file named day one, and `backupCount=14` prunes per start-date rather than
across a rolling window.

### 5. `single_instance.acquire` still has a (smaller) race

RFC 0002 made the claim atomic with `O_CREAT | O_EXCL`, but the owner record is
written *after* the create. A second process landing in that window opens the
file, reads zero bytes, concludes there is no live owner, **deletes the winner's
lock** and claims it — so both instances run. The window is microseconds, but it
is the exact scenario the lock exists for (two launches at once).

### 6. The Ctrl+K palette round-trips every keystroke in the application

`core/command_palette.render()` creates `ui.keyboard(on_key=…, ignore=[])`. The
empty `ignore` list is deliberate and correct — rule E-06 wants the palette
reachable "at all times", including from inside a text field — but it makes
NiceGUI forward **every** keydown anywhere in the app over the websocket to the
server, where `_on_global_key` discards all but the chord. Typing a regex, a
path or a password costs one server round-trip per character.

## Decision

### 1. Read subscribers without creating them

`publish()` uses `.get(event_type)`; `unsubscribe()` drops a topic's entry once
its last handler is removed. `subscribe()` keeps using `defaultdict` insertion,
which is the one place an entry *should* be created.

### 2. Replace the polling drain with an `asyncio.Queue` handoff

The worker thread hands each report to the loop with
`loop.call_soon_threadsafe(queue.put_nowait, payload)`; the consumer awaits the
queue and stops on a sentinel queued by the task's done-callback. No polling, no
executor slot per interval, and — because the sentinel is queued *behind* every
report the thread already made — no dropped tail.

`poll_seconds` is retained as an accepted-and-ignored parameter rather than
removed, so the existing call sites and tests keep working; it is documented as
obsolete.

### 3. Give `storage` a write-behind cache

An in-memory mirror becomes the read path, and a single daemon writer thread
owns every TinyDB write:

- `store_set` / `store_delete` update the mirror and mark it dirty — no disk,
  no lock held across I/O, so they return in microseconds.
- `store_get` reads the mirror, which is authoritative, so a read immediately
  after a write sees the new value.
- Writes are coalesced: a burst of keystrokes collapses into one flush after a
  short debounce, instead of one full database write per character.
- `flush()` blocks until the mirror is on disk, and is called from the
  application's shutdown hook.

The trade is durability: a hard kill can lose up to the debounce window of
*preference* writes (last-used directory, theme, recents). That is acceptable
for this data and is documented at the call site. Nothing in `data/omniforge.db`
is user content — module outputs are files, and the recycle store has its own
manifest.

### 4. Let the handler own the rotation, not the filename

`LOG_FILE_TEMPLATE` becomes the static `"omniforge.log"`. The
`TimedRotatingFileHandler` then does what it is designed to do: the live file is
always `omniforge.log`, and midnight rollovers become `omniforge.log.2026-07-24`,
with `backupCount=14` pruning a genuine 14-day window. `log_file_path()` keeps
its `today` parameter (ignored) so existing callers are unaffected.

### 5. Treat an empty lock file as "in flight", not "stale"

The reader retries a bounded number of times before concluding a lock is
abandoned. A live winner finishes writing its record within microseconds, so it
is found on a subsequent read; a genuinely stale lock from a crashed process
stays empty for the whole retry window and is still reclaimed. This preserves
RFC 0002's crash recovery while closing the window it left open.

### 6. Filter the palette chord in the browser

The `ui.keyboard` element is replaced by a document-level `keydown` listener
installed once via `ui.add_head_html`, which calls NiceGUI's `emitEvent` only
when the chord actually matches. Python subscribes with `ui.on`. Rule E-06 is
still satisfied — the listener is on `document` in the capture phase, so it
fires from inside inputs too — but exactly one message crosses the socket per
palette open instead of one per keystroke.

## Alternatives considered

- **Make `storage` async and `await` it everywhere** — rejected: it would push
  `async` through 31 call sites and every module UI's change handler, for a
  problem an in-process cache solves without touching any caller.
- **Throttle the output-directory input at each call site** instead of fixing
  storage — rejected: twelve modules would each need the same guard, and any
  future caller re-opens the hole.
- **`CachingMiddleware` from TinyDB** — rejected: it caches reads and still
  flushes writes synchronously on the calling thread, so the event loop keeps
  the 3.4 ms hit.
- **Drop `ignore=[]` and accept that Ctrl+K does not work in text fields** —
  rejected: it directly contradicts rule E-06, and the field is where a user is
  most likely to want the palette.
- **Keep the dated log filename and drop the rotating handler** — rejected: a
  session running past midnight would then keep one ever-growing file for its
  whole life, and nothing would enforce the 14-day retention.
