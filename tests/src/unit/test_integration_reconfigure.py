"""Tests for generic config-entry reconfiguration."""

import asyncio
import inspect
import json
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import config_entry_flow
from ha_mcp.tools.component_config_entries import UNKNOWN_UNIQUE_ID, EntryUniqueId
from ha_mcp.tools.config_entry_flow import (
    PreparedReconfigure,
    ReconfigureIdentity,
    _classify_related_entries,
    reconfigure_config_entry,
    set_config_subentry,
)
from ha_mcp.tools.config_entry_flow_walker import (
    ReconfigureStatus,
    _handle_config_subentry_flow_steps,
)
from ha_mcp.tools.integration_reconfigure import _reconfigure_preflight_token
from ha_mcp.tools.tools_integrations import IntegrationTools


@pytest.fixture(autouse=True)
def _legacy_registry_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the legacy whole-registry reads for this module.

    The reconfigure path prefers the custom component's server-side filtered
    reads. These tests drive plain client doubles, so the component probe is
    stubbed to a miss here; the component-first path has its own tests below.
    """
    monkeypatch.setattr(
        config_entry_flow,
        "fetch_entities_for_config_entry_via_component",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        config_entry_flow,
        "fetch_device_list_via_component",
        AsyncMock(return_value=None),
    )

    async def _entry_unique_id(client: Any, entry_id: str) -> EntryUniqueId:
        value = getattr(client, "_test_entry_unique_id", UNKNOWN_UNIQUE_ID)
        if isinstance(value, list):
            # Successive reads (before the flow, then after) — the last entry
            # repeats for the verification retries.
            return value.pop(0) if len(value) > 1 else value[0]
        return value

    # Home Assistant's config-entry fragment has no unique_id, so the value can
    # only come from the ha_mcp_tools component. The doubles carry it per
    # client, defaulting to "component installed" — see reconfigure_client.
    monkeypatch.setattr(
        config_entry_flow, "fetch_config_entry_unique_id", _entry_unique_id
    )

    async def _domain_unique_ids(client: Any, domain: str) -> dict[str, str] | None:
        # None models "no component / unreadable", which must stop the scan
        # from claiming it compared unique_ids.
        return getattr(client, "_test_domain_unique_ids", None)

    monkeypatch.setattr(
        config_entry_flow, "fetch_domain_unique_ids", _domain_unique_ids
    )

    async def _subscribe(client: Any) -> Any:
        # None = no change stream, so the suite keeps exercising the polled
        # fallback by default. Tests that care set _test_entry_events.
        events = getattr(client, "_test_entry_events", None)
        if events is None:
            return None
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for ev in events:
            queue.put_nowait(ev)
        ws = MagicMock()
        ws.unsubscribe_command = AsyncMock()
        return ws, (1, queue)

    monkeypatch.setattr(config_entry_flow, "_subscribe_entry_changes", _subscribe)


_UNSET = object()


def reconfigure_client(
    *,
    entity_rows: list[dict[str, Any]] | None = None,
    device_rows: list[dict[str, Any]] | None = None,
    unique_id: str | list[str | None] | None = "AA:BB:CC:DD:EE:FF",
    unique_id_known: bool = True,
    domain_unique_ids: Any = _UNSET,
) -> Any:
    """Build a client double whose registry reads behave like the real client.

    ``HomeAssistantClient.list_entity_registry`` / ``list_device_registry``
    return a list or raise — never a non-awaitable. A bare ``MagicMock()``
    returns a ``MagicMock``, which used to be read as "registry unavailable"
    and silently skipped the identity and duplicate logic these tests exist to
    cover. Starting from this factory means an unstubbed registry read fails
    loudly instead.
    """
    client = MagicMock()
    client.list_entity_registry = AsyncMock(return_value=list(entity_rows or []))
    client.list_device_registry = AsyncMock(return_value=list(device_rows or []))
    # Defaults to a component install reporting the fixture entry's unique_id.
    # Pass unique_id_known=False for an add-on / Docker / PyPI install with no
    # custom component, where the value is simply unreadable.
    if isinstance(unique_id, list):
        client._test_entry_unique_id = [
            EntryUniqueId(known=True, value=item) for item in unique_id
        ]
    else:
        client._test_entry_unique_id = EntryUniqueId(
            known=unique_id_known, value=unique_id if unique_id_known else None
        )
    # entry_id -> unique_id for the domain, used by the duplicate scan. HA's
    # own config-entry rows carry no unique_id, so the scan can only compare
    # them through the component; None models an install without it.
    # MagicMock auto-creates attributes, so this must be set explicitly or
    # getattr(...) returns a truthy child mock and every test would take the
    # subscription path.
    client._test_entry_events = None
    client._test_domain_unique_ids = (
        ({} if unique_id_known else None)
        if domain_unique_ids is _UNSET
        else domain_unique_ids
    )
    return client


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


def test_reconfigure_internal_contract_accepts_only_generic_config() -> None:
    """The reconfigure helper must not expose a parallel host/port API."""
    parameters = inspect.signature(reconfigure_config_entry).parameters

    assert "config" in parameters
    assert "host" not in parameters
    assert "port" not in parameters


def test_reconfigure_apply_contract_uses_one_prepared_request() -> None:
    """Prepared entry, flow config, and identity travel as one value object."""
    parameters = inspect.signature(reconfigure_config_entry).parameters

    assert "prepared" in parameters
    assert "_prepared_entry" not in parameters
    assert "_prepared_flow_config" not in parameters
    assert "_prepared_identity" not in parameters
    assert set(PreparedReconfigure.__dataclass_fields__) >= {
        "entry_id",
        "entry",
        "flow_config",
        "identity",
        "expected_identity",
    }


@pytest.mark.asyncio
async def test_reconfigure_result_preserves_caller_config_values(
    reconfig_entry: dict[str, object],
) -> None:
    """Reconfigure results do not transform caller-supplied configuration."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(side_effect=[reconfig_entry, reconfig_entry])
    client.list_config_entries = AsyncMock(return_value=[reconfig_entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-config-values",
            "type": "form",
            "step_id": "reconfigure",
            "data_schema": [
                {"name": "host", "required": True},
                {"name": "password", "required": True},
            ],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client,
        "entry-123",
        config={"host": "192.0.2.170", "password": "caller-value"},
    )

    assert result["target_config"] == {
        "host": "192.0.2.170",
        "password": "caller-value",
    }


def test_reconfigure_token_ignores_volatile_entry_state() -> None:
    """A reload-state transition must not invalidate the confirmation hash."""
    entry = {
        "entry_id": "entry-123",
        "domain": "shelly",
        "title": "Living room relay",
        "state": "setup_retry",
        "reason": "cannot_connect",
        "unique_id": "AA:BB:CC:DD:EE:FF",
        "supports_reconfigure": True,
    }
    expected_identity = {
        "device_id": None,
        "unique_id": None,
        "mac": None,
        "entity_ids": [],
    }

    original = _reconfigure_preflight_token(
        entry=entry,
        target_config={"host": "192.0.2.170"},
        expected_identity=expected_identity,
        identity=ReconfigureIdentity(),
    )
    changed_state = {
        **entry,
        "state": "setup_in_progress",
        "reason": "reloading",
    }

    assert (
        _reconfigure_preflight_token(
            entry=changed_state,
            target_config={"host": "192.0.2.170"},
            expected_identity=expected_identity,
            identity=ReconfigureIdentity(),
        )
        == original
    )


@pytest.mark.asyncio
async def test_reconfigure_preserves_entry_and_submits_host_and_port(
    reconfig_entry: dict[str, object],
) -> None:
    """The official reconfigure flow updates the existing entry in place."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(side_effect=[reconfig_entry, reconfig_entry])
    client.list_config_entries = AsyncMock(return_value=[reconfig_entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-123",
            "type": "form",
            "step_id": "reconfigure",
            "data_schema": [
                {"name": "host", "required": True},
                {"name": "port", "required": False, "default": 80},
            ],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client, "entry-123", config={"host": "192.0.2.170", "port": 80}
    )

    assert result["success"] is True
    assert result["status"] == ReconfigureStatus.APPLIED_AND_VERIFIED
    assert result["operation"] == "reconfigure"
    assert result["entry_id"] == "entry-123"
    assert result["domain"] == "shelly"
    assert result["rollback"]["strategy"] == "official_reconfigure_flow"
    assert result["rollback"]["automatic"] is False
    assert result["rollback"]["operator_action_required"] is True
    assert result["rollback"]["manual_required"] is True
    assert result["rollback"]["manual_reason"] == "previous_config_unavailable"
    assert result["target_config"] == {"host": "192.0.2.170", "port": 80}
    assert result["verification"] == {
        "entry_state": "loaded",
        "operational_state_verified": True,
        "unique_id_preserved": True,
        "unique_id_verification": "preserved",
        # The entry registers no devices or entities; empty before and empty
        # after is preserved identity, not a failure to verify.
        "device_id_verification": "absent",
        "entity_verification": "absent",
        "identity_verification": "complete",
        "duplicate_scan": "unique_id_and_shared_device",
        "cross_domain_related_entries": [],
        # No change stream in this double, so the state was polled.
        "operational_state_source": "polled",
    }
    client.start_reconfigure_flow.assert_awaited_once_with("shelly", "entry-123")
    client.submit_config_flow_step.assert_awaited_once_with(
        "flow-123", {"host": "192.0.2.170", "port": 80}
    )


@pytest.mark.asyncio
async def test_reconfigure_drives_multiple_form_steps(
    reconfig_entry: dict[str, object],
) -> None:
    """The generic walker can finish a reconfigure flow with multiple forms."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(side_effect=[reconfig_entry, reconfig_entry])
    client.list_config_entries = AsyncMock(return_value=[reconfig_entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-multi",
            "type": "form",
            "step_id": "host",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        side_effect=[
            {
                "flow_id": "flow-multi",
                "type": "form",
                "step_id": "port",
                "data_schema": [{"name": "port", "required": True}],
            },
            {"type": "abort", "reason": "reconfigure_successful"},
        ]
    )

    result = await reconfigure_config_entry(
        client, "entry-123", config={"host": "192.0.2.173", "port": 8080}
    )

    assert result["success"] is True
    assert client.submit_config_flow_step.await_args_list[0].args == (
        "flow-multi",
        {"host": "192.0.2.173"},
    )
    assert client.submit_config_flow_step.await_args_list[1].args == (
        "flow-multi",
        {"port": 8080},
    )


@pytest.mark.asyncio
async def test_reconfigure_fails_when_success_abort_ignores_requested_values(
    reconfig_entry: dict[str, object],
) -> None:
    """A successful HA abort must not hide values that the flow never consumed."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-ignored",
            "type": "form",
            "step_id": "address",
            "data_schema": [{"name": "address", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )
    client.abort_config_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "192.0.2.180"}
        )

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "consum" in payload["error"]["message"].lower()
    assert payload["status"] == "applied_but_incomplete"
    client.abort_config_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_accepts_generic_config_and_menu_selection(
    reconfig_entry: dict[str, object],
) -> None:
    """Generic integrations can receive arbitrary fields and menu choices."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(side_effect=[reconfig_entry, reconfig_entry])
    client.list_config_entries = AsyncMock(return_value=[reconfig_entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-generic",
            "type": "menu",
            "step_id": "choose_transport",
            "menu_options": ["network", "cloud"],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        side_effect=[
            {
                "flow_id": "flow-generic",
                "type": "form",
                "step_id": "network",
                "data_schema": [{"name": "address", "required": True}],
            },
            {"type": "abort", "reason": "reconfigure_successful"},
        ]
    )

    result = await reconfigure_config_entry(
        client,
        "entry-123",
        config={"next_step_id": "network", "address": "192.0.2.181"},
    )

    assert result["success"] is True
    assert client.submit_config_flow_step.await_args_list[0].args == (
        "flow-generic",
        {"next_step_id": "network"},
    )
    assert client.submit_config_flow_step.await_args_list[1].args == (
        "flow-generic",
        {"address": "192.0.2.181"},
    )


@pytest.mark.asyncio
async def test_reconfigure_allows_offline_entry_with_registry_identity() -> None:
    """A setup_retry entry can be changed without contacting the physical device."""
    before = {
        "entry_id": "offline-entry",
        "domain": "shelly",
        "state": "setup_retry",
        "supports_reconfigure": True,
    }
    after = {**before, "state": "loaded", "unique_id": "AABBCC001122"}
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(side_effect=[before, after])
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
    client.list_config_entries = AsyncMock(return_value=[after])
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
    assert result["status"] == "applied_and_verified"
    assert result["verification"]["identity_verification"] == "complete"
    assert result["verification"]["device_id_verification"] == "preserved"
    assert result["verification"]["entity_verification"] == "preserved"


@pytest.mark.asyncio
async def test_reconfigure_apply_accepts_prepared_identity_anchors() -> None:
    """The confirmed path must preserve anchors carried by PreparedReconfigure."""
    entry = {
        "entry_id": "prepared-entry",
        "domain": "shelly",
        "state": "loaded",
        "supports_reconfigure": True,
    }
    after = {**entry, "unique_id": "AABBCC001122"}
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=after)
    client.list_entity_registry = AsyncMock(
        return_value=[
            {
                "entity_id": "switch.prepared",
                "config_entry_id": "prepared-entry",
                "device_id": "device-prepared",
            }
        ]
    )
    client.list_device_registry = AsyncMock(
        return_value=[
            {
                "id": "device-prepared",
                "connections": [["mac", "AA:BB:CC:00:11:22"]],
            }
        ]
    )
    client.list_config_entries = AsyncMock(return_value=[after])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "prepared-flow",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )
    prepared = PreparedReconfigure(
        entry_id="prepared-entry",
        entry=entry,
        flow_config={"host": "192.0.2.170"},
        identity=ReconfigureIdentity(
            device_ids=["device-prepared"],
            entity_ids=["switch.prepared"],
            macs=["AABBCC001122"],
        ),
        expected_identity={
            "device_id": "device-prepared",
            "unique_id": None,
            "mac": None,
            "entity_ids": ["switch.prepared"],
        },
    )

    result = await reconfigure_config_entry(
        client,
        "prepared-entry",
        prepared=prepared,
    )

    assert result["success"] is True
    assert result["status"] == ReconfigureStatus.APPLIED_AND_VERIFIED
    assert result["verification"]["identity_verification"] == "complete"
    client.submit_config_flow_step.assert_awaited_once_with(
        "prepared-flow", {"host": "192.0.2.170"}
    )


@pytest.mark.asyncio
async def test_reconfigure_does_not_verify_setup_retry_as_loaded() -> None:
    """Identity preservation is insufficient when HA leaves the entry degraded."""
    before = {
        "entry_id": "degraded-entry",
        "domain": "shelly",
        "state": "loaded",
        "supports_reconfigure": True,
        "unique_id": "AABBCC001122",
    }
    after = {**before, "state": "setup_retry"}
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(side_effect=[before, after])
    client.list_entity_registry = AsyncMock(
        return_value=[
            {
                "entity_id": "switch.degraded",
                "config_entry_id": "degraded-entry",
                "device_id": "device-degraded",
            }
        ]
    )
    client.list_device_registry = AsyncMock(
        return_value=[
            {
                "id": "device-degraded",
                "connections": [["mac", "AA:BB:CC:00:11:22"]],
            }
        ]
    )
    client.list_config_entries = AsyncMock(return_value=[after])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "degraded-flow",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client,
        "degraded-entry",
        config={"host": "192.0.2.170"},
        expected_device_id="device-degraded",
        expected_mac="AA-BB-CC-00-11-22",
        expected_entity_ids=["switch.degraded"],
    )

    assert result["status"] == "applied_but_unverified"
    assert result["verification"]["identity_verification"] == "complete"
    assert result["verification"]["entry_state"] == "setup_retry"
    assert result["verification"]["operational_state_verified"] is False


