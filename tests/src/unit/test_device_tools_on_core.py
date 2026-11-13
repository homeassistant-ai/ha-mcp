"""The device tools on real Core, on an install without the component.

``ha_get_device`` and ``ha_remove_device`` read Core's device and entity
registries and its config entries through the server's own client, so every
row, list and refusal here is Core's. Where a test needs an answer this Core
cannot give (Core before 2026.9 without ``remove``, a composite device that
2026.8's migration split, a refused read), it patches one Core function or
pops the ``remove`` command, and Core still writes the reply.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import attr
import pytest
from homeassistant.components import websocket_api
from homeassistant.components.config import config_entries as config_entries_ws
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
)

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_registry import RegistryTools

from ._client_spies import sent_messages
from ._removable_device import removable_device
from ._tool_errors import rejected_call_body


@pytest.fixture
async def tools(hass: HomeAssistant, core_client: HomeAssistantClient) -> RegistryTools:
    """The registry tools on Core with its ``config`` commands set up."""
    assert await async_setup_component(hass, "config", {})
    return RegistryTools(core_client)


def _device(hass: HomeAssistant, domain: str, **kwargs: Any) -> dr.DeviceEntry:
    """A device in Core's registry, owned by a new config entry of ``domain``."""
    entry = MockConfigEntry(domain=domain)
    entry.add_to_hass(hass)
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **kwargs
    )


async def test_summary_list_is_compact_and_full_keeps_cores_row(
    hass: HomeAssistant, tools: RegistryTools
) -> None:
    """A summary row carries the main fields of Core's row and none of its
    bulky ones; a full row keeps all of Core's row plus the device's entity
    rows, so no field is out of reach."""
    dev = _device(
        hass,
        "hue",
        identifiers={("hue", "0xAABB")},
        name="Lamp",
        manufacturer="Signify",
        model="LCT015",
    )
    er.async_get(hass).async_get_or_create("light", "hue", "lamp", device_id=dev.id)
    core_row = json.loads(json.dumps(dev.dict_repr))
    core_entities = [
        json.loads(json.dumps(entry.as_partial_dict))
        for entry in er.async_entries_for_device(er.async_get(hass), dev.id)
    ]

    summary = (await tools.ha_get_device())["devices"][0]
    full = (await tools.ha_get_device(detail_level="full"))["devices"][0]

    # The fields the detail_level description names.
    assert {
        "id",
        "name",
        "name_by_user",
        "manufacturer",
        "model",
        "area_id",
        "effective_area_id",
        "disabled_by",
        "parent_device_id",
        "via_device_id",
        "integration_type",
    } <= set(summary)
    shared = summary.keys() & core_row.keys()
    assert {k: summary[k] for k in shared} == {k: core_row[k] for k in shared}
    assert not {"identifiers", "connections", "config_entry_id"} & set(summary)
    assert {k: full[k] for k in core_row} == core_row
    assert full["entities"] == core_entities


