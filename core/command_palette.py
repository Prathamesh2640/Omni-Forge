"""Ctrl+K command palette — fuzzy module search from any screen (rule E-06).

The module splits cleanly in two:

- **Index and matching** — :func:`fuzzy_score`, :func:`search`,
  :func:`build_entries`. Pure functions with no NiceGUI dependency.
- **Presentation** — :class:`CommandPalette`, the dialog with arrow-key
  navigation and Enter-to-activate.

Matching is subsequence-based: every character of the query must appear in
order, so ``lmpk`` finds "LLM Packager". Scoring favours prefix matches,
whole-substring matches, word-boundary starts and consecutive runs, so the
most obvious candidate lands first.
"""
from __future__ import annotations

import enum
import json
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from nicegui import ui
from pydantic import BaseModel, Field

from core.logger import get_logger
from core.models import ModuleMetadata
from core.storage import store_get, store_set
from shared.constants import (
    COLOR_BORDER,
    COLOR_CARD_BG,
    COLOR_PRIMARY,
    COLOR_PRIMARY_SOFT,
    COLOR_TEXT_DIM,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_PRIMARY,
    FUZZY_CHARACTER_SCORE,
    FUZZY_CONSECUTIVE_BONUS,
    FUZZY_GAP_PENALTY,
    FUZZY_KEYWORD_WEIGHT,
    FUZZY_PREFIX_BONUS,
    FUZZY_SUBSTRING_BONUS,
    FUZZY_SUBTITLE_WEIGHT,
    FUZZY_TITLE_WEIGHT,
    FUZZY_WORD_BOUNDARY_BONUS,
    FUZZY_WORD_SEPARATORS,
    PALETTE_OPEN_EVENT,
    PALETTE_RESULT_LIMIT,
    RECENT_OPERATIONS_LIMIT,
    STORAGE_KEY_RECENT_OPERATIONS,
    STORAGE_TABLE_APP,
)

logger = get_logger(__name__)


class EntryKind(enum.StrEnum):
    """Where a palette entry came from."""

    MODULE = "module"
    RECENT = "recent"


class PaletteEntry(BaseModel):
    """One selectable row in the command palette.

    Attributes:
        entry_id: The module_id this entry activates.
        title: Primary label, matched with the highest weight.
        subtitle: Secondary label (module description or pillar).
        keywords: Manifest tags, matched at medium weight.
        kind: Whether this is a module or a recently used operation.
    """

    entry_id: str
    title: str
    subtitle: str = ""
    keywords: list[str] = Field(default_factory=list)
    kind: EntryKind = EntryKind.MODULE


# ─── Matching ─────────────────────────────────────────────────────────────────


def fuzzy_score(query: str, text: str) -> int | None:
    """Score how well *text* matches *query* as a subsequence.

    Args:
        query: The user's search string. Case-insensitive.
        text: The candidate string.

    Returns:
        A score where higher is better, or ``None`` when *text* does not
        contain every character of *query* in order. An empty query scores 0
        against everything, so callers can show an unfiltered list.
    """
    needle = query.strip().lower()
    haystack = text.lower()
    if not needle:
        return 0
    if not haystack:
        return None

    score = 0
    cursor = 0
    previous_index = -2

    for character in needle:
        index = haystack.find(character, cursor)
        if index == -1:
            return None

        score += FUZZY_CHARACTER_SCORE
        if index == previous_index + 1:
            score += FUZZY_CONSECUTIVE_BONUS
        if index == 0 or haystack[index - 1] in FUZZY_WORD_SEPARATORS:
            score += FUZZY_WORD_BOUNDARY_BONUS
        score -= (index - cursor) * FUZZY_GAP_PENALTY

        previous_index = index
        cursor = index + 1

    if haystack.startswith(needle):
        score += FUZZY_PREFIX_BONUS
    elif needle in haystack:
        score += FUZZY_SUBSTRING_BONUS

    return score


def score_entry(entry: PaletteEntry, query: str) -> int | None:
    """Score *entry* against *query* across all of its searchable fields.

    The best-scoring field wins, weighted so that a title hit outranks the
    same hit in a tag or description.

    Args:
        entry: The entry to score.
        query: The user's search string.

    Returns:
        The entry's score, or ``None`` when no field matches.
    """
    weighted: list[int] = []

    title_score = fuzzy_score(query, entry.title)
    if title_score is not None:
        weighted.append(title_score * FUZZY_TITLE_WEIGHT)

    for keyword in entry.keywords:
        keyword_score = fuzzy_score(query, keyword)
        if keyword_score is not None:
            weighted.append(keyword_score * FUZZY_KEYWORD_WEIGHT)

    subtitle_score = fuzzy_score(query, entry.subtitle)
    if subtitle_score is not None:
        weighted.append(subtitle_score * FUZZY_SUBTITLE_WEIGHT)

    return max(weighted) if weighted else None


