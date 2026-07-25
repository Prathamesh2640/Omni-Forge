# RFC 0004 — Recycle manifest trust boundary and registry load race

**Status:** Accepted
**Rule affected:** A-06 (`core/` is immutable without an RFC)

## Problem

A P0 audit of the shipped phase 0–5 code surfaced two defects in `core/` that
the test suite did not cover, both in the "destroys data" / "runs the operation
twice" class rather than the cosmetic one.

### 1. `core/recycle_store.py` treats its own manifest as trusted input

`RecycleBatch` is deserialised straight from `data/recycle/<batch_id>/manifest.json`
and both paths it carries are then acted on without any check that they point
where they are supposed to:

- `_remove_batch_dir()` builds its delete set from
  `entry.stored_path.parent.parent` and hands it to `shutil.rmtree`. A manifest
  whose `stored_path` reads `C:\Users\me\Documents\taxes\files\x` makes the
  purge — which runs **automatically at every application startup**, from
  `app._on_startup` → `purge_expired()` — recursively delete
  `C:\Users\me\Documents\taxes`.
- `restore_batch()` moves `stored_path` → `original_path` with both sides taken
  from the same file, so a crafted manifest turns the Restore button into an
  arbitrary-file mover: name any file on disk as `stored_path` and any
  destination as `original_path`.

The file is not attacker-supplied in the normal case, which is why this has not
bitten. But it is plain JSON in a predictable location, it is written by an
earlier version of software that will keep evolving, and a *corrupt* manifest
(partial write, disk error, hand-edit during debugging) reaches exactly the same
code paths as a malicious one. A store that can be talked into deleting
arbitrary directories is not a safe undo store, whatever the odds of it
happening.

The layout is already rigidly specified — `<root>/<batch_id>/files/<name>`,
where `<root>` is either the central store or a volume-local
`.omniforge-recycle` — so nothing about the design requires trusting the file.
The check was simply never written.

### 2. `core/registry.py.discover_and_load()` is not concurrency-safe

`app.index()` is a NiceGUI `@ui.page("/")` handler, so it runs **per browser
client**, and it awaits `registry.discover_and_load()`. The guard against
double-loading is:

```python
if module_id in self._modules or module_id in self._degraded:
    continue
await self._load_from_manifest(manifest_path)
```

`_load_from_manifest` awaits (`instance.on_load()`, `event_bus.publish`) before
recording the module, so the check and the record are not atomic across the
await. Two clients connecting close together — a second tab, a refresh during
startup, the native window plus a browser tab — both pass the check for the same
module and both load it. Each `on_load()` calls `logic.register()`, which
`event_bus.subscribe()`s the same handler again, and the EventBus dispatches to
**every** registered handler. One click on Purge then runs the purge twice, one
Merge writes two files, one kill fires at two PIDs.

This is the same class of bug as the single-instance TOCTOU closed in RFC 0002,
one layer up.

## Decision

### 1. Confine every recycle path to a valid store root before acting on it

Add a private `_is_valid_store_path(stored_path, batch_id)` predicate encoding
the layout the store itself writes:

- the path is `<root>/<batch_id>/files/<name>`,
- `<root>` is either `recycle_root()` or a directory named
  `RECYCLE_LOCAL_DIRNAME` (the volume-local store),
- the `<batch_id>` component matches the batch being operated on, so one
  batch's manifest can never reach into another's payload.

Both consumers are gated on it:

- `_remove_batch_dir()` only adds a payload directory to its delete set when the
  entry validates. The central batch directory is still removed unconditionally
  — that path is derived from `batch_id`, not read from the file.
- `restore_batch()` skips (and warns about) an entry whose `stored_path` is not
  inside a store, so a manifest cannot nominate a file it does not own.

`original_path` is deliberately **not** confined: restoring means putting a file
back wherever the user originally had it, which is legitimately anywhere on the
filesystem. The protection is on the source side — an entry may only ever move
*out of* a real store directory — which is what makes the destination
uninteresting to an attacker.

Validation failures are logged and skipped rather than raised: a partly-corrupt
manifest should cost the user the entries it broke, not the whole batch.

### 2. Serialise discovery behind an `asyncio.Lock`

`discover_and_load()` takes a registry-owned lock for the whole walk, making
check-and-load one indivisible step. The second concurrent caller waits, then
finds every module already present and does nothing — the existing
"safe to call multiple times" contract now holds under concurrency too.

The lock is created lazily on first use rather than in `__init__`, because
`registry` is a module-level singleton constructed at import time, when there is
no running event loop for `asyncio.Lock()` to bind to.

## Alternatives considered

- **Load modules once in `app._on_startup` instead of per page.** The cleaner
  end state, and worth doing later, but it changes the app's startup contract
  and how tests drive the registry. The lock fixes the defect without moving
  that boundary, and remains correct if discovery is ever called from elsewhere.
- **Sign or checksum the manifest.** Rejected — it defends against tampering
  but not against corruption, needs key management for an offline app (rule
  C-01), and the layout check is both stronger for this threat and free.
- **Raise on an invalid manifest entry rather than skipping.** Rejected — one
  bad entry would then make the whole batch permanently unrestorable and
  unpurgeable, turning a data-recovery feature into a dead end. Skip and warn
  keeps the good entries usable.
- **Confine `original_path` to the app's write roots (rule B-07).** Rejected —
  B-07 governs where *modules* write their outputs; the recycle store's entire
  job is to put a user's file back where it came from, which is by definition
  outside those roots.
