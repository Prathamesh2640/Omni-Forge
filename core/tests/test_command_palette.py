"""Unit tests for core.command_palette (rule E-06)."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core import command_palette as palette_mod
from core import storage
from core.command_palette import (
    CommandPalette,
    EntryKind,
    PaletteEntry,
    build_entries,
    fuzzy_score,
    load_recent,
    record_recent,
    score_entry,
    search,
)
from core.models import ModuleMetadata
from shared.constants import PALETTE_OPEN_EVENT, PALETTE_RESULT_LIMIT, RECENT_OPERATIONS_LIMIT


@pytest.fixture(autouse=True)
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate recent-operation persistence in a throwaway database.

    ``storage.reset()`` is what actually isolates the test: the module keeps a
    process-global in-memory mirror, so redirecting ``_DB_PATH`` alone would
    leave the previously loaded contents (including the real application
    database's) visible to this test.
    """
    storage.reset()
    monkeypatch.setattr(storage, "_DB_PATH", tmp_path / "palette.db")
    yield
    storage.reset()


def _entry(title: str, **overrides: object) -> PaletteEntry:
    """Build a palette entry with sensible defaults."""
    defaults: dict[str, object] = {
        "entry_id": title.lower().replace(" ", "_"),
        "title": title,
    }
    return PaletteEntry(**{**defaults, **overrides})  # type: ignore[arg-type]


def _metadata(module_id: str, name: str, **overrides: object) -> ModuleMetadata:
    """Build module metadata with sensible defaults."""
    defaults: dict[str, object] = {
        "module_id": module_id,
        "name": name,
        "pillar": module_id.split(".")[0],
    }
    return ModuleMetadata(**{**defaults, **overrides})  # type: ignore[arg-type]


# ─── fuzzy_score ──────────────────────────────────────────────────────────────


class TestFuzzyScore:
    def test_empty_query_matches_everything_neutrally(self) -> None:
        assert fuzzy_score("", "LLM Packager") == 0

    def test_whitespace_only_query_is_treated_as_empty(self) -> None:
        assert fuzzy_score("   ", "LLM Packager") == 0

    def test_empty_text_never_matches_a_real_query(self) -> None:
        assert fuzzy_score("abc", "") is None

    def test_exact_match_scores(self) -> None:
        assert fuzzy_score("live monitor", "Live Monitor") is not None

    def test_matching_is_case_insensitive(self) -> None:
        assert fuzzy_score("LIVE", "live monitor") == fuzzy_score("live", "Live Monitor")

    def test_subsequence_matches_across_words(self) -> None:
        """`lmpk` should still find "LLM Packager"."""
        assert fuzzy_score("lmpk", "LLM Packager") is not None

    def test_out_of_order_characters_do_not_match(self) -> None:
        assert fuzzy_score("rotinom", "Live Monitor") is None

    def test_absent_character_does_not_match(self) -> None:
        assert fuzzy_score("livez", "Live Monitor") is None

    def test_query_longer_than_text_does_not_match(self) -> None:
        assert fuzzy_score("live monitor extended", "Live") is None

    def test_prefix_outranks_a_later_match(self) -> None:
        prefix = fuzzy_score("mon", "Monitor Tools")
        later = fuzzy_score("mon", "Tools Monitor")
        assert prefix is not None and later is not None
        assert prefix > later

    def test_contiguous_run_outranks_a_scattered_one(self) -> None:
        contiguous = fuzzy_score("port", "Port Monitor")
        scattered = fuzzy_score("port", "Package Order Utility")
        assert contiguous is not None and scattered is not None
        assert contiguous > scattered

    def test_word_boundary_start_outranks_a_mid_word_hit(self) -> None:
        """Both hits land at the same index, so only the boundary bonus differs."""
        boundary = fuzzy_score("m", "Disk Monitor")
        mid_word = fuzzy_score("m", "Diskxmonitor")
        assert boundary is not None and mid_word is not None
        assert boundary > mid_word


# ─── score_entry ──────────────────────────────────────────────────────────────


class TestScoreEntry:
    def test_returns_none_when_no_field_matches(self) -> None:
        entry = _entry("Live Monitor", subtitle="System metrics", keywords=["cpu"])
        assert score_entry(entry, "zzzz") is None

    def test_matches_against_keywords(self) -> None:
        entry = _entry("Live Monitor", keywords=["psutil", "cpu", "ram"])
        assert score_entry(entry, "psutil") is not None

    def test_matches_against_the_subtitle(self) -> None:
        entry = _entry("Live Monitor", subtitle="Real-time system metrics")
        assert score_entry(entry, "real-time") is not None

    def test_a_title_hit_outranks_the_same_hit_in_a_keyword(self) -> None:
        titled = _entry("Docker Janitor", keywords=["cleanup"])
        tagged = _entry("Cache Purger", keywords=["docker"])
        title_score = score_entry(titled, "docker")
        keyword_score = score_entry(tagged, "docker")

        assert title_score is not None and keyword_score is not None
        assert title_score > keyword_score

    def test_the_best_scoring_field_is_the_one_that_counts(self) -> None:
        """A weak subtitle hit must not drag down a strong title hit."""
        entry = _entry("Port Monitor", subtitle="p o r t s everywhere")
        assert score_entry(entry, "port") == score_entry(_entry("Port Monitor"), "port")


