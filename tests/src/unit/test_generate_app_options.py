"""The app (add-on) options are generated from the ``Settings`` model.

``scripts/generate_app_options.py`` writes the ``options:`` and ``schema:``
blocks of both ``config.yaml`` files and the ``app_options.json`` table that
``start.py`` reads.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import generate_app_options  # noqa: E402


def test_app_options_match_the_settings_model() -> None:
    """A hand edit of a generated block, or a settings change without a
    regeneration, would let the app page and start.py disagree with the
    server about an option."""
    assert generate_app_options.check() == 0


def test_regeneration_keeps_the_rest_of_config_yaml() -> None:
    """The release job edits ``version:`` and the file holds hand-written
    keys and comments around the blocks; regenerating must not touch them."""
    text = (
        'version: "1.0"\n'
        "# Image comment\n"
        f"{generate_app_options.MARKER}"
        "options:\n"
        "  old_option: true\n"
        "schema:\n"
        "  old_option: bool?\n"
        "# Port comment\n"
        "ports:\n"
        "  9583/tcp: 9583\n"
    )

    result = generate_app_options.replace_blocks(text, "NEW BLOCKS\n", "config.yaml")

    assert result == (
        'version: "1.0"\n'
        "# Image comment\n"
        "NEW BLOCKS\n"
        "# Port comment\n"
        "ports:\n"
        "  9583/tcp: 9583\n"
    )