def search(
    entries: Sequence[PaletteEntry],
    query: str,
    limit: int = PALETTE_RESULT_LIMIT,
) -> list[PaletteEntry]:
    """Return the best-matching entries for *query*, highest score first.

    Args:
        entries: The full palette index.
        query: The user's search string. Empty returns the head of *entries*
            unfiltered, preserving their existing order.
        limit: Maximum number of results.

    Returns:
        Up to *limit* entries. Ties keep their original relative order.
    """
    if not query.strip():
        return list(entries[:limit])

    scored: list[tuple[int, int, PaletteEntry]] = []
    for position, entry in enumerate(entries):
        entry_score = score_entry(entry, query)
        if entry_score is not None:
            # Position is the tie-breaker, keeping the sort stable and
            # predictable rather than dependent on dict ordering.
            scored.append((entry_score, -position, entry))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [entry for _score, _position, entry in scored[:limit]]


# ─── Index construction ───────────────────────────────────────────────────────


def build_entries(
    metadata: Iterable[ModuleMetadata],
    recent_ids: Sequence[str] = (),
) -> list[PaletteEntry]:
    """Build the palette index from module metadata.

    Recently used modules are listed first so the palette opens on what the
    user is most likely to want.

    Args:
        metadata: Metadata for every loaded module.
        recent_ids: Module IDs in most-recently-used order.

    Returns:
        The palette index, recents first, then the remaining modules.
    """
    by_id = {item.module_id: item for item in metadata}

    entries: list[PaletteEntry] = []
    seen: set[str] = set()

    for module_id in recent_ids:
        item = by_id.get(module_id)
        if item is None or module_id in seen:
            continue
        entries.append(_entry_from_metadata(item, EntryKind.RECENT))
        seen.add(module_id)

    for module_id, item in by_id.items():
        if module_id not in seen:
            entries.append(_entry_from_metadata(item, EntryKind.MODULE))

    return entries


def _entry_from_metadata(item: ModuleMetadata, kind: EntryKind) -> PaletteEntry:
    """Convert module metadata into a palette entry."""
    return PaletteEntry(
        entry_id=item.module_id,
        title=item.name,
        subtitle=item.description or item.pillar.replace("_", " ").title(),
        keywords=[*item.tags, item.pillar],
        kind=kind,
    )


# ─── Recent operations ────────────────────────────────────────────────────────


def load_recent() -> list[str]:
    """Return recently activated module IDs, most recent first.

    Returns:
        A list of module IDs, empty when nothing has been recorded.
    """
    stored = store_get(STORAGE_TABLE_APP, STORAGE_KEY_RECENT_OPERATIONS, default=[])
    if not isinstance(stored, list):
        return []
    return [str(item) for item in stored]


def record_recent(module_id: str) -> None:
    """Record *module_id* as the most recently used module.

    Re-recording an existing entry moves it to the front rather than
    duplicating it. The list is capped at
    :data:`~shared.constants.RECENT_OPERATIONS_LIMIT`.

    Args:
        module_id: The module that was just activated.
    """
    recent = [item for item in load_recent() if item != module_id]
    recent.insert(0, module_id)
    store_set(
        STORAGE_TABLE_APP,
        STORAGE_KEY_RECENT_OPERATIONS,
        recent[:RECENT_OPERATIONS_LIMIT],
    )


# ─── Presentation ─────────────────────────────────────────────────────────────


