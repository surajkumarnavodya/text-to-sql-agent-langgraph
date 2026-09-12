"""Unit tests for ui/theme.py's pure logic.

CSS content itself isn't meaningfully unit-testable (no existing precedent
in this repo for asserting on injected `<style>` text) -- these cover the
one piece of real logic, `get_theme_mode()`'s default/read-back behavior.
`inject_theme_css()`/`render_theme_toggle()` just call `st.markdown`/
`st.toggle` and are exercised by the manual Streamlit smoke test instead.

`st.session_state` is monkeypatched to a plain dict, same as a real
session's `.get()`-compatible behavior -- `ui.theme` only ever reads it via
`.get()`, never anything dict-incompatible.
"""

from __future__ import annotations

import streamlit as st

from ui.theme import DEFAULT_THEME_MODE, THEME_VARIABLES, get_theme_mode


class TestGetThemeMode:
    def test_defaults_to_light_when_unset(self, monkeypatch):
        monkeypatch.setattr(st, "session_state", {})
        assert get_theme_mode() == "light"
        assert DEFAULT_THEME_MODE == "light"

    def test_reflects_dark_once_set(self, monkeypatch):
        monkeypatch.setattr(st, "session_state", {"theme_mode": "dark"})
        assert get_theme_mode() == "dark"

    def test_reflects_light_once_set(self, monkeypatch):
        monkeypatch.setattr(st, "session_state", {"theme_mode": "light"})
        assert get_theme_mode() == "light"

    def test_unrecognized_value_falls_back_to_light(self, monkeypatch):
        """Defensive: session_state is a plain, unvalidated dict -- any
        stray/corrupted value must never propagate as a theme mode neither
        THEME_VARIABLES nor the CSS template recognizes."""
        monkeypatch.setattr(st, "session_state", {"theme_mode": "not-a-real-theme"})
        assert get_theme_mode() == "light"


class TestThemeVariables:
    def test_light_and_dark_both_defined(self):
        assert set(THEME_VARIABLES) == {"light", "dark"}

    def test_both_themes_define_the_same_variable_keys(self):
        """The CSS template substitutes the same set of keys regardless of
        mode -- a theme missing one would raise a KeyError at render time,
        not a silently broken style."""
        assert set(THEME_VARIABLES["light"]) == set(THEME_VARIABLES["dark"])

    def test_light_theme_values_are_unchanged_from_the_original_palette(self):
        """Flipping the toggle must never alter the default experience for
        anyone who doesn't touch it -- the light palette must stay exactly
        what it was before dark mode existed."""
        light = THEME_VARIABLES["light"]
        assert light["primary"] == "#4f46e5"
        assert light["card"] == "#ffffff"
        assert light["text"] == "#1e293b"
