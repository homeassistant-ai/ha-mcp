"""Static compatibility checks for the HACS custom component."""

from __future__ import annotations

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[3]


def test_hacs_requires_supported_home_assistant_version() -> None:
    """Keep the component installable for external-server tools users."""
    metadata = json.loads((_REPO_ROOT / "hacs.json").read_text(encoding="utf-8"))

    assert metadata["homeassistant"] == "2024.11.0"
