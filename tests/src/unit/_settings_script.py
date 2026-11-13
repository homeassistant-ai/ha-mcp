"""The served settings page's script, for the JS behaviour tests."""

from __future__ import annotations

import pytest

from ._js_harness import extract_script_body


@pytest.fixture
def settings_script() -> str:
    # Function scope: rendering reads the theme prefs from the data dir, which
    # each test's own environment points at an empty directory.
    from ha_mcp.settings_ui import render_settings_html

    return extract_script_body(render_settings_html())
