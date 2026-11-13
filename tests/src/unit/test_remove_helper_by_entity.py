"""ha_remove_helpers_integrations with an entity_id and no helper_type.

The entity registry identifies the helper, so an agent need not know which
helper type created an entity, and an entity of a non-helper integration is
refused instead of deleting that integration's config entry.

The tool runs on the server's own client against a real ``hass``: the registry
rows, config entries and storage helpers are Core's own.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp import backup_manager as bm
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.errors import DEFAULT_SUGGESTIONS, ErrorCode
from ha_mcp.tools import auto_backup, helper_flows
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._client_spies import sent_messages
from ._core_helper_flows import CORE_HELPER_FLOWS


@pytest.fixture
async def tools(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> IntegrationTools:
    # ``config`` serves the registry commands and the config entry delete.
    assert await async_setup_component(hass, "config", {})
    return IntegrationTools(core_client)


def _flow_entity(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    platform: str,
    *,
    options: dict[str, Any] | None = None,
) -> tuple[str, MockConfigEntry]:
    """An entity of ``platform`` that belongs to a config entry of its own."""
    entry = MockConfigEntry(domain=platform, title=platform, options=options or {})
    entry.add_to_hass(hass)
    entity = entity_registry.async_get_or_create(
        "sensor", platform, uuid.uuid4().hex, config_entry=entry
    )
    return entity.entity_id, entry


async def _storage_entity(hass: HomeAssistant, client: HomeAssistantClient) -> str:
    """A stored input_boolean's entity_id."""
    assert await async_setup_component(hass, "input_boolean", {})
    created = await client.send_websocket_message(
        {"type": "input_boolean/create", "name": f"Guest {uuid.uuid4().hex[:8]}"}
    )
    assert created["success"], created
    await hass.async_block_till_done()
    return f"input_boolean.{created['result']['id']}"


def _input_boolean_ids(hass: HomeAssistant) -> list[str]:
    return hass.states.async_entity_ids("input_boolean")


async def _remove(
    tools: IntegrationTools, entity_id: str, *, confirm: bool = True, **kwargs: Any
) -> dict[str, Any]:
    result: dict[str, Any] = await tools.ha_remove_helpers_integrations(
        target=entity_id, confirm=confirm, wait=False, **kwargs
    )
    return result


@contextmanager
def _no_deletes(client: HomeAssistantClient) -> Iterator[None]:
    """Fail when the call sends a config entry delete or a storage delete."""
    with (
        patch.object(
            client, "delete_config_entry", wraps=client.delete_config_entry
        ) as entry_deletes,
        sent_messages(client) as sent,
    ):
        yield
    entry_deletes.assert_not_awaited()
    assert not [
        call for call in sent.call_args_list if call.args[0]["type"].endswith("/delete")
    ]


async def _refused(
    tools: IntegrationTools, client: HomeAssistantClient, entity_id: str, **kwargs: Any
) -> dict[str, Any]:
    with _no_deletes(client), pytest.raises(ToolError) as exc_info:
        await _remove(tools, entity_id, **kwargs)
    error: dict[str, Any] = json.loads(str(exc_info.value))["error"]
    return error


@pytest.fixture
def auto_backup_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        auto_backup,
        "get_global_settings",
        lambda: SimpleNamespace(enable_auto_backup=True, auto_backup_dir=str(tmp_path)),
    )


