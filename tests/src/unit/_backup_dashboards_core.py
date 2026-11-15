"""Core's real dashboards for the dashboard snapshot tests.

``dashboard_manager`` is the backup manager of ``core_client`` on a ``hass``
with Core's ``lovelace`` integration set up. A test creates, saves, edits and
deletes dashboards through Core's own WebSocket commands and reads them back
the same way.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantClient

from ._backup_core import HEADER

BODY: dict[str, Any] = {"views": [{"title": "Kitchen", "cards": []}]}


@pytest.fixture
async def dashboard_manager(
    hass: HomeAssistant, snapshot_manager: bm.BackupManager
) -> bm.BackupManager:
    """``snapshot_manager`` on a ``hass`` that has Core's dashboards."""
    assert await async_setup_component(hass, "lovelace", {})
    return snapshot_manager


async def ws_ok(client: HomeAssistantClient, message: dict[str, Any]) -> Any:
    """Send ``message`` to Core and return its result, which it must accept."""
    reply = await client.send_websocket_message(message)
    assert reply["success"], reply
    return reply.get("result")


async def add_dashboard(
    client: HomeAssistantClient,
    url_path: str = "kitchen-dash",
    *,
    body: dict[str, Any] | None = BODY,
    icon: str | None = None,
    **fields: Any,
) -> None:
    """A storage dashboard, saved with ``body`` unless that is ``None``."""
    await ws_ok(
        client,
        {
            "type": "lovelace/dashboards/create",
            "url_path": url_path,
            "title": "Kitchen",
            "allow_single_word": True,
            **({"icon": icon} if icon else {}),
            **fields,
        },
    )
    if body is not None:
        await ws_ok(
            client,
            {"type": "lovelace/config/save", "url_path": url_path, "config": body},
        )


async def dashboard_row(client: HomeAssistantClient, url_path: str) -> dict[str, Any]:
    """The row ``lovelace/dashboards/list`` holds for ``url_path``."""
    rows = await ws_ok(client, {"type": "lovelace/dashboards/list"})
    return next(row for row in rows if row["url_path"] == url_path)


async def dashboard_paths(client: HomeAssistantClient) -> list[str]:
    """The url_paths ``lovelace/dashboards/list`` holds."""
    rows = await ws_ok(client, {"type": "lovelace/dashboards/list"})
    return [row["url_path"] for row in rows]


async def stored_config(
    client: HomeAssistantClient, url_path: str | None
) -> dict[str, Any] | None:
    """The config Core holds for a dashboard; ``None`` when nothing is stored."""
    message: dict[str, Any] = {"type": "lovelace/config", "force": True}
    if url_path is not None:
        message["url_path"] = url_path
    reply = await client.send_websocket_message(message)
    if reply["success"]:
        config: dict[str, Any] = reply["result"]
        return config
    assert reply["error_code"] == "config_not_found", reply
    return None


def rewrite_snapshot(path: Path, change: Callable[[dict[str, Any]], None]) -> None:
    """Edit a snapshot file's payload in place, as an older release left it."""
    payload = yaml.safe_load(path.read_text())
    change(payload)
    path.write_text(HEADER + yaml.safe_dump(payload, sort_keys=False))
