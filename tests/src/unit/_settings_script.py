"""The settings page's script, extracted once for the JS behaviour tests."""

from __future__ import annotations

import pytest

from ._js_harness import extract_script_body


@pytest.fixture(scope="module")
def settings_script() -> str:
    from ha_mcp.settings_ui import _SETTINGS_HTML

    return extract_script_body(_SETTINGS_HTML)