# ─── search ───────────────────────────────────────────────────────────────────


class TestSearch:
    @pytest.fixture
    def index(self) -> list[PaletteEntry]:
        return [
            _entry("Live Monitor", keywords=["cpu", "ram"]),
            _entry("Port Monitor", keywords=["network", "tcp"]),
            _entry("LLM Packager", keywords=["context", "tokens"]),
            _entry("Docker Janitor", keywords=["containers"]),
        ]

    def test_empty_query_returns_the_index_unfiltered(
        self, index: list[PaletteEntry]
    ) -> None:
        assert [e.title for e in search(index, "")] == [e.title for e in index]

    def test_filters_out_non_matches(self, index: list[PaletteEntry]) -> None:
        titles = [e.title for e in search(index, "monitor")]
        assert titles == ["Live Monitor", "Port Monitor"]

    def test_best_match_ranks_first(self, index: list[PaletteEntry]) -> None:
        assert search(index, "docker")[0].title == "Docker Janitor"

    def test_a_tag_only_match_is_still_found(self, index: list[PaletteEntry]) -> None:
        assert search(index, "tcp")[0].title == "Port Monitor"

    def test_no_matches_returns_empty(self, index: list[PaletteEntry]) -> None:
        assert search(index, "kubernetes") == []

    def test_respects_the_result_limit(self, index: list[PaletteEntry]) -> None:
        assert len(search(index, "o", limit=2)) == 2

    def test_default_limit_is_applied(self) -> None:
        many = [_entry(f"Module {i}") for i in range(PALETTE_RESULT_LIMIT + 5)]
        assert len(search(many, "module")) == PALETTE_RESULT_LIMIT

    def test_empty_query_also_respects_the_limit(self) -> None:
        many = [_entry(f"Module {i}") for i in range(PALETTE_RESULT_LIMIT + 5)]
        assert len(search(many, "")) == PALETTE_RESULT_LIMIT

    def test_equal_scores_keep_their_original_order(self) -> None:
        """Stable ranking keeps the list from jittering between keystrokes."""
        entries = [_entry("Alpha Tool"), _entry("Alpha Tool")]
        entries[0].entry_id = "first"
        entries[1].entry_id = "second"
        assert [e.entry_id for e in search(entries, "alpha")] == ["first", "second"]

    def test_searching_an_empty_index_is_safe(self) -> None:
        assert search([], "anything") == []


# ─── build_entries ────────────────────────────────────────────────────────────


class TestBuildEntries:
    @pytest.fixture
    def metadata(self) -> list[ModuleMetadata]:
        return [
            _metadata("extractors.llm_packager", "LLM Packager",
                      description="Pack a codebase.", tags=["llm", "context"]),
            _metadata("converters.image_suite", "Image Suite",
                      description="Convert and compress images.", tags=["image"]),
        ]

    def test_builds_one_entry_per_module(self, metadata: list[ModuleMetadata]) -> None:
        assert len(build_entries(metadata)) == 2

    def test_entry_id_is_the_module_id(self, metadata: list[ModuleMetadata]) -> None:
        assert build_entries(metadata)[0].entry_id == "extractors.llm_packager"

    def test_description_becomes_the_subtitle(self, metadata: list[ModuleMetadata]) -> None:
        assert build_entries(metadata)[0].subtitle == "Pack a codebase."

    def test_pillar_is_the_subtitle_fallback(self) -> None:
        entries = build_entries([_metadata("converters.media_suite", "Media Suite")])
        assert entries[0].subtitle == "Converters"

    def test_tags_and_pillar_become_keywords(self, metadata: list[ModuleMetadata]) -> None:
        keywords = build_entries(metadata)[0].keywords
        assert "llm" in keywords
        assert "extractors" in keywords

    def test_recent_modules_are_listed_first(self, metadata: list[ModuleMetadata]) -> None:
        entries = build_entries(metadata, recent_ids=["converters.image_suite"])
        assert entries[0].entry_id == "converters.image_suite"
        assert entries[0].kind is EntryKind.RECENT

    def test_non_recent_modules_keep_the_module_kind(
        self, metadata: list[ModuleMetadata]
    ) -> None:
        entries = build_entries(metadata, recent_ids=["converters.image_suite"])
        assert entries[1].kind is EntryKind.MODULE

    def test_a_module_appears_only_once(self, metadata: list[ModuleMetadata]) -> None:
        entries = build_entries(metadata, recent_ids=["converters.image_suite"])
        assert len(entries) == 2

    def test_duplicate_recent_ids_do_not_duplicate_entries(
        self, metadata: list[ModuleMetadata]
    ) -> None:
        recent = ["converters.image_suite", "converters.image_suite"]
        assert len(build_entries(metadata, recent_ids=recent)) == 2

    def test_a_recent_id_for_an_unloaded_module_is_ignored(
        self, metadata: list[ModuleMetadata]
    ) -> None:
        """A module removed since it was last used must not create a dead row."""
        entries = build_entries(metadata, recent_ids=["converters.pdf_suite"])
        assert len(entries) == 2
        assert all(e.kind is EntryKind.MODULE for e in entries)

    def test_an_empty_registry_yields_an_empty_index(self) -> None:
        assert build_entries([]) == []


