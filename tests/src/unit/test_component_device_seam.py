"""Cross-seam contract tests for the component's ``device_get`` / ``device_list``.

The tools run on the server's own client against a real ``hass`` that also
answers ``ha_mcp_tools/*``: Core's registries, the component's handlers and the
tools that read their rows are all real, so a key a consumer reads that Core or
the component stops emitting fails here, as does a consumer that misreads the
component's output.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from operator import itemgetter
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import frame
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    websocket_manager,
)
from ha_mcp.tools.tools_radio import RadioTools
from ha_mcp.tools.tools_registry import RegistryTools

# The whole-registry reads a single-device consumer must not fall back to.
REGISTRY_DUMPS = {"config/device_registry/list", "config/entity_registry/list"}


@pytest.fixture
async def socket(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> HomeAssistantWebSocketClient:
    """The pooled WebSocket client every tool command goes through, Core's and
    the component's; sets up Core's ``config`` commands first."""
    assert await async_setup_component(hass, "config", {})
    return await websocket_manager.get_client(
        url=component_core_client.base_url,
        token=component_core_client.token,
        verify_ssl=False,
    )


@contextmanager
def _sent(socket: HomeAssistantWebSocketClient) -> Iterator[AsyncMock]:
    """Spies on every command the tools send; it calls through."""
    with patch.object(socket, "send_command", wraps=socket.send_command) as spy:
        yield spy


def _types(spy: AsyncMock) -> set[str]:
    return {call.args[0] for call in spy.call_args_list}


class TestDeviceSeam:
    """Core's device and entity rows reach every consumer of ``device_get``.

    ``ha_get_device`` (its device row and its entity join through
    ``include_entities``), ``ha_remove_device`` (its device lookup) and
    ``ha_manage_radio``'s ZHA actions (their ``identifiers`` read) each get
    the device from ``device_get``, never from a whole-registry read.
    """

    @pytest.fixture
    def dev(self, hass: HomeAssistant) -> dr.DeviceEntry:
        """A ZHA device in Core's own registries, owned by config entry ``cfg-1``."""
        MockConfigEntry(domain="zha", entry_id="cfg-1").add_to_hass(hass)
        registry = dr.async_get(hass)
        device = registry.async_get_or_create(
            config_entry_id="cfg-1",
            identifiers={("zha", "00:11:22:33:44:55:66:77")},
            name="Kitchen Sensor",
            manufacturer="Aqara",
            model="T1",
            suggested_area="Kitchen",
        )
        updated = registry.async_update_device(
            device.id,
            name_by_user="Kitchen",
            labels={lr.async_get(hass).async_create("important").label_id},
        )
        assert updated is not None
        return updated

    async def test_get_device_keeps_cores_device_and_entity_rows(
        self,
        hass: HomeAssistant,
        dev: dr.DeviceEntry,
        component_core_client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """A field of Core's device row or of its entities' registry rows that
        the single lookup drops, renames or rewrites fails here; the lookup
        reads neither registry whole."""
        entity_registry = er.async_get(hass)
        sensor = entity_registry.async_get_or_create(
            "sensor", "zha", "kitchen-temp", device_id=dev.id
        )
        entity_registry.async_update_entity(sensor.entity_id, name="Kitchen Temp")
        entity_registry.async_get_or_create(
            "update", "zha", "kitchen-fw", device_id=dev.id
        )
        # config/entity_registry/list lists disabled entities too.
        entity_registry.async_get_or_create(
            "sensor",
            "zha",
            "kitchen-rssi",
            device_id=dev.id,
            disabled_by=er.RegistryEntryDisabler.USER,
        )
        with _sent(socket) as spy:
            resp = await RegistryTools(component_core_client).ha_get_device(
                device_id=dev.id
            )

        assert resp["success"] is True
        info = resp["device"]
        core_row = json.loads(json.dumps(dev.dict_repr))
        assert {k: info[k] for k in core_row} == core_row
        assert info["effective_area_id"] == "kitchen"
        assert info["integration_type"] == "zha"
        assert info["ieee_address"] == "00:11:22:33:44:55:66:77"
        assert resp["entity_count"] == 3
        assert "entities" not in resp
        assert not REGISTRY_DUMPS & _types(spy)

        # The entities are Core's config/entity_registry/list rows.
        core_entities = [
            json.loads(json.dumps(entry.as_partial_dict))
            for entry in er.async_entries_for_device(
                entity_registry, dev.id, include_disabled_entities=True
            )
        ]
        by_id = itemgetter("entity_id")
        assert sorted(info["entities"], key=by_id) == sorted(core_entities, key=by_id)

    async def test_core_2026_9_child_device_is_retained_with_effective_area(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Core's reduced child row reaches the public tools.

        The child's own ``area_id`` is unset and it names its owner only in
        ``config_entry_id``. ``area_id`` stays the child's own empty one in the
        list and the single lookup, ``effective_area_id`` carries Core's
        parent-derived area and ``integration_type`` the owning entry's domain.
        The component reads the registry without the deprecated mapping use
        of ``registry.devices``, which Core reports through its frame helper.
        """
        MockConfigEntry(domain="hue", entry_id="cfg-hue").add_to_hass(hass)
        registry = dr.async_get(hass)
        parent = registry.async_get_or_create(
            config_entry_id="cfg-hue",
            identifiers={("hue", "bridge")},
            name="Bridge",
            suggested_area="Office",
        )
        child = registry.async_get_or_create_child(
            config_entry_id="cfg-hue",
            identifiers={("hue", "ch1")},
            name="Channel 1",
            parent_device_id=parent.id,
        )
        tools = RegistryTools(component_core_client)
        with _sent(socket) as spy:
            listed = await tools.ha_get_device()
            single = await tools.ha_get_device(device_id=child.id)

        assert {"ha_mcp_tools/device_list", "ha_mcp_tools/device_get"} <= _types(spy)
        by_id = {row["id"]: row for row in listed["devices"]}
        assert set(by_id) == {parent.id, child.id}
        for row in (by_id[child.id], single["device"]):
            assert (row["area_id"], row["effective_area_id"]) == (None, "office")
            assert row["parent_device_id"] == parent.id
        assert single["device"]["integration_type"] == "hue"
        # Core reports a code line once per test, so a report from setup counts too.
        records = caplog.get_records("setup") + caplog.records
        assert [r.getMessage() for r in records if r.name == frame.__name__] == []

    async def test_remove_device_looks_up_cores_row_without_a_registry_dump(
        self,
        dev: dr.DeviceEntry,
        component_core_client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """The removal finds the device through the component's ``device_get``
        and sends Core's ``remove`` for its id; a lookup that misreads the
        component's row, or falls back to the whole device list, fails here.
        The entry was never set up, so Core refuses the removal itself."""
        with (
            _sent(socket) as spy,
            pytest.raises(ToolError, match="does not support device removal"),
        ):
            await RegistryTools(component_core_client).ha_remove_device(
                device_id=dev.id
            )

        assert "ha_mcp_tools/device_get" in _types(spy)
        removals = [
            call.kwargs["device_id"]
            for call in spy.call_args_list
            if call.args[0] == "config/device_registry/remove"
        ]
        assert removals == [dev.id]
        # A refusal of ``remove`` is Core's answer, not a cue for its alias.
        assert "config/device_registry/remove_config_entry" not in _types(spy)
        assert not REGISTRY_DUMPS & _types(spy)

    async def test_zha_action_targets_the_ieee_of_cores_identifier(
        self,
        dev: dr.DeviceEntry,
        component_core_client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """A ZHA action addresses the IEEE address in the ``identifiers`` of the
        row the component serves; a changed shape of that field fails here.
        ZHA is not loaded, so Core refuses ``zha/device`` itself."""
        with _sent(socket) as spy, pytest.raises(ToolError):
            await RadioTools(component_core_client).ha_manage_radio(
                radio="zigbee", action="diagnostics", device_id=dev.id
            )

        zha_calls = [
            call.kwargs["ieee"]
            for call in spy.call_args_list
            if call.args[0] == "zha/device"
        ]
        assert zha_calls == ["00:11:22:33:44:55:66:77"]
        assert "ha_mcp_tools/device_get" in _types(spy)
        assert not REGISTRY_DUMPS & _types(spy)
