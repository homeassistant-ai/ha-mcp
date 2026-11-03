"""Tests for generic config-entry reconfiguration."""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import config_entry_identity
from ha_mcp.tools.component_config_entries import UNKNOWN_UNIQUE_ID, EntryUniqueId
from ha_mcp.tools.config_entry_flow import set_config_subentry
from ha_mcp.tools.config_entry_flow_errors import FlowRef, ReconfigureStatus
from ha_mcp.tools.config_entry_flow_walker import _handle_config_subentry_flow_steps
from ha_mcp.tools.config_entry_identity import (
    ReconfigureIdentity,
    _classify_related_entries,
)
from ha_mcp.tools.integration_flow_steps import run_integration_step
from ha_mcp.tools.tools_integrations import IntegrationTools


@pytest.fixture(autouse=True)
def _legacy_registry_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the legacy whole-registry reads for this module.

    The reconfigure path prefers the custom component's server-side filtered
    reads. These tests drive plain client doubles, so the component probe is
    stubbed to a miss here; the component-first path has its own tests below.
    """
    monkeypatch.setattr(
        config_entry_identity,
        "fetch_entities_for_config_entry_via_component",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        config_entry_identity,
        "fetch_device_list_via_component",
        AsyncMock(return_value=None),
    )

    async def _entry_unique_id(client: Any, entry_id: str) -> EntryUniqueId:
        return getattr(client, "_test_entry_unique_id", UNKNOWN_UNIQUE_ID)

    # Home Assistant's config-entry fragment has no unique_id, so the value can
    # only come from the ha_mcp_tools component. The doubles carry it per
    # client, defaulting to "component installed" — see reconfigure_client.
    monkeypatch.setattr(
        config_entry_identity, "fetch_config_entry_unique_id", _entry_unique_id
    )


def reconfigure_client(
    *,
    entity_rows: list[dict[str, Any]] | None = None,
    device_rows: list[dict[str, Any]] | None = None,
    unique_id: str | None = "AA:BB:CC:DD:EE:FF",
    unique_id_known: bool = True,
) -> Any:
    """Build a client double whose registry reads behave like the real client.

    ``HomeAssistantClient.list_entity_registry`` / ``list_device_registry``
    return a list or raise — never a non-awaitable. A bare ``MagicMock()``
    returns a ``MagicMock``, which used to be read as "registry unavailable"
    and silently skipped the identity and duplicate logic these tests exist to
    cover. Starting from this factory means an unstubbed registry read fails
    loudly instead.
    """
    # A menu first step: the first call opens the flow and returns it.
    menu = {"type": "menu", "flow_id": "flow-preview"}
    client = MagicMock(start_reconfigure_flow=AsyncMock(return_value=menu))
    client.abort_config_flow = AsyncMock()
    client.list_entity_registry = AsyncMock(return_value=list(entity_rows or []))
    client.list_device_registry = AsyncMock(return_value=list(device_rows or []))
    # Defaults to a component install reporting the fixture entry's unique_id.
    # Pass unique_id_known=False for an add-on / Docker / PyPI install with no
    # custom component, where the value is simply unreadable.
    client._test_entry_unique_id = EntryUniqueId(
        known=unique_id_known, value=unique_id if unique_id_known else None
    )
    return client


async def reconfigure_config_entry(
    client: Any,
    entry_id: str,
    *,
    config: dict[str, Any] | None = None,
    expected_device_id: str | None = None,
    expected_unique_id: str | None = None,
    expected_mac: str | None = None,
    expected_entity_ids: list[str] | None = None,
) -> dict[str, Any]:
    """One reconfigure call of ``ha_set_integration``, without its auto-backup."""
    return await run_integration_step(
        client,
        domain=None,
        entry_id=entry_id,
        flow_id=None,
        config=config,
        menu_choice=None,
        abort=False,
        describe=False,
        expected_identity={
            "device_id": expected_device_id,
            "unique_id": expected_unique_id,
            "mac": expected_mac,
            "entity_ids": list(expected_entity_ids or []),
        },
    )


@pytest.fixture
def reconfig_entry() -> dict[str, object]:
    """Return a minimal Home Assistant config-entry representation."""
    return {
        "entry_id": "entry-123",
        "domain": "shelly",
        "title": "Living room relay",
        "state": "loaded",
        "unique_id": "AA:BB:CC:DD:EE:FF",
        "supports_reconfigure": True,
    }


@pytest.mark.asyncio
async def test_reconfigure_allows_offline_entry_with_registry_identity() -> None:
    """A setup_retry entry can be changed without contacting the physical device."""
    before = {
        "entry_id": "offline-entry",
        "domain": "shelly",
        "state": "setup_retry",
        "supports_reconfigure": True,
    }
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=before)
    client.list_entity_registry = AsyncMock(
        return_value=[
            {
                "entity_id": "switch.demo_relay",
                "config_entry_id": "offline-entry",
                "device_id": "device-a1",
            }
        ]
    )
    client.list_device_registry = AsyncMock(
        return_value=[
            {
                "id": "device-a1",
                "connections": [["mac", "AA:BB:CC:00:11:22"]],
            }
        ]
    )
    client.list_config_entries = AsyncMock(return_value=[before])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "offline-flow",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client,
        "offline-entry",
        config={"host": "192.0.2.170"},
        expected_device_id="device-a1",
        expected_mac="AA-BB-CC-00-11-22",
        expected_entity_ids=["switch.demo_relay"],
    )

    assert result["success"] is True
    assert result["reason"] == "reconfigure_successful"
    client.submit_config_flow_step.assert_awaited_once_with(
        "offline-flow", {"host": "192.0.2.170"}
    )


@pytest.mark.asyncio
async def test_reconfigure_rejects_registry_duplicate_without_unique_id() -> None:
    """A second entry sharing the registered device is unsafe even without unique_id."""
    before = {
        "entry_id": "offline-entry",
        "domain": "shelly",
        "state": "setup_retry",
        "supports_reconfigure": True,
    }
    client = reconfigure_client(
        entity_rows=[
            {
                "entity_id": "switch.a1",
                "config_entry_id": "offline-entry",
                "device_id": "device-a1",
            }
        ],
        # HA records every entry that contributed to a device here, so this is
        # what tells us a same-domain sibling shares the hardware.
        device_rows=[
            {
                "id": "device-a1",
                "config_entries": ["offline-entry", "duplicate-entry"],
                "connections": [],
                "identifiers": [],
            }
        ],
    )
    client.get_config_entry = AsyncMock(return_value=before)
    client.list_config_entries = AsyncMock(
        return_value=[before, {"entry_id": "duplicate-entry", "domain": "shelly"}]
    )
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client,
            "offline-entry",
            config={"host": "192.0.2.185"},
        )

    payload = json.loads(str(exc_info.value))
    assert "same domain" in payload["error"]["message"].lower()
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_rejects_registry_transport_failure_before_flow() -> None:
    """A dead registry transport must block the mutating flow."""
    before = {
        "entry_id": "offline-entry",
        "domain": "shelly",
        "state": "setup_retry",
        "supports_reconfigure": True,
    }
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=before)
    client.list_entity_registry = AsyncMock(
        side_effect=HomeAssistantConnectionError("registry unavailable")
    )
    client.list_device_registry = AsyncMock(return_value=[])
    client.list_config_entries = AsyncMock(return_value=[before])
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(HomeAssistantConnectionError, match="registry unavailable"):
        await reconfigure_config_entry(
            client,
            "offline-entry",
            config={"host": "192.0.2.185"},
        )

    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_allows_auxiliary_entry_sharing_same_device() -> None:
    """A switch_as_x light is not a duplicate physical Shelly entry."""
    shelly_entry = {
        "entry_id": "shelly-entry",
        "domain": "shelly",
        "title": "Hallway lamp",
        "state": "setup_retry",
        "supports_reconfigure": True,
    }
    switch_as_x_entry = {
        "entry_id": "switch-as-x-entry",
        "domain": "switch_as_x",
        "title": "demo_lamp_switch_0",
        "state": "loaded",
        "supports_reconfigure": False,
    }
    entity_rows = [
        {
            "entity_id": "switch.demo_lamp_switch_0",
            "config_entry_id": "shelly-entry",
            "device_id": "shared-device",
            "platform": "shelly",
        },
        {
            "entity_id": "light.demo_lamp",
            "config_entry_id": "switch-as-x-entry",
            "device_id": "shared-device",
            "platform": "switch_as_x",
        },
    ]
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(
        side_effect=[shelly_entry, {**shelly_entry, "state": "loaded"}]
    )
    client.list_entity_registry = AsyncMock(return_value=entity_rows)
    client.list_device_registry = AsyncMock(
        return_value=[
            {
                "id": "shared-device",
                "connections": [["mac", "AA:BB:CC:33:44:55"]],
                "config_entries": ["shelly-entry", "switch-as-x-entry"],
            }
        ]
    )
    client.list_config_entries = AsyncMock(
        return_value=[shelly_entry, switch_as_x_entry]
    )
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-auxiliary-entry",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client,
        "shelly-entry",
        config={"host": "192.0.2.51"},
        expected_device_id="shared-device",
        expected_mac="AA:BB:CC:33:44:55",
    )

    assert result["success"] is True
    client.start_reconfigure_flow.assert_awaited_once_with("shelly", "shelly-entry")


@pytest.mark.asyncio
async def test_reconfigure_allows_known_auxiliary_handler_without_domain() -> None:
    """A known helper remains allowed when HA exposes its handler, not domain."""
    shelly_entry = {
        "entry_id": "shelly-entry",
        "domain": "shelly",
        "state": "setup_retry",
        "supports_reconfigure": True,
    }
    auxiliary_entry = {
        "entry_id": "switch-as-x-entry",
        "handler": "switch_as_x",
        "state": "loaded",
    }
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=shelly_entry)
    client.list_entity_registry = AsyncMock(
        return_value=[
            {
                "entity_id": "switch.source",
                "config_entry_id": "shelly-entry",
                "device_id": "shared-device",
            },
            {
                "entity_id": "light.derived",
                "config_entry_id": "switch-as-x-entry",
                "device_id": "shared-device",
            },
        ]
    )
    client.list_device_registry = AsyncMock(return_value=[])
    client.list_config_entries = AsyncMock(return_value=[shelly_entry, auxiliary_entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-handler-only-auxiliary",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client,
        "shelly-entry",
        config={"host": "192.0.2.52"},
        expected_device_id="shared-device",
    )

    assert result["success"] is True
    client.start_reconfigure_flow.assert_awaited_once_with("shelly", "shelly-entry")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auxiliary_domain",
    [
        "derivative",
        "history_stats",
        "integration",
        "statistics",
        "threshold",
        "trend",
        "utility_meter",
        "switch_as_x",
    ],
)
async def test_related_entries_known_auxiliary_domain_is_silent(
    auxiliary_domain: str,
) -> None:
    """A helper platform on the source device neither blocks nor warns.

    These attach their entities to the source device exactly as switch_as_x
    does. Blocking on them made every entry carrying one unreconfigurable.
    """
    client = reconfigure_client()
    client.list_config_entries = AsyncMock(
        return_value=[
            {"entry_id": "primary", "domain": "shelly"},
            {"entry_id": "auxiliary", "domain": auxiliary_domain},
        ]
    )

    related = await _classify_related_entries(
        client,
        ReconfigureIdentity(related_entry_ids=["primary", "auxiliary"]),
        entry_id="primary",
        domain="shelly",
    )

    assert related.blocking == []
    assert related.cross_domain == []


@pytest.mark.asyncio
async def test_related_entries_unknown_cross_domain_warns_without_blocking() -> None:
    """An unrecognised cross-domain relation is reported, not refused."""
    client = reconfigure_client()
    client.list_config_entries = AsyncMock(
        return_value=[
            {"entry_id": "primary", "domain": "shelly"},
            {"entry_id": "other", "domain": "some_other_integration"},
        ]
    )

    related = await _classify_related_entries(
        client,
        ReconfigureIdentity(related_entry_ids=["primary", "other"]),
        entry_id="primary",
        domain="shelly",
    )

    assert related.blocking == []
    assert related.cross_domain == ["some_other_integration (other)"]
    warnings = config_entry_identity.cross_domain_warnings(related.cross_domain)
    assert len(warnings) == 1
    assert "some_other_integration (other)" in warnings[0]


@pytest.mark.asyncio
async def test_related_entries_same_domain_blocks() -> None:
    """A second entry from the SAME integration is the real duplicate risk."""
    client = reconfigure_client()
    client.list_config_entries = AsyncMock(
        return_value=[
            {"entry_id": "primary", "domain": "shelly"},
            {"entry_id": "twin", "domain": "shelly"},
        ]
    )

    related = await _classify_related_entries(
        client,
        ReconfigureIdentity(related_entry_ids=["primary", "twin"]),
        entry_id="primary",
        domain="shelly",
    )

    assert related.blocking == ["twin"]
    assert related.cross_domain == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [
        pytest.param(None, id="entry_deleted_mid_flight"),
        pytest.param({"entry_id": "ghost"}, id="row_without_domain"),
        pytest.param({"entry_id": "ghost", "domain": ""}, id="row_with_empty_domain"),
    ],
)
async def test_related_entries_unresolvable_domain_fails_closed(
    row: dict[str, Any] | None,
) -> None:
    """A relation whose domain cannot be read blocks rather than being waved through."""
    entries: list[dict[str, Any]] = [{"entry_id": "primary", "domain": "shelly"}]
    if row is not None:
        entries.append(row)
    client = reconfigure_client()
    client.list_config_entries = AsyncMock(return_value=entries)

    related = await _classify_related_entries(
        client,
        ReconfigureIdentity(related_entry_ids=["primary", "ghost"]),
        entry_id="primary",
        domain="shelly",
    )

    assert related.blocking == ["ghost"]


@pytest.mark.asyncio
async def test_reconfigure_blocks_on_same_domain_device_sharing() -> None:
    """A same-domain sibling on the device stops the flow before it starts."""
    shelly_entry = {
        "entry_id": "shelly-entry",
        "domain": "shelly",
        "state": "setup_retry",
        "supports_reconfigure": True,
    }
    twin_entry = {"entry_id": "twin-entry", "domain": "shelly", "state": "loaded"}
    client = reconfigure_client(
        entity_rows=[
            {
                "entity_id": "switch.source",
                "config_entry_id": "shelly-entry",
                "device_id": "shared-device",
            }
        ],
        device_rows=[
            {
                "id": "shared-device",
                "config_entries": ["shelly-entry", "twin-entry"],
                "connections": [],
                "identifiers": [],
            }
        ],
    )
    client.get_config_entry = AsyncMock(return_value=shelly_entry)
    client.list_config_entries = AsyncMock(return_value=[shelly_entry, twin_entry])
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client,
            "shelly-entry",
            config={"host": "192.0.2.53"},
            expected_device_id="shared-device",
        )

    payload = json.loads(str(exc_info.value))
    assert "same domain" in payload["error"]["message"].lower()
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_rejects_expected_unique_id_mismatch_before_flow(
    reconfig_entry: dict[str, object],
) -> None:
    """A known entry identity mismatch must fail before any mutating flow starts."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client,
            "entry-123",
            config={"host": "192.0.2.183"},
            expected_unique_id="different-device",
        )

    payload = json.loads(str(exc_info.value))
    assert "expected unique_id" in payload["error"]["message"]
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_rejects_expected_mac_mismatch_before_flow(
    reconfig_entry: dict[str, object],
) -> None:
    """A known registry MAC mismatch must fail before any mutating flow starts."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.list_entity_registry = AsyncMock(
        return_value=[
            {
                "entity_id": "switch.living_room",
                "config_entry_id": "entry-123",
                "device_id": "device-living-room",
            }
        ]
    )
    client.list_device_registry = AsyncMock(
        return_value=[
            {
                "id": "device-living-room",
                "connections": [["mac", "AA:BB:CC:DD:EE:FF"]],
            }
        ]
    )
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client,
            "entry-123",
            config={"host": "192.0.2.184"},
            expected_mac="11:22:33:44:55:66",
        )

    payload = json.loads(str(exc_info.value))
    assert "expected MAC" in payload["error"]["message"]
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_rejects_entry_without_identity_before_flow() -> None:
    """An offline entry without an anchor is rejected before mutation.

    No custom component, so the entry's unique_id is unreadable — the normal
    state on an add-on / Docker / PyPI install.
    """
    before = {
        "entry_id": "offline-entry",
        "domain": "shelly",
        "state": "setup_retry",
        "supports_reconfigure": True,
    }
    after = {
        **before,
        "state": "loaded",
        "unique_id": "new-device-identity",
    }
    client = reconfigure_client(unique_id_known=False)
    client.get_config_entry = AsyncMock(side_effect=[before, after])
    client.list_entity_registry = AsyncMock(
        side_effect=[
            [],
            [
                {
                    "entity_id": "switch.offline",
                    "config_entry_id": "offline-entry",
                    "device_id": "new-device",
                }
            ],
        ]
    )
    client.list_device_registry = AsyncMock(
        side_effect=[
            [],
            [
                {
                    "id": "new-device",
                    "config_entries": ["offline-entry"],
                }
            ],
        ]
    )
    client.list_config_entries = AsyncMock(return_value=[after])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-offline-no-identity",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client,
            "offline-entry",
            config={"host": "192.0.2.187"},
        )

    payload = json.loads(str(exc_info.value))
    assert "identity anchor" in payload["error"]["message"]
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_rejects_entry_without_identity_anchor_before_flow() -> None:
    """A bare config entry must not reach the mutating flow without an anchor.

    An MQTT-style entry has no device or entity rows, and without the custom
    component its unique_id cannot be read either — so nothing anchors it.
    """
    entry = {
        "entry_id": "unanchored-entry",
        "domain": "mqtt",
        "state": "loaded",
        "supports_reconfigure": True,
    }
    client = reconfigure_client(unique_id_known=False)
    client.get_config_entry = AsyncMock(return_value=entry)
    client.list_config_entries = AsyncMock(return_value=[entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-unanchored",
            "type": "form",
            "data_schema": [{"name": "broker", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client,
            "unanchored-entry",
            config={"broker": "mosquitto"},
        )

    payload = json.loads(str(exc_info.value))
    assert "identity anchor" in payload["error"]["message"]
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_starts_official_reconfigure_flow_with_entry_id() -> None:
    """The REST client uses entry_id, which makes HA select source=reconfigure."""
    client = HomeAssistantClient(
        base_url="http://homeassistant.local",
        token="test-token",
        verify_ssl=True,
    )
    client._request = AsyncMock(return_value={"flow_id": "flow-1", "type": "form"})

    result = await client.start_reconfigure_flow("esphome", "entry-123")

    assert result["flow_id"] == "flow-1"
    client._request.assert_awaited_once_with(
        "POST",
        "/config/config_entries/flow",
        json={"handler": "esphome", "entry_id": "entry-123"},
    )
    await client.close()


@pytest.mark.asyncio
async def test_reconfigure_rejects_entries_without_official_support(
    reconfig_entry: dict[str, object],
) -> None:
    """Entries without async_step_reconfigure are rejected before any flow starts."""
    reconfig_entry["supports_reconfigure"] = False
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "192.0.2.170"}
        )

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "reconfigure" in payload["error"]["message"].lower()
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_stops_when_registry_transport_is_unavailable() -> None:
    """A dead registry transport must not disable duplicate protection."""
    before = {
        "entry_id": "offline-entry",
        "domain": "shelly",
        "supports_reconfigure": True,
        "unique_id": "device-1",
    }
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=before)
    client.list_entity_registry = AsyncMock(
        side_effect=HomeAssistantConnectionError("registry unavailable")
    )
    client.list_device_registry = AsyncMock(return_value=[])
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(HomeAssistantConnectionError):
        await reconfigure_config_entry(
            client, "offline-entry", config={"host": "192.0.2.185"}
        )

    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_rejects_malformed_registry_response() -> None:
    """A malformed registry payload cannot be treated as an empty registry."""
    before = {
        "entry_id": "malformed-registry-entry",
        "domain": "shelly",
        "supports_reconfigure": True,
        "unique_id": "device-1",
    }
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=before)
    client.list_entity_registry = AsyncMock(return_value={"error": "degraded"})
    client.list_device_registry = AsyncMock(return_value=[])
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(HomeAssistantAPIError):
        await reconfigure_config_entry(
            client,
            "malformed-registry-entry",
            config={"host": "192.0.2.186"},
        )

    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_rejects_malformed_config_entry_row() -> None:
    """A malformed config-entry row cannot disable duplicate protection."""
    before = {
        "entry_id": "malformed-entry-row",
        "domain": "shelly",
        "supports_reconfigure": True,
    }
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=before)
    client.list_entity_registry = AsyncMock(
        return_value=[
            {
                "entity_id": "switch.malformed",
                "config_entry_id": "malformed-entry-row",
                "device_id": "shared-device",
            }
        ]
    )
    client.list_device_registry = AsyncMock(
        return_value=[
            {
                "id": "shared-device",
                "config_entries": ["malformed-entry-row", "related-entry"],
            }
        ]
    )
    client.list_config_entries = AsyncMock(return_value=["malformed"])
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(HomeAssistantAPIError):
        await reconfigure_config_entry(
            client,
            "malformed-entry-row",
            config={"host": "192.0.2.191"},
        )

    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_reads_mac_from_device_connections(
    reconfig_entry: dict[str, object],
) -> None:
    """Expected MAC validation includes device registry connections."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.list_entity_registry = AsyncMock(
        return_value=[
            {
                "entity_id": "switch.living_room",
                "config_entry_id": "entry-123",
                "device_id": "device-living-room",
            }
        ]
    )
    client.list_device_registry = AsyncMock(
        return_value=[
            {
                "id": "device-living-room",
                "connections": [["mac", "AA:BB:CC:DD:EE:FF"]],
                "identifiers": [],
            }
        ]
    )
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client,
            "entry-123",
            config={"host": "192.0.2.187"},
            expected_mac="11:22:33:44:55:66",
        )

    payload = json.loads(str(exc_info.value))
    assert "expected MAC" in payload["error"]["message"]
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_reads_mac_from_device_identifiers(
    reconfig_entry: dict[str, object],
) -> None:
    """Expected MAC validation also accepts device-registry identifiers."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.list_entity_registry = AsyncMock(
        return_value=[
            {
                "entity_id": "switch.living_room",
                "config_entry_id": "entry-123",
                "device_id": "device-living-room",
            }
        ]
    )
    client.list_device_registry = AsyncMock(
        return_value=[
            {
                "id": "device-living-room",
                "connections": [],
                "identifiers": [["shelly", "AA:BB:CC:DD:EE:FF"]],
            }
        ]
    )
    client.list_config_entries = AsyncMock(return_value=[reconfig_entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-identifier-mac",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client,
        "entry-123",
        config={"host": "192.0.2.187"},
        expected_mac="AA:BB:CC:DD:EE:FF",
    )

    assert result["success"] is True


@pytest.mark.asyncio
async def test_reconfigure_rejects_malformed_registry_row() -> None:
    """A malformed registry row fails closed with an API error."""
    before = {
        "entry_id": "malformed-row-entry",
        "domain": "shelly",
        "supports_reconfigure": True,
    }
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=before)
    client.list_entity_registry = AsyncMock(return_value=["malformed"])
    client.list_device_registry = AsyncMock(return_value=[])
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(HomeAssistantAPIError):
        await reconfigure_config_entry(
            client,
            "malformed-row-entry",
            config={"host": "192.0.2.189"},
        )

    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_seeds_device_identity_without_entities() -> None:
    """A device linked directly to an entry remains an identity anchor without entities."""
    before = {
        "entry_id": "device-only-entry",
        "domain": "shelly",
        "supports_reconfigure": True,
        "state": "setup_retry",
    }
    after = {**before, "state": "loaded"}
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(side_effect=[before, after])
    client.list_entity_registry = AsyncMock(return_value=[])
    client.list_device_registry = AsyncMock(
        return_value=[
            {
                "id": "device-only",
                "config_entries": ["device-only-entry"],
                "connections": [["mac", "AA:BB:CC:DD:EE:FF"]],
            }
        ]
    )
    client.list_config_entries = AsyncMock(return_value=[after])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-device-only",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client,
        "device-only-entry",
        config={"host": "192.0.2.188"},
        expected_device_id="device-only",
        expected_mac="AA:BB:CC:DD:EE:FF",
    )

    assert result["success"] is True


@pytest.mark.asyncio
async def test_reconfigure_create_entry_is_applied_but_unverified(
    reconfig_entry: dict[str, object],
) -> None:
    """A reconfigure flow must reject create_entry instead of reporting success."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.list_config_entries = AsyncMock(return_value=[reconfig_entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-created-entry",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "create_entry", "result": {"entry_id": "new-entry"}}
    )

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "192.0.2.189"}
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == "applied_but_unverified"