@pytest.mark.asyncio
async def test_reconfigure_retries_transient_reload_before_classifying_state() -> None:
    """A transient setup-in-progress state is re-read before reporting unknown."""
    before = {
        "entry_id": "reload-entry",
        "domain": "shelly",
        "state": "loaded",
        "supports_reconfigure": True,
        "unique_id": "AABBCC001122",
    }
    transitional = {**before, "state": "setup_in_progress", "reason": "reloading"}
    after = {**before, "state": "loaded"}
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(side_effect=[before, transitional, after])
    client.list_entity_registry = AsyncMock(
        return_value=[
            {
                "entity_id": "switch.reload",
                "config_entry_id": "reload-entry",
                "device_id": "device-reload",
            }
        ]
    )
    client.list_device_registry = AsyncMock(
        return_value=[
            {
                "id": "device-reload",
                "connections": [["mac", "AA:BB:CC:00:11:22"]],
            }
        ]
    )
    client.list_config_entries = AsyncMock(return_value=[after])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "reload-flow",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client,
        "reload-entry",
        config={"host": "192.0.2.170"},
    )

    assert result["status"] == "applied_and_verified"
    assert result["verification"]["entry_state"] == "loaded"
    assert client.get_config_entry.await_count == 3


@pytest.mark.asyncio
async def test_reconfigure_reports_applied_but_unverified_after_commit(
    reconfig_entry: dict[str, object],
) -> None:
    """A post-commit HA read failure is not presented as a clean apply failure.

    The read-back keeps failing for every attempt, so the operator must get
    the real cause — not the StopAsyncIteration a drained side_effect list
    would report.
    """
    calls: list[int] = []

    async def read_entry(_entry_id: str) -> dict[str, object]:
        calls.append(1)
        if len(calls) == 1:
            return reconfig_entry
        raise RuntimeError("HA read timeout")

    client = reconfigure_client()
    client.get_config_entry = AsyncMock(side_effect=read_entry)
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-unknown",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "192.0.2.182"}
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == ReconfigureStatus.APPLIED_BUT_UNVERIFIED
    assert "HA read timeout" in payload["verification_error"]
    # The structured error block survives: `verification_error` must not be
    # named `error`, which create_error_response merges over the top level.
    assert payload["error"]["code"] == "SERVICE_CALL_FAILED"


