"""Registry-reference validation for ``ha_set_device`` (issue #2159).

``config/device_registry/update`` stores whatever ``area_id`` / ``labels`` it is
handed, so a typo or a since-deleted ID left the device pointing at registry
entries that do not exist while the tool reported success. The write now
preflights both against their registries, and fails closed when a registry
cannot be read — a degraded lookup must not let a dangling reference through.
"""

from __future__ import annotations

import json
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.tools_registry import RegistryTools

from ._client_spies import sent_messages


@pytest.fixture
async def device(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry
) -> dr.DeviceEntry:
    """A device in Core's registry, with the config WebSocket API loaded."""
    assert await async_setup_component(hass, "config", {})
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    return device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "dev1")}
    )


@pytest.fixture
def tools(core_client: HomeAssistantClient) -> RegistryTools:
    return RegistryTools(core_client)


class TestSetDeviceRegistryReferences:
    """Unknown area/label IDs must be rejected before the registry write."""

    async def test_unknown_area_rejected(
        self,
        tools: RegistryTools,
        device: dr.DeviceEntry,
        device_registry: dr.DeviceRegistry,
        area_registry: ar.AreaRegistry,
    ) -> None:
        area_registry.async_create("Living Room")

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_device(device_id=device.id, area_id="ghost_area")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert error_data["area_id"] == "ghost_area"
        stored = device_registry.async_get(device.id)
        assert stored is not None
        assert stored.area_id is None

    async def test_unknown_label_rejected(
        self,
        tools: RegistryTools,
        device: dr.DeviceEntry,
        device_registry: dr.DeviceRegistry,
        label_registry: lr.LabelRegistry,
    ) -> None:
        label_registry.async_create("Important")

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_device(device_id=device.id, labels=["ghost_label"])

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert error_data["unknown_labels"] == ["ghost_label"]
        stored = device_registry.async_get(device.id)
        assert stored is not None
        assert stored.labels == set()

    async def test_registry_lookup_failure_fails_closed(
        self,
        tools: RegistryTools,
        core_client: HomeAssistantClient,
        device: dr.DeviceEntry,
        device_registry: dr.DeviceRegistry,
        area_registry: ar.AreaRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        area_registry.async_create("Living Room")
        monkeypatch.setattr(
            core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("no answer")),
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_device(device_id=device.id, area_id="living_room")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "CONNECTION_FAILED"
        send = cast(AsyncMock, core_client.send_websocket_message)
        assert [c.args[0]["type"] for c in send.call_args_list] == [
            "config/area_registry/list"
        ]
        stored = device_registry.async_get(device.id)
        assert stored is not None
        assert stored.area_id is None

    async def test_existing_area_and_labels_pass_through(
        self,
        tools: RegistryTools,
        device: dr.DeviceEntry,
        device_registry: dr.DeviceRegistry,
        area_registry: ar.AreaRegistry,
        label_registry: lr.LabelRegistry,
    ) -> None:
        area = area_registry.async_create("Living Room")
        label = label_registry.async_create("Important")

        result = await tools.ha_set_device(
            device_id=device.id, area_id=area.id, labels=[label.label_id]
        )

        assert result["success"] is True
        stored = device_registry.async_get(device.id)
        assert stored is not None
        assert stored.area_id == "living_room"
        assert stored.labels == {"important"}

    async def test_empty_label_list_clears_without_lookup(
        self,
        tools: RegistryTools,
        core_client: HomeAssistantClient,
        device: dr.DeviceEntry,
        device_registry: dr.DeviceRegistry,
        label_registry: lr.LabelRegistry,
    ) -> None:
        """An empty list is the documented clear sentinel — nothing to validate."""
        label = label_registry.async_create("Important")
        device_registry.async_update_device(device.id, labels={label.label_id})

        with sent_messages(core_client) as sent:
            result = await tools.ha_set_device(device_id=device.id, labels=[])

        sent_types = [call.args[0]["type"] for call in sent.call_args_list]
        assert "config/label_registry/list" not in sent_types
        assert "config/device_registry/update" in sent_types

        assert result["success"] is True
        stored = device_registry.async_get(device.id)
        assert stored is not None
        assert stored.labels == set()


class TestSetDeviceCoreAnswers:
    """Core's answer to the update passes through: its row, or its refusal code."""

    async def test_core_row_passes_through(
        self,
        tools: RegistryTools,
        device: dr.DeviceEntry,
        device_registry: dr.DeviceRegistry,
    ) -> None:
        """``device_entry`` is Core's updated row, every field as Core sends it."""
        result = await tools.ha_set_device(device_id=device.id, name="Den")

        stored = device_registry.async_get(device.id)
        assert stored is not None
        assert result["device_entry"] == json.loads(json.dumps(stored.dict_repr))

    @pytest.mark.parametrize(
        ("change", "code", "expected", "message"),
        [
            # device_id overrides the existing device's id.
            (
                {"device_id": "ghost", "name": "Den"},
                "not_found",
                "RESOURCE_NOT_FOUND",
                "Device not found",
            ),
            (
                {"disabled_by": "integration"},
                "invalid_format",
                "VALIDATION_INVALID_PARAMETER",
                "at 'disabled_by'",
            ),
        ],
    )
    async def test_core_refusal_keeps_its_code(
        self,
        tools: RegistryTools,
        device: dr.DeviceEntry,
        change: dict[str, str],
        code: str,
        expected: str,
        message: str,
    ) -> None:
        """Core's refusal keeps its code as ``ha_error_code`` and its message
        without the client's prefix."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_device(**{"device_id": device.id, **change})

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == expected
        assert message in error_data["error"]["message"]
        assert "Command failed" not in error_data["error"]["message"]
        assert error_data["ha_error_code"] == code
        # Only not_found points at ha_get_device; for invalid_format Core's
        # message says what is wrong.
        suggests_lookup = "ha_get_device" in json.dumps(error_data["error"])
        assert suggests_lookup == (code == "not_found")

    async def test_unknown_id_before_core_2026_9_suggests_the_lookup(
        self,
        tools: RegistryTools,
        device: dr.DeviceEntry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Core before 2026.9 had no not_found check: its registry raised
        KeyError for an unknown id, which Core answers as unknown_error."""
        monkeypatch.setattr(
            dr.DeviceRegistry,
            "async_update_device",
            Mock(side_effect=KeyError(device.id)),
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_device(device_id=device.id, name="Den")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "SERVICE_CALL_FAILED"
        assert error_data["ha_error_code"] == "unknown_error"
        assert "ha_get_device" in json.dumps(error_data["error"])
