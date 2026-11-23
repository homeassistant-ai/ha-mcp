"""The panel URL's ``?tab=``/``&token=`` reaches the embedded settings app (#3104).

Home Assistant hands the panel route its query string, but the boot page embeds
the settings app at a fixed URL, so a notification link such as
``/ha-mcp?tab=tool-security-policies&token=<token>`` landed on the default tab.
These tests drive the shipped boot script through the JSDOM harness and assert
what it would load, including what it refuses to carry over.
"""

from __future__ import annotations

import json

from custom_components.ha_mcp_tools import ui_panel

from ._js_harness import run_script
from ._settings_ui_js_helpers import probe

# The boot page's own DOM: the status element it writes to and the iframe it
# fills. The script runs as soon as it is parsed, so both must exist.
_HTML = """<!DOCTYPE html><html><body>
<div class="msg"></div><iframe></iframe>
</body></html>"""


def _forwarded(search: str) -> str:
    """Return the query the boot script would forward, as ``key:value`` pairs."""
    result = run_script(
        ui_panel.render_boot_script(),
        initial_html=_HTML,
        settle_ms=10,  # no token retry loop: the parent never yields one here
        invoke=(
            "(() => {"
            f"const params = window.forwardedQuery({json.dumps(search)});"
            "const parts = [];"
            "params.forEach((value, key) => { parts.push(key + ':' + value); });"
            "document.body.dataset.forwarded = parts.join('|');"
            "})();"
        ),
    )
    forwarded = probe(result, "forwarded")
    assert forwarded is not None, result.errors
    return forwarded


def test_boot_script_wires_the_parent_query_into_the_app_url() -> None:
    """The wiring itself: read the panel's query and build the iframe URL from it."""
    script = ui_panel.render_boot_script()

    assert "window.parent.location.search" in script
    assert "forwardedQuery(parentSearch())" in script
    assert "APPROVAL_TOKEN_PATTERN" in script


def test_known_tab_and_token_are_forwarded() -> None:
    assert (
        _forwarded("?tab=tool-security-policies&token=abc-123")
        == "tab:tool-security-policies|token:abc-123"
    )


def test_unknown_tab_and_foreign_parameters_are_dropped() -> None:
    """Only the two documented keys cross into the app; the query is untrusted."""
    assert _forwarded("?tab=../../etc&drop=me&token=abc") == "token:abc"
    assert _forwarded("?tab=not-a-panel&token=abc") == "token:abc"
    assert _forwarded("?tab=tool-security-policies") == "tab:tool-security-policies"


def test_token_is_length_capped_and_character_checked() -> None:
    """A crafted link must not smuggle arbitrary text through the token."""
    assert _forwarded("?token=" + "a" * 129) == ""
    assert _forwarded("?token=abc%0Adef") == ""
    assert _forwarded("?token=" + "a" * 128) == "token:" + "a" * 128