@pytest.mark.asyncio
async def test_reconfigure_verification_recovers_from_a_transient_read_failure(
    reconfig_entry: dict[str, object],
) -> None:
    """A read-back that fails once and then succeeds still verifies.

    Pins the break/backoff wiring: without it the loop would either give up on
    the first exception or spin past a successful attempt.
    """
    calls: list[int] = []

    async def read_entry(_entry_id: str) -> dict[str, object]:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("transient")
        return reconfig_entry

    client = reconfigure_client()
    client.get_config_entry = AsyncMock(side_effect=read_entry)
    client.list_config_entries = AsyncMock(return_value=[reconfig_entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-transient",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client, "entry-123", config={"host": "192.0.2.182"}
    )

    assert result["status"] == ReconfigureStatus.APPLIED_AND_VERIFIED
    # 1 preflight read + a failed attempt + the attempt that succeeded.
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_reconfigure_submit_timeout_is_applied_but_unverified(
    reconfig_entry: dict[str, object],
) -> None:
    """A submit timeout may follow a commit and must retain rollback context."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-submit-timeout",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(side_effect=TimeoutError())
    client.abort_config_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "192.0.2.183"}
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == "applied_but_unverified"
    assert payload["rollback"]["manual_required"] is True
    assert "no answer" in payload["error"]["message"]
    client.abort_config_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_cancellation_aborts_pending_flow(
    reconfig_entry: dict[str, object],
) -> None:
    """Caller cancellation must not leave a pending HA reconfigure flow."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-cancelled",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(side_effect=asyncio.CancelledError())
    client.abort_config_flow = AsyncMock()

    with pytest.raises(asyncio.CancelledError):
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "192.0.2.185"}
        )

    client.abort_config_flow.assert_awaited_once_with("flow-cancelled")


