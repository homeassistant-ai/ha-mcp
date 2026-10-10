"""Helper types come from Core's helper flow list, read at call time (#2632)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp import backup_manager as bm
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools import auto_backup, flow_helper_lookup, helper_flows
from ha_mcp.tools.backup import _edits_create
from ha_mcp.tools.config_entry_backup import resolve_config_entry_backup_domain
from ha_mcp.tools.config_helpers.schemas import SIMPLE_HELPER_TYPES
from ha_mcp.tools.flow_helper_lookup import _raise_platform_mismatch
from ha_mcp.tools.tools_config_helpers import HelperConfigTools
from ha_mcp.tools.tools_integrations import IntegrationTools

from .test_backup_manager import _StubClient, _StubSettings

# The real Core read, kept before conftest's autouse fixture replaces it.
_REAL_FETCH = helper_flows._fetch_helper_flow_types


class _Client:
    """A REST client double that can be weakly referenced, like the real one."""

    def __init__(self, reply: Any) -> None:
        self._request = AsyncMock(return_value=reply)


def _error(exc_info: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(exc_info.value))["error"]


@pytest.mark.asyncio
async def test_helper_types_are_read_from_core_once_per_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(helper_flows, "_fetch_helper_flow_types", _REAL_FETCH)
    client = _Client(["template", "my_helper"])
    for _ in range(2):
        assert await helper_flows.helper_flow_types(client) == {"template", "my_helper"}
    client._request.assert_awaited_once_with(
        "GET", "/config/config_entries/flow_handlers", params={"type": "helper"}
    )


@pytest.mark.asyncio
async def test_a_helper_integration_installed_later_shows_up_after_the_cache_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(helper_flows, "_fetch_helper_flow_types", _REAL_FETCH)
    monkeypatch.setattr(helper_flows, "_TTL_SECONDS", 0.0)
    client = _Client(["template"])
    await helper_flows.helper_flow_types(client)
    await helper_flows.helper_flow_types(client)
    assert client._request.await_count == 2


@pytest.mark.asyncio
async def test_a_client_that_cannot_be_weakly_referenced_is_still_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(helper_flows, "_fetch_helper_flow_types", _REAL_FETCH)
    client = SimpleNamespace(_request=AsyncMock(return_value=["template"]))
    assert await helper_flows.helper_flow_types(client) == {"template"}


@pytest.mark.asyncio
async def test_an_unreadable_helper_list_is_a_connection_error_not_an_empty_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(helper_flows, "_fetch_helper_flow_types", _REAL_FETCH)
    with pytest.raises(HomeAssistantConnectionError):
        await helper_flows.helper_flow_types(_Client({}))


@pytest.mark.asyncio
@pytest.mark.parametrize("helper_type,also", [("input_boolean", ()), ("all", ("all",))])
async def test_storage_types_need_no_core_read(
    monkeypatch: pytest.MonkeyPatch, helper_type: str, also: tuple[str, ...]
) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(helper_flows, "_fetch_helper_flow_types", fetch)
    await helper_flows.require_helper_type(_Client([]), helper_type, *also)
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_otp_is_accepted_like_any_helper_flow_core_lists() -> None:
    await helper_flows.require_helper_type(_Client([]), "otp")


@pytest.mark.asyncio
async def test_a_helper_integration_installed_since_the_last_read_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch = AsyncMock(side_effect=[frozenset({"template"}), frozenset({"new_helper"})])
    monkeypatch.setattr(helper_flows, "_fetch_helper_flow_types", fetch)
    client = _Client([])
    await helper_flows.helper_flow_types(client)  # cached before the install
    await helper_flows.require_helper_type(client, "new_helper")
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_a_fresh_read_is_not_repeated_for_an_unknown_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch = AsyncMock(return_value=frozenset({"template"}))
    monkeypatch.setattr(helper_flows, "_fetch_helper_flow_types", fetch)
    with pytest.raises(ToolError):
        await helper_flows.require_helper_type(_Client([]), "no_such_helper")
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_an_entity_of_a_newly_installed_helper_integration_is_a_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch = AsyncMock(side_effect=[frozenset({"template"}), frozenset({"new_helper"})])
    monkeypatch.setattr(helper_flows, "_fetch_helper_flow_types", fetch)
    monkeypatch.setattr(
        flow_helper_lookup,
        "_read_registry_entry",
        AsyncMock(return_value=({"platform": "new_helper"}, "ok")),
    )
    client = _Client([])
    await helper_flows.helper_flow_types(client)  # cached before the install
    assert await flow_helper_lookup.resolve_helper_entity(client, "sensor.new") == (
        "new_helper",
        "sensor.new",
    )


_UNKNOWN_TYPE_CALLS = [
    lambda: HelperConfigTools(_Client([])).ha_config_list_helpers(
        helper_type="no_such_helper"
    ),
    lambda: HelperConfigTools(_Client([])).ha_config_set_helper(
        helper_type="no_such_helper", name="X"
    ),
    lambda: IntegrationTools(_Client([])).ha_remove_helpers_integrations(
        target="no_such_helper.x", helper_type="no_such_helper", confirm=True
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("call", _UNKNOWN_TYPE_CALLS, ids=["list", "set", "remove"])
async def test_an_unknown_helper_type_is_refused_naming_the_known_ones(
    call: Any,
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await call()
    error = _error(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "no_such_helper" in error["message"]
    suggestions = " | ".join(error["suggestions"])
    assert "Storage helpers: counter" in suggestions
    assert "Helper flows on this instance: " in suggestions
    assert "template" in suggestions


@pytest.mark.asyncio
@pytest.mark.parametrize("call", _UNKNOWN_TYPE_CALLS, ids=["list", "set", "remove"])
async def test_a_failed_helper_flow_read_is_a_structured_connection_error(
    monkeypatch: pytest.MonkeyPatch, call: Any
) -> None:
    monkeypatch.setattr(
        helper_flows,
        "_fetch_helper_flow_types",
        AsyncMock(side_effect=HomeAssistantConnectionError("down")),
    )
    with pytest.raises(ToolError) as exc_info:
        await call()
    assert _error(exc_info)["code"] == "CONNECTION_FAILED"


def _enable_auto_backup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        auto_backup,
        "get_global_settings",
        lambda: SimpleNamespace(enable_auto_backup=True, auto_backup_dir=str(tmp_path)),
    )


@pytest.mark.asyncio
async def test_an_update_with_an_unknown_helper_type_is_refused_before_any_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """with_auto_backup captures before the tool body checks helper_type; the
    update is still refused before its options flow starts."""
    _enable_auto_backup(monkeypatch, tmp_path)

    async def fake_ws_send(client: Any, message: dict[str, Any]) -> Any:
        return {"covered_types": [], "helpers": []}

    monkeypatch.setattr(bm, "_ws_send", fake_ws_send)
    client = _Client([])
    client.start_options_flow = AsyncMock()
    with pytest.raises(ToolError) as exc_info:
        await HelperConfigTools(client).ha_config_set_helper(
            helper_type="no_such_helper", helper_id="entry_1", config={"x": 1}
        )
    assert _error(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
    client.start_options_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_entry_delete_proceeds_when_the_helper_flow_read_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Deciding whether the deleted entry is a helper needs Core's helper flow
    list; when that read fails the delete still runs, with the skipped backup
    reported."""
    _enable_auto_backup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        helper_flows,
        "_fetch_helper_flow_types",
        AsyncMock(side_effect=HomeAssistantConnectionError("down")),
    )
    # A capture that got past the domain decision would read through here.
    monkeypatch.setattr(bm, "_ws_send", AsyncMock(return_value=[]))
    client = _Client([])
    client.get_config_entry = AsyncMock(return_value={"domain": "template"})
    client.delete_config_entry = AsyncMock(return_value={"require_restart": False})
    result = await IntegrationTools(client).ha_remove_helpers_integrations(
        target="entry_1", confirm=True
    )
    client.delete_config_entry.assert_awaited_once_with("entry_1")
    [warning] = result.get("warnings") or [None]
    assert warning is not None
    assert warning.startswith("No pre-write backup was taken")