async def test_entity_of_a_non_helper_integration_is_refused(
    hass: HomeAssistant,
    tools: IntegrationTools,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """A Hue light named without helper_type must not take Hue with it."""
    entity_id, entry = _flow_entity(hass, entity_registry, "hue")

    error = await _refused(tools, core_client, entity_id)

    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert hass.config_entries.async_get_entry(entry.entry_id) is not None


@pytest.mark.parametrize("platform", ["utility_meter", "otp"])
async def test_a_flow_helper_is_removed_by_its_entity_without_naming_its_type(
    hass: HomeAssistant,
    tools: IntegrationTools,
    entity_registry: er.EntityRegistry,
    platform: str,
) -> None:
    """otp is a helper flow in Core's list like utility_meter, so the entity
    alone removes its config entry."""
    entity_id, entry = _flow_entity(hass, entity_registry, platform)

    result = await _remove(tools, entity_id)

    assert result["helper_type"] == platform
    assert result["entry_id"] == entry.entry_id
    assert hass.config_entries.async_get_entry(entry.entry_id) is None


async def test_a_storage_helper_is_removed_by_its_entity_without_naming_its_type(
    hass: HomeAssistant, tools: IntegrationTools, core_client: HomeAssistantClient
) -> None:
    entity_id = await _storage_entity(hass, core_client)

    with patch.object(
        core_client, "delete_config_entry", wraps=core_client.delete_config_entry
    ) as entry_deletes:
        result = await _remove(tools, entity_id)
    await hass.async_block_till_done()

    assert result["success"] is True
    assert _input_boolean_ids(hass) == []
    entry_deletes.assert_not_awaited()


async def test_unconfirmed_entity_only_call_deletes_nothing(
    hass: HomeAssistant,
    tools: IntegrationTools,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    entity_id, entry = _flow_entity(hass, entity_registry, "utility_meter")

    error = await _refused(tools, core_client, entity_id, confirm=False)

    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert hass.config_entries.async_get_entry(entry.entry_id) is not None


async def test_an_unread_helper_flow_list_is_a_connection_error_not_a_refusal(
    hass: HomeAssistant,
    tools: IntegrationTools,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No answer to the helper flow read must not read as 'not a helper'."""
    monkeypatch.setattr(
        helper_flows,
        "helper_flow_types",
        AsyncMock(side_effect=HomeAssistantConnectionError("flow_handlers dropped")),
    )
    entity_id, entry = _flow_entity(hass, entity_registry, "otp")

    error = await _refused(tools, core_client, entity_id)

    assert error["code"] == "CONNECTION_FAILED"
    assert hass.config_entries.async_get_entry(entry.entry_id) is not None


async def test_entity_missing_from_the_registry_is_not_found(
    tools: IntegrationTools, core_client: HomeAssistantClient
) -> None:
    error = await _refused(tools, core_client, "sensor.typo")
    assert error["code"] == "ENTITY_NOT_FOUND"


async def test_yaml_helper_that_only_core_lists_is_not_found(
    tools: IntegrationTools,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """A Core-listed helper platform without a config entry is YAML-configured,
    not a foreign integration."""
    entity = entity_registry.async_get_or_create("sensor", "otp", "yaml_otp")

    error = await _refused(tools, core_client, entity.entity_id)

    assert error["code"] == "RESOURCE_NOT_FOUND"


async def test_entity_without_a_registry_entry_is_not_reported_missing(
    hass: HomeAssistant, tools: IntegrationTools, core_client: HomeAssistantClient
) -> None:
    """zone.home has a state but no registry entry; telling the caller it is
    missing sends them to ha_search(), which finds it."""
    assert await async_setup_component(hass, "zone", {})
    assert hass.states.get("zone.home") is not None

    error = await _refused(tools, core_client, "zone.home")

    assert error["code"] == "RESOURCE_NOT_FOUND"


async def test_a_dropped_registry_read_names_its_cause(
    tools: IntegrationTools, core_client: HomeAssistantClient
) -> None:
    with (
        patch.object(
            core_client,
            "send_websocket_message",
            side_effect=HomeAssistantConnectionError("ws drop"),
        ),
        pytest.raises(ToolError) as exc_info,
    ):
        await _remove(tools, "sensor.my_otp")

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "CONNECTION_FAILED"
    assert "ws drop" in error["message"]


# Core closes the refused connection; aiohttp's heartbeat timer outlives it.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_a_refused_registry_read_keeps_its_token_guidance(
    tools: IntegrationTools, core_client: HomeAssistantClient
) -> None:
    """Core refuses the token: the error keeps its token guidance and adds the
    entry_id route, which needs no registry read. Adding it must not leak into
    the shared defaults every other tool's errors are built from."""
    defaults = {code: list(hints) for code, hints in DEFAULT_SUGGESTIONS.items()}

    with (
        patch.object(core_client, "token", "rejected-token"),
        pytest.raises(ToolError) as exc_info,
    ):
        await _remove(tools, "sensor.my_otp")

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == ErrorCode.AUTH_INVALID_TOKEN
    *classified, entry_id_route = error["suggestions"]
    assert classified == DEFAULT_SUGGESTIONS[ErrorCode.AUTH_INVALID_TOKEN]
    assert "config entry_id" in entry_id_route
    assert defaults == DEFAULT_SUGGESTIONS


@pytest.mark.parametrize("kind", ["flow", "storage"])
async def test_entity_route_logs_one_tool_call(
    hass: HomeAssistant,
    tools: IntegrationTools,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """The resolved removal must not log a second, synthetic tool call."""
    if kind == "flow":
        entity_id, _ = _flow_entity(hass, entity_registry, "utility_meter")
    else:
        entity_id = await _storage_entity(hass, core_client)
    logged: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "ha_mcp.tools.helpers.log_tool_call", lambda **kw: logged.append(kw)
    )

    await _remove(tools, entity_id)

    assert [(c["tool_name"], c["parameters"]["target"]) for c in logged] == [
        ("ha_remove_helpers_integrations", entity_id)
    ]


@pytest.mark.usefixtures("auto_backup_on")
@pytest.mark.parametrize("kind", ["flow", "storage"])
async def test_entity_route_backs_up_the_resolved_helper_before_deleting(
    hass: HomeAssistant,
    tools: IntegrationTools,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """A helper removed by its entity_id gets one snapshot attempt for the
    resolved helper, made while Core still has it."""
    if kind == "flow":
        entity_id, entry = _flow_entity(hass, entity_registry, "otp")
        expected = ("helper_otp", entry.entry_id)

        def present() -> bool:
            return hass.config_entries.async_get_entry(entry.entry_id) is not None
    else:
        entity_id = await _storage_entity(hass, core_client)
        expected = ("helper_input_boolean", entity_id)

        def present() -> bool:
            return hass.states.get(entity_id) is not None

    taken: list[tuple[str, str, bool]] = []

    async def record(_mgr: Any, domain: str, target: str, **_: Any) -> None:
        taken.append((domain, target, present()))

    monkeypatch.setattr(bm.BackupManager, "maybe_snapshot", record)

    await _remove(tools, entity_id)
    await hass.async_block_till_done()

    assert taken == [(*expected, True)]
    assert not present()


@pytest.mark.usefixtures("auto_backup_on")
@pytest.mark.parametrize(
    ("platform", "reason"),
    [
        ("otp", "has no stored options to back up or restore"),
        ("my_custom_helper", "whose options the component withholds"),
    ],
    ids=["otp", "custom_only"],
)
async def test_a_helper_whose_options_are_not_backed_up_is_removed_with_the_reason(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    platform: str,
    reason: str,
) -> None:
    """Through the real capture: the by-design skip lets the delete run, tells
    the caller why no backup was taken, and is not logged as a fetch failure.

    otp keeps everything in entry.data, so the component lists empty options;
    a custom helper integration's options are withheld by the component."""
    from custom_components.ha_mcp_tools.websocket_api import flow_domains

    flows = CORE_HELPER_FLOWS | {"my_custom_helper"}
    # A custom helper integration is listed by Core's loader and by Core's
    # flow_handlers?type=helper, which a unit test's Core cannot install;
    # conftest's ``_server_helper_flows`` names the read to patch for that.
    monkeypatch.setattr(
        flow_domains, "async_get_config_flows", AsyncMock(return_value=set(flows))
    )
    monkeypatch.setattr(
        helper_flows, "_fetch_helper_flow_types", AsyncMock(return_value=flows)
    )
    assert await async_setup_component(hass, "config", {})
    entity_id, entry = _flow_entity(
        hass,
        entity_registry,
        platform,
        options=None if platform == "otp" else {"note": "x"},
    )

    with caplog.at_level(logging.INFO, logger=bm.logger.name):
        result = await _remove(IntegrationTools(component_core_client), entity_id)

    assert hass.config_entries.async_get_entry(entry.entry_id) is None
    [warning] = result["warnings"]
    assert warning.startswith(
        f"No pre-write backup of helper_{platform}:{entry.entry_id} was taken"
    )
    assert reason in warning
    backup_logs = [r for r in caplog.records if r.name == bm.logger.name]
    assert all(r.levelno < logging.WARNING for r in backup_logs)
    assert any(
        r.levelno == logging.INFO and reason in r.getMessage() for r in backup_logs
    )


@pytest.mark.usefixtures("auto_backup_on")
async def test_a_flow_helper_named_by_type_is_snapshotted_once_before_deleting(
    hass: HomeAssistant,
    tools: IntegrationTools,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit flow helper_type is left by the outer capture to the flow
    path, which snapshots the resolved entry once."""
    entity_id, entry = _flow_entity(hass, entity_registry, "utility_meter")
    taken: list[tuple[str, str, bool]] = []

    async def record(_mgr: Any, domain: str, target: str, **_: Any) -> None:
        present = hass.config_entries.async_get_entry(entry.entry_id) is not None
        taken.append((domain, target, present))

    monkeypatch.setattr(bm.BackupManager, "maybe_snapshot", record)

    result = await _remove(tools, entity_id, helper_type="utility_meter")

    assert result["success"] is True
    assert taken == [("helper_utility_meter", entry.entry_id, True)]
