"""The settings UI's translation catalogs as shipped, for the JS behaviour tests."""

from __future__ import annotations

import json
from pathlib import Path

import ha_mcp.settings_ui


def catalog_messages(locale: str) -> dict[str, str]:
    """``locale``'s own messages as shipped, without the English fallback.

    The served code is lower case (``zh-hans``); the file keeps its own case.
    """
    locales = Path(ha_mcp.settings_ui.__file__).parent / "locales"
    [catalog] = [path for path in locales.glob("*.json") if path.stem.lower() == locale]
    messages: dict[str, str] = json.loads(catalog.read_text(encoding="utf-8"))[
        "messages"
    ]
    return messages
