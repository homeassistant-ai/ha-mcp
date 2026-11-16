"""Blueprints, automations and scripts on real Core for the ``ha_manage_blueprints`` tests.

Blueprints are files Core's ``automation`` and ``script`` integrations load
from the test's config directory; automations and scripts that use them are set
up from YAML, so Core's own usage index answers whether a blueprint is in use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import dump

from ha_mcp.backup_manager import BackupManager, get_backup_manager
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import get_global_settings, reset_global_settings

from ._config_dir_yaml import setup_from_configuration, write_config_files

PATH = "user/motion.yaml"
SCRIPT_PATH = "user/bedtime.yaml"

MOTION = """\
blueprint:
  name: Motion Light
  description: Turn on a light on motion.
  domain: automation
  input:
    motion_sensor:
      name: Motion Sensor
triggers:
  - trigger: state
    entity_id: !input motion_sensor
    to: 'on'
actions:
  - action: light.turn_on
"""

MOTION_V2 = MOTION.replace("Motion Light", "Motion Light v2")

NO_INPUTS = """\
blueprint:
  name: Porch
  domain: automation
actions:
  - action: light.turn_on
"""

BEDTIME = """\
blueprint:
  name: Bedtime
  domain: script
  input:
    light:
      name: Light
sequence:
  - action: light.turn_off
    target:
      entity_id: !input light
"""

BEDTIME_V2 = BEDTIME.replace("name: Bedtime", "name: Bedtime v2")

BARE_YAML = "blueprint:\n  name: Motion Light\n  domain: automation\n"


def automation_using(alias: str, sensor: str, path: str = PATH) -> dict[str, Any]:
    """An automation built on the blueprint at ``path``."""
    return {
        "id": alias.lower().replace(" ", "_"),
        "alias": alias,
        "use_blueprint": {"path": path, "input": {"motion_sensor": sensor}},
    }


async def set_up_core(
    hass: HomeAssistant,
    blueprints: dict[str, str] | None = None,
    *,
    automations: list[dict[str, Any]] | None = None,
    scripts: dict[str, Any] | None = None,
    search: bool = False,
) -> None:
    """Set up the automation and script integrations over ``blueprints``.

    ``blueprints`` maps ``"<domain>/<path>"`` to the file's text. With
    ``search`` the ``search/related`` command Core answers the usage lookup with
    exists; without it Core has no such command.
    """
    files = {f"blueprints/{key}": text for key, text in (blueprints or {}).items()}
    files["automations.yaml"] = dump(automations or [])
    files["scripts.yaml"] = dump(scripts or {})
    files["configuration.yaml"] = (
        "automation: !include automations.yaml\nscript: !include scripts.yaml\n"
    )
    await write_config_files(hass, files)
    await setup_from_configuration(hass, "automation", "script")
    if search:
        assert await async_setup_component(hass, "search", {})


async def installed_text(hass: HomeAssistant, domain: str, path: str) -> str | None:
    """The text of the blueprint file at ``path``, or ``None`` when it is gone."""
    target = Path(hass.config.path("blueprints", domain, path))

    def read() -> str | None:
        return target.read_text("utf-8") if target.is_file() else None

    return await hass.async_add_executor_job(read)


def frames(sent: AsyncMock, frame_type: str) -> list[dict[str, Any]]:
    """Every ``frame_type`` message the client sent over its WebSocket bridge."""
    return [c.args[0] for c in sent.call_args_list if c.args[0]["type"] == frame_type]


def enable_auto_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: HomeAssistantClient
) -> BackupManager:
    """Auto-backup on, unthrottled and writing under ``tmp_path``; its manager."""
    monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")
    monkeypatch.setenv("AUTO_BACKUP_THROTTLE_MINUTES", "0")
    monkeypatch.setenv("HAMCP_BACKUP_DIR", str(tmp_path / "backups"))
    reset_global_settings()
    return get_backup_manager(client, get_global_settings())


def snapshot_texts(manager: BackupManager, domain: str, path: str) -> list[str]:
    """The YAML text of every snapshot taken of the blueprint at ``path``."""
    return [
        manager.read_snapshot(row["name"])["config"]
        for row in manager.list_snapshots(domain=domain, entity_id=path)
    ]