class CommandPalette:
    """The Ctrl+K dialog: search field, result list, keyboard navigation.

    Args:
        on_activate: Called with the chosen entry's ``entry_id`` when the user
            presses Enter or clicks a result.
    """

    def __init__(self, on_activate: Callable[[str], None]) -> None:
        self._on_activate = on_activate
        self._entries: list[PaletteEntry] = []
        self._results: list[PaletteEntry] = []
        self._selected: int = 0
        self._dialog: ui.dialog | None = None
        self._input: ui.input | None = None
        self._result_column: ui.column | None = None

    def set_entries(self, entries: list[PaletteEntry]) -> None:
        """Replace the searchable index.

        Args:
            entries: The new palette index.
        """
        self._entries = entries

    def render(self) -> None:
        """Build the (hidden) dialog and register the global Ctrl+K binding.

        The chord is matched **in the browser**. ``ui.keyboard`` forwards every
        keydown to the server, and the ``ignore=[]`` that rule E-06 requires —
        so the palette also opens from inside a text field, which is where a
        user most often wants it — meant every character typed anywhere in the
        app cost a websocket round-trip, only to be discarded server-side. A
        capture-phase listener on ``document`` keeps the same reach while
        sending exactly one message per palette open (RFC 0005).
        """
        ui.add_head_html(
            f"<script>document.addEventListener('keydown', (event) => {{"
            f"  if (!event.ctrlKey || event.key?.toLowerCase() !== 'k') return;"
            f"  event.preventDefault();"
            f"  emitEvent({json.dumps(PALETTE_OPEN_EVENT)});"
            f"}}, true);</script>"
        )
        ui.on(PALETTE_OPEN_EVENT, self._on_palette_chord)

        with ui.dialog().props("transition-show=none transition-hide=none") as dialog:
            self._dialog = dialog
            with ui.card().style(
                f"background: {COLOR_CARD_BG}; border: 1px solid {COLOR_BORDER};"
                " border-radius: 12px; padding: 0; width: 560px; max-width: 92vw;"
            ).props("flat"):
                self._input = (
                    ui.input(placeholder="Search modules…", on_change=self._on_query_change)
                    .props("autofocus borderless dense")
                    .style(
                        f"color: {COLOR_TEXT_PRIMARY}; font-size: 15px;"
                        f" padding: 14px 18px; width: 100%;"
                        f" border-bottom: 1px solid {COLOR_BORDER};"
                    )
                )
                self._input.on("keydown", self._on_input_key)
                self._result_column = ui.column().style(
                    "gap: 0; width: 100%; padding: 6px; max-height: 320px;"
                    " overflow-y: auto;"
                )

    def open(self) -> None:
        """Show the palette, reset to an empty query, and focus the field."""
        if self._dialog is None:
            return
        self._selected = 0
        if self._input is not None:
            self._input.set_value("")
        self._refresh("")
        self._dialog.open()

    # ─── Keyboard ─────────────────────────────────────────────────────────────

    def _on_palette_chord(self, _event: Any = None) -> None:
        """Open the palette — the browser has already matched the chord.

        Args:
            _event: NiceGUI's generic event payload; the chord carries no data.
        """
        self.open()

    def _on_input_key(self, event: Any) -> None:
        """Handle arrow navigation and Enter inside the search field."""
        key = str(event.args.get("key", ""))
        if key == "ArrowDown":
            self._move_selection(1)
        elif key == "ArrowUp":
            self._move_selection(-1)
        elif key == "Enter":
            self._activate_selected()
        elif key == "Escape" and self._dialog is not None:
            self._dialog.close()

    def _move_selection(self, delta: int) -> None:
        """Move the highlight by *delta*, clamped to the result list."""
        if not self._results:
            return
        self._selected = max(0, min(self._selected + delta, len(self._results) - 1))
        self._render_results()

    def _activate_selected(self) -> None:
        """Activate the highlighted result and close the palette."""
        if not self._results:
            return
        chosen = self._results[self._selected]
        if self._dialog is not None:
            self._dialog.close()
        logger.info("palette.activated — module=%s", chosen.entry_id)
        self._on_activate(chosen.entry_id)

    # ─── Rendering ────────────────────────────────────────────────────────────

    def _on_query_change(self, event: Any) -> None:
        """Re-run the search whenever the query changes."""
        self._refresh(str(event.value or ""))

    def _refresh(self, query: str) -> None:
        """Recompute results for *query* and repaint the list."""
        self._results = search(self._entries, query)
        self._selected = 0
        self._render_results()

    def _render_results(self) -> None:
        """Repaint the result rows, highlighting the current selection."""
        if self._result_column is None:
            return

        self._result_column.clear()
        with self._result_column:
            if not self._results:
                ui.label("No matching modules.").style(
                    f"color: {COLOR_TEXT_DIM}; font-size: 13px; padding: 18px;"
                )
                return

            for index, entry in enumerate(self._results):
                self._render_row(index, entry)

    def _render_row(self, index: int, entry: PaletteEntry) -> None:
        """Render a single result row.

        Args:
            index: Position in the result list.
            entry: The entry to render.
        """
        is_selected = index == self._selected
        background = COLOR_PRIMARY_SOFT if is_selected else "transparent"

        with ui.row().style(
            f"align-items: center; gap: 10px; width: 100%; padding: 9px 14px;"
            f" border-radius: 6px; cursor: pointer; background: {background};"
        ).on("click", lambda _e, i=index: self._activate_index(i)):
            if entry.kind is EntryKind.RECENT:
                ui.icon("history").style(f"color: {COLOR_TEXT_DIM}; font-size: 15px;")
            with ui.column().style("gap: 0; flex: 1;"):
                ui.label(entry.title).style(
                    f"color: {COLOR_TEXT_PRIMARY if is_selected else COLOR_TEXT_MUTED};"
                    " font-size: 13px; font-weight: 500;"
                )
                if entry.subtitle:
                    ui.label(entry.subtitle).style(
                        f"color: {COLOR_TEXT_DIM}; font-size: 11px;"
                    )
            if is_selected:
                ui.label("↵").style(f"color: {COLOR_PRIMARY}; font-size: 13px;")

    def _activate_index(self, index: int) -> None:
        """Select *index* and activate it (used by mouse clicks)."""
        self._selected = index
        self._activate_selected()
