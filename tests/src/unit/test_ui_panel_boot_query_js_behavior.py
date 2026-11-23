"""The panel URL's ``?tab=``/``&token=`` reaches the embedded settings app (#3104).

Home Assistant hands the panel route its query string, but the boot page embeds
the settings app at a fixed URL, so a notification link such as
``/ha-mcp?tab=tool-security-policies&token=<token>`` landed on the default tab.

``buildAppUrl()`` returns the URL ``showApp()`` assigns to the iframe's ``src``
verbatim, so asserting on it asserts the load target. The harness runs the boot
script in JSDOM's top window, where ``window.parent`` is the window itself and
cannot be swapped for a fake one; ``parentSearch()`` is therefore covered
separately against fake parent windows, including a cross-origin one.
"""

from __future__ import annotations

from custom_components.ha_mcp_tools import ui_panel

from ._js_harness import run_script
from ._settings_ui_js_helpers import probe

# The boot page's own DOM: the status element it writes to and the iframe it
# fills. The script runs as soon as it is parsed, so both must exist.
_HTML = """<!DOCTYPE html><html><body>
<div class="msg"></div><iframe></iframe>
</body></html>"""

_APP = "/api/ha_mcp_tools/ui/app/settings"


def _value(expression: str) -> str:
    """Evaluate ``expression`` after the boot script and return it as a string."""
    result = run_script(
        ui_panel.render_boot_script(),
        initial_html=_HTML,
        settle_ms=10,  # no token retry loop: the top window yields no bearer
        invoke=(
            "(() => {"
            "const value = " + expression + ";"
            "document.body.dataset.value = String(value).replace(/&/g, '|');"
            "})();"
        ),
    )
    value = probe(result, "value")
    assert value is not None, result.errors
    return value


def test_app_url_carries_the_forwarded_query_and_language() -> None:
    """The built URL keeps both forwarded values and the language hint."""
    assert _value(
        "window.buildAppUrl('?tab=tool-security-policies&token=abc-123&drop=me', 'en')"
    ) == _APP + "?tab=tool-security-policies|token=abc-123|ha_lang=en"


def test_app_url_without_a_forwarded_query_keeps_the_language() -> None:
    assert _value("window.buildAppUrl('', 'en')") == _APP + "?ha_lang=en"


def test_app_url_without_a_query_or_language_is_the_plain_prefix() -> None:
    assert _value("window.buildAppUrl('', '')") == _APP


def test_app_url_for_the_top_window_is_the_plain_prefix() -> None:
    """Outside a parent frame there is nothing to forward (JSDOM's top window)."""
    assert _value("window.appUrl()") == _APP


def test_parent_search_reads_the_parent_query() -> None:
    assert (
        _value("window.parentSearch({parent: {location: {search: '?tab=backups'}}})")
        == "?tab=backups"
    )


def test_parent_search_ignores_the_top_window() -> None:
    assert _value("window.parentSearch(window)") == ""


def test_parent_search_survives_a_cross_origin_parent() -> None:
    assert _value(
        "window.parentSearch({get parent() { throw new Error('cross-origin'); }})"
    ) == ""


def test_forwarded_query_drops_unknown_tab_and_foreign_parameters() -> None:
    """Only the two documented keys cross into the app; the query is untrusted."""
    assert (
        _value("window.forwardedQuery('?tab=../../etc&drop=me&token=abc').toString()")
        == "token=abc"
    )
    assert (
        _value("window.forwardedQuery('?tab=not-a-panel&token=abc').toString()")
        == "token=abc"
    )
    assert (
        _value("window.forwardedQuery('?tab=tool-security-policies').toString()")
        == "tab=tool-security-policies"
    )


def test_forwarded_query_caps_and_checks_the_token() -> None:
    """A crafted link must not smuggle arbitrary text through the token."""
    assert _value("window.forwardedQuery('?token=" + "a" * 129 + "').toString()") == ""
    assert _value("window.forwardedQuery('?token=abc%0Adef').toString()") == ""
    assert (
        _value("window.forwardedQuery('?token=" + "a" * 128 + "').toString()")
        == "token=" + "a" * 128
    )
