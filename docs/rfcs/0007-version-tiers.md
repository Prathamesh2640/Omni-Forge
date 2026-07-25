# RFC 0007 — Version tiers and the V1 ship gate

**Status:** Accepted
**Rule affected:** A-06 (`core/` is immutable without an RFC)

## Problem

OmniForge stopped being a single 18-module release and became a **versioned
product**. V1 — the "Offline Developer File Toolkit" — ships only the two file
pillars: **Converters** (pdf_suite, document_suite, image_suite, media_suite)
and **Extractors** (llm_packager, file_filter, duplicate_finder, bulk_renamer).
The System (system_matrix) and Security (network_vault) pillars are built and
tested but belong to later versions.

The registry loaded *everything* it found under `modules/`. There was no notion
of a version, so "what V1 ships" was defined only by which directories happened
to be present — a fact no code stated and nothing enforced. Archiving the later
pillars out of the tree removes them from *this* build, but leaves the product
with no declared boundary: dropping a V2 module back under `modules/` to work on
it would silently ship it, and a manifest could omit any version marker without
complaint.

## Decision

### 1. A required `tier` manifest field

Every `manifest.json` declares `"tier": "v1" | "v2" | "v3"`. `tier` joins the
registry's required-key set, so a manifest without it degrades with a clear
reason rather than loading into an undefined version.

The valid tiers and the tier(s) the current build ships live in
`shared/constants.py`:

```python
VALID_TIERS: frozenset[str]  = frozenset({"v1", "v2", "v3"})
SHIPPED_TIERS: frozenset[str] = frozenset({"v1"})
```

### 2. The registry gates on tier before importing

`registry._load_from_manifest` validates the tier against `VALID_TIERS` (an
unknown tier is a manifest error → degraded) and, for a valid-but-unshipped
tier, **skips the module before importing its package** — the same shape as the
existing `disabled_on` platform skip. Skipping *before* the import matters: a
parked module may import dependencies this build no longer installs (scapy,
docker, …), so importing it to discover its tier would raise rather than skip
cleanly.

The effect: a parked tier's module can sit fully built under `modules/` and
never load, never appear in the UI, and never pull its dependencies. Promoting a
tier to shipped is a one-line edit to `SHIPPED_TIERS`.

### 3. Docstring examples refreshed to V1 modules

`core/base_module.py` and `core/event_bus.py` used `system_matrix.live_monitor`
as their canonical identifier example. That pillar is no longer in the V1 tree,
so the examples now use V1 modules (`extractors.llm_packager`,
`converters.pdf_suite`). Documentation-only; no behaviour changes.

## Alternatives considered

- **Filter by pillar name instead of a tier field.** Rejected — pillars are a
  taxonomy of *what a module does*, not *when it ships*; a future version could
  add a converter, and hard-coding pillar names into the ship gate would conflate
  the two. A `tier` field states the versioning intent directly.
- **Gate only by physically moving modules out of the tree** (no code change).
  Rejected — it leaves the boundary implicit and unenforced. A dropped-in module
  would ship by accident, which is exactly the failure the required field + skip
  prevents. The archive and the gate are complementary: the archive keeps the V1
  tree clean, the gate makes the boundary a declared, enforced property.
- **Skip after import (filter the loaded instances).** Rejected — importing a
  parked module runs its top-level imports, which this build deliberately no
  longer satisfies. The skip must precede the import.
