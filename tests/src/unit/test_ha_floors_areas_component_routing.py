"""Routing tests for ``ha_list_floors_areas`` over the ``ha_mcp_tools`` component gate.

``ha_list_floors_areas`` fetched the area AND floor registries via two
concurrent but independent WebSocket list calls, a TOCTOU window where a
registry change between the two reads could misclassify an area as
orphaned/unassigned. When the component advertises ``registries``, both
registries come from ONE in-process ``ha_mcp_tools/registries`` read, and
neither legacy list call is sent. Every backend degradation still falls back to
the legacy 2-call gather: no component, ``unknown_command`` → invalidate + fall
back, a non-unknown command error, and a dropped connection.

The auto-backup capture fetchers take the same component read and fall back
to their own one-shot socket, so a fault on the pooled socket never costs a
write its capture.

Everything runs on a real ``hass`` through the server's own client; Core's
registries hold the areas, floors, labels and categories.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    websocket_manager,
)
from ha_mcp.tools import component_registries
from ha_mcp.tools.component_api import component_supports, get_component_caps
from ha_mcp.tools.tools_areas import AreaTools

from ._client_spies import sent_commands, sent_messages
from ._component_frames import drop_connection_on
from ._core_setup import with_config_component

_LEGACY_LISTS = ("config/area_registry/list", "config/floor_registry/list")


@pytest.fixture
async def client(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """``core_client`` with Core's registry commands (``config``)."""
    return await with_config_component(hass, core_client)


