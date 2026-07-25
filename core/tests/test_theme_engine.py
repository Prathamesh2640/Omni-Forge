"""Unit tests for core.theme_engine (rule E-07)."""
from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from core import storage, theme_engine
from core.theme_engine import (
    DEFAULT_THEME,
    PALETTES,
    Theme,
    build_css,
    is_dark,
    load_theme,
    save_theme,
)
from shared import constants

_VAR_REFERENCE = re.compile(r"^var\(--of-([a-z-]+)\)$")


@pytest.fixture(autouse=True)
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate theme persistence in a throwaway database.

    ``storage.reset()`` is what actually isolates the test: the module keeps a
    process-global in-memory mirror, so redirecting ``_DB_PATH`` alone would
    leave the previously loaded contents (including the real application
    database's) visible to this test.
    """
    storage.reset()
    monkeypatch.setattr(storage, "_DB_PATH", tmp_path / "theme.db")
    yield
    storage.reset()


def _declared_tokens() -> set[str]:
    """Return every ``--of-*`` token referenced by a COLOR_* constant."""
    tokens: set[str] = set()
    for name, value in vars(constants).items():
        if not name.startswith("COLOR_") or not isinstance(value, str):
            continue
        match = _VAR_REFERENCE.match(value)
        assert match is not None, f"{name} is not a var(--of-*) reference: {value!r}"
        tokens.add(match.group(1))
    return tokens


# ─── Palette integrity ────────────────────────────────────────────────────────


class TestPalettes:
    def test_every_theme_has_a_palette(self) -> None:
        assert set(PALETTES) == set(Theme)

    @pytest.mark.parametrize("theme", list(Theme))
    def test_palette_defines_every_referenced_token(self, theme: Theme) -> None:
        """The guard against a UI constant pointing at a token no palette defines."""
        missing = _declared_tokens() - PALETTES[theme].keys()
        assert not missing, f"{theme.value} palette is missing: {sorted(missing)}"

    def test_all_palettes_define_the_same_token_set(self) -> None:
        """A token present in one theme but not another causes a partial repaint."""
        token_sets = [frozenset(palette) for palette in PALETTES.values()]
        assert len(set(token_sets)) == 1

    @pytest.mark.parametrize("theme", list(Theme))
    def test_no_palette_value_is_empty(self, theme: Theme) -> None:
        assert all(value.strip() for value in PALETTES[theme].values())

    def test_themes_are_visually_distinct(self) -> None:
        """Two themes resolving to identical colours would be a copy-paste slip."""
        rendered = {theme: build_css(theme) for theme in Theme}
        assert len(set(rendered.values())) == len(Theme)


# ─── build_css ────────────────────────────────────────────────────────────────


class TestBuildCss:
    @pytest.mark.parametrize("theme", list(Theme))
    def test_targets_the_root_selector(self, theme: Theme) -> None:
        assert build_css(theme).startswith(":root {")

    @pytest.mark.parametrize("theme", list(Theme))
    def test_declares_every_token_of_the_palette(self, theme: Theme) -> None:
        css = build_css(theme)
        for token, value in PALETTES[theme].items():
            assert f"--of-{token}: {value};" in css

    def test_dark_theme_carries_its_primary(self) -> None:
        assert "--of-primary: #6366f1;" in build_css(Theme.DARK)

    def test_css_is_a_single_balanced_block(self) -> None:
        css = build_css(Theme.LIGHT)
        assert css.count("{") == 1
        assert css.count("}") == 1
        assert css.endswith("}")


# ─── is_dark ──────────────────────────────────────────────────────────────────


class TestIsDark:
    @pytest.mark.parametrize(
        ("theme", "expected"),
        [(Theme.DARK, True), (Theme.CYBERPUNK, True), (Theme.LIGHT, False)],
    )
    def test_classifies_each_theme(self, theme: Theme, expected: bool) -> None:
        assert is_dark(theme) is expected


# ─── Persistence ──────────────────────────────────────────────────────────────


class TestPersistence:
    def test_default_theme_is_dark(self) -> None:
        assert DEFAULT_THEME is Theme.DARK

    def test_unset_preference_falls_back_to_the_default(self) -> None:
        assert load_theme() is DEFAULT_THEME

    @pytest.mark.parametrize("theme", list(Theme))
    def test_saved_theme_round_trips(self, theme: Theme) -> None:
        save_theme(theme)
        assert load_theme() is theme

    def test_the_stored_value_is_the_plain_string(self) -> None:
        """Keeps the database readable and forward-compatible."""
        save_theme(Theme.CYBERPUNK)
        assert storage.store_get("app", "theme") == "cyberpunk"

    def test_an_unrecognised_stored_value_falls_back(self) -> None:
        """A theme removed in a later build must not break startup."""
        storage.store_set("app", "theme", "vaporwave")
        assert load_theme() is DEFAULT_THEME

    def test_saving_overwrites_the_previous_choice(self) -> None:
        save_theme(Theme.LIGHT)
        save_theme(Theme.CYBERPUNK)
        assert load_theme() is Theme.CYBERPUNK


# ─── Live application ─────────────────────────────────────────────────────────


class TestApplyTheme:
    @pytest.fixture
    def emitted_js(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """Capture the JavaScript the theme engine sends to the page."""
        calls: list[str] = []
        monkeypatch.setattr(theme_engine.ui, "run_javascript", calls.append)
        return calls

    def test_switching_persists_the_choice(self, emitted_js: list[str]) -> None:
        theme_engine.apply_theme(Theme.LIGHT)
        assert load_theme() is Theme.LIGHT

    def test_switching_rewrites_the_style_element(self, emitted_js: list[str]) -> None:
        theme_engine.apply_theme(Theme.CYBERPUNK)

        script = emitted_js[0]
        assert "omniforge-theme" in script
        assert "textContent" in script

    def test_the_new_palette_is_embedded_in_the_script(
        self, emitted_js: list[str]
    ) -> None:
        theme_engine.apply_theme(Theme.CYBERPUNK)
        assert PALETTES[Theme.CYBERPUNK]["primary"] in emitted_js[0]

    def test_the_style_element_is_created_when_absent(
        self, emitted_js: list[str]
    ) -> None:
        """First switch after a hot reload must not silently no-op."""
        theme_engine.apply_theme(Theme.LIGHT)
        assert "createElement('style')" in emitted_js[0]
