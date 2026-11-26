"""The Tools tab links administrators to the embedded integration options."""

from __future__ import annotations

import pytest

from ha_mcp.settings_ui import render_settings_html

from ._js_harness import _assert_clean_init, run_script
from ._settings_ui_dom import DEFAULT_FETCHES
from ._settings_ui_js_helpers import probe


@pytest.mark.parametrize(
    ("mode", "path", "href"),
    [
        (
            "embedded",
            "/api/ha_mcp_tools/ui/app/settings",
            "/config/integrations/integration/ha_mcp_tools",
        ),
        (
            "embedded",
            "/mcp/settings",
            "https://my.home-assistant.io/redirect/integration/?domain=ha_mcp_tools",
        ),
        ("docker", "/mcp/settings", ""),
        ("sidecar", "/mcp/settings", ""),
        ("addon", "/mcp/settings", ""),
    ],
)
def test_integration_settings_link(
    settings_script: str, mode: str, path: str, href: str
) -> None:
    result = run_script(
        settings_script,
        initial_html=render_settings_html(),
        prelude=f"history.replaceState(null, '', '{path}');",
        fetch_map={
            **DEFAULT_FETCHES,
            "/api/settings/info": {"status": 200, "json": {"deployment_mode": mode}},
        },
        invoke="""
            await window.applyInfoChrome();
            const notice = document.getElementById('sharedSettingsNotice');
            const link = notice.querySelector('a');
            document.body.dataset.href = link.getAttribute('href') || '';
            document.body.dataset.disabled = link.getAttribute('aria-disabled') || '';
            document.body.dataset.target = link.target;
            document.body.dataset.copy = [...notice.querySelectorAll('span')]
              .filter(span => !span.hidden).map(span => span.textContent).join(' ');
        """,
    )
    _assert_clean_init(result)
    assert probe(result, "href") == href
    assert probe(result, "disabled") == ("" if mode == "embedded" else "true")
    assert probe(result, "target") == "_blank"
    copy = probe(result, "copy") or ""
    assert ("select the gear" in copy) == (mode == "embedded")
    assert ("app (add-on)" in copy) == (mode != "embedded")
