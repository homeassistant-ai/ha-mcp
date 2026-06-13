"""Unit tests for _js_harness.extract_script_body behavior.

Pins the contract that ``data-purpose`` scripts are skipped when
extracting the main inline script, so callers asking for "the script
body" get the primary one, not an auxiliary anti-FOUC or server-prefs
snippet.
"""

from __future__ import annotations

import pytest

from ._js_harness import extract_script_body


def test_skips_data_purpose_script_and_returns_plain_script() -> None:
    """When both a data-purpose script and a plain script are present,
    extract_script_body must return the plain script's body.

    The data-purpose attribute tags auxiliary inline snippets (anti-FOUC,
    server-prefs, theme-toggle) that are separate from the page's main
    script. Callers asking for "the inline script body" mean the main
    one.
    """
    html = """
    <!DOCTYPE html>
    <html>
    <head>
      <script data-purpose="anti-fouc">
        // This is the anti-FOUC script; it should be skipped.
        const theme = "dark";
      </script>
    </head>
    <body>
      <script>
        // This is the main script; it should be returned.
        window.mainScript = true;
      </script>
    </body>
    </html>
    """
    body = extract_script_body(html)
    assert "mainScript" in body
    assert "anti-fouc" not in body.lower()
    assert "theme" not in body


def test_skips_multiple_data_purpose_scripts() -> None:
    """Multiple data-purpose scripts are all skipped; first plain script wins."""
    html = """
    <script data-purpose="server-prefs">const prefs = {};</script>
    <script data-purpose="anti-fouc">const theme = "auto";</script>
    <script>
        // Main
        console.log("main");
    </script>
    """
    body = extract_script_body(html)
    assert "main" in body
    assert "prefs" not in body
    assert "theme" not in body


def test_raises_when_only_data_purpose_scripts_exist() -> None:
    """If there are only data-purpose scripts and no plain script,
    extract_script_body must raise ValueError so the caller knows there
    is no main script to extract.
    """
    html = """
    <script data-purpose="anti-fouc">const x = 1;</script>
    <script data-purpose="server-prefs">const y = 2;</script>
    """
    with pytest.raises(ValueError, match="no inline <script> tag found"):
        extract_script_body(html)
