"""Native edit routing preserves results and never repeats an ambiguous write."""

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
