"""Real-Core helpers for the dashboard edit routing tests.

``test-dashboard`` is a storage dashboard in Core's own ``lovelace``. The
server's client edits it through the component's ``dashboard_edit`` command
("native"), through Core's ``lovelace/config/save`` when no component answers
("absent"), or after the component it believed in answered ``unknown_command``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools import card_definitions
from custom_components.ha_mcp_tools import websocket_api as component_commands
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools
from ha_mcp.utils.config_hash import compute_config_hash

from ._command_faults import dropped_on, pooled_socket, received
from ._held_command import held_command
from ._tool_errors import error_body, rejected_call_body

URL = "test-dashboard"
DOCUMENT: dict[str, Any] = {
    "views": [{"cards": [{"type": "button", "icon": "mdi:lamp"}]}]
}
EDIT = "ha_mcp_tools/dashboard_edit"
INFO = "ha_mcp_tools/info"
READ = "lovelace/config"
SAVE = "lovelace/config/save"
# One edit per mode, each emptying the views of ``DOCUMENT``.
EDITS: dict[str, Any] = {
    "patch": [{"op": "remove", "path": "/views/0"}],
    "python_transform": "config['views'] = []",
    "config": {"views": []},
}


def use_backend(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    """``absent``: no component. ``native``: the component's commands.
    ``unknown_command``: the component still advertises ``dashboard_edit``,
    but Core no longer has the command, as after a downgrade mid-session."""
    if backend == "absent":
        return
    # The component keeps its card index, and the task that builds it, for the
    # whole process: one left unfinished by an earlier test belongs to that
    # test's event loop, and a save here would wait on it until it timed out.
    card_definitions.reset_card_definitions_state()
    component_commands.async_register_commands(hass)
    if backend == "unknown_command":
        monkeypatch.delitem(hass.data[websocket_api.DOMAIN], EDIT)


async def lovelace(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    config: Any = DOCUMENT,
    *,
    url_path: str = URL,
    **fields: Any,
) -> str:
    """``url_path`` created in Core with ``config`` saved (None: never saved).

    Returns Core's id for the dashboard.
    """
    assert await async_setup_component(hass, "lovelace", {})
    created = await client.send_websocket_message(
        {
            "type": "lovelace/dashboards/create",
            "url_path": url_path,
            "title": "Test",
            **fields,
        }
    )
    assert created["success"], created
    if config is not None:
        saved = await client.send_websocket_message(
            {"type": SAVE, "url_path": url_path, "config": config}
        )
        assert saved["success"], saved
    dashboard_id: str = created["result"]["id"]
    return dashboard_id


async def stored(client: HomeAssistantClient, url_path: str = URL) -> Any:
    """The config Core holds for ``url_path``; None when it holds none."""
    reply = await client.send_websocket_message(
        {"type": READ, "url_path": url_path, "force": True}
    )
    return reply["result"] if reply["success"] else None


async def registry_row(
    client: HomeAssistantClient, url_path: str
) -> dict[str, Any] | None:
    """Core's dashboard registry row for ``url_path``."""
    listed = await client.send_websocket_message({"type": "lovelace/dashboards/list"})
    return next((r for r in listed["result"] if r["url_path"] == url_path), None)


def live_dashboard(hass: HomeAssistant, url_path: str = URL) -> Any:
    """The dashboard object Core's lovelace serves ``url_path`` from."""
    return hass.data[LOVELACE_DATA].dashboards[url_path]


@contextmanager
def failing(
    hass: HomeAssistant, method: str, *, from_call: int = 1, after_real: bool = False
) -> Iterator[None]:
    """The live dashboard's own ``method`` raises from its ``from_call``-th
    call on; with ``after_real`` it has run first, so what it did stays done."""
    dashboard = live_dashboard(hass)
    real = getattr(dashboard, method)
    calls = 0

    async def run(*args: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls < from_call:
            return await real(*args)
        if after_real:
            await real(*args)
        raise RuntimeError(f"{method} failed")

    with patch.object(dashboard, method, side_effect=run):
        yield


def after_load(hass: HomeAssistant, after: Callable[[], Awaitable[None]]) -> Any:
    """The live dashboard's own load runs, then ``after``."""
    dashboard = live_dashboard(hass)
    real = dashboard.async_load

    async def load(force: bool) -> Any:
        config = await real(force)
        await after()
        return config

    return patch.object(dashboard, "async_load", side_effect=load)


def saved_over(hass: HomeAssistant, later: dict[str, Any]) -> Any:
    """Right after the live dashboard's own save, another writer saves ``later``."""
    dashboard = live_dashboard(hass)
    real = dashboard.async_save

    async def save(config: dict[str, Any]) -> None:
        await real(config)
        await real(later)

    return patch.object(dashboard, "async_save", side_effect=save)


async def set_dashboard(
    client: HomeAssistantClient, url_path: str = URL, **kwargs: Any
) -> dict[str, Any]:
    """``ha_config_set_dashboard`` as an agent calls it."""
    result: dict[str, Any] = await DashboardConfigTools(client).ha_config_set_dashboard(
        url_path=url_path, MandatoryBPS=False, **kwargs
    )
    return result


async def refused(
    client: HomeAssistantClient, url_path: str = URL, **kwargs: Any
) -> dict[str, Any]:
    """The body of the error ``ha_config_set_dashboard`` raises."""
    return await rejected_call_body(set_dashboard(client, url_path, **kwargs))


async def refused_on_save(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    fault: str | None,
    url_path: str = URL,
    **kwargs: Any,
) -> tuple[dict[str, Any], int]:
    """The error of an edit whose save meets ``fault``, and the saves Core got.

    ``dropped``: the connection drops once Core has the save. ``cancelled``:
    the call is cancelled while Core holds the save, which Core then runs.
    ``core_error``: Core applies the save, then fails. ``None``: no fault.
    """
    socket = await pooled_socket(client)
    with ExitStack() as stack:
        if fault == "dropped":
            stack.enter_context(dropped_on(hass, SAVE, socket))
        elif fault == "core_error":
            stack.enter_context(failing(hass, "async_save", after_real=True))
        seen = stack.enter_context(received(hass, SAVE))
        if fault != "cancelled":
            body = await refused(client, url_path, **kwargs)
            return body, len(seen[SAVE])
        async with held_command(hass, SAVE) as held:
            task = asyncio.create_task(set_dashboard(client, url_path, **kwargs))
            await held.reached_by(task)
            task.cancel()
            with pytest.raises(ToolError) as caught:
                await asyncio.gather(task)
        return error_body(caught), len(seen[SAVE])


def advice(body: dict[str, Any]) -> str:
    """Every suggestion of an error body, as one string."""
    return " ".join(body["error"]["suggestions"])


DOCUMENT_HASH = compute_config_hash(DOCUMENT)
