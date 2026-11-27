"""Read editor-owned search result bodies without resolving Home Assistant YAML tags."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from homeassistant.config import AUTOMATION_CONFIG_PATH, SCRIPT_CONFIG_PATH
from homeassistant.core import HomeAssistant

from ..yaml_rt import make_yaml, yaml_jsonify

EDITOR_FILES = {"automation": AUTOMATION_CONFIG_PATH, "script": SCRIPT_CONFIG_PATH}


def editor_bodies(
    hass: HomeAssistant, domains: list[str]
) -> dict[str, dict[str, dict[str, Any]]]:
    """Index only the requested editor sources. An unreadable source yields no bodies."""
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for domain in domains:
        path = Path(hass.config.path(EDITOR_FILES[domain]))
        try:
            with path.open(encoding="utf-8") as stream:
                document = yaml_jsonify(make_yaml().load(stream))
        except Exception:  # noqa: BLE001
            # A missing, malformed or unsupported file cannot establish a safe body.
            continue
        if domain == "automation" and isinstance(document, list):
            result[domain] = {
                str(item["id"]): item
                for item in document
                if isinstance(item, dict) and "id" in item
            }
        elif domain == "script" and isinstance(document, dict):
            result[domain] = {
                key: item for key, item in document.items() if isinstance(item, dict)
            }
    return result