def _custom_flow_client() -> MagicMock:
    """A client whose config flow for my_custom_helper takes a name and a source."""
    client = MagicMock()
    client.start_config_flow = AsyncMock(
        return_value={
            "type": "form",
            "flow_id": "f1",
            "step_id": "user",
            "data_schema": [{"name": "name"}, {"name": "source"}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={
            "type": "create_entry",
            "result": {"entry_id": "e-custom", "domain": "my_custom_helper"},
        }
    )
    client.abort_config_flow = AsyncMock(return_value={})
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": []}
    )
    return client


@pytest.mark.asyncio
async def test_a_custom_helper_flow_is_created_through_its_config_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A helper integration outside Core's built-in list is created through
    its own config flow, like template or group."""
    monkeypatch.setattr(
        helper_flows,
        "_fetch_helper_flow_types",
        AsyncMock(return_value=frozenset({"template", "my_custom_helper"})),
    )
    client = _custom_flow_client()
    result = await HelperConfigTools(client).ha_config_set_helper(
        helper_type="my_custom_helper",
        name="Gate",
        config={"source": "sensor.x"},
        wait=False,
    )
    assert result["success"] is True, result
    assert {c.args[0] for c in client.start_config_flow.await_args_list} == {
        "my_custom_helper"
    }
    submitted = [c.args[1] for c in client.submit_config_flow_step.await_args_list]
    assert {"name": "Gate", "source": "sensor.x"} in submitted


@pytest.mark.asyncio
async def test_a_custom_helper_flow_describes_its_config_flow_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        helper_flows,
        "_fetch_helper_flow_types",
        AsyncMock(return_value=frozenset({"template", "my_custom_helper"})),
    )
    client = _custom_flow_client()
    result = await HelperConfigTools(client).ha_config_list_helpers(
        helper_type="my_custom_helper", describe=True
    )
    client.start_config_flow.assert_awaited_with("my_custom_helper")
    assert [field["name"] for field in result["fields"]] == ["name", "source"]


@pytest.mark.asyncio
async def test_a_custom_helper_flow_is_removed_through_the_flow_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        helper_flows,
        "_fetch_helper_flow_types",
        AsyncMock(return_value=frozenset({"template", "my_custom_helper"})),
    )
    delete_flow = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(IntegrationTools, "_delete_flow_helper", delete_flow)
    await IntegrationTools(_Client([])).ha_remove_helpers_integrations(
        target="sensor.custom", helper_type="my_custom_helper", confirm=True
    )
    assert delete_flow.await_args.args[0] == "my_custom_helper"


@pytest.mark.parametrize("helper_type", ["template", "otp", "my_custom_helper"])
def test_any_flow_helper_type_gets_an_options_handler(
    tmp_path: Path, helper_type: str
) -> None:
    mgr = bm.get_backup_manager(
        _StubClient(), _StubSettings(auto_backup_dir=str(tmp_path))
    )
    handler = mgr.handler_for(f"helper_{helper_type}")
    assert handler is not None
    assert handler.fetch.__qualname__.startswith("_make_flow_helper_handler")


@pytest.mark.parametrize("domain", ["helper_input_boolean", "helper_config_subentry"])
def test_storage_and_subentry_domains_get_no_flow_handler(
    tmp_path: Path, domain: str
) -> None:
    mgr = bm.get_backup_manager(
        _StubClient(), _StubSettings(auto_backup_dir=str(tmp_path))
    )
    handler = mgr.handler_for(domain)
    assert handler is not None
    assert not handler.fetch.__qualname__.startswith("_make_flow_helper_handler")


@pytest.mark.parametrize(
    "domain,is_flow",
    [
        ("helper_template", True),
        ("helper_my_custom_helper", True),
        ("helper_input_boolean", False),
        ("helper_config_subentry", False),
        ("helper_", False),
        ("automation", False),
    ],
)
def test_flow_helper_snapshot_domains_need_no_list_of_flow_types(
    domain: str, is_flow: bool
) -> None:
    assert bm._is_flow_helper_domain(domain) is is_flow


def test_every_storage_type_is_snapshotted_as_storage_not_as_a_flow() -> None:
    assert bm._HELPER_LIST_TYPES == SIMPLE_HELPER_TYPES


def test_a_mistyped_flow_domain_does_not_stay_in_the_supported_domains(
    tmp_path: Path,
) -> None:
    mgr = bm.get_backup_manager(
        _StubClient(), _StubSettings(auto_backup_dir=str(tmp_path))
    )
    before = mgr.supported_domains()
    assert mgr.handler_for("helper_input_bolean") is not None
    assert mgr.supported_domains() == before


@pytest.mark.asyncio
async def test_an_on_demand_snapshot_of_an_unlisted_helper_type_is_refused(
    tmp_path: Path,
) -> None:
    mgr = bm.get_backup_manager(
        _StubClient(), _StubSettings(auto_backup_dir=str(tmp_path))
    )
    with pytest.raises(ToolError) as exc_info:
        await _edits_create(mgr, "edits", "create", "helper_input_bolean", "x.y")
    assert _error(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"


def test_a_platform_that_is_a_custom_helper_flow_is_offered_as_the_retry() -> None:
    with pytest.raises(ToolError) as exc_info:
        _raise_platform_mismatch(
            "sensor.x", "template", "my_custom_helper", frozenset({"my_custom_helper"})
        )
    assert _error(exc_info)["suggestion"] == (
        "Pass helper_type='my_custom_helper', or omit helper_type."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry_domain,backup_domain",
    [("template", "helper_template"), ("hue", "integration")],
)
async def test_an_entry_delete_is_backed_up_as_a_helper_only_for_a_helper_flow(
    entry_domain: str, backup_domain: str
) -> None:
    client = MagicMock(
        get_config_entry=AsyncMock(return_value={"domain": entry_domain})
    )
    domain = await resolve_config_entry_backup_domain(
        client, {"target": "e1"}, "integration", "e1"
    )
    assert domain == backup_domain


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record,reason",
    [
        (
            {"options": None, "options_withheld": "custom_integration"},
            "options_withheld",
        ),
        ({"options": {}}, "invalid_options"),
    ],
)
async def test_a_helper_without_readable_options_is_not_backed_up_and_says_why(
    monkeypatch: pytest.MonkeyPatch, record: dict[str, Any], reason: str
) -> None:
    async def fake_ws_send(client: Any, message: dict[str, Any]) -> Any:
        if message["type"] == "ha_mcp_tools/helpers_list":
            return {
                "covered_types": ["my_helper"],
                "helpers": [
                    {"kind": "flow", "helper_type": "my_helper", "entry_id": "e1"}
                    | record
                ],
            }
        return []

    monkeypatch.setattr(bm, "_ws_send", fake_ws_send)
    with pytest.raises(bm._FlowHelperReadError) as exc_info:
        await bm._fetch_flow_helper(None, "e1", "my_helper")
    assert exc_info.value.reason == reason