@pytest.mark.asyncio
async def test_subentry_reconfigure_rejects_unconsumed_values() -> None:
    """Subentry reconfigure aborts enforce the same consumed-key contract."""
    client = reconfigure_client()
    with pytest.raises(ToolError) as exc_info:
        await _handle_config_subentry_flow_steps(
            client,
            "flow-subentry",
            {"type": "abort", "reason": "reconfigure_successful"},
            {"unexpected": True},
            is_reconfigure=True,
            flow=FlowRef(None, "config_subentries.demo", "entry-123"),
        )

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "SERVICE_CALL_FAILED"
    assert payload["status"] == "applied_but_incomplete"


@pytest.mark.asyncio
async def test_subentry_reconfigure_step_budget_aborts_pending_flow() -> None:
    """Subentry budget exhaustion aborts the pending official flow too."""
    client = reconfigure_client()
    client.start_config_subentry_flow = AsyncMock(
        return_value={
            "flow_id": "flow-subentry-budget",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_subentry_flow_step = AsyncMock(
        return_value={
            "flow_id": "flow-subentry-budget",
            "type": "form",
            "step_id": "reconfigure",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.abort_config_subentry_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await set_config_subentry(
            client,
            "entry-123",
            "network",
            {"host": "192.0.2.190"},
            subentry_id="subentry-123",
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == "flow_aborted_before_apply"
    assert payload["flow_budget_exhausted"] is True
    client.abort_config_subentry_flow.assert_awaited_once_with("flow-subentry-budget")


@pytest.mark.asyncio
async def test_subentry_reconfigure_cancellation_aborts_pending_flow() -> None:
    """Cancellation must also clean up a pending config subentry flow."""
    client = reconfigure_client()
    client.start_config_subentry_flow = AsyncMock(
        return_value={
            "flow_id": "flow-subentry-cancelled",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_subentry_flow_step = AsyncMock(
        side_effect=asyncio.CancelledError()
    )
    client.abort_config_subentry_flow = AsyncMock()

    with pytest.raises(asyncio.CancelledError):
        await set_config_subentry(
            client,
            "entry-123",
            "network",
            {"host": "192.0.2.191"},
            subentry_id="subentry-123",
        )

    client.abort_config_subentry_flow.assert_awaited_once_with(
        "flow-subentry-cancelled"
    )


# === Coverage for the branches the review called out as untested ===


@pytest.mark.asyncio
async def test_reconfigure_reports_a_flow_that_never_returned_a_flow_id(
    reconfig_entry: dict[str, object],
) -> None:
    """HA answering start_reconfigure_flow without a flow_id is apply_failed."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(return_value={"type": "abort"})
    client.submit_config_flow_step = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "192.0.2.10"}
        )

    payload = json.loads(str(exc_info.value))
    assert "reconfigure flow without a flow_id" in payload["error"]["message"]
    client.submit_config_flow_step.assert_not_called()


@pytest.mark.asyncio
async def test_reconfigure_tool_wraps_transport_failures_for_the_client(
    reconfig_entry: dict[str, object],
) -> None:
    """A transport failure reaches the MCP caller as a structured ToolError.

    The other transport tests call the module function directly, so nothing
    asserted what a client actually receives through ha_set_integration.
    """
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(
        side_effect=HomeAssistantConnectionError("HA unreachable")
    )
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(
            entry_id="entry-123",
            reconfigure=True,
            config={"host": "192.0.2.11"},
        )

    payload = json.loads(str(exc_info.value))
    assert payload["success"] is False
    assert payload["error"]["code"]
    assert payload["entry_id"] == "entry-123"
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("step_type", "aborted"),
    [
        # An OAuth step cannot be driven: refused and its flow aborted.
        pytest.param("external", True, id="external"),
        pytest.param("external_done", True, id="external_done"),
        # An unknown result type is an error that keeps the flow open.
        pytest.param("something_new", False, id="unexpected-type"),
    ],
)
async def test_reconfigure_stamps_apply_failed_on_undrivable_steps(
    reconfig_entry: dict[str, object], step_type: str, aborted: bool
) -> None:
    """Nothing was committed: the error says so."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(
        return_value={"flow_id": "flow-undrivable", "type": step_type}
    )
    client.abort_config_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "192.0.2.12"}
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == ReconfigureStatus.APPLY_FAILED
    if aborted:
        client.abort_config_flow.assert_awaited_once_with("flow-undrivable")
    else:
        client.abort_config_flow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connection_type", "connection_value", "expected_mac", "should_match"),
    [
        pytest.param("mac", "AA:BB:CC:00:11:22", "AA:BB:CC:00:11:22", True, id="mac"),
        pytest.param(
            "ieee", "00:12:4B:00:1C:A1:B2:C3", "00124B001CA1B2C3", True, id="ieee"
        ),
        pytest.param(
            "zigbee", "00:12:4B:00:1C:A1:B2:C4", "00124B001CA1B2C4", True, id="zigbee"
        ),
        pytest.param(
            "upnp",
            "uuid:0000-1111-2222-3333",
            "uuid:0000-1111-2222-3333",
            False,
            id="upnp_excluded",
        ),
    ],
)
async def test_expected_mac_accepts_hardware_connection_types_only(
    connection_type: str,
    connection_value: str,
    expected_mac: str,
    should_match: bool,
) -> None:
    """expected_mac matches MAC/IEEE/Zigbee connections; other types are not identity.

    A upnp UUID is not a hardware address, so it must not satisfy an
    expected_mac anchor even though it sits in the same connections list.
    """
    entry = {
        "entry_id": "mac-entry",
        "domain": "shelly",
        "state": "loaded",
        "supports_reconfigure": True,
    }
    client = reconfigure_client(
        entity_rows=[
            {
                "entity_id": "switch.mac",
                "config_entry_id": "mac-entry",
                "device_id": "device-mac",
            }
        ],
        device_rows=[
            {
                "id": "device-mac",
                "config_entries": ["mac-entry"],
                "connections": [[connection_type, connection_value]],
                # A malformed row must be skipped rather than crash the read.
                "identifiers": [["shelly"], "not-a-pair", None],
            }
        ],
    )
    client.get_config_entry = AsyncMock(return_value=entry)
    client.list_config_entries = AsyncMock(return_value=[entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-mac",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    if should_match:
        result = await reconfigure_config_entry(
            client,
            "mac-entry",
            config={"host": "192.0.2.20"},
            expected_mac=expected_mac,
        )
        assert result["success"] is True
    else:
        with pytest.raises(ToolError) as exc_info:
            await reconfigure_config_entry(
                client,
                "mac-entry",
                config={"host": "192.0.2.20"},
                expected_mac=expected_mac,
            )
        payload = json.loads(str(exc_info.value))
        assert "expected mac" in payload["error"]["message"].lower()
        client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_subentry_reconfigure_incomplete_does_not_abort_a_committed_flow() -> (
    None
):
    """A committed subentry flow must not be aborted on the way out.

    Mirrors the main-flow guard: once HA reports reconfigure_successful the
    change has landed, so aborting the finished flow is both pointless and
    misleading. Only the unconsumed keys are reported.
    """
    client = MagicMock()
    client.start_config_subentry_flow = AsyncMock(
        return_value={
            "flow_id": "flow-subentry-committed",
            "type": "form",
            "data_schema": [{"name": "name", "required": True}],
        }
    )
    client.submit_config_subentry_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )
    client.abort_config_subentry_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await set_config_subentry(
            client,
            "parent-entry",
            "conversation",
            {"name": "kept", "junk": "never consumed"},
            subentry_id="sub-1",
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == ReconfigureStatus.APPLIED_BUT_INCOMPLETE
    assert payload["unconsumed_config_keys"] == ["junk"]
    client.abort_config_subentry_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_subentry_reconfigure_rejects_create_entry_result() -> None:
    """A subentry reconfigure that creates a new entry is not a success.

    The main-flow twin is covered; this is its sibling on the subentry walker.
    """
    client = MagicMock()
    client.submit_config_subentry_flow_step = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await _handle_config_subentry_flow_steps(
            client,
            "flow-subentry-create",
            {"type": "create_entry", "result": {"subentry_id": "new"}},
            {},
            is_reconfigure=True,
            flow=FlowRef(None, "config_subentries.demo", "entry-123"),
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == ReconfigureStatus.APPLIED_BUT_UNVERIFIED
    assert "instead of updating" in payload["error"]["message"]


# === unique_id: component installs vs add-on / Docker / PyPI installs ===
#
# Home Assistant withholds a config entry's unique_id from every one of its
# endpoints (ConfigEntry.as_json_fragment has no such key, and the REST list,
# config_entries/get and get_single all serialize that fragment). Only the
# ha_mcp_tools custom component, which holds the live ConfigEntry, can supply
# it — so the value has three states and the code must not conflate them.


@pytest.mark.asyncio
async def test_expected_unique_id_is_rejected_without_the_component(
    reconfig_entry: dict[str, object],
) -> None:
    """Without the component the anchor is refused, naming the real reason.

    The failure must not read as "the entry has no unique_id" — nothing could
    read it. An operator on the add-on needs to know which anchors do work.
    """
    client = reconfigure_client(unique_id_known=False)
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client,
            "entry-123",
            config={"host": "192.0.2.30"},
            expected_unique_id="AA:BB:CC:DD:EE:FF",
        )

    message = json.loads(str(exc_info.value))["error"]["message"]
    assert "ha_mcp_tools" in message
    assert "expected_device_id" in message
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_each_reconfigure_step_checks_the_identity_again(
    reconfig_entry: dict[str, object],
) -> None:
    """The flow is answered one step per call, like setup and options, and
    every call that may submit re-reads the entry's identity first."""
    form = {
        "flow_id": "flow-multi",
        "type": "form",
        "step_id": "host",
        "data_schema": [{"name": "host", "required": True}],
    }
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(return_value=form)
    client._request = AsyncMock(return_value=form)  # the step read before a submit
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )
    tools = IntegrationTools(client)

    opened = await tools.ha_set_integration(entry_id="entry-123", reconfigure=True)
    done = await tools.ha_set_integration(
        entry_id="entry-123",
        reconfigure=True,
        flow_id=opened["flow_id"],
        config={"host": "192.0.2.10"},
    )

    assert opened["step_id"] == "host"
    assert done["message"] == "shelly integration reconfigured"
    assert client.get_config_entry.await_count == 2
    client.submit_config_flow_step.assert_awaited_once_with(
        "flow-multi", {"host": "192.0.2.10"}
    )
