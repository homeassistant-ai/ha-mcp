"""Storage helpers, tags and the Settings restore route on a real ``hass``, for
the backup target identity tests.

Items are created through Core's own ``<type>/create`` commands, their entities
are renamed in the entity registry, and the manager reads them back over the
client. A snapshot file is placed under the name an older capture left it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from starlette.requests import Request

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.settings_ui import build_settings_handlers
from ha_mcp.tools import tools_filesystem
from ha_mcp.tools.helpers import raise_tool_error
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._backup_core import manage_backup, place_snapshot
from ._config_dir_yaml import write_config_files


async def create_named(
    client: HomeAssistantClient, helper_type: str, name: str, **fields: Any
) -> str:
    """A storage helper called ``name``; its storage id (Core's slug of the name)."""
    created = await client.send_websocket_message(
        {"type": f"{helper_type}/create", "name": name, **fields}
    )
    assert created["success"], created
    return str(created["result"]["id"])


async def rename_item(
    client: HomeAssistantClient, helper_type: str, item_id: str, name: str
) -> None:
    """Change the stored name of a helper, as an edit after a backup does."""
    updated = await client.send_websocket_message(
        {"type": f"{helper_type}/update", f"{helper_type}_id": item_id, "name": name}
    )
    assert updated["success"], updated


def entity_of(registry: er.EntityRegistry, platform: str, item_id: str) -> str:
    """The entity_id of the registry entry of storage item ``item_id``."""
    entity_id = registry.async_get_entity_id(platform, platform, item_id)
    assert entity_id is not None, f"{platform} {item_id} has no registry entry"
    return entity_id


def move_entity(
    registry: er.EntityRegistry, platform: str, item_id: str, entity_id: str
) -> None:
    """Rename the entity of ``item_id``, as a user does in the entity settings."""
    registry.async_update_entity(
        entity_of(registry, platform, item_id), new_entity_id=entity_id
    )


async def held_by(
    manager: bm.BackupManager,
    client: HomeAssistantClient,
    domain: str,
    target: str,
) -> Any:
    """What the manager's handler for ``domain`` snapshots for ``target``."""
    handler = manager.handler_for(domain)
    assert handler is not None
    return await handler.fetch(client, target)


async def create_tag(
    client: HomeAssistantClient, tag_id: str, name: str | None = None
) -> None:
    created = await client.send_websocket_message(
        {"type": "tag/create", "tag_id": tag_id, **({"name": name} if name else {})}
    )
    assert created["success"], created


async def tag_ids(client: HomeAssistantClient) -> list[str]:
    listed = await client.send_websocket_message({"type": "tag/list"})
    assert listed["success"], listed
    return [str(tag["id"]) for tag in listed["result"]]


async def remove_tag(
    client: HomeAssistantClient, target: str, *, wait: bool = False
) -> dict[str, Any]:
    result: dict[str, Any] = await IntegrationTools(
        client
    ).ha_remove_helpers_integrations(
        target=target, helper_type="tag", confirm=True, wait=wait
    )
    return result


def place_item_snapshot(
    directory: Path,
    domain: str,
    entity_id: str,
    item_id: str,
    name: str,
    **recorded: Any,
) -> str:
    """A snapshot of storage item ``item_id`` keyed by the ``entity_id`` it had;
    its file name."""
    placed = place_snapshot(
        directory,
        f"{domain}.{entity_id}.20260521_120000.yaml",
        entity_id,
        domain=domain,
        held={"id": item_id, "name": name, **recorded},
        text=False,
    )
    return placed.name


async def yaml_snapshot_whose_write_is_refused(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """A snapshot of the YAML key ``alert2``, which the operator allowed, whose
    restore reads its safety backup and then has its write refused with the
    server's own component error. The component refuses only at its token
    bootstrap, ahead of that read, so the server call is the seam for the
    write alone. The snapshot's name."""
    monkeypatch.setattr(settings, "extra_yaml_write_keys", "alert2")
    await write_config_files(
        hass, {"packages/alerts.yaml": "alert2:\n  defaults: {}\n"}
    )
    call = tools_filesystem.call_mcp_tools_service

    async def refuse_the_write(
        client: Any, service: str, data: dict[str, Any], **kw: Any
    ) -> Any:
        if service == "edit_yaml_config":
            raise_tool_error(
                create_error_response(
                    ErrorCode.COMPONENT_NOT_INSTALLED,
                    "The installed ha_mcp_tools custom component is too old.",
                    suggestions=["Update it and restart Home Assistant"],
                )
            )
        return await call(client, service, data, **kw)

    monkeypatch.setattr(tools_filesystem, "call_mcp_tools_service", refuse_the_write)
    return place_snapshot(
        manager.backup_dir,
        "yaml.alerts.20260521_120000.yaml",
        "packages/alerts.yaml::alert2",
        domain="yaml",
        held="defaults:\n  severity: low\n",
    ).name


async def config_file_text(hass: HomeAssistant, path: str) -> str:
    """The text of ``path`` in Core's config directory."""
    return await hass.async_add_executor_job(Path(hass.config.path(path)).read_text)


def rejected_script_snapshot(directory: Path) -> str:
    """A snapshot of script ``hall_lights`` with a key Core refuses to save; the
    snapshot's name."""
    return place_snapshot(
        directory,
        "script.hall_lights.20260521_120000.yaml",
        "hall_lights",
        domain="script",
        held={"alias": "Hall", "sequence": [], "bogus_key": 1},
        text=False,
    ).name


async def restore_of(client: HomeAssistantClient, name: str) -> dict[str, Any]:
    """``ha_manage_backup`` restoring the snapshot ``name``."""
    restored: dict[str, Any] = await manage_backup(
        client, scope="edits", action="restore", backup_name=name
    )
    return restored


async def failed_restore(
    client: HomeAssistantClient, name: str
) -> tuple[dict[str, Any], ToolError]:
    """The reply a restore that raises carries, and the error that carried it."""
    with pytest.raises(ToolError) as caught:
        await restore_of(client, name)
    reply: dict[str, Any] = json.loads(str(caught.value))
    return reply, caught.value


async def settings_restore(
    client: HomeAssistantClient, name: str
) -> tuple[int, dict[str, Any]]:
    """The Settings route restoring ``name`` on a real server: its status and body."""
    server = HomeAssistantSmartMCPServer(client=client)
    handler = build_settings_handlers(server)["restore_backup"]
    response = await handler(Request({"type": "http", "path_params": {"name": name}}))
    body: dict[str, Any] = json.loads(bytes(response.body))
    return response.status_code, body
