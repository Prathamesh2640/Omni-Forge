"""Theme engine — Dark (default), Light, and Cyberpunk variants (rule E-07).

Colours are delivered as CSS custom properties on ``:root``. Every UI file
references them indirectly through the ``COLOR_*`` constants in
:mod:`shared.constants`, each of which is a ``var(--of-*)`` reference rather
than a literal. Switching a theme therefore only rewrites one ``<style>``
element — no widget is rebuilt and no restart is needed.

Adding a token means adding it to *every* palette;
``test_theme_engine.py`` fails the build if a palette falls behind.

Usage::

    from core.theme_engine import Theme, apply_theme, inject_theme, load_theme

    inject_theme(load_theme())      # once, while building the page head
    apply_theme(Theme.CYBERPUNK)    # later, from the theme switcher
"""
from __future__ import annotations

import enum
import json
from typing import Final

from nicegui import ui

from core.logger import get_logger
from core.storage import store_get, store_set
from shared.constants import (
    STORAGE_KEY_THEME,
    STORAGE_TABLE_APP,
    THEME_STYLE_ELEMENT_ID,
)

logger = get_logger(__name__)


class Theme(enum.StrEnum):
    """The available colour themes."""

    DARK = "dark"
    LIGHT = "light"
    CYBERPUNK = "cyberpunk"


DEFAULT_THEME: Final[Theme] = Theme.DARK

#: Token name → colour, one entry per theme. Token names correspond to the
#: ``var(--of-<token>)`` references in :mod:`shared.constants`.
PALETTES: Final[dict[Theme, dict[str, str]]] = {
    Theme.DARK: {
        "primary": "#6366f1",
        "secondary": "#8b5cf6",
        "accent": "#06b6d4",
        "positive": "#10b981",
        "negative": "#ef4444",
        "warning": "#f59e0b",
        "info": "#3b82f6",
        "bg": "#0f172a",
        "surface": "#1e293b",
        "border": "#334155",
        "text-primary": "#f1f5f9",
        "text-muted": "#94a3b8",
        "text-dim": "#64748b",
        "primary-soft": "rgba(99, 102, 241, 0.15)",
        "negative-soft": "rgba(239, 68, 68, 0.08)",
        "overlay": "rgba(0, 0, 0, 0.20)",
    },
    Theme.LIGHT: {
        "primary": "#4f46e5",
        "secondary": "#7c3aed",
        "accent": "#0891b2",
        "positive": "#059669",
        "negative": "#dc2626",
        "warning": "#d97706",
        "info": "#2563eb",
        "bg": "#f8fafc",
        "surface": "#ffffff",
        "border": "#cbd5e1",
        "text-primary": "#0f172a",
        "text-muted": "#475569",
        "text-dim": "#94a3b8",
        "primary-soft": "rgba(79, 70, 229, 0.10)",
        "negative-soft": "rgba(220, 38, 38, 0.08)",
        "overlay": "rgba(15, 23, 42, 0.04)",
    },
    Theme.CYBERPUNK: {
        "primary": "#f0db4f",
        "secondary": "#ff2e88",
        "accent": "#00f0ff",
        "positive": "#39ff14",
        "negative": "#ff3131",
        "warning": "#ffa500",
        "info": "#00f0ff",
        "bg": "#0a0012",
        "surface": "#15082b",
        "border": "#3d1f6b",
        "text-primary": "#e9d5ff",
        "text-muted": "#a78bfa",
        "text-dim": "#7c5fbd",
        "primary-soft": "rgba(240, 219, 79, 0.15)",
        "negative-soft": "rgba(255, 49, 49, 0.10)",
        "overlay": "rgba(0, 0, 0, 0.35)",
    },
}


def build_css(theme: Theme) -> str:
    """Render *theme* as a ``:root`` CSS custom-property block.

    Args:
        theme: The theme to render.

    Returns:
        A CSS string such as ``:root { --of-primary: #6366f1; ... }``.
    """
    declarations = " ".join(
        f"--of-{token}: {value};" for token, value in PALETTES[theme].items()
    )
    return f":root {{ {declarations} }}"


def is_dark(theme: Theme) -> bool:
    """Return True when *theme* is a dark variant.

    Used to keep Quasar's own light/dark switch in step with the palette.

    Args:
        theme: The theme to inspect.
    """
    return theme is not Theme.LIGHT


def load_theme() -> Theme:
    """Return the persisted theme, falling back to the default.

    An unrecognised stored value (e.g. from an older build) is discarded
    rather than raising.

    Returns:
        The active theme.
    """
    stored = store_get(STORAGE_TABLE_APP, STORAGE_KEY_THEME, default=DEFAULT_THEME.value)
    try:
        return Theme(str(stored))
    except ValueError:
        logger.warning("theme.unknown_stored_value — value=%s", stored)
        return DEFAULT_THEME


def save_theme(theme: Theme) -> None:
    """Persist *theme* as the user's choice.

    Args:
        theme: The theme to store.
    """
    store_set(STORAGE_TABLE_APP, STORAGE_KEY_THEME, theme.value)
    logger.info("theme.saved — theme=%s", theme.value)


def inject_theme(theme: Theme) -> None:
    """Add the theme's style element to the page head.

    Call once while the page is being built. Use :func:`apply_theme` to
    change the theme afterwards.

    Args:
        theme: The theme to render into the page.
    """
    ui.add_head_html(
        f'<style id="{THEME_STYLE_ELEMENT_ID}">{build_css(theme)}</style>'
    )


def apply_theme(theme: Theme) -> None:
    """Switch the live page to *theme* and persist the choice.

    Rewrites the existing style element in place, so the change is instant
    and no widget is rebuilt (rule E-07).

    Args:
        theme: The theme to activate.
    """
    save_theme(theme)
    ui.run_javascript(
        f"(() => {{"
        f"  let el = document.getElementById({json.dumps(THEME_STYLE_ELEMENT_ID)});"
        f"  if (!el) {{"
        f"    el = document.createElement('style');"
        f"    el.id = {json.dumps(THEME_STYLE_ELEMENT_ID)};"
        f"    document.head.appendChild(el);"
        f"  }}"
        f"  el.textContent = {json.dumps(build_css(theme))};"
        f"}})()"
    )