async def test_single_lookup_lists_cores_linked_devices(
    hass: HomeAssistant, tools: RegistryTools
) -> None:
    """The single lookup carries the devices Core links to it (the device
    page's Linked devices card): those of other integrations that share one
    of its connections."""
    mac = (dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:dd:ee:ff")
    shelly = _device(hass, "shelly", connections={mac})
    unifi = _device(hass, "unifi", connections={mac})

    result = await tools.ha_get_device(device_id=shelly.id)

    assert result["success"] is True
    assert result["device"]["linked_devices"] == [unifi.id]
    # A device without a radio read gets no warnings.
    assert "warnings" not in result


async def test_single_lookup_ignores_summary_detail_level(
    hass: HomeAssistant, tools: RegistryTools
) -> None:
    """``detail_level`` shapes the list only: a single lookup with
    ``"summary"`` still returns Core's whole row."""
    dev = _device(hass, "hue", identifiers={("hue", "0xAABB")})

    result = await tools.ha_get_device(device_id=dev.id, detail_level="summary")

    assert {"identifiers", "config_entries"} <= set(result["device"])


async def test_refused_linked_devices_read_fails_the_lookup(
    hass: HomeAssistant, tools: RegistryTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused linked-devices read fails the lookup in Core's words instead
    of answering without the field. Core refuses it for a device that is gone
    by the time it reads it; this Core is made to not find the device there."""
    dev = _device(hass, "hue", identifiers={("hue", "0xAABB")})
    monkeypatch.setattr(dr.DeviceRegistry, "async_get", lambda self, device_id: None)

    error = await rejected_call_body(tools.ha_get_device(device_id=dev.id))
    assert (
        error["error"]["message"] == "Failed to read linked devices: Device not found"
    )
    assert error["ha_error_code"] == "not_found"


async def test_integration_comes_from_the_owning_config_entry(
    hass: HomeAssistant, tools: RegistryTools
) -> None:
    """The integration is the domain of the device's config entry, not a guess
    from its identifiers: an ESPHome node's main device carries no identifiers
    and a BTHome device only ``("bluetooth", MAC)`` (#2761)."""
    esp = _device(
        hass, "esphome", connections={(dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:00:00:01")}
    )
    bt = _device(hass, "bthome", identifiers={("bluetooth", "AA:BB:CC:DD:EE:FF")})

    esphome = await tools.ha_get_device(integration="esphome")
    bthome = await tools.ha_get_device(integration="bthome")
    bluetooth = await tools.ha_get_device(integration="bluetooth")

    assert [(d["id"], d["integration_type"]) for d in esphome["devices"]] == [
        (esp.id, "esphome")
    ]
    assert [d["id"] for d in bthome["devices"]] == [bt.id]
    assert bluetooth["devices"] == []


async def test_z2m_bridge_found_by_identifier_and_mqtt_filter_keeps_z2m(
    hass: HomeAssistant, tools: RegistryTools
) -> None:
    """The Zigbee2MQTT bridge is the row with Z2M's bridge identifier, whatever
    its name; a device's topic follows the name Zigbee2MQTT gave it, never the
    one set in Home Assistant; ``integration="mqtt"`` still lists both."""
    entry = MockConfigEntry(domain="mqtt")
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    bulb = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("mqtt", "zigbee2mqtt_0x0001")},
        name="kitchen_bulb",
    )
    registry.async_update_device(bulb.id, name_by_user="Kitchen bulb (HA name)")
    coord = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("mqtt", "zigbee2mqtt_bridge_0x00ff")},
        name="Coordinator",
    )

    z2m = await tools.ha_get_device(integration="zigbee2mqtt")
    mqtt = await tools.ha_get_device(integration="mqtt")

    assert z2m["bridge"] == {
        "id": coord.id,
        "name": "Coordinator",
        "ieee_address": "0x00ff",
    }
    rows = {d["id"]: d for d in z2m["devices"]}
    assert rows[bulb.id]["integration_type"] == "zigbee2mqtt"
    assert rows[bulb.id]["friendly_name"] == "kitchen_bulb"
    assert rows[bulb.id]["mqtt_topic_hint"] == "zigbee2mqtt/kitchen_bulb/..."
    assert not {"friendly_name", "mqtt_topic_hint"} & set(rows[coord.id])
    assert {d["id"] for d in mqtt["devices"]} == {bulb.id, coord.id}


async def test_refused_config_entries_read_fails_the_call(
    hass: HomeAssistant, tools: RegistryTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the entries every integration would read as unknown, a negative
    answer the caller could not tell from a real one, so a refused
    ``config_entries/get`` fails the call."""
    _device(hass, "hue", identifiers={("hue", "0xAABB")})

    async def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise HomeAssistantError("entries unavailable")

    monkeypatch.setattr(
        config_entries_ws, "_async_matching_config_entries_json_fragments", refuse
    )

    error = await rejected_call_body(tools.ha_get_device(integration="hue"))

    assert error["error"]["message"] == (
        "Failed to read config entries: entries unavailable"
    )


@pytest.mark.parametrize("tool", ["ha_get_device", "ha_remove_device"])
async def test_split_composite_id_names_the_devices_that_replaced_it(
    hass: HomeAssistant,
    tools: RegistryTools,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
) -> None:
    """An id that 2026.8's migration retired by splitting a composite device
    into one device per config entry is not found, and the error carries
    Core's ``split_ids`` and ``primary_id`` for it; another unknown id gets
    neither. Only a migration from older storage splits a device, so this
    Core is given the split."""
    hue = _device(hass, "hue", identifiers={("hue", "0xAABB")})
    matter = _device(hass, "matter", identifiers={("matter", "node-1")})
    primary = attr.evolve(hue, composite_primary_config_entry=hue.config_entry_id)
    monkeypatch.setattr(
        dr.ActiveDeviceRegistryItems,
        "get_composite_splits",
        lambda self: {"composite": [primary, matter]},
    )

    error = await rejected_call_body(getattr(tools, tool)(device_id="composite"))
    assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert (error["split_ids"], error["primary_id"]) == ([hue.id, matter.id], hue.id)
    assert "split_ids" in error["error"]["suggestion"]

    error = await rejected_call_body(getattr(tools, tool)(device_id="ghost"))
    assert not {"split_ids", "primary_id"} & set(error)
    assert "split_ids" not in error["error"]["suggestion"]


async def test_refused_split_read_fails_the_not_found(
    hass: HomeAssistant, tools: RegistryTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unknown id may be one Home Assistant split, so a refused split read
    fails the call instead of reporting a plain not-found."""

    def refuse(self: Any) -> Any:
        raise HomeAssistantError("splits unavailable")

    monkeypatch.setattr(dr.ActiveDeviceRegistryItems, "get_composite_splits", refuse)

    error = await rejected_call_body(tools.ha_get_device(device_id="composite"))
    assert error["error"]["code"] == "SERVICE_CALL_FAILED"
    assert error["error"]["message"] == (
        "Failed to read composite device splits: splits unavailable"
    )
    assert "split_ids" not in error


async def test_core_without_remove_passes_the_fallbacks_refusal_on(
    hass: HomeAssistant, tools: RegistryTools
) -> None:
    """On Core without ``remove``, a refused ``remove_config_entry`` fails the
    call in Core's words, with Core's code, and the device stays."""
    dev = _device(hass, "hue", identifiers={("hue", "0xAABB")})
    hass.data[websocket_api.DOMAIN].pop("config/device_registry/remove")

    error = await rejected_call_body(tools.ha_remove_device(device_id=dev.id))

    assert error["error"]["message"] == (
        "Failed to remove device: Config entry does not support device removal"
    )
    assert error["ha_error_code"] == "home_assistant_error"
    assert "disabled_by='user'" in error["error"]["suggestion"]
    assert dr.async_get(hass).async_get(dev.id) is not None


async def test_remove_removes_the_device_with_cores_remove(
    hass: HomeAssistant, tools: RegistryTools, core_client: HomeAssistantClient
) -> None:
    """On Core with ``config/device_registry/remove``, that command removes the
    device and the deprecated ``remove_config_entry`` is never sent."""
    dev = await removable_device(hass)

    with sent_messages(core_client) as spy:
        result = await tools.ha_remove_device(device_id=dev.id)

    assert result["success"] is True
    assert dr.async_get(hass).async_get(dev.id) is None
    sent = [c.args[0]["type"] for c in spy.call_args_list]
    assert "config/device_registry/remove" in sent
    assert "config/device_registry/remove_config_entry" not in sent


async def test_core_without_remove_detaches_the_devices_entry(
    hass: HomeAssistant, tools: RegistryTools, caplog: pytest.LogCaptureFixture
) -> None:
    """Core before 2026.9 has no ``config/device_registry/remove``; the tool
    detaches the device's config entry with ``remove_config_entry`` instead,
    which removes the device. This Core is given no ``remove`` command, and
    the expected unknown-command answer logs no error."""
    dev = await removable_device(hass)
    hass.data[websocket_api.DOMAIN].pop("config/device_registry/remove")

    result = await tools.ha_remove_device(device_id=dev.id)

    assert result["success"] is True
    assert dr.async_get(hass).async_get(dev.id) is None
    assert not [
        r
        for r in caplog.records
        if r.name.startswith("ha_mcp") and r.levelno >= logging.ERROR
    ]


@pytest.mark.parametrize(
    ("domain", "identifier", "command", "warning"),
    [
        (
            "zha",
            "00:11:22:33:44:55:66:77",
            "zha/devices",
            "ZHA radio metrics unavailable",
        ),
        (
            "zwave_js",
            "3232323232-5",
            "zwave_js/node_status",
            "Z-Wave node status unavailable",
        ),
        (
            "matter",
            "deadbeef-1",
            "matter/node_diagnostics",
            "Matter node diagnostics unavailable",
        ),
    ],
)
async def test_radio_refusal_warns_in_cores_words(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tools: RegistryTools,
    domain: str,
    identifier: str,
    command: str,
    warning: str,
) -> None:
    """A radio read Core refuses (the radio integration is not loaded) leaves
    the device without its radio fields and warns with Core's own message,
    without the client's ``Command failed:`` prefix."""
    dev = _device(hass, domain, identifiers={(domain, identifier)})

    with sent_messages(core_client) as spy:
        result = await tools.ha_get_device(device_id=dev.id)

    # ZHA's command lists every device; the other two ask for this one.
    expected: dict[str, Any] = {"type": command}
    if domain != "zha":
        expected["device_id"] = dev.id
    assert expected in [c.args[0] for c in spy.call_args_list]

    # Contract: top-level list[str], never nested under ``device``.
    assert result["warnings"] == [f"{warning}: Unknown command."], result
    assert "warnings" not in result["device"]
    assert not {"radio_metrics", "node_status", "node_diagnostics"} & set(
        result["device"]
    )


async def test_zha_device_without_an_ieee_address_is_not_read(
    hass: HomeAssistant, tools: RegistryTools
) -> None:
    """A ZHA device without an IEEE address gives the radio read nothing to
    ask for: none is made, so Core refuses nothing and the answer carries no
    warnings. (A Z-Wave device's node is always Z-Wave JS's to resolve.)"""
    dev = _device(hass, "zha", identifiers={("other", "1")})

    result = await tools.ha_get_device(device_id=dev.id)

    assert result["device"]["integration_type"] == "zha"
    assert "warnings" not in result