@pytest.mark.asyncio
async def test_reconfigure_step_budget_aborts_pending_flow(
    reconfig_entry: dict[str, object],
) -> None:
    """Exhausting the walker budget aborts the still-pending reconfigure flow."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-budget",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={
            "flow_id": "flow-budget",
            "type": "form",
            "step_id": "reconfigure",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.abort_config_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "192.0.2.184"}
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == "flow_aborted_before_apply"
    assert payload["flow_budget_exhausted"] is True
    client.abort_config_flow.assert_awaited_once_with("flow-budget")


@pytest.mark.asyncio
async def test_reconfigure_verification_failure_includes_rollback(
    reconfig_entry: dict[str, object],
) -> None:
    """Post-commit identity failures retain the operator rollback path."""
    # A CLEARED unique_id is the real violation — a re-key is legitimate.
    after = {key: value for key, value in reconfig_entry.items() if key != "unique_id"}
    client = reconfigure_client(unique_id=["AA:BB:CC:DD:EE:FF", None])
    client.get_config_entry = AsyncMock(side_effect=[reconfig_entry, after])
    client.list_config_entries = AsyncMock(return_value=[after])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-identity-mismatch",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "192.0.2.183"}
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == ReconfigureStatus.APPLIED_IDENTITY_MISMATCH
    assert payload["rollback"]["strategy"] == "official_reconfigure_flow"
    assert payload["rollback"]["operator_action_required"] is True
    assert payload["rollback"]["backup_restore_supported"] is False


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
    warnings = config_entry_flow.cross_domain_warnings(related.cross_domain)
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
async def test_reconfigure_rejects_cleared_unique_id_after_commit(
    reconfig_entry: dict[str, object],
) -> None:
    """A post-flow missing identifier is an identity change, not a soft warning.

    The component still answers after the flow — it reports the entry now has
    no unique_id, which is a real loss, not an unreadable value.
    """
    after = {key: value for key, value in reconfig_entry.items() if key != "unique_id"}
    client = reconfigure_client(unique_id=["AA:BB:CC:DD:EE:FF", None])
    client.get_config_entry = AsyncMock(side_effect=[reconfig_entry, after])
    client.list_config_entries = AsyncMock(return_value=[after])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-temporary-identity",
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
            "entry-123",
            config={"host": "192.0.2.186"},
        )

    payload = json.loads(str(exc_info.value))
    assert "cleared the entry unique_id" in payload["error"]["message"]
    assert payload["status"] == ReconfigureStatus.APPLIED_IDENTITY_MISMATCH


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
async def test_reconfigure_fails_closed_when_original_entry_cannot_be_verified(
    reconfig_entry: dict[str, object],
) -> None:
    """A completed flow is not reported as success if HA returns another entry."""
    changed_entry = dict(reconfig_entry)
    changed_entry["entry_id"] = "different-entry"
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(side_effect=[reconfig_entry, changed_entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-verify",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )
    client.abort_config_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "192.0.2.174"}
        )

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "SERVICE_CALL_FAILED"
    assert payload["entry_id"] == "entry-123"


@pytest.mark.asyncio
async def test_reconfigure_detects_duplicate_identity(
    reconfig_entry: dict[str, object],
) -> None:
    """Post-flight verification rejects a second entry sharing the identity."""
    duplicate_entry = dict(reconfig_entry)
    duplicate_entry["entry_id"] = "entry-duplicate"
    # The duplicate is found through the component's domain map: Home
    # Assistant's own rows carry no unique_id, so putting one in the mocked
    # REST row (as this test used to) would pass while production could not
    # detect the duplicate at all.
    client = reconfigure_client(
        domain_unique_ids={
            "entry-123": "AA:BB:CC:DD:EE:FF",
            "entry-duplicate": "AA:BB:CC:DD:EE:FF",
        }
    )
    client.get_config_entry = AsyncMock(side_effect=[reconfig_entry, reconfig_entry])
    client.list_config_entries = AsyncMock(
        return_value=[reconfig_entry, duplicate_entry]
    )
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-duplicate",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "192.0.2.175"}
        )

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "duplicate" in payload["error"]["message"].lower()


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
async def test_reconfigure_without_token_previews_and_never_writes(
    reconfig_entry: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting confirm_token is the preflight; it starts no flow.

    The handshake is the token alone, matching ha_config_set_yaml — there is
    no separate confirm flag that could contradict it.
    """
    monkeypatch.setattr(
        "ha_mcp.tools.auto_backup.get_global_settings",
        lambda: pytest.fail("preflight must not resolve auto-backup settings"),
    )
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock()
    tools = IntegrationTools(client)

    preview = await tools.ha_set_integration(
        entry_id="entry-123",
        reconfigure=True,
        config={},
    )

    assert preview["success"] is True
    assert "preview" not in preview  # status is the single signal
    assert preview["status"] == ReconfigureStatus.PREVIEW
    assert isinstance(preview["confirm_token"], str)
    assert preview["confirm_token"].startswith("sha256:")
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_rejects_stale_confirmation_token(
    reconfig_entry: dict[str, object],
) -> None:
    """A changed entry invalidates a previously issued preflight token."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(
        side_effect=[reconfig_entry, {**reconfig_entry, "title": "changed"}]
    )
    client.start_reconfigure_flow = AsyncMock()
    tools = IntegrationTools(client)

    preview = await tools.ha_set_integration(
        entry_id="entry-123",
        reconfigure=True,
        config={"host": "192.0.2.171"},
    )

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_set_integration(
            entry_id="entry-123",
            reconfigure=True,
            config={"host": "192.0.2.171"},
            confirm_token=preview["confirm_token"],
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == ReconfigureStatus.STALE_PREFLIGHT
    assert "preview" not in payload
    assert "preview" not in payload.get("context", {})
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("backup_capture_fails", [False, True])
async def test_confirmed_reconfigure_uses_normal_auto_backup_policy(
    reconfig_entry: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    backup_capture_fails: bool,
) -> None:
    """Confirmed reconfigure snapshots before applying without a mandatory gate."""
    from ha_mcp.config import reset_global_settings
    from ha_mcp.utils.data_paths import get_data_dir

    config_dir = tmp_path / "config"
    backup_dir = tmp_path / "backups"
    config_dir.mkdir()
    (config_dir / "backup_settings.json").write_text(
        json.dumps(
            {
                "enable_auto_backup": True,
                "auto_backup_dir": str(backup_dir),
            }
        )
    )
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")
    monkeypatch.setenv("HAMCP_BACKUP_DIR", str(backup_dir))
    get_data_dir.cache_clear()
    reset_global_settings()
    from ha_mcp.config import get_global_settings

    settings = get_global_settings()
    assert settings.enable_auto_backup is True
    assert settings.auto_backup_dir == str(backup_dir)

    async def fake_backup_ws(_client: Any, message: dict[str, Any]) -> Any:
        assert message == {"type": "config_entries/get"}
        if backup_capture_fails:
            raise HomeAssistantConnectionError("transient backup capture failure")
        return [
            {
                "entry_id": "entry-123",
                "domain": "shelly",
                "data": {"host": "192.0.2.170", "port": 80},
            }
        ]

    monkeypatch.setattr("ha_mcp.backup_manager._ws_send", fake_backup_ws)
    from ha_mcp.backup_manager import BackupManager

    original_maybe_snapshot = BackupManager.maybe_snapshot
    observed_mandatory: list[Any] = []

    async def checked_maybe_snapshot(self: Any, *args: Any, **kwargs: Any) -> Any:
        observed_mandatory.append(kwargs.get("mandatory", "<missing>"))
        return await original_maybe_snapshot(self, *args, **kwargs)

    monkeypatch.setattr(BackupManager, "maybe_snapshot", checked_maybe_snapshot)

    client = reconfigure_client()
    client.base_url = "http://homeassistant.local"
    client.token = "test-token"
    client.verify_ssl = True
    client.get_config_entry = AsyncMock(return_value=dict(reconfig_entry))
    client.list_entity_registry = AsyncMock(return_value=[])
    client.list_device_registry = AsyncMock(return_value=[])
    client.list_config_entries = AsyncMock(return_value=[dict(reconfig_entry)])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-123",
            "type": "form",
            "step_id": "reconfigure",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    try:
        tools = IntegrationTools(client)
        preview = await tools.ha_set_integration(
            entry_id="entry-123",
            reconfigure=True,
            config={"host": "192.0.2.171"},
        )
        result = await tools.ha_set_integration(
            entry_id="entry-123",
            reconfigure=True,
            config={"host": "192.0.2.171"},
            confirm_token=preview["confirm_token"],
        )
    finally:
        reset_global_settings()
        get_data_dir.cache_clear()

    snapshots = list(backup_dir.glob("integration.*.yaml"))
    assert len(snapshots) == (0 if backup_capture_fails else 1)
    assert observed_mandatory == [False]
    if not backup_capture_fails:
        assert "entry-123" in snapshots[0].read_text()
    assert result["status"] == "applied_and_verified"
    client.start_reconfigure_flow.assert_awaited_once_with("shelly", "entry-123")
    client.submit_config_flow_step.assert_awaited()


@pytest.mark.asyncio
async def test_reconfigure_rejects_non_object_config(
    reconfig_entry: dict[str, object],
) -> None:
    """The generic flow payload must be an object."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", config=cast(Any, ["invalid"])
        )

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "config" in payload["error"]["message"].lower()
    client.get_config_entry.assert_not_awaited()


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
async def test_reconfigure_rejects_expected_mac_when_after_registry_loses_it(
    reconfig_entry: dict[str, object],
) -> None:
    """An expected hardware anchor must remain verifiable after apply."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(side_effect=[reconfig_entry, reconfig_entry])
    client.list_entity_registry = AsyncMock(return_value=[])
    client.list_device_registry = AsyncMock(
        side_effect=[
            [
                {
                    "id": "device-living-room",
                    "config_entries": ["entry-123"],
                    "connections": [["mac", "AA:BB:CC:DD:EE:FF"]],
                }
            ],
            [
                {
                    "id": "device-living-room",
                    "config_entries": ["entry-123"],
                    "connections": [],
                }
            ],
        ]
    )
    client.list_config_entries = AsyncMock(return_value=[reconfig_entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-missing-after-mac",
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
            "entry-123",
            config={"host": "192.0.2.188"},
            expected_mac="AA:BB:CC:DD:EE:FF",
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == ReconfigureStatus.APPLIED_IDENTITY_MISMATCH
    assert "expected MAC" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_reconfigure_rejects_mac_identity_drift(
    reconfig_entry: dict[str, object],
) -> None:
    """A changed registry MAC is applied-but-unverified, not a clean success."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.list_entity_registry = AsyncMock(side_effect=[[], []])
    client.list_device_registry = AsyncMock(
        side_effect=[
            [
                {
                    "id": "device-living-room",
                    "config_entries": ["entry-123"],
                    "connections": [["mac", "AA:BB:CC:DD:EE:FF"]],
                }
            ],
            [
                {
                    "id": "device-living-room",
                    "config_entries": ["entry-123"],
                    "connections": [["mac", "11:22:33:44:55:66"]],
                }
            ],
        ]
    )
    client.list_config_entries = AsyncMock(return_value=[reconfig_entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-mac-drift",
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
            "entry-123",
            config={"host": "192.0.2.188"},
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == ReconfigureStatus.APPLIED_IDENTITY_MISMATCH
    assert "MAC" in payload["error"]["message"]


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
    assert payload["rollback"]["strategy"] == "official_reconfigure_flow"


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
    assert payload["status"] == ReconfigureStatus.APPLY_FAILED
    assert "reconfigure flow" in payload["error"]["message"].lower()
    client.submit_config_flow_step.assert_not_awaited()


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
    assert payload["config_keys"] == ["host"]
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "step_type", ["external", "external_done", "progress", "progress_done"]
)
async def test_reconfigure_stamps_apply_failed_on_undrivable_steps(
    reconfig_entry: dict[str, object], step_type: str
) -> None:
    """An OAuth/progress step cannot be driven, and nothing was committed."""
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
    client.abort_config_flow.assert_awaited_once_with("flow-undrivable")


@pytest.mark.asyncio
async def test_reconfigure_stamps_apply_failed_on_an_unexpected_result_type(
    reconfig_entry: dict[str, object],
) -> None:
    """A flow result type the walker does not know is a pre-commit failure."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(
        return_value={"flow_id": "flow-weird", "type": "something_new"}
    )
    client.abort_config_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "192.0.2.13"}
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == ReconfigureStatus.APPLY_FAILED
    client.abort_config_flow.assert_awaited_once_with("flow-weird")


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
        assert result["status"] == ReconfigureStatus.APPLIED_AND_VERIFIED
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
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == ReconfigureStatus.APPLIED_BUT_UNVERIFIED
    assert "instead of updating" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_confirm_token_is_bound_to_the_requested_config(
    reconfig_entry: dict[str, object],
) -> None:
    """A token issued for one target config cannot apply a different one.

    Mutating the entry title proves the token tracks entry state; this pins
    the property that actually protects the caller — the token is bound to
    the change it previewed, so a swapped `config` cannot ride an old token.
    """
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock()
    tools = IntegrationTools(client)

    preview = await tools.ha_set_integration(
        entry_id="entry-123",
        reconfigure=True,
        config={"host": "192.0.2.171"},
    )

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_set_integration(
            entry_id="entry-123",
            reconfigure=True,
            config={"host": "192.0.2.199"},
            confirm_token=preview["confirm_token"],
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == ReconfigureStatus.STALE_PREFLIGHT
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_token_is_bound_to_the_supplied_identity_anchors() -> None:
    """A token previewed without identity anchors cannot apply with them.

    The anchors change what the confirmed call verifies, so a token issued
    before they were supplied has not previewed the same operation.
    """
    entry = {
        "entry_id": "anchor-entry",
        "domain": "shelly",
        "state": "loaded",
        "supports_reconfigure": True,
        "unique_id": "AA:BB:CC:DD:EE:FF",
    }
    client = reconfigure_client(
        entity_rows=[
            {
                "entity_id": "switch.anchor",
                "config_entry_id": "anchor-entry",
                "device_id": "device-anchor",
            }
        ],
        device_rows=[
            {
                "id": "device-anchor",
                "config_entries": ["anchor-entry"],
                "connections": [],
                "identifiers": [],
            }
        ],
    )
    client.get_config_entry = AsyncMock(return_value=entry)
    client.list_config_entries = AsyncMock(return_value=[entry])
    client.start_reconfigure_flow = AsyncMock()
    tools = IntegrationTools(client)

    preview = await tools.ha_set_integration(
        entry_id="anchor-entry",
        reconfigure=True,
        config={"host": "192.0.2.200"},
    )

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_set_integration(
            entry_id="anchor-entry",
            reconfigure=True,
            config={"host": "192.0.2.200"},
            expected_device_id="device-anchor",
            confirm_token=preview["confirm_token"],
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == ReconfigureStatus.STALE_PREFLIGHT
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_ascii_confirm_token_is_rejected_not_an_internal_error(
    reconfig_entry: dict[str, object],
) -> None:
    """A junk token lands on stale_preflight, not INTERNAL_ERROR.

    hmac.compare_digest raises TypeError on a non-ASCII str operand, which the
    outer handler would have re-mapped to an internal error.
    """
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(
            entry_id="entry-123",
            reconfigure=True,
            config={"host": "192.0.2.171"},
            confirm_token="sha256:café–ünïcode",
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == ReconfigureStatus.STALE_PREFLIGHT
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    client.start_reconfigure_flow.assert_not_awaited()


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
async def test_verification_reports_unique_id_unavailable_without_the_component() -> (
    None
):
    """A non-component install must not claim the unique_id was preserved.

    before == after == None is two unknowns, not evidence of preservation.
    """
    entry = {
        "entry_id": "addon-entry",
        "domain": "shelly",
        "state": "loaded",
        "supports_reconfigure": True,
    }
    client = reconfigure_client(
        unique_id_known=False,
        entity_rows=[
            {
                "entity_id": "switch.addon",
                "config_entry_id": "addon-entry",
                "device_id": "device-addon",
            }
        ],
        device_rows=[
            {
                "id": "device-addon",
                "config_entries": ["addon-entry"],
                "connections": [],
                "identifiers": [],
            }
        ],
    )
    client.get_config_entry = AsyncMock(return_value=entry)
    client.list_config_entries = AsyncMock(return_value=[entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-addon",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client, "addon-entry", config={"host": "192.0.2.31"}
    )

    verification = result["verification"]
    assert verification["unique_id_verification"] == "unavailable_without_component"
    assert verification["unique_id_preserved"] is None
    assert verification["duplicate_scan"] == "shared_device_only"
    # The device and entity anchors still verify, so the apply is still fully
    # verified — losing unique_id must not degrade the whole outcome.
    assert verification["identity_verification"] == "complete"
    assert result["status"] == ReconfigureStatus.APPLIED_AND_VERIFIED


@pytest.mark.asyncio
async def test_component_reporting_no_unique_id_is_distinct_from_unreadable() -> None:
    """An MQTT-style entry genuinely without one is known-absent, not unknown.

    known=True with value None means the component answered and the entry has
    no unique_id — the duplicate scan and the guards can trust that.
    """
    entry = {
        "entry_id": "mqtt-entry",
        "domain": "mqtt",
        "state": "loaded",
        "supports_reconfigure": True,
    }
    client = reconfigure_client(
        unique_id=None,
        entity_rows=[
            {
                "entity_id": "sensor.mqtt",
                "config_entry_id": "mqtt-entry",
                "device_id": "device-mqtt",
            }
        ],
        device_rows=[
            {
                "id": "device-mqtt",
                "config_entries": ["mqtt-entry"],
                "connections": [],
                "identifiers": [],
            }
        ],
    )
    client.get_config_entry = AsyncMock(return_value=entry)
    client.list_config_entries = AsyncMock(return_value=[entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-mqtt",
            "type": "form",
            "data_schema": [{"name": "broker", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client, "mqtt-entry", config={"broker": "mosquitto"}
    )

    verification = result["verification"]
    # Compared, and both sides were absent — that IS preservation, unlike the
    # unreadable case above.
    assert verification["unique_id_verification"] == "absent"
    assert verification["unique_id_preserved"] is True
    assert result["status"] == ReconfigureStatus.APPLIED_AND_VERIFIED


@pytest.mark.asyncio
async def test_a_legitimate_unique_id_rekey_is_reported_not_refused() -> None:
    """An integration may re-key on reconfigure; that is not a violation.

    `filesize` sets its unique_id to the file path, so reconfiguring the path
    changes it via async_update_reload_and_abort(unique_id=...). Losing the
    unique_id is the duplicate hazard; changing it is not.
    """
    entry = {
        "entry_id": "rekey-entry",
        "domain": "filesize",
        "state": "loaded",
        "supports_reconfigure": True,
    }
    client = reconfigure_client(unique_id=["/config/www/a.txt", "/config/www/b.txt"])
    client.get_config_entry = AsyncMock(return_value=entry)
    client.list_config_entries = AsyncMock(return_value=[entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-rekey",
            "type": "form",
            "data_schema": [{"name": "file_path", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client, "rekey-entry", config={"file_path": "/config/www/b.txt"}
    )

    assert result["status"] == ReconfigureStatus.APPLIED_AND_VERIFIED
    assert result["verification"]["unique_id_verification"] == "changed_during_change"
    assert result["verification"]["unique_id_preserved"] is False


@pytest.mark.asyncio
async def test_expected_unique_id_pins_the_value_against_a_rekey() -> None:
    """A caller who needs the unique_id held says so, and it is enforced."""
    entry = {
        "entry_id": "rekey-entry",
        "domain": "filesize",
        "state": "loaded",
        "supports_reconfigure": True,
    }
    client = reconfigure_client(unique_id=["/config/www/a.txt", "/config/www/b.txt"])
    client.get_config_entry = AsyncMock(return_value=entry)
    client.list_config_entries = AsyncMock(return_value=[entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-rekey-pinned",
            "type": "form",
            "data_schema": [{"name": "file_path", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client,
            "rekey-entry",
            config={"file_path": "/config/www/b.txt"},
            expected_unique_id="/config/www/a.txt",
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == ReconfigureStatus.APPLIED_IDENTITY_MISMATCH
    assert "does not match expected unique_id" in payload["error"]["message"]


# === Regression coverage for the Codex review findings ===


def test_confirm_token_binds_the_discovered_identity() -> None:
    """The token must cover what the PREVIEW showed, not just typed anchors.

    Most callers supply no expected_* and rely on the preview's discovered
    identity. If the entry's devices, entities or MACs move between preview
    and confirm, a token hashing only the caller's empty anchors would stay
    valid and apply against associations the caller never approved.
    """
    entry = {"entry_id": "e1", "domain": "shelly", "title": "Relay"}
    expected = {"device_id": None, "unique_id": None, "mac": None, "entity_ids": []}
    target = {"host": "192.0.2.1"}

    before = _reconfigure_preflight_token(
        entry=entry,
        target_config=target,
        expected_identity=expected,
        identity=ReconfigureIdentity(
            device_ids=["dev-1"], entity_ids=["switch.a"], macs=["AABBCC001122"]
        ),
    )
    moved_device = _reconfigure_preflight_token(
        entry=entry,
        target_config=target,
        expected_identity=expected,
        identity=ReconfigureIdentity(
            device_ids=["dev-2"], entity_ids=["switch.a"], macs=["AABBCC001122"]
        ),
    )
    moved_entities = _reconfigure_preflight_token(
        entry=entry,
        target_config=target,
        expected_identity=expected,
        identity=ReconfigureIdentity(
            device_ids=["dev-1"], entity_ids=["switch.b"], macs=["AABBCC001122"]
        ),
    )
    moved_macs = _reconfigure_preflight_token(
        entry=entry,
        target_config=target,
        expected_identity=expected,
        identity=ReconfigureIdentity(
            device_ids=["dev-1"], entity_ids=["switch.a"], macs=["AABBCC334455"]
        ),
    )

    assert len({before, moved_device, moved_entities, moved_macs}) == 4


@pytest.mark.asyncio
async def test_unreadable_unique_id_after_commit_does_not_pass_a_pinned_anchor() -> (
    None
):
    """A pinned anchor we cannot re-read post-commit is unchecked, not passed.

    The device and entity sets being unchanged must not let the result claim
    identity_verification complete when expected_unique_id was never verified
    against the applied state.
    """
    entry = {
        "entry_id": "anchor-lost",
        "domain": "shelly",
        "state": "loaded",
        "supports_reconfigure": True,
    }
    client = reconfigure_client(
        entity_rows=[
            {
                "entity_id": "switch.a",
                "config_entry_id": "anchor-lost",
                "device_id": "dev-1",
            }
        ],
        device_rows=[
            {
                "id": "dev-1",
                "config_entries": ["anchor-lost"],
                "connections": [],
                "identifiers": [],
            }
        ],
    )
    # Readable before the flow, unreadable after (component/transport dropped).
    client._test_entry_unique_id = [
        EntryUniqueId(known=True, value="AA:BB:CC:DD:EE:FF"),
        UNKNOWN_UNIQUE_ID,
    ]
    client.get_config_entry = AsyncMock(return_value=entry)
    client.list_config_entries = AsyncMock(return_value=[entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-anchor-lost",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client,
        "anchor-lost",
        config={"host": "192.0.2.40"},
        expected_unique_id="AA:BB:CC:DD:EE:FF",
    )

    verification = result["verification"]
    assert verification["unique_id_verification"] == "anchor_unverifiable_after_change"
    assert verification["identity_verification"] == "partial"
    assert result["status"] == ReconfigureStatus.APPLIED_BUT_UNVERIFIED


@pytest.mark.asyncio
async def test_duplicate_scan_does_not_claim_unique_ids_it_cannot_read() -> None:
    """Without the component the scan compares entry_ids only, and says so."""
    entry = {
        "entry_id": "solo",
        "domain": "shelly",
        "state": "loaded",
        "supports_reconfigure": True,
    }
    client = reconfigure_client(
        entity_rows=[
            {"entity_id": "switch.a", "config_entry_id": "solo", "device_id": "dev-1"}
        ],
        device_rows=[
            {
                "id": "dev-1",
                "config_entries": ["solo"],
                "connections": [],
                "identifiers": [],
            }
        ],
        domain_unique_ids=None,
    )
    client.get_config_entry = AsyncMock(return_value=entry)
    client.list_config_entries = AsyncMock(return_value=[entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-solo",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client, "solo", config={"host": "192.0.2.41"}
    )

    assert result["verification"]["duplicate_scan"] == "shared_device_only"


@pytest.mark.asyncio
async def test_a_disabled_entry_reconfigures_to_verified() -> None:
    """not_loaded is a disabled entry's TERMINAL state, not a failure.

    Demanding "loaded" reported every disabled entry's clean reconfigure as
    unverified.
    """
    entry = {
        "entry_id": "disabled-entry",
        "domain": "shelly",
        "state": "not_loaded",
        "disabled_by": "user",
        "supports_reconfigure": True,
    }
    client = reconfigure_client(
        entity_rows=[
            {
                "entity_id": "switch.a",
                "config_entry_id": "disabled-entry",
                "device_id": "dev-1",
            }
        ],
        device_rows=[
            {
                "id": "dev-1",
                "config_entries": ["disabled-entry"],
                "connections": [],
                "identifiers": [],
            }
        ],
    )
    client.get_config_entry = AsyncMock(return_value=entry)
    client.list_config_entries = AsyncMock(return_value=[entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-disabled",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client, "disabled-entry", config={"host": "192.0.2.42"}
    )

    assert result["verification"]["operational_state_verified"] is True
    assert result["status"] == ReconfigureStatus.APPLIED_AND_VERIFIED


# === Patch76 review: the submit path's no-answer failures ===


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "no_answer",
    [
        pytest.param(TimeoutError(), id="asyncio_timeout"),
        pytest.param(
            HomeAssistantConnectionError("connection reset"), id="connection_reset"
        ),
        pytest.param(
            HomeAssistantCommandTimeout("ws stopped answering"), id="ws_timeout"
        ),
        pytest.param(ConnectionResetError("peer reset"), id="oserror"),
    ],
)
async def test_losing_the_submit_answer_is_applied_but_unverified(
    reconfig_entry: dict[str, object], no_answer: Exception
) -> None:
    """Every no-answer class carries the post-commit status and skips the abort.

    The submit is the call that probes, commits and reloads, so losing its
    answer is exactly the applied_but_unverified ambiguity. rest_client funnels
    every httpx transport failure into HomeAssistantConnectionError, so a plain
    connection reset after the POST reached HA takes this path — it used to
    escape with no status at all, and the generic handler upstream aborted a
    flow that may already have committed. A caller reads a bare connection
    error as "nothing happened" and retries.
    """
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-no-answer",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(side_effect=no_answer)
    client.abort_config_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "192.0.2.183"}
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == ReconfigureStatus.APPLIED_BUT_UNVERIFIED
    assert payload["rollback"]["manual_required"] is True
    # Never abort a flow Home Assistant may already have committed.
    client.abort_config_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_commit_same_domain_device_sharing_blocks() -> None:
    """The POST-COMMIT half of the shared-device guard, not just the preflight.

    Both halves exist; only the preflight one was pinned, so a mutation of
    this branch left the suite green.
    """
    entry = {
        "entry_id": "primary",
        "domain": "shelly",
        "state": "loaded",
        "supports_reconfigure": True,
    }
    twin = {"entry_id": "twin", "domain": "shelly", "state": "loaded"}
    # Clean before the flow; the sibling appears on the device only afterwards.
    client = reconfigure_client(
        entity_rows=[
            {
                "entity_id": "switch.a",
                "config_entry_id": "primary",
                "device_id": "dev-1",
            }
        ]
    )
    client.list_device_registry = AsyncMock(
        side_effect=[
            [
                {
                    "id": "dev-1",
                    "config_entries": ["primary"],
                    "connections": [],
                    "identifiers": [],
                }
            ],
            [
                {
                    "id": "dev-1",
                    "config_entries": ["primary", "twin"],
                    "connections": [],
                    "identifiers": [],
                }
            ],
        ]
    )
    client.get_config_entry = AsyncMock(return_value=entry)
    client.list_config_entries = AsyncMock(return_value=[entry, twin])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-post-dup",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(client, "primary", config={"host": "192.0.2.60"})

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == ReconfigureStatus.APPLIED_IDENTITY_MISMATCH
    assert "same domain" in payload["error"]["message"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("after_entry_id", "after_domain", "expected"),
    [
        pytest.param("other-entry", "shelly", "entry_id", id="wrong_entry_id_only"),
        pytest.param("entry-123", "tasmota", "domain", id="wrong_domain_only"),
    ],
)
async def test_each_arm_of_the_entry_identity_guard_fires_alone(
    reconfig_entry: dict[str, object],
    after_entry_id: str,
    after_domain: str,
    expected: str,
) -> None:
    """Both disjuncts are load-bearing.

    Changing the `or` to `and` left the suite green, because no test exercised
    a wrong domain alone or a wrong entry_id alone.
    """
    after = {**reconfig_entry, "entry_id": after_entry_id, "domain": after_domain}
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(side_effect=[reconfig_entry, after])
    client.list_config_entries = AsyncMock(return_value=[after])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-identity-arm",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "192.0.2.61"}
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == ReconfigureStatus.APPLIED_IDENTITY_MISMATCH
    assert "could not be verified" in payload["error"]["message"]
    assert expected  # both arms reach the same guard, by design


# === Patch76's question: the read-back can also land BEFORE the reload ===


@pytest.mark.asyncio
async def test_a_stale_pre_reload_read_cannot_settle_as_verified(
    reconfig_entry: dict[str, object],
) -> None:
    """A poll can sample the pre-reload `loaded` and mistake it for the result.

    HA queues the reload with async_create_task and returns, so the entry can
    still be sitting in its old `loaded` state when the read-back lands. The
    change stream is opened BEFORE the flow, so what it reports is the state
    the reload actually reached — here `setup_retry`, which the poll never saw.
    """
    client = reconfigure_client()
    # Every poll returns the stale pre-reload `loaded`.
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.list_config_entries = AsyncMock(return_value=[reconfig_entry])
    client._test_entry_events = [
        {
            "id": 1,
            "type": "event",
            "event": [
                {
                    "type": "updated",
                    "entry": {**reconfig_entry, "state": "setup_in_progress"},
                }
            ],
        },
        {
            "id": 1,
            "type": "event",
            "event": [
                {
                    "type": "updated",
                    "entry": {**reconfig_entry, "state": "setup_retry"},
                }
            ],
        },
    ]
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-stale-read",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client, "entry-123", config={"host": "192.0.2.70"}
    )

    verification = result["verification"]
    assert verification["operational_state_source"] == "observed"
    # The observed outcome wins over the stale poll.
    assert verification["entry_state"] == "setup_retry"
    assert verification["operational_state_verified"] is False
    assert result["status"] == ReconfigureStatus.APPLIED_BUT_UNVERIFIED


@pytest.mark.asyncio
async def test_an_observed_clean_reload_verifies(
    reconfig_entry: dict[str, object],
) -> None:
    """A reload observed reaching `loaded` is the fully verified outcome."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.list_config_entries = AsyncMock(return_value=[reconfig_entry])
    client._test_entry_events = [
        {
            "id": 1,
            "type": "event",
            "event": [
                {
                    "type": "updated",
                    "entry": {**reconfig_entry, "state": "setup_in_progress"},
                }
            ],
        },
        {
            "id": 1,
            "type": "event",
            "event": [
                {"type": "updated", "entry": {**reconfig_entry, "state": "loaded"}}
            ],
        },
    ]
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-observed-ok",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client, "entry-123", config={"host": "192.0.2.71"}
    )

    assert result["verification"]["operational_state_source"] == "observed"
    assert result["status"] == ReconfigureStatus.APPLIED_AND_VERIFIED


@pytest.mark.asyncio
async def test_without_a_change_stream_the_source_is_reported_as_polled(
    reconfig_entry: dict[str, object],
) -> None:
    """Degrading to polling is fine, but the caller must be able to tell."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.list_config_entries = AsyncMock(return_value=[reconfig_entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-polled",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client, "entry-123", config={"host": "192.0.2.72"}
    )

    assert result["verification"]["operational_state_source"] == "polled"
