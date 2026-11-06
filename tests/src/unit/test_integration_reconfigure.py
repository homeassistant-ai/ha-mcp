"""Tests for generic config-entry reconfiguration."""

import asyncio
import contextlib
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
from ha_mcp.tools import config_entry_identity, integration_flow_steps
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

FORM_SCHEMA = [{"name": "host", "required": True}]
ABORT_OK = {"type": "abort", "reason": "reconfigure_successful"}


def _watch(reloaded: dict[str, Any] | None) -> Any:
    """A ``watch_entry_reload`` whose stream shows ``reloaded`` settling."""

    @contextlib.asynccontextmanager
    async def watch(_client: Any, _entry_id: str) -> Any:
        async def settled() -> dict[str, Any] | None:
            return reloaded

        yield settled

    return watch


@pytest.fixture(autouse=True)
def _no_entry_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """No WebSocket here: the reload after a reconfigure is not observed."""
    monkeypatch.setattr(integration_flow_steps, "watch_entry_reload", _watch(None))


@pytest.fixture(autouse=True)
def _legacy_registry_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the custom-component probes to a miss: these tests drive plain client doubles."""
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
    """Client double whose registry reads return a list or raise, like the real client.

    A bare ``MagicMock()`` would return a non-awaitable and silently skip the
    identity and duplicate logic; ``unique_id_known=False`` models an install
    without the custom component, where the unique_id is unreadable.
    """
    menu = {"type": "menu", "flow_id": "flow-preview"}
    client = MagicMock(start_reconfigure_flow=AsyncMock(return_value=menu))
    client.abort_config_flow = AsyncMock()
    client.list_entity_registry = AsyncMock(return_value=list(entity_rows or []))
    client.list_device_registry = AsyncMock(return_value=list(device_rows or []))
    client._test_entry_unique_id = EntryUniqueId(
        known=unique_id_known, value=unique_id if unique_id_known else None
    )
    return client


def open_client(entry: dict[str, Any], **kwargs: Any) -> Any:
    """``reconfigure_client`` whose flow opens a host form and then aborts successfully."""
    client = reconfigure_client(**kwargs)
    client.get_config_entry = AsyncMock(return_value=entry)
    client.list_config_entries = AsyncMock(return_value=[entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={"flow_id": "flow-1", "type": "form", "data_schema": FORM_SCHEMA}
    )
    client.submit_config_flow_step = AsyncMock(return_value=ABORT_OK)
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
        expected_identity={
            "device_id": expected_device_id,
            "unique_id": expected_unique_id,
            "mac": expected_mac,
            "entity_ids": list(expected_entity_ids or []),
        },
    )


async def rejected(client: Any, entry_id: str, **kwargs: Any) -> dict[str, Any]:
    """Run a reconfigure that must raise ToolError before any flow starts; return its payload."""
    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, entry_id, config={"host": "192.0.2.1"}, **kwargs
        )
    client.start_reconfigure_flow.assert_not_awaited()
    return json.loads(str(exc_info.value))


def entity_row(entry_id: str, device_id: str, entity_id: str = "switch.x") -> dict:
    return {"entity_id": entity_id, "config_entry_id": entry_id, "device_id": device_id}


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
@pytest.mark.parametrize(
    ("reloaded", "status"),
    [
        ({"state": "loaded"}, "applied_and_verified"),
        ({"state": "setup_retry"}, "applied_but_unverified"),
        (None, "applied_but_unverified"),
    ],
)
@pytest.mark.parametrize(
    "call",
    [{"config": {"host": "192.0.2.10"}}, {"flow_id": "flow-1"}],
    ids=["submit", "re-read"],
)
async def test_a_finished_reconfigure_reports_the_state_after_the_reload(
    monkeypatch: pytest.MonkeyPatch,
    opened_flows: dict[str, Any],
    reconfig_entry: dict[str, object],
    reloaded: dict[str, Any] | None,
    status: str,
    call: dict[str, Any],
) -> None:
    monkeypatch.setattr(integration_flow_steps, "watch_entry_reload", _watch(reloaded))
    # A re-read finishes a flow whose progress step ended between calls.
    opened_flows["flow-1"] = ("reconfigure", "entry-123")
    client = open_client(reconfig_entry)
    client._request = AsyncMock(return_value=ABORT_OK)
    result = await IntegrationTools(client).ha_set_integration(
        entry_id="entry-123", reconfigure=True, **call
    )
    assert result["status"] == status
    assert result["entry_state"] == (reloaded or {}).get("state")


@pytest.mark.asyncio
async def test_a_reconfigure_answered_with_another_form_reports_no_state(
    monkeypatch: pytest.MonkeyPatch, reconfig_entry: dict[str, object]
) -> None:
    """Only a finished flow waits for the reload: a second form applied nothing."""
    settled = AsyncMock(return_value={"state": "loaded"})

    @contextlib.asynccontextmanager
    async def watch(_client: Any, _entry_id: str) -> Any:
        yield settled

    monkeypatch.setattr(integration_flow_steps, "watch_entry_reload", watch)
    client = open_client(reconfig_entry)
    client.submit_config_flow_step = AsyncMock(
        return_value={
            "flow_id": "flow-1",
            "type": "form",
            "step_id": "second",
            "data_schema": FORM_SCHEMA,
        }
    )
    result = await IntegrationTools(client).ha_set_integration(
        entry_id="entry-123", reconfigure=True, config={"host": "192.0.2.10"}
    )
    assert result["step_id"] == "second"
    assert "status" not in result
    assert "entry_state" not in result
    settled.assert_not_awaited()


def shelly(entry_id: str = "offline-entry", **extra: Any) -> dict[str, Any]:
    return {
        "entry_id": entry_id,
        "domain": "shelly",
        "state": "setup_retry",
        "supports_reconfigure": True,
        **extra,
    }


@pytest.mark.asyncio
async def test_reconfigure_allows_offline_entry_with_registry_identity() -> None:
    """A setup_retry entry can be changed without contacting the physical device;
    an entry of another integration on the same device is reported, not refused."""
    client = open_client(
        shelly(),
        entity_rows=[entity_row("offline-entry", "device-a1", "switch.demo_relay")],
        device_rows=[
            {
                "id": "device-a1",
                "config_entries": ["offline-entry", "zha-entry"],
                "connections": [["mac", "AA:BB:CC:00:11:22"]],
            }
        ],
    )
    client.list_config_entries.return_value.append(
        {"entry_id": "zha-entry", "domain": "zha"}
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
    assert (result["entry_id"], result["domain"]) == ("offline-entry", "shelly")
    assert "zha (zha-entry)" in result["warnings"][0]
    client.submit_config_flow_step.assert_awaited_once_with(
        "flow-1", {"host": "192.0.2.170"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("expected_device_id", [None, "shared-device"])
async def test_reconfigure_rejects_same_domain_device_sharing(
    expected_device_id: str | None,
) -> None:
    """A same-domain sibling on the registered device stops the flow, with or without unique_id."""
    entry = shelly("shelly-entry")
    client = open_client(
        entry,
        entity_rows=[entity_row("shelly-entry", "shared-device")],
        device_rows=[
            {
                "id": "shared-device",
                "config_entries": ["shelly-entry", "twin-entry"],
                "connections": [],
                "identifiers": [],
            }
        ],
    )
    client.list_config_entries = AsyncMock(
        return_value=[entry, {"entry_id": "twin-entry", "domain": "shelly"}]
    )

    payload = await rejected(
        client, "shelly-entry", expected_device_id=expected_device_id
    )

    assert "same domain" in payload["error"]["message"].lower()


@pytest.mark.asyncio
async def test_reconfigure_rejects_registry_transport_failure_before_flow() -> None:
    """A dead registry transport must block the mutating flow."""
    client = open_client(shelly())
    client.list_entity_registry = AsyncMock(
        side_effect=HomeAssistantConnectionError("registry unavailable")
    )

    with pytest.raises(HomeAssistantConnectionError, match="registry unavailable"):
        await reconfigure_config_entry(
            client, "offline-entry", config={"host": "192.0.2.185"}
        )

    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auxiliary",
    [
        {"entry_id": "switch-as-x-entry", "domain": "switch_as_x"},
        {"entry_id": "switch-as-x-entry", "handler": "switch_as_x"},
    ],
    ids=["domain", "handler_only"],
)
async def test_reconfigure_allows_auxiliary_entry_sharing_same_device(
    auxiliary: dict[str, str],
) -> None:
    """A switch_as_x helper is not a duplicate physical entry, even when only its handler is exposed."""
    entry = shelly("shelly-entry")
    client = open_client(
        entry,
        entity_rows=[
            entity_row("shelly-entry", "shared-device"),
            entity_row("switch-as-x-entry", "shared-device", "light.derived"),
        ],
        device_rows=[
            {
                "id": "shared-device",
                "connections": [["mac", "AA:BB:CC:33:44:55"]],
                "config_entries": ["shelly-entry", "switch-as-x-entry"],
            }
        ],
    )
    client.list_config_entries = AsyncMock(return_value=[entry, auxiliary])

    result = await reconfigure_config_entry(
        client,
        "shelly-entry",
        config={"host": "192.0.2.51"},
        expected_device_id="shared-device",
    )

    assert result["success"] is True
    client.start_reconfigure_flow.assert_awaited_once_with("shelly", "shelly-entry")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("other_domain", "blocking", "cross_domain"),
    [
        *[
            pytest.param(domain, [], [], id=domain)
            for domain in (
                "derivative",
                "history_stats",
                "integration",
                "statistics",
                "threshold",
                "trend",
                "utility_meter",
                "switch_as_x",
            )
        ],
        pytest.param(
            "some_other_integration",
            [],
            ["some_other_integration (other)"],
            id="unknown_cross_domain_warns",
        ),
        pytest.param("shelly", ["other"], [], id="same_domain_blocks"),
    ],
)
async def test_related_entries_classification(
    other_domain: str, blocking: list[str], cross_domain: list[str]
) -> None:
    """Known helper platforms are silent, unknown domains warn, same-domain twins block."""
    client = reconfigure_client()
    client.list_config_entries = AsyncMock(
        return_value=[
            {"entry_id": "primary", "domain": "shelly"},
            {"entry_id": "other", "domain": other_domain},
        ]
    )

    related = await _classify_related_entries(
        client,
        ReconfigureIdentity(related_entry_ids=["primary", "other"]),
        entry_id="primary",
        domain="shelly",
    )

    assert related.blocking == blocking
    assert related.cross_domain == cross_domain
    if cross_domain:
        warnings = config_entry_identity.cross_domain_warnings(related.cross_domain)
        assert len(warnings) == 1
        assert cross_domain[0] in warnings[0]


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
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        pytest.param(
            {"expected_unique_id": "different-device"},
            "expected unique_id",
            id="unique_id",
        ),
        pytest.param({"expected_mac": "11:22:33:44:55:66"}, "expected MAC", id="mac"),
        pytest.param(
            {"expected_device_id": "device-kitchen"},
            "expected device_id",
            id="device_id",
        ),
        pytest.param(
            {"expected_entity_ids": ["switch.x", "switch.y"]},
            "expected entity_ids",
            id="entity_ids",
        ),
    ],
)
async def test_reconfigure_rejects_expected_identity_mismatch_before_flow(
    reconfig_entry: dict[str, object], kwargs: dict[str, str], message: str
) -> None:
    """A known identity mismatch must fail before any mutating flow starts."""
    client = open_client(
        reconfig_entry,
        entity_rows=[entity_row("entry-123", "device-living-room")],
        device_rows=[
            {
                "id": "device-living-room",
                "connections": [["mac", "AA:BB:CC:DD:EE:FF"]],
            }
        ],
    )

    payload = await rejected(client, "entry-123", **kwargs)

    assert message in payload["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    [
        shelly(),
        {
            "entry_id": "offline-entry",
            "domain": "mqtt",
            "state": "loaded",
            "supports_reconfigure": True,
        },
    ],
    ids=["offline_device_entry", "bare_mqtt_style_entry"],
)
async def test_reconfigure_rejects_entry_without_identity_anchor_before_flow(
    entry: dict[str, Any],
) -> None:
    """Without the component, an entry with no device/entity rows has nothing anchoring it."""
    client = open_client(entry, unique_id_known=False)

    payload = await rejected(client, "offline-entry")

    assert "identity anchor" in payload["error"]["message"]


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
    client = open_client(reconfig_entry)

    payload = await rejected(client, "entry-123")

    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "reconfigure" in payload["error"]["message"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"list_entity_registry": {"error": "degraded"}}, id="registry_response"
        ),
        pytest.param({"list_entity_registry": ["malformed"]}, id="registry_row"),
        pytest.param(
            {
                "list_entity_registry": [entity_row("offline-entry", "shared-device")],
                "list_device_registry": [
                    {
                        "id": "shared-device",
                        "config_entries": ["offline-entry", "related-entry"],
                    }
                ],
                "list_config_entries": ["malformed"],
            },
            id="config_entry_row",
        ),
    ],
)
async def test_reconfigure_rejects_malformed_registry_data(
    overrides: dict[str, Any],
) -> None:
    """Malformed registry or config-entry payloads fail closed instead of reading as empty."""
    client = open_client(shelly())
    for method, value in overrides.items():
        setattr(client, method, AsyncMock(return_value=value))

    with pytest.raises(HomeAssistantAPIError):
        await reconfigure_config_entry(
            client, "offline-entry", config={"host": "192.0.2.186"}
        )

    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_seeds_device_identity_without_entities() -> None:
    """A device linked directly to an entry remains an identity anchor without entities."""
    client = open_client(
        shelly("device-only-entry"),
        device_rows=[
            {
                "id": "device-only",
                "config_entries": ["device-only-entry"],
                "connections": [["mac", "AA:BB:CC:DD:EE:FF"]],
            }
        ],
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
async def test_subentry_reconfigure_rejects_unconsumed_values() -> None:
    """Subentry reconfigure aborts enforce the same consumed-key contract."""
    client = reconfigure_client()
    with pytest.raises(ToolError) as exc_info:
        await _handle_config_subentry_flow_steps(
            client,
            "flow-subentry",
            ABORT_OK,
            {"unexpected": True},
            is_reconfigure=True,
            flow=FlowRef(None, "config_subentries.demo", "entry-123"),
        )

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "SERVICE_CALL_FAILED"
    assert payload["status"] == "applied_but_incomplete"


def subentry_client(flow_id: str, field: str = "host", **submit: Any) -> Any:
    """Client double for ``set_config_subentry`` with an open one-field form."""
    client = reconfigure_client()
    client.start_config_subentry_flow = AsyncMock(
        return_value={
            "flow_id": flow_id,
            "type": "form",
            "data_schema": [{"name": field, "required": True}],
        }
    )
    client.submit_config_subentry_flow_step = AsyncMock(**submit)
    client.abort_config_subentry_flow = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_subentry_reconfigure_step_budget_aborts_pending_flow() -> None:
    """Subentry budget exhaustion aborts the pending official flow too."""
    client = subentry_client(
        "flow-subentry-budget",
        return_value={
            "flow_id": "flow-subentry-budget",
            "type": "form",
            "step_id": "reconfigure",
            "data_schema": FORM_SCHEMA,
        },
    )

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
    client = subentry_client(
        "flow-subentry-cancelled", side_effect=asyncio.CancelledError()
    )

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


@pytest.mark.asyncio
async def test_reconfigure_reports_a_flow_that_never_returned_a_flow_id(
    reconfig_entry: dict[str, object],
) -> None:
    """HA answering start_reconfigure_flow without a flow_id submits nothing."""
    client = open_client(reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(return_value={"type": "abort"})

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
    """A transport failure reaches the MCP caller through ha_set_integration as a structured ToolError."""
    client = open_client(reconfig_entry)
    client.get_config_entry = AsyncMock(
        side_effect=HomeAssistantConnectionError("HA unreachable")
    )

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
    client = open_client(reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(
        return_value={"flow_id": "flow-undrivable", "type": step_type}
    )

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
        # A MAC-shaped device identifier counts, whatever its type.
        pytest.param(
            "shelly", "AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:FF", True, id="identifier"
        ),
    ],
)
async def test_expected_mac_accepts_hardware_connection_types_only(
    connection_type: str,
    connection_value: str,
    expected_mac: str,
    should_match: bool,
) -> None:
    """expected_mac matches MAC/IEEE/Zigbee connections; a upnp UUID is not a hardware address."""
    device: dict[str, Any] = {
        "id": "device-mac",
        "config_entries": ["mac-entry"],
        "connections": [],
        # A malformed row must be skipped rather than crash the read.
        "identifiers": [["shelly"], "not-a-pair", None],
    }
    device["identifiers" if connection_type == "shelly" else "connections"].append(
        [connection_type, connection_value]
    )
    client = open_client(
        shelly("mac-entry", state="loaded"),
        entity_rows=[entity_row("mac-entry", "device-mac")],
        device_rows=[device],
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
        payload = await rejected(client, "mac-entry", expected_mac=expected_mac)
        assert "expected mac" in payload["error"]["message"].lower()


@pytest.mark.asyncio
async def test_subentry_reconfigure_incomplete_does_not_abort_a_committed_flow() -> (
    None
):
    """Once HA reports reconfigure_successful the flow is not aborted; only unconsumed keys are reported."""
    client = subentry_client("flow-subentry-committed", "name", return_value=ABORT_OK)

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
    """A subentry reconfigure that creates a new entry is not a success."""
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


@pytest.mark.asyncio
async def test_expected_unique_id_is_rejected_without_the_component(
    reconfig_entry: dict[str, object],
) -> None:
    """Without the component the unique_id anchor is refused, naming the real reason."""
    client = open_client(reconfig_entry, unique_id_known=False)

    payload = await rejected(
        client, "entry-123", expected_unique_id="AA:BB:CC:DD:EE:FF"
    )

    assert "ha_mcp_tools" in payload["error"]["message"]
    assert "expected_device_id" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_each_reconfigure_step_checks_the_identity_again(
    reconfig_entry: dict[str, object],
) -> None:
    """Every call that may submit re-reads the entry's identity first; an abort
    does not, so a flow whose entry is gone can still be ended. A finished flow
    is forgotten, so it is neither continued nor ended; the test opens the flow
    again before the identity-free abort."""
    form = {
        "flow_id": "flow-multi",
        "type": "form",
        "step_id": "host",
        "data_schema": FORM_SCHEMA,
    }
    client = open_client(reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(return_value=form)
    client._request = AsyncMock(return_value=form)  # the step read before a submit
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
    client.start_reconfigure_flow.assert_awaited_once()
    client.submit_config_flow_step.assert_awaited_once_with(
        "flow-multi", {"host": "192.0.2.10"}
    )

    with pytest.raises(ToolError):
        await tools.ha_set_integration(
            entry_id="entry-123", reconfigure=True, flow_id="flow-multi", abort=True
        )
    client.abort_config_flow.assert_not_awaited()

    await tools.ha_set_integration(entry_id="entry-123", reconfigure=True)
    client.get_config_entry.side_effect = HomeAssistantConnectionError("gone")
    await tools.ha_set_integration(
        entry_id="entry-123", reconfigure=True, flow_id="flow-multi", abort=True
    )
    client.abort_config_flow.assert_awaited_once_with("flow-multi")
