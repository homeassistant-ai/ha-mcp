"""The settings UI's backup diff view (JSDOM)."""

from __future__ import annotations

from ._js_harness import _assert_clean_init, run_script
from ._settings_ui_dom import DEFAULT_FETCHES, MIN_DOM
from ._settings_ui_js_helpers import probe


def test_a_diff_shows_its_warnings_above_the_diff(settings_script: str) -> None:
    """What the comparison could not settle is shown with the diff, as text in
    a callout with its icon."""
    warning = "The order of <b>labels</b> could not be told."
    fetches = {
        **DEFAULT_FETCHES,
        "/backups/snap_a/diff": {
            "status": 200,
            "json": {"success": True, "diff": "-a\n+b", "warnings": [warning]},
        },
    }
    result = run_script(
        settings_script,
        initial_html=MIN_DOM,
        fetch_map=fetches,
        settle_ms=300,
        invoke=(
            "await new Promise(r => setTimeout(r, 100));\n"
            "await window.backupAction('diff', 'snap_a');\n"
            "const body = document.getElementById('modalBody');\n"
            "document.body.setAttribute('data-text', body.textContent);\n"
            "document.body.setAttribute('data-injected', "
            "body.querySelectorAll('b').length);\n"
            "document.body.setAttribute('data-icons', "
            "body.querySelectorAll('.ha-alert > .ha-alert-icon').length);\n"
        ),
    )
    _assert_clean_init(result)
    text = probe(result, "text") or ""
    assert text.index(warning) < text.index("-a")
    assert probe(result, "injected") == "0"
    assert probe(result, "icons") == "1"