# ─── Recent operations ────────────────────────────────────────────────────────


class TestRecentOperations:
    def test_starts_empty(self) -> None:
        assert load_recent() == []

    def test_records_a_module(self) -> None:
        record_recent("extractors.llm_packager")
        assert load_recent() == ["extractors.llm_packager"]

    def test_most_recent_is_first(self) -> None:
        record_recent("a")
        record_recent("b")
        assert load_recent() == ["b", "a"]

    def test_re_recording_moves_to_front_without_duplicating(self) -> None:
        record_recent("a")
        record_recent("b")
        record_recent("a")
        assert load_recent() == ["a", "b"]

    def test_the_history_is_capped(self) -> None:
        for i in range(RECENT_OPERATIONS_LIMIT + 10):
            record_recent(f"module_{i}")
        assert len(load_recent()) == RECENT_OPERATIONS_LIMIT

    def test_the_cap_discards_the_oldest_entries(self) -> None:
        for i in range(RECENT_OPERATIONS_LIMIT + 1):
            record_recent(f"module_{i}")
        assert "module_0" not in load_recent()

    def test_a_corrupt_stored_value_is_ignored(self) -> None:
        storage.store_set("app", "recent_operations", "not-a-list")
        assert load_recent() == []


# ─── Keyboard navigation ──────────────────────────────────────────────────────


class TestKeyboardNavigation:
    """Exercises selection handling without rendering the dialog.

    ``_render_results`` and the dialog calls short-circuit while their
    NiceGUI elements are unset, so the navigation logic runs standalone.
    """

    @pytest.fixture
    def palette(self) -> tuple[CommandPalette, list[str]]:
        activated: list[str] = []
        instance = CommandPalette(on_activate=activated.append)
        instance.set_entries(
            [_entry("Live Monitor"), _entry("Port Monitor"), _entry("LLM Packager")]
        )
        instance._refresh("")
        return instance, activated

    def test_selection_starts_at_the_top(
        self, palette: tuple[CommandPalette, list[str]]
    ) -> None:
        instance, _ = palette
        assert instance._selected == 0

    def test_arrow_down_advances_the_selection(
        self, palette: tuple[CommandPalette, list[str]]
    ) -> None:
        instance, _ = palette
        instance._move_selection(1)
        assert instance._selected == 1

    def test_selection_clamps_at_the_last_result(
        self, palette: tuple[CommandPalette, list[str]]
    ) -> None:
        """Holding ArrowDown must not run off the end of the list."""
        instance, _ = palette
        for _ in range(10):
            instance._move_selection(1)
        assert instance._selected == 2

    def test_selection_clamps_at_the_first_result(
        self, palette: tuple[CommandPalette, list[str]]
    ) -> None:
        instance, _ = palette
        instance._move_selection(-5)
        assert instance._selected == 0

    def test_enter_activates_the_highlighted_entry(
        self, palette: tuple[CommandPalette, list[str]]
    ) -> None:
        instance, activated = palette
        instance._move_selection(1)
        instance._activate_selected()
        assert activated == ["port_monitor"]

    def test_activation_is_a_noop_with_no_results(self) -> None:
        activated: list[str] = []
        instance = CommandPalette(on_activate=activated.append)
        instance.set_entries([_entry("Live Monitor")])
        instance._refresh("kubernetes")

        instance._activate_selected()

        assert activated == []

    def test_moving_with_no_results_is_safe(self) -> None:
        instance = CommandPalette(on_activate=lambda _id: None)
        instance.set_entries([])
        instance._refresh("")
        instance._move_selection(1)
        assert instance._selected == 0

    def test_a_new_query_resets_the_selection(
        self, palette: tuple[CommandPalette, list[str]]
    ) -> None:
        """Otherwise the highlight could point past the end of a shorter list."""
        instance, _ = palette
        instance._move_selection(2)
        instance._refresh("packager")
        assert instance._selected == 0

    def test_a_query_narrows_the_results(
        self, palette: tuple[CommandPalette, list[str]]
    ) -> None:
        instance, _ = palette
        instance._refresh("packager")
        assert [e.title for e in instance._results] == ["LLM Packager"]

    @pytest.mark.parametrize(
        ("key", "expected_index"), [("ArrowDown", 1), ("ArrowUp", 0)]
    )
    def test_arrow_keys_are_dispatched(
        self, palette: tuple[CommandPalette, list[str]], key: str, expected_index: int
    ) -> None:
        instance, _ = palette
        instance._on_input_key(_KeyEvent(key))
        assert instance._selected == expected_index

    def test_enter_key_is_dispatched(
        self, palette: tuple[CommandPalette, list[str]]
    ) -> None:
        instance, activated = palette
        instance._on_input_key(_KeyEvent("Enter"))
        assert activated == ["live_monitor"]

    def test_an_unhandled_key_changes_nothing(
        self, palette: tuple[CommandPalette, list[str]]
    ) -> None:
        instance, activated = palette
        instance._on_input_key(_KeyEvent("a"))
        assert instance._selected == 0
        assert activated == []

    def test_clicking_a_row_activates_that_row(
        self, palette: tuple[CommandPalette, list[str]]
    ) -> None:
        instance, activated = palette
        instance._activate_index(2)
        assert activated == ["llm_packager"]


