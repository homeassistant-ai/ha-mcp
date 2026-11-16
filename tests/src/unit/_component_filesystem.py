"""Setup shared by the File & YAML Tools filesystem tests.

Home Assistant's config directory is each test's ``tmp_path``; the tests set the
tools entry up with ``make_file_service`` and call its services the way ha-mcp
does.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

import custom_components.ha_mcp_tools as comp

# The keys the component's three stores persist under in ``.storage``.
CALLER_TOKEN_STORE = "ha_mcp_tools_auth"
ALLOWED_PATHS_STORE = "ha_mcp_tools_allowed_paths"
EXTRA_YAML_KEYS_STORE = "ha_mcp_tools_extra_yaml_keys"
LEGACY_BACKUP_DIR = ".ha_mcp_tools_backups"


def config_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.config_dir)


async def put(hass: HomeAssistant, files: Mapping[str, str | bytes]) -> None:
    """Write ``files`` (path relative to the config directory: content) as sent."""
    root = config_dir(hass)

    def write() -> None:
        for name, data in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data if isinstance(data, bytes) else data.encode())

    await hass.async_add_executor_job(write)


def refused(result: Mapping[str, Any]) -> bool:
    """True when the component turned the path away, rather than failing on it."""
    return result["success"] is False and "not allowed" in result["error"]


def use_volume_roots(monkeypatch: pytest.MonkeyPatch, *roots: Path) -> None:
    """Treat ``roots`` as the HAOS sibling volumes, mounted into Core."""
    monkeypatch.setattr(comp, "ALLOWED_VOLUME_ROOTS", tuple(str(r) for r in roots))
    monkeypatch.setattr(comp, "on_mounted_volume", lambda target, volume: True)


def use_volume_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    """Treat ``root`` as the one HAOS sibling volume, mounted into Core."""
    use_volume_roots(monkeypatch, root)
    return root