@pytest.fixture
def component(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """The same client with the component's commands registered."""
    return component_core_client


@pytest.fixture
async def socket(client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    return await websocket_manager.get_client(
        url=client.base_url, token=client.token, verify_ssl=False
    )


@pytest.fixture
def invalidated(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """A spy on the capability invalidation; the real one still runs."""
    spy = MagicMock(wraps=component_registries.invalidate_caps)
    monkeypatch.setattr(component_registries, "invalidate_caps", spy)
    return spy


def _office_on_ground(hass: HomeAssistant) -> tuple[str, str]:
    """A Ground floor with an Office area on it; returns (floor_id, area_id)."""
    floor = fr.async_get(hass).async_create("Ground")
    area = ar.async_get(hass).async_create("Office", floor_id=floor.floor_id)
    return floor.floor_id, area.id


def _sent_types(spy: AsyncMock) -> list[str]:
    return [str(call.args[0].get("type")) for call in spy.await_args_list]


def _legacy_gather(spy: AsyncMock) -> list[str]:
    return sorted(t for t in _sent_types(spy) if t in _LEGACY_LISTS)


def _registries_frames(spy: AsyncMock) -> list[Any]:
    return [
        c for c in spy.call_args_list if c.args[0] == component_registries.WS_REGISTRIES
    ]


async def _list(client: HomeAssistantClient) -> tuple[dict[str, Any], AsyncMock]:
    """Run the tool; returns its result and a spy on the legacy WS bridge."""
    with sent_messages(client) as bridge:
        resp: dict[str, Any] = await AreaTools(client).ha_list_floors_areas()
    assert resp["success"] is True
    return resp, bridge


async def test_component_served_single_snapshot_skips_legacy_calls(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    component: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """A single ``registries(registries=["area","floor"])`` frame serves both
    registries; NEITHER legacy list call is sent, and the floor nesting /
    unassigned / orphaned partition holds over the component's rows."""
    floor_id, office = _office_on_ground(hass)
    garage = ar.async_get(hass).async_create("Garage")  # unassigned: no floor_id
    # Core keeps a floor_id it was given without checking the floor exists.
    attic = ar.async_get(hass).async_create("Attic", floor_id="ghost")

    with sent_commands(socket) as sent:
        resp, bridge = await _list(client)

    assert _legacy_gather(bridge) == []
    (frame,) = _registries_frames(sent)
    assert set(frame.kwargs["registries"]) == {"area", "floor"}
    assert resp["floor_count"] == 1
    assert resp["area_count"] == 3
    assert resp["unassigned_count"] == 1
    assert resp["orphaned_count"] == 1
    assert resp["floors"][0]["floor_id"] == floor_id
    assert [a["area_id"] for a in resp["floors"][0]["areas"]] == [office]
    assert resp["unassigned_areas"][0]["area_id"] == garage.id
    assert resp["orphaned_areas"][0]["area_id"] == attic.id


async def test_without_the_component_uses_legacy_two_call_gather(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """No component (``info`` is ``unknown_command``) → legacy 2-call gather."""
    _office_on_ground(hass)

    with sent_commands(socket) as sent:
        resp, bridge = await _list(client)

    assert _legacy_gather(bridge) == sorted(_LEGACY_LISTS)
    assert not _registries_frames(sent)
    assert resp["area_count"] == 1


async def test_unknown_command_falls_back_and_invalidates_caps(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    component: HomeAssistantClient,
    invalidated: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``unknown_command`` on ``registries`` (Core's answer for a command it
    never registered) → invalidate caps + legacy 2-call gather."""
    _office_on_ground(hass)
    monkeypatch.setattr(
        component_registries, "WS_REGISTRIES", "ha_mcp_tools/registries_retired"
    )

    resp, bridge = await _list(client)

    assert _legacy_gather(bridge) == sorted(_LEGACY_LISTS)
    assert resp["area_count"] == 1
    invalidated.assert_called_once_with(client)


async def test_a_refused_read_falls_back_without_invalidating_caps(
    client: HomeAssistantClient, component: HomeAssistantClient, invalidated: MagicMock
) -> None:
    """A component error that is not ``unknown_command`` (here its refusal of a
    category read without scopes) is a fallback, not a downgrade: the
    capability is still advertised, only this one read failed."""
    result = await component_registries.fetch_registries_via_component(
        client, ["category"]
    )

    assert result is None
    invalidated.assert_not_called()


async def test_connection_drop_falls_back_to_legacy(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    component: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
    invalidated: MagicMock,
) -> None:
    """The connection dropping on the component read falls back to the legacy
    2-call gather (on a fresh connection) rather than propagating, and keeps
    the capability: a transient drop is not a downgrade."""
    _office_on_ground(hass)
    drop_connection_on(hass, component_registries.WS_REGISTRIES, socket)

    resp, bridge = await _list(client)

    assert _legacy_gather(bridge) == sorted(_LEGACY_LISTS)
    assert resp["area_count"] == 1
    invalidated.assert_not_called()


# --- the auto-backup capture fetchers -------------------------------------------


def _manager(client: HomeAssistantClient, tmp_path: Path) -> bm.BackupManager:
    return bm.get_backup_manager(
        client,
        SimpleNamespace(
            enable_auto_backup=True,
            auto_backup_throttle_minutes=0,
            auto_backup_retain_per_entity=5,
            auto_backup_dir=str(tmp_path / "backups"),
        ),
    )


async def _core_row(
    client: HomeAssistantClient, message: dict[str, Any], key: str, value: str
) -> dict[str, Any]:
    """The row Core's own list command answers for ``value``."""
    reply = await client.send_websocket_message(message)
    (row,) = [r for r in reply["result"] if r[key] == value]
    return dict(row)


async def _label(hass: HomeAssistant, client: HomeAssistantClient) -> tuple[Any, ...]:
    label = lr.async_get(hass).async_create("Favorites")
    row = await _core_row(
        client, {"type": "config/label_registry/list"}, "label_id", label.label_id
    )
    return "label", label.label_id, row


async def _category(
    hass: HomeAssistant, client: HomeAssistantClient
) -> tuple[Any, ...]:
    category = cr.async_get(hass).async_create(scope="automation", name="Lights")
    row = await _core_row(
        client,
        {"type": "config/category_registry/list", "scope": "automation"},
        "category_id",
        category.category_id,
    )
    return (
        "category",
        f"automation:{category.category_id}",
        {"scope": "automation", **row},
    )


async def _area(hass: HomeAssistant, client: HomeAssistantClient) -> tuple[Any, ...]:
    area = ar.async_get(hass).async_create("Office")
    row = await _core_row(
        client, {"type": "config/area_registry/list"}, "area_id", area.id
    )
    return "area_or_floor", f"area:{area.id}", {"kind": "area", **row}


async def _floor(hass: HomeAssistant, client: HomeAssistantClient) -> tuple[Any, ...]:
    floor = fr.async_get(hass).async_create("Ground")
    row = await _core_row(
        client, {"type": "config/floor_registry/list"}, "floor_id", floor.floor_id
    )
    return "area_or_floor", f"floor:{floor.floor_id}", {"kind": "floor", **row}


def _pooled_fails(socket: HomeAssistantWebSocketClient, *, timeout: bool) -> Any:
    """The pooled socket gets no answer: a command timeout, or a dead connection."""
    if timeout:
        return patch.object(
            socket,
            "send_command",
            AsyncMock(side_effect=HomeAssistantCommandTimeout("timeout")),
        )
    return patch.object(
        socket,
        "send_command",
        AsyncMock(side_effect=HomeAssistantConnectionError("pooled socket down")),
    )


_CAPTURES = [
    pytest.param(_label, id="label"),
    pytest.param(_category, id="category"),
    pytest.param(_area, id="area"),
    pytest.param(_floor, id="floor"),
]


class TestBackupCaptureLegacyFallback:
    """The capture fetchers fall back to the legacy per-registry list on their
    own one-shot socket when the component is absent, when the component read
    gets no answer in time, and when the pooled connection is down. Neither
    degradation may surface as a missing item: the legacy fetch still returns
    Core's row."""

    @pytest.mark.parametrize("make", _CAPTURES)
    async def test_without_the_component_falls_back_to_legacy(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        tmp_path: Path,
        make: Any,
    ) -> None:
        domain, target, expected = await make(hass, client)
        handler = _manager(client, tmp_path).handler_for(domain)
        assert handler is not None

        assert await handler.fetch(client, target) == expected

    @pytest.mark.parametrize(
        "timeout", [True, False], ids=["command-timeout", "connection-error"]
    )
    @pytest.mark.parametrize("make", _CAPTURES)
    async def test_a_failed_component_read_falls_back_to_legacy(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        component: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        tmp_path: Path,
        make: Any,
        timeout: bool,
    ) -> None:
        """Losing the capture would cost the wrapped write its backup, or block
        a mandatory one; the pooled socket's failure must not reach it."""
        domain, target, expected = await make(hass, client)
        handler = _manager(client, tmp_path).handler_for(domain)
        assert handler is not None
        assert component_supports(await get_component_caps(client), "registries")

        with _pooled_fails(socket, timeout=timeout) as pooled:
            got = await handler.fetch(client, target)

        assert got == expected
        assert _registries_frames(pooled)
