"""Custom-paths save on the settings page when the answer is not JSON."""

from __future__ import annotations

import pytest

from ._js_harness import run_script
from ._settings_ui_dom import DEFAULT_FETCHES, MIN_DOM

_EDITOR = (
    '<textarea id="fsCustomPathsInput">pyscript</textarea>'
    '<button id="fsCustomPathsSave"></button>'
    '<div id="fsCustomPathsStatus"></div>'
)


@pytest.mark.parametrize("status", [502, 200])
def test_save_names_the_status_of_a_proxy_error_page(
    status: int, settings_script: str
) -> None:
    """A proxy replaces the body of a 502, so there is no JSON to parse."""
    result = run_script(
        settings_script,
        initial_html=MIN_DOM + _EDITOR,
        fetch_map={
            **DEFAULT_FETCHES,
            "/api/settings/fs-custom-paths": {
                "status": status,
                "body": "<html>Bad gateway</html>",
            },
        },
        invoke="await saveFsCustomPaths();",
    )

    assert f"Save failed (HTTP {status})" in result.dom
    assert "SyntaxError" not in result.dom
