"""Native edit routing preserves results and never repeats an ambiguous write."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandNotSent,
    HomeAssistantCommandTimeout,
)
from ha_mcp.tools import auto_backup, component_dashboard_edit, tools_config_dashboards
from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools
from ha_mcp.utils.config_hash import compute_config_hash


@pytest.fixture
def legacy_dashboard(monkeypatch):
    document = {"views": [{"cards": [{"type": "button", "icon": "mdi:lamp"}]}]}
    client = MagicMock(base_url="http://ha.local:8123", token="test", verify_ssl=True)
    messages = []

    async def send(message):
        messages.append(deepcopy(message))
        if message["type"] == "lovelace/config":
            return {"success": True, "result": deepcopy(document)}
        if message["type"] == "lovelace/config/save":
            document.clear()
            document.update(deepcopy(message["config"]))
            return {"success": True, "result": None}
        return {"success": True, "result": []}

    client.send_websocket_message = AsyncMock(side_effect=send)
    monkeypatch.setattr(
        tools_config_dashboards, "get_component_caps", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        auto_backup,
        "get_global_settings",
        lambda: SimpleNamespace(enable_auto_backup=False),
    )
    monkeypatch.setattr(
        component_dashboard_edit, "get_component_caps", AsyncMock(return_value=None)
    )
    return client, document, messages


async def test_structured_edit_works_without_component(legacy_dashboard):
    client, document, messages = legacy_dashboard
    result = await DashboardConfigTools(client).ha_config_set_dashboard(
        url_path="test-dashboard",
        config_hash=compute_config_hash(document),
        patch=[
            {
                "op": "replace",
                "path": "/views/0/cards/0/icon",
                "value": "mdi:music-box-multiple",
            }
        ],
        MandatoryBPS=False,
    )
    assert document["views"][0]["cards"][0]["icon"] == "mdi:music-box-multiple"
    assert result["config_hash"] == compute_config_hash(document)
    assert result["write_committed"] is True
    assert result["post_write_verified"] is True
    assert sum(m["type"] == "lovelace/config/save" for m in messages) == 1


async def test_patch_conflict_never_saves(legacy_dashboard):
    client, document, messages = legacy_dashboard
    before = deepcopy(document)
    with pytest.raises(ToolError, match="conflict"):
        await DashboardConfigTools(client).ha_config_set_dashboard(
            url_path="test-dashboard",
            config_hash="stale",
            patch=[{"op": "remove", "path": "/views/0"}],
            MandatoryBPS=False,
        )
    assert document == before
    assert not any(m["type"] == "lovelace/config/save" for m in messages)


async def test_sent_native_timeout_is_not_a_fallback(monkeypatch):
    from ha_mcp.tools import component_dashboard_edit as native

    monkeypatch.setattr(
        native,
        "get_component_caps",
        AsyncMock(
            return_value=SimpleNamespace(capabilities=frozenset({"dashboard_edit"}))
        ),
    )
    ws = SimpleNamespace(
        send_command=AsyncMock(side_effect=HomeAssistantCommandTimeout("timeout"))
    )
    monkeypatch.setattr(native, "get_websocket_client", AsyncMock(return_value=ws))
    client = SimpleNamespace(base_url="http://ha.local", token="test", verify_ssl=True)
    with pytest.raises(ToolError, match="unknown"):
        await native.edit_dashboard_via_component(
            client,
            "test-dashboard",
            expected_hash="old",
            patch=[{"op": "remove", "path": "/views/0"}],
        )
    assert ws.send_command.await_count == 1


@pytest.fixture
def native_socket(monkeypatch):
    monkeypatch.setattr(
        component_dashboard_edit,
        "get_component_caps",
        AsyncMock(return_value=SimpleNamespace(capabilities={"dashboard_edit"})),
    )
    ws = SimpleNamespace(send_command=AsyncMock())
    monkeypatch.setattr(
        component_dashboard_edit, "get_websocket_client", AsyncMock(return_value=ws)
    )
    return ws


def _success(config, **overrides):
    return {
        "success": True,
        "result": {
            "success": True,
            "config": config,
            "config_hash": compute_config_hash(config),
            "write_committed": True,
            "post_write_verified": True,
            "previous_config_size": 100,
            **overrides,
        },
    }


@pytest.mark.parametrize("mode", ["patch", "python_transform", "config"])
async def test_native_modes_use_authoritative_result_without_extra_read(
    legacy_dashboard, native_socket, mode, monkeypatch
):
    client, document, messages = legacy_dashboard
    # A full replacement still resolves the registry and preserves metadata.
    tools = DashboardConfigTools(client)
    monkeypatch.setattr(
        tools,
        "_ensure_dashboard_exists",
        AsyncMock(return_value=(True, "id", False, None)),
    )
    final_config = {"views": [{"title": "Authoritative", "path": "home"}]}
    native_socket.send_command.return_value = _success(final_config)
    edits = {
        "patch": [{"op": "replace", "path": "/views/0/title", "value": "New"}],
        "python_transform": "config['views'][0]['title'] = 'New'",
        "config": {"views": [{"title": "New"}]},
    }
    expected_hash = compute_config_hash(document)
    result = await tools.ha_config_set_dashboard(
        url_path="test-dashboard",
        config_hash=expected_hash,
        MandatoryBPS=False,
        **{mode: edits[mode]},
    )
    assert result["config_hash"] == compute_config_hash(final_config)
    assert result["post_write_verified"] is True
    assert result["write_committed"] is True
    assert result["render_paths"][0]["render_path"] == "test-dashboard/home"
    assert not any(m["type"] == "lovelace/config/save" for m in messages)
    assert len(messages) == (1 if mode == "python_transform" else 0)
    native_socket.send_command.assert_awaited_once()
    args, kwargs = native_socket.send_command.call_args
    assert args == ("ha_mcp_tools/dashboard_edit",)
    assert kwargs["expected_hash"] == expected_hash
    assert "python_transform" not in kwargs
    assert "action" not in kwargs
    if mode == "python_transform":
        assert kwargs["config"]["views"][0]["title"] == "New"
    else:
        assert kwargs[mode] == edits[mode]


@pytest.mark.parametrize("caps", [None, SimpleNamespace(capabilities={"dashboards"})])
async def test_absent_or_old_capabilities_do_not_send(native_socket, monkeypatch, caps):
    monkeypatch.setattr(
        component_dashboard_edit, "get_component_caps", AsyncMock(return_value=caps)
    )
    client = SimpleNamespace(base_url="http://ha.local", token="test")
    result = await component_dashboard_edit.edit_dashboard_via_component(
        client, "test-dashboard", config={"views": []}
    )
    assert result is None
    native_socket.send_command.assert_not_awaited()


async def test_unknown_command_falls_back_once(legacy_dashboard, native_socket):
    client, document, messages = legacy_dashboard
    native_socket.send_command.side_effect = HomeAssistantCommandError(
        "Unknown command", "unknown_command"
    )
    result = await DashboardConfigTools(client).ha_config_set_dashboard(
        "test-dashboard",
        config_hash=compute_config_hash(document),
        patch=[
            {"op": "replace", "path": "/views/0/cards/0/icon", "value": "mdi:music"}
        ],
        MandatoryBPS=False,
    )
    assert result["success"] is True
    assert document["views"][0]["cards"][0]["icon"] == "mdi:music"
    assert sum(m["type"] == "lovelace/config/save" for m in messages) == 1
    native_socket.send_command.assert_awaited_once()


@pytest.mark.parametrize(
    "reply",
    [
        None,
        {"success": True, "result": None},
        _success({}, config_hash=None),
        _success({}, warnings="not a list"),
        _success({}, previous_config_size=True),
        _success({}, previous_config_size=-1),
        _success({}, post_write_verified=False),
    ],
)
async def test_malformed_native_reply_never_falls_back(
    legacy_dashboard, native_socket, reply
):
    client, document, messages = legacy_dashboard
    native_socket.send_command.return_value = reply
    with pytest.raises(ToolError, match="unknown") as caught:
        await DashboardConfigTools(client).ha_config_set_dashboard(
            "test-dashboard",
            config_hash=compute_config_hash(document),
            patch=[{"op": "remove", "path": "/views/0"}],
            MandatoryBPS=False,
        )
    assert json.loads(str(caught.value))["write_committed"] is None
    assert messages == []
    native_socket.send_command.assert_awaited_once()


@pytest.mark.parametrize(
    "error,committed",
    [
        (HomeAssistantCommandTimeout("timeout"), None),
        (ConnectionError("connection lost after send"), None),
        (HomeAssistantCommandNotSent("not authenticated"), False),
    ],
)
async def test_native_transport_errors_do_not_duplicate_write(
    legacy_dashboard, native_socket, error, committed
):
    client, document, messages = legacy_dashboard
    native_socket.send_command.side_effect = error
    with pytest.raises(ToolError) as caught:
        await DashboardConfigTools(client).ha_config_set_dashboard(
            "test-dashboard",
            config_hash=compute_config_hash(document),
            patch=[{"op": "remove", "path": "/views/0"}],
            MandatoryBPS=False,
        )
    assert json.loads(str(caught.value))["write_committed"] is committed
    assert messages == []
    native_socket.send_command.assert_awaited_once()


async def test_native_hash_conflict_does_not_save_again(
    legacy_dashboard, native_socket
):
    client, document, messages = legacy_dashboard
    native_socket.send_command.return_value = {
        "success": True,
        "result": {
            "success": False,
            "error": {"code": "conflict", "message": "Dashboard changed (conflict)"},
            "write_committed": False,
        },
    }
    with pytest.raises(ToolError, match="conflict"):
        await DashboardConfigTools(client).ha_config_set_dashboard(
            "test-dashboard",
            config_hash=compute_config_hash(document),
            python_transform="config['title'] = 'Changed'",
            MandatoryBPS=False,
        )
    assert len(messages) == 1  # Only the pre-transform read.
    assert messages[0]["type"] == "lovelace/config"
    assert "title" not in document
    native_socket.send_command.assert_awaited_once()


@pytest.mark.parametrize("as_json", [False, True])
async def test_patch_values_remain_literal(legacy_dashboard, as_json):
    client, document, _ = legacy_dashboard
    value = '{"literal": "{{ do_not_render }}"}'
    patch = [{"op": "add", "path": "/title", "value": value}]
    result = await DashboardConfigTools(client).ha_config_set_dashboard(
        "test-dashboard",
        config_hash=compute_config_hash(document),
        patch=json.dumps(patch) if as_json else patch,
        MandatoryBPS=False,
    )
    assert result["action"] == "patch"
    assert document["title"] == value


@pytest.mark.parametrize(
    "extra", [{"config": {}}, {"python_transform": "config['title'] = 'New'"}]
)
async def test_patch_is_mutually_exclusive(legacy_dashboard, extra):
    client, _, messages = legacy_dashboard
    with pytest.raises(ToolError, match="simultaneously"):
        await DashboardConfigTools(client).ha_config_set_dashboard(
            "test-dashboard", patch=[], MandatoryBPS=False, **extra
        )
    assert messages == []


async def test_patch_requires_hash_before_read_or_write(legacy_dashboard):
    client, _, messages = legacy_dashboard
    with pytest.raises(ToolError, match="config_hash is required"):
        await DashboardConfigTools(client).ha_config_set_dashboard(
            "test-dashboard", patch=[], MandatoryBPS=False
        )
    assert messages == []


async def test_native_unverified_save_preserves_warning_without_readback(
    legacy_dashboard, native_socket, monkeypatch
):
    client, document, messages = legacy_dashboard
    native_socket.send_command.return_value = _success(
        document,
        config_hash=None,
        post_write_verified=False,
        warnings=["Post-save read failed"],
    )
    tools = DashboardConfigTools(client)
    monkeypatch.setattr(
        tools,
        "_ensure_dashboard_exists",
        AsyncMock(return_value=(True, "id", False, None)),
    )
    result = await tools.ha_config_set_dashboard(
        "test-dashboard", config={"views": []}, MandatoryBPS=False
    )
    assert result["write_committed"] is True
    assert result["post_write_verified"] is False
    assert result["config_hash"] is None
    assert "render_paths" not in result
    assert "Post-save read failed" in result["warnings"]
    assert messages == []


async def test_native_noop_patch_returns_fresh_paths_without_write(
    legacy_dashboard, native_socket
):
    client, document, messages = legacy_dashboard
    native_socket.send_command.return_value = _success(
        document, unchanged=True, write_committed=False
    )
    result = await DashboardConfigTools(client).ha_config_set_dashboard(
        "test-dashboard",
        patch=[],
        config_hash=compute_config_hash(document),
        MandatoryBPS=False,
    )
    assert result["unchanged"] is True
    assert result["write_committed"] is False
    assert result["post_write_verified"] is True
    assert result["config_hash"] == compute_config_hash(document)
    assert "render_paths" in result
    assert messages == []


async def test_native_replacement_preserves_large_config_warning(
    legacy_dashboard, native_socket, monkeypatch
):
    client, document, messages = legacy_dashboard
    native_socket.send_command.return_value = _success(
        document, previous_config_size=12000
    )
    tools = DashboardConfigTools(client)
    monkeypatch.setattr(
        tools,
        "_ensure_dashboard_exists",
        AsyncMock(return_value=(True, "id", False, None)),
    )
    result = await tools.ha_config_set_dashboard(
        "test-dashboard", config={"views": []}, MandatoryBPS=False
    )
    assert any("12,000 bytes" in warning for warning in result["warnings"])
    assert messages == []


async def test_socket_acquisition_failure_is_known_not_written(
    legacy_dashboard, native_socket, monkeypatch
):
    client, document, messages = legacy_dashboard
    monkeypatch.setattr(
        component_dashboard_edit,
        "get_websocket_client",
        AsyncMock(side_effect=ConnectionError("unavailable")),
    )
    with pytest.raises(ToolError) as caught:
        await DashboardConfigTools(client).ha_config_set_dashboard(
            "test-dashboard",
            patch=[],
            config_hash=compute_config_hash(document),
            MandatoryBPS=False,
        )
    assert json.loads(str(caught.value))["write_committed"] is False
    assert messages == []
    native_socket.send_command.assert_not_awaited()


async def test_native_conflict_reports_metadata_already_updated(
    legacy_dashboard, native_socket, monkeypatch
):
    client, document, messages = legacy_dashboard
    dashboard = {
        "id": "test_dashboard",
        "url_path": "test-dashboard",
        "title": "Before",
        "mode": "storage",
    }
    original_send = client.send_websocket_message.side_effect

    async def send(message):
        if message["type"] == "lovelace/dashboards/update":
            messages.append(deepcopy(message))
            dashboard["title"] = message["title"]
            return {"success": True, "result": deepcopy(dashboard)}
        return await original_send(message)

    client.send_websocket_message.side_effect = send
    monkeypatch.setattr(
        tools_config_dashboards,
        "fetch_dashboards_list",
        AsyncMock(return_value=[dashboard]),
    )
    native_socket.send_command.return_value = {
        "success": True,
        "result": {
            "success": False,
            "error": {"code": "conflict", "message": "Dashboard changed (conflict)"},
            "write_committed": False,
        },
    }
    before = deepcopy(document)
    with pytest.raises(ToolError, match="conflict") as caught:
        await DashboardConfigTools(client).ha_config_set_dashboard(
            "test-dashboard",
            title="After",
            config={"views": []},
            config_hash="stale",
            MandatoryBPS=False,
        )
    error = json.loads(str(caught.value))
    assert dashboard["title"] == "After"
    assert document == before
    assert error["metadata_updated"] is True
    assert error["dashboard_created"] is False
    assert error["write_committed"] is True
    assert error["config_write_committed"] is False
    assert [message["type"] for message in messages] == ["lovelace/dashboards/update"]
    native_socket.send_command.assert_awaited_once()


async def test_native_connection_failure_reports_dashboard_already_created(
    legacy_dashboard, native_socket, monkeypatch
):
    client, _, messages = legacy_dashboard
    dashboards = []
    original_send = client.send_websocket_message.side_effect

    async def send(message):
        if message["type"] == "lovelace/dashboards/create":
            messages.append(deepcopy(message))
            dashboard = {**message, "id": "new_dashboard", "mode": "storage"}
            dashboards.append(dashboard)
            return {"success": True, "result": deepcopy(dashboard)}
        return await original_send(message)

    client.send_websocket_message.side_effect = send
    monkeypatch.setattr(
        tools_config_dashboards,
        "fetch_dashboards_list",
        AsyncMock(return_value=dashboards),
    )
    monkeypatch.setattr(
        component_dashboard_edit,
        "get_websocket_client",
        AsyncMock(side_effect=ConnectionError("unavailable")),
    )
    with pytest.raises(ToolError) as caught:
        await DashboardConfigTools(client).ha_config_set_dashboard(
            "new-dashboard", config={"views": []}, MandatoryBPS=False
        )
    error = json.loads(str(caught.value))
    assert dashboards[0]["url_path"] == "new-dashboard"
    assert error["dashboard_created"] is True
    assert error["metadata_updated"] is False
    assert error["write_committed"] is True
    assert error["config_write_committed"] is False
    assert [message["type"] for message in messages] == ["lovelace/dashboards/create"]
    native_socket.send_command.assert_not_awaited()


@pytest.mark.parametrize(
    "patch",
    [
        [],
        [{"op": "test", "path": "/views/0/cards/0/icon", "value": "mdi:lamp"}],
        [{"op": "replace", "path": "/views/0/cards/0/icon", "value": "mdi:lamp"}],
    ],
)
async def test_legacy_unchanged_patch_does_not_save(legacy_dashboard, patch):
    client, document, messages = legacy_dashboard
    original = deepcopy(document)
    original_hash = compute_config_hash(document)
    result = await DashboardConfigTools(client).ha_config_set_dashboard(
        "test-dashboard",
        patch=patch,
        config_hash=original_hash,
        MandatoryBPS=False,
    )
    assert document == original
    assert result["write_committed"] is False
    assert result["post_write_verified"] is True
    assert result["unchanged"] is True
    assert result["config_hash"] == original_hash
    assert "render_paths" in result
    assert [message["type"] for message in messages] == ["lovelace/config"]


async def test_legacy_patch_boolean_to_number_is_a_change(legacy_dashboard):
    client, document, messages = legacy_dashboard
    document["counter"] = True
    result = await DashboardConfigTools(client).ha_config_set_dashboard(
        "test-dashboard",
        patch=[{"op": "replace", "path": "/counter", "value": 1}],
        config_hash=compute_config_hash(document),
        MandatoryBPS=False,
    )
    assert document["counter"] == 1
    assert document["counter"] is not True
    assert result["write_committed"] is True
    assert result.get("unchanged") is not True
    assert sum(message["type"] == "lovelace/config/save" for message in messages) == 1


@pytest.mark.parametrize("backend", ["native", "legacy"])
@pytest.mark.parametrize(
    "metadata",
    [
        {"title": "Renamed"},
        {"icon": "mdi:home"},
        {"require_admin": True},
        {"require_admin": False},
        {"show_in_sidebar": True},
        {"show_in_sidebar": False},
    ],
)
async def test_patch_metadata_is_rejected_before_edit(
    legacy_dashboard, native_socket, monkeypatch, backend, metadata
):
    client, document, messages = legacy_dashboard
    if backend == "legacy":
        monkeypatch.setattr(
            component_dashboard_edit,
            "get_component_caps",
            AsyncMock(return_value=None),
        )
    native_socket.send_command.return_value = _success({"views": []})
    before = deepcopy(document)
    with pytest.raises(ToolError, match="metadata") as caught:
        await DashboardConfigTools(client).ha_config_set_dashboard(
            "test-dashboard",
            patch=[{"op": "remove", "path": "/views/0"}],
            config_hash=compute_config_hash(document),
            MandatoryBPS=False,
            **metadata,
        )
    assert json.loads(str(caught.value))["write_committed"] is False
    assert document == before
    assert messages == []
    native_socket.send_command.assert_not_awaited()


async def test_native_cancellation_after_dispatch_reports_unknown_without_retry(
    legacy_dashboard, native_socket
):
    client, document, messages = legacy_dashboard
    dispatched = asyncio.Event()

    async def send(*args, **kwargs):
        # HA commits the edit, but the response has not reached the caller.
        document["views"] = []
        dispatched.set()
        await asyncio.Event().wait()

    native_socket.send_command.side_effect = send
    task = asyncio.create_task(
        DashboardConfigTools(client).ha_config_set_dashboard(
            "test-dashboard",
            patch=[{"op": "remove", "path": "/views/0"}],
            config_hash=compute_config_hash(document),
            MandatoryBPS=False,
        )
    )
    try:
        await asyncio.wait_for(dispatched.wait(), timeout=5)
        task.cancel()
        with pytest.raises(ToolError, match="unknown") as caught:
            await asyncio.gather(task)
        error = json.loads(str(caught.value))
        assert error["reason"] == "write_outcome_unknown"
        assert error["write_committed"] is None
        assert error["post_write_verified"] is False
        assert document["views"] == []
        assert messages == []
        native_socket.send_command.assert_awaited_once()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("stage", ["get_component_caps", "get_websocket_client"])
async def test_native_cancellation_before_dispatch_propagates(
    legacy_dashboard, native_socket, monkeypatch, stage
):
    client, document, messages = legacy_dashboard
    monkeypatch.setattr(
        component_dashboard_edit, stage, AsyncMock(side_effect=asyncio.CancelledError)
    )
    with pytest.raises(asyncio.CancelledError):
        await component_dashboard_edit.edit_dashboard_via_component(
            client, "test-dashboard", config={"views": []}
        )
    assert document["views"]
    assert messages == []
    native_socket.send_command.assert_not_awaited()


@pytest.mark.parametrize("backend", ["absent", "unknown_command"])
@pytest.mark.parametrize("prior_change", ["metadata", "create"])
@pytest.mark.parametrize("failure", ["conflict", "rejected", "disconnected", "invalid"])
async def test_legacy_config_failure_preserves_prior_write(
    legacy_dashboard, native_socket, monkeypatch, backend, prior_change, failure
):
    client, document, messages = legacy_dashboard
    dashboard = {
        "id": "test_dashboard",
        "url_path": "test-dashboard",
        "title": "Before",
        "mode": "storage",
    }
    dashboards = [dashboard] if prior_change == "metadata" else []
    monkeypatch.setattr(
        tools_config_dashboards,
        "fetch_dashboards_list",
        AsyncMock(return_value=dashboards),
    )
    if backend == "absent":
        monkeypatch.setattr(
            component_dashboard_edit,
            "get_component_caps",
            AsyncMock(return_value=None),
        )
    else:
        native_socket.send_command.side_effect = HomeAssistantCommandError(
            "Unknown command", "unknown_command"
        )
    original_send = client.send_websocket_message.side_effect

    async def send(message):
        command = message["type"]
        if command in {"lovelace/dashboards/create", "lovelace/dashboards/update"}:
            messages.append(deepcopy(message))
            dashboard.update(message)
            return {"success": True, "result": deepcopy(dashboard)}
        if command == "lovelace/config/save":
            messages.append(deepcopy(message))
            if failure == "disconnected":
                document.clear()
                document.update(deepcopy(message["config"]))
                raise ConnectionError("connection lost after save")
            return {"success": False, "error": {"message": "Save rejected"}}
        return await original_send(message)

    client.send_websocket_message.side_effect = send
    before = deepcopy(document)
    config = "[]" if failure == "invalid" else {"views": []}
    # A newly created dashboard has no prior config to hash-check; exercise
    # that topology's save rejection instead of inventing a conflict there.
    expected_error = {
        "invalid": "dict/object",
        "disconnected": "connection lost",
        "rejected": "Save rejected",
        "conflict": "conflict" if prior_change == "metadata" else "Save rejected",
    }[failure]
    with pytest.raises(ToolError, match=expected_error) as caught:
        await DashboardConfigTools(client).ha_config_set_dashboard(
            "test-dashboard",
            title="After",
            config=config,
            config_hash="stale" if failure == "conflict" else None,
            MandatoryBPS=False,
        )
    error = json.loads(str(caught.value))
    assert dashboard["title"] == "After"
    assert error["dashboard_created"] is (prior_change == "create")
    assert error["metadata_updated"] is (prior_change == "metadata")
    assert error["write_committed"] is True
    assert error["config_write_committed"] is (
        None if failure == "disconnected" else False
    )
    if failure == "disconnected":
        assert document == {"views": []}
    else:
        assert document == before
    saves = [m for m in messages if m["type"] == "lovelace/config/save"]
    assert len(saves) == (
        0
        if failure == "invalid"
        or (failure == "conflict" and prior_change == "metadata")
        else 1
    )
    assert native_socket.send_command.await_count == (
        1 if backend == "unknown_command" and failure != "invalid" else 0
    )


@pytest.mark.parametrize("prior_change", ["metadata", "create", "none"])
@pytest.mark.parametrize("stage", ["probe", "socket", "legacy_save"])
async def test_config_cancellation_preserves_prior_registry_write(
    legacy_dashboard, native_socket, monkeypatch, prior_change, stage
):
    client, document, messages = legacy_dashboard
    tools = DashboardConfigTools(client)
    monkeypatch.setattr(
        tools,
        "_ensure_dashboard_exists",
        AsyncMock(
            return_value=(
                prior_change != "create",
                "id",
                prior_change == "metadata",
                None,
            )
        ),
    )
    if stage == "legacy_save":
        monkeypatch.setattr(
            component_dashboard_edit, "get_component_caps", AsyncMock(return_value=None)
        )
        original_send = client.send_websocket_message.side_effect

        async def send(message):
            if message["type"] == "lovelace/config/save":
                messages.append(deepcopy(message))
                document.clear()
                document.update(deepcopy(message["config"]))
                raise asyncio.CancelledError
            return await original_send(message)

        client.send_websocket_message.side_effect = send
    else:
        monkeypatch.setattr(
            component_dashboard_edit,
            "get_component_caps" if stage == "probe" else "get_websocket_client",
            AsyncMock(side_effect=asyncio.CancelledError),
        )
    if prior_change == "none" and stage != "legacy_save":
        with pytest.raises(asyncio.CancelledError):
            await tools.ha_config_set_dashboard(
                "test-dashboard", config={"views": []}, MandatoryBPS=False
            )
    else:
        with pytest.raises(ToolError) as caught:
            await tools.ha_config_set_dashboard(
                "test-dashboard", config={"views": []}, MandatoryBPS=False
            )
        error = json.loads(str(caught.value))
        if prior_change != "none":
            assert error["write_committed"] is True
            assert error["dashboard_created"] is (prior_change == "create")
            assert error["metadata_updated"] is (prior_change == "metadata")
            assert error["config_write_committed"] is (
                None if stage == "legacy_save" else False
            )
        else:
            assert error["write_committed"] is None
    assert len([m for m in messages if m["type"] == "lovelace/config/save"]) == (
        1 if stage == "legacy_save" else 0
    )
    native_socket.send_command.assert_not_awaited()


@pytest.mark.parametrize("mode", ["patch", "python_transform"])
@pytest.mark.parametrize("backend", ["absent", "unknown_command"])
@pytest.mark.parametrize(
    "failure", ["timeout", "disconnected", "cancelled", "not_sent", "rejected"]
)
async def test_legacy_edit_save_failure_reports_outcome_without_retry(
    legacy_dashboard, native_socket, monkeypatch, mode, backend, failure
):
    client, document, messages = legacy_dashboard
    if backend == "absent":
        monkeypatch.setattr(
            component_dashboard_edit, "get_component_caps", AsyncMock(return_value=None)
        )
    else:
        native_socket.send_command.side_effect = HomeAssistantCommandError(
            "Unknown command", "unknown_command"
        )
    before = deepcopy(document)
    original_send = client.send_websocket_message.side_effect

    async def send(message):
        if message["type"] != "lovelace/config/save":
            return await original_send(message)
        messages.append(deepcopy(message))
        if failure == "not_sent":
            raise HomeAssistantCommandNotSent("not authenticated")
        if failure == "rejected":
            return {"success": False, "error": {"message": "Save rejected"}}
        document.clear()
        document.update(deepcopy(message["config"]))
        if failure == "cancelled":
            raise asyncio.CancelledError
        if failure == "timeout":
            raise HomeAssistantCommandTimeout("timeout after save")
        raise ConnectionError("connection lost after save")

    client.send_websocket_message.side_effect = send
    edit = (
        [{"op": "remove", "path": "/views/0"}]
        if mode == "patch"
        else "config['views'] = []"
    )
    with pytest.raises(ToolError) as caught:
        await DashboardConfigTools(client).ha_config_set_dashboard(
            "test-dashboard",
            config_hash=compute_config_hash(document),
            MandatoryBPS=False,
            **{mode: edit},
        )
    error = json.loads(str(caught.value))
    assert error["action"] == mode
    if failure == "rejected":
        assert ("transformed config" in str(caught.value)) is (
            mode == "python_transform"
        )
    known_unwritten = failure in {"not_sent", "rejected"}
    assert error["write_committed"] is (False if known_unwritten else None)
    if not known_unwritten:
        assert error["reason"] == "write_outcome_unknown"
        assert document == {"views": []}
    else:
        assert document == before
    assert sum(m["type"] == "lovelace/config/save" for m in messages) == 1
    assert native_socket.send_command.await_count == (backend == "unknown_command")


@pytest.mark.parametrize("mode", ["patch", "python_transform", "config"])
@pytest.mark.parametrize("failure", ["rejected", "malformed", "timeout", "not_sent", "probe"])
async def test_native_edit_failure_preserves_caller_action(
    legacy_dashboard, native_socket, monkeypatch, mode, failure
):
    client, document, messages = legacy_dashboard
    tools = DashboardConfigTools(client)
    monkeypatch.setattr(
        tools,
        "_ensure_dashboard_exists",
        AsyncMock(return_value=(True, "id", False, None)),
    )
    native_socket.send_command.return_value = {
        "success": True,
        "result": {
            "success": False,
            "error": {"code": "validation_failed", "message": "Edit rejected"},
            "write_committed": False,
        },
    }
    if failure == "malformed":
        native_socket.send_command.return_value = {"success": True, "result": {}}
    elif failure == "timeout":
        native_socket.send_command.side_effect = HomeAssistantCommandTimeout("timeout")
    elif failure == "not_sent":
        native_socket.send_command.side_effect = HomeAssistantCommandNotSent("offline")
    elif failure == "probe":
        monkeypatch.setattr(
            component_dashboard_edit,
            "get_component_caps",
            AsyncMock(side_effect=ConnectionError("offline")),
        )
    edits = {
        "patch": [{"op": "remove", "path": "/views/0"}],
        "python_transform": "config['views'] = []",
        "config": {"views": []},
    }
    with pytest.raises(ToolError) as caught:
        await tools.ha_config_set_dashboard(
            "test-dashboard",
            config_hash=compute_config_hash(document),
            MandatoryBPS=False,
            **{mode: edits[mode]},
        )
    error = json.loads(str(caught.value))
    assert error["action"] == ("set" if mode == "config" else mode)
    assert error["write_committed"] is (
        None if failure in {"timeout", "malformed"} else False
    )
    assert not any(m["type"] == "lovelace/config/save" for m in messages)
    assert native_socket.send_command.await_count == (failure != "probe")


@pytest.mark.parametrize("failure", ["metadata", "url_path", "view_path", "read"])
async def test_patch_preparation_failure_preserves_action(legacy_dashboard, failure):
    client, document, messages = legacy_dashboard
    kwargs = {"url_path": "test-dashboard"}
    if failure == "metadata":
        kwargs["title"] = "Invalid with patch"
    elif failure == "url_path":
        kwargs["url_path"] = "bad/path"
    elif failure == "view_path":
        kwargs.update(return_screenshot=True, view_path=" ")
    else:
        client.send_websocket_message.side_effect = ConnectionError("offline")
    with pytest.raises(ToolError) as caught:
        await DashboardConfigTools(client).ha_config_set_dashboard(
            config_hash=compute_config_hash(document),
            patch=[{"op": "remove", "path": "/views/0"}],
            MandatoryBPS=False,
            **kwargs,
        )
    assert json.loads(str(caught.value))["action"] == "patch"
    assert not any(m["type"] == "lovelace/config/save" for m in messages)