class _KeyEvent:
    """Minimal stand-in for a NiceGUI keydown event."""

    def __init__(self, key: str) -> None:
        self.args = {"key": key}


class _NullElement:
    """Stands in for a NiceGUI element in tests that never render."""

    def props(self, *_a: Any, **_kw: Any) -> _NullElement:
        return self

    def style(self, *_a: Any, **_kw: Any) -> _NullElement:
        return self

    def on(self, *_a: Any, **_kw: Any) -> _NullElement:
        return self


class _NullContext(_NullElement):
    """A no-op context manager for ui.dialog()/ui.card()."""

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        pass

    def __enter__(self) -> _NullContext:
        return self

    def __exit__(self, *_a: Any) -> bool:
        return False


# ─── §3.11d — Ctrl+K really is global (rule E-06) ────────────────────────────


class TestGlobalBinding:
    """The palette must open from every screen "at all times" (rule E-06).

    The chord is matched in the browser now: ``ui.keyboard`` forwarded every
    keydown in the app to the server just to discard all but Ctrl+K, which cost
    a websocket round-trip per character typed (RFC 0005). The reach must not
    regress in the process — a capture-phase ``document`` listener still fires
    inside inputs and textareas, which is exactly what the old ``ignore=[]``
    was there to guarantee.
    """

    def _render_capturing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[list[str], dict[str, Any]]:
        """Render a palette, capturing the injected script and event binding."""
        scripts: list[str] = []
        handlers: dict[str, Any] = {}

        monkeypatch.setattr(palette_mod.ui, "add_head_html", scripts.append)
        monkeypatch.setattr(
            palette_mod.ui, "on", lambda name, handler: handlers.__setitem__(name, handler)
        )
        monkeypatch.setattr(palette_mod.ui, "dialog", _NullContext)
        monkeypatch.setattr(palette_mod.ui, "card", _NullContext)
        monkeypatch.setattr(palette_mod.ui, "input", lambda **_kw: _NullElement())
        monkeypatch.setattr(palette_mod.ui, "column", lambda **_kw: _NullElement())

        CommandPalette(on_activate=lambda _id: None).render()
        return scripts, handlers

    def test_ordinary_typing_never_reaches_the_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scripts, _handlers = self._render_capturing(monkeypatch)
        script = "".join(scripts)

        # The guard clause returns before emitting for anything but the chord.
        assert "emitEvent" in script
        assert "event.ctrlKey" in script
        assert "!== 'k'" in script
        assert "return;" in script

    def test_the_listener_still_fires_inside_form_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Capture phase on `document` is what keeps rule E-06 satisfied."""
        scripts, _handlers = self._render_capturing(monkeypatch)
        script = "".join(scripts)

        assert "document.addEventListener" in script
        # The trailing `true` is the capture flag — without it a field that
        # stops propagation would swallow the chord.
        assert script.rstrip().endswith("}, true);</script>")

    def test_the_browser_event_is_wired_to_opening_the_palette(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scripts, handlers = self._render_capturing(monkeypatch)

        assert PALETTE_OPEN_EVENT in "".join(scripts)
        assert PALETTE_OPEN_EVENT in handlers

    def test_the_wired_handler_opens_the_palette(self) -> None:
        opened: list[bool] = []
        palette = CommandPalette(on_activate=lambda _id: None)
        palette.open = lambda: opened.append(True)  # type: ignore[method-assign]

        palette._on_palette_chord(None)

        assert opened == [True]
