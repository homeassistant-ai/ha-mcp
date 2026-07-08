"""Unit tests for tools_dev (developer-mode tools, issue #1775)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.config import _reset_global_settings, get_global_settings
from ha_mcp.tools import tools_dev
from ha_mcp.tools.tools_dev import (
    FEATURE_FLAG,
    DevTools,
    is_dev_mode_enabled,
    register_dev_tools,
)
from ha_mcp.utils.data_paths import get_data_dir


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Isolate the data dir and the Settings singleton per test.

    ``is_dev_mode_enabled()`` and the settings tool read through
    ``get_global_settings()`` (cached) and persist to
    ``feature_flags.json`` under ``get_data_dir()`` (memoized) — both
    must be reset so tests can't see each other's state or the real
    user data dir.
    """
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv(FEATURE_FLAG, raising=False)
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    get_data_dir.cache_clear()
    _reset_global_settings()
    yield
    get_data_dir.cache_clear()
    _reset_global_settings()


def _override_file_path():
    from ha_mcp.config import _FEATURE_FLAG_OVERRIDE_FILENAME

    return get_data_dir() / _FEATURE_FLAG_OVERRIDE_FILENAME


async def _drain_background_tasks():
    """Await any fire-and-forget tasks the tool spawned."""
    for task in list(tools_dev._BACKGROUND_TASKS):
        await task


class TestRegistrationGating:
    """Dev tools must not exist at all unless the flag is on."""

    def test_flag_disabled_by_default(self):
        assert is_dev_mode_enabled() is False

    def test_flag_enabled_via_env(self, monkeypatch):
        monkeypatch.setenv(FEATURE_FLAG, "true")
        _reset_global_settings()
        assert is_dev_mode_enabled() is True

    def test_register_noop_when_disabled(self):
        mcp = MagicMock()
        register_dev_tools(mcp, MagicMock())
        mcp.add_tool.assert_not_called()

    def test_register_adds_tools_when_enabled(self, monkeypatch):
        monkeypatch.setenv(FEATURE_FLAG, "true")
        _reset_global_settings()
        mcp = MagicMock()
        register_dev_tools(mcp, MagicMock())
        registered = {call.args[0].__name__ for call in mcp.add_tool.call_args_list}
        assert registered == {"ha_dev_manage_server", "ha_dev_manage_settings"}

    def test_flag_persisted_via_override_file(self):
        """The web-UI toggle path: value in feature_flags.json, no env var."""
        _override_file_path().write_text(json.dumps({"enable_dev_mode": True}))
        _reset_global_settings()
        assert is_dev_mode_enabled() is True


class TestManageSettings:
    @pytest.fixture
    def dev_tools(self):
        return DevTools(MagicMock())

    async def test_list_includes_dev_flag_row(self, dev_tools):
        result = await dev_tools.ha_dev_manage_settings(action="list")
        rows = {r["setting"]: r for r in result["data"]["settings"]}
        row = rows["enable_dev_mode"]
        assert row["registry"] == "advanced"
        assert row["section"] == "developer"
        assert row["value"] is False
        assert rows["log_level"]["editable"] is True

    async def test_list_masks_token(self, dev_tools, monkeypatch):
        monkeypatch.setenv("HOMEASSISTANT_TOKEN", "super-secret-token")
        _reset_global_settings()
        result = await dev_tools.ha_dev_manage_settings(action="list")
        rows = {r["setting"]: r for r in result["data"]["settings"]}
        assert rows["homeassistant_token"]["value"] == "*****"
        assert "super-secret-token" not in json.dumps(result)

    async def test_set_writes_override_file(self, dev_tools):
        result = await dev_tools.ha_dev_manage_settings(
            action="set", setting="log_level", value="DEBUG"
        )
        assert result["data"]["restart_required"] is True
        assert result["data"]["mode"] == "file"
        persisted = json.loads(_override_file_path().read_text())
        assert persisted == {"log_level": "DEBUG"}
        assert get_global_settings().log_level == "DEBUG"

    async def test_set_merges_with_existing_overrides(self, dev_tools):
        _override_file_path().write_text(json.dumps({"debug": True}))
        await dev_tools.ha_dev_manage_settings(
            action="set", setting="log_level", value="ERROR"
        )
        persisted = json.loads(_override_file_path().read_text())
        assert persisted == {"debug": True, "log_level": "ERROR"}

    async def test_set_rejects_env_locked(self, dev_tools, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        _reset_global_settings()
        with pytest.raises(ToolError, match="locked by env"):
            await dev_tools.ha_dev_manage_settings(
                action="set", setting="log_level", value="DEBUG"
            )

    async def test_set_rejects_unknown_setting(self, dev_tools):
        with pytest.raises(ToolError, match="Unknown setting"):
            await dev_tools.ha_dev_manage_settings(
                action="set", setting="no_such_setting", value=1
            )

    async def test_set_rejects_display_only_field(self, dev_tools):
        with pytest.raises(ToolError):
            await dev_tools.ha_dev_manage_settings(
                action="set", setting="homeassistant_url", value="http://x"
            )

    async def test_set_validates_bounds(self, dev_tools):
        with pytest.raises(ToolError, match="between"):
            await dev_tools.ha_dev_manage_settings(
                action="set", setting="fuzzy_threshold", value=500
            )

    async def test_set_validates_choices(self, dev_tools):
        with pytest.raises(ToolError, match="one of"):
            await dev_tools.ha_dev_manage_settings(
                action="set", setting="log_level", value="VERBOSE"
            )

    async def test_set_validates_type(self, dev_tools):
        with pytest.raises(ToolError, match="must be of type"):
            await dev_tools.ha_dev_manage_settings(
                action="set", setting="debug", value=3.14
            )

    async def test_set_coerces_bool_strings(self, dev_tools):
        await dev_tools.ha_dev_manage_settings(
            action="set", setting="debug", value="true"
        )
        persisted = json.loads(_override_file_path().read_text())
        assert persisted == {"debug": True}

    async def test_set_enforces_beta_master_gate(self, dev_tools):
        with pytest.raises(ToolError, match="beta"):
            await dev_tools.ha_dev_manage_settings(
                action="set", setting="enable_filesystem_tools", value=True
            )
        # Enabling the master first unblocks the sub-flag.
        await dev_tools.ha_dev_manage_settings(
            action="set", setting="enable_beta_features", value=True
        )
        result = await dev_tools.ha_dev_manage_settings(
            action="set", setting="enable_filesystem_tools", value=True
        )
        assert result["data"]["value"] is True

    async def test_set_requires_setting_and_value(self, dev_tools):
        with pytest.raises(ToolError, match="'setting' is required"):
            await dev_tools.ha_dev_manage_settings(action="set")
        with pytest.raises(ToolError, match="'value' is required"):
            await dev_tools.ha_dev_manage_settings(action="set", setting="debug")

    async def test_reset_removes_override(self, dev_tools):
        await dev_tools.ha_dev_manage_settings(
            action="set", setting="log_level", value="DEBUG"
        )
        result = await dev_tools.ha_dev_manage_settings(
            action="reset", setting="log_level"
        )
        assert result["data"]["removed_override"] is True
        assert "log_level" not in json.loads(_override_file_path().read_text())
        assert get_global_settings().log_level == "INFO"

    async def test_reset_without_override_is_noop(self, dev_tools):
        result = await dev_tools.ha_dev_manage_settings(
            action="reset", setting="log_level"
        )
        assert result["data"]["removed_override"] is False

    async def test_reset_rejects_env_pinned(self, dev_tools, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        _reset_global_settings()
        with pytest.raises(ToolError, match="env var"):
            await dev_tools.ha_dev_manage_settings(action="reset", setting="log_level")


def _mock_client(entries=None, flows=None):
    """Client mock with config-entry and options-flow surfaces."""
    client = MagicMock()
    client.get_config = AsyncMock(return_value={"version": "2025.1.0"})
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": entries or []}
    )
    client.start_options_flow = AsyncMock(side_effect=flows or [])
    client.abort_options_flow = AsyncMock(return_value={})
    client.submit_options_flow_step = AsyncMock(return_value={"type": "create_entry"})
    client._request = AsyncMock(return_value={})
    return client


_SERVER_FLOW = {
    "type": "form",
    "flow_id": "flow-1",
    "data_schema": [
        {"name": "channel", "default": "stable"},
        {"name": "pip_spec", "default": ""},
    ],
}


class TestManageServer:
    async def test_info_standalone_without_component(self):
        client = _mock_client()
        result = await DevTools(client).ha_dev_manage_server(action="info")
        data = result["data"]
        assert data["deployment_mode"] == "standalone"
        assert data["ha_version"] == "2025.1.0"
        assert data["component_server_entry"] is None
        assert isinstance(data["server_version"], str)

    async def test_info_detects_server_entry(self):
        client = _mock_client(
            entries=[{"entry_id": "tools-e"}, {"entry_id": "server-e"}],
            flows=[{"type": "abort", "reason": "no_options"}, dict(_SERVER_FLOW)],
        )
        result = await DevTools(client).ha_dev_manage_server(action="info")
        entry = result["data"]["component_server_entry"]
        assert entry == {
            "entry_id": "server-e",
            "channel": "stable",
            "pip_spec": "",
        }
        client.abort_options_flow.assert_awaited_with("flow-1")

    async def test_update_source_requires_params(self):
        with pytest.raises(ToolError, match="channel"):
            await DevTools(_mock_client()).ha_dev_manage_server(action="update_source")

    async def test_update_source_rejects_bad_channel(self):
        with pytest.raises(ToolError, match="channel must be one of"):
            await DevTools(_mock_client()).ha_dev_manage_server(
                action="update_source", channel="nightly"
            )

    async def test_update_source_rejects_multiline_pip_spec(self):
        with pytest.raises(ToolError, match="single-line"):
            await DevTools(_mock_client()).ha_dev_manage_server(
                action="update_source", pip_spec="a\nb"
            )

    async def test_update_source_errors_without_server_entry(self):
        client = _mock_client(entries=[])
        with pytest.raises(ToolError, match="server entry"):
            await DevTools(client).ha_dev_manage_server(
                action="update_source", channel="dev"
            )

    async def test_update_source_submits_options_flow(self):
        client = _mock_client(
            entries=[{"entry_id": "server-e"}], flows=[dict(_SERVER_FLOW)]
        )
        result = await DevTools(client).ha_dev_manage_server(
            action="update_source", channel="dev"
        )
        client.submit_options_flow_step.assert_awaited_once_with(
            "flow-1", {"channel": "dev"}
        )
        assert result["data"]["applied"] == {"channel": "dev"}
        assert result["data"]["previous"]["channel"] == "stable"

    async def test_update_source_surfaces_flow_rejection(self):
        client = _mock_client(
            entries=[{"entry_id": "server-e"}], flows=[dict(_SERVER_FLOW)]
        )
        client.submit_options_flow_step = AsyncMock(
            return_value={"type": "form", "errors": {"base": "invalid"}}
        )
        with pytest.raises(ToolError, match="did not apply"):
            await DevTools(client).ha_dev_manage_server(
                action="update_source", pip_spec="ha-mcp==0.0.0"
            )

    async def test_update_source_embedded_defers_submit(self, monkeypatch):
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        monkeypatch.setattr(tools_dev, "_SELF_ACTION_FLUSH_DELAY_S", 0)
        client = _mock_client(
            entries=[{"entry_id": "server-e"}], flows=[dict(_SERVER_FLOW)]
        )
        result = await DevTools(client).ha_dev_manage_server(
            action="update_source",
            pip_spec="https://github.com/homeassistant-ai/ha-mcp/archive/refs/pull/1/head.tar.gz",
        )
        assert result["data"]["scheduled"] is True
        client.submit_options_flow_step.assert_not_awaited()
        await _drain_background_tasks()
        client.submit_options_flow_step.assert_awaited_once()

    async def test_restart_standalone_errors(self):
        with pytest.raises(ToolError, match="standalone"):
            await DevTools(_mock_client()).ha_dev_manage_server(action="restart")

    async def test_restart_embedded_defers_entry_reload(self, monkeypatch):
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        monkeypatch.setattr(tools_dev, "_SELF_ACTION_FLUSH_DELAY_S", 0)
        client = _mock_client(
            entries=[{"entry_id": "server-e"}], flows=[dict(_SERVER_FLOW)]
        )
        result = await DevTools(client).ha_dev_manage_server(action="restart")
        assert result["data"] == {
            "scheduled": True,
            "mode": "embedded",
            "note": result["data"]["note"],
        }
        await _drain_background_tasks()
        client._request.assert_awaited_once_with(
            "POST", "/config/config_entries/entry/server-e/reload"
        )

    async def test_restart_addon_schedules_supervisor_restart(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
        _reset_global_settings()
        with patch("ha_mcp.settings_ui._schedule_supervisor_self_restart") as sched:
            result = await DevTools(_mock_client()).ha_dev_manage_server(
                action="restart"
            )
        sched.assert_called_once()
        assert result["data"]["mode"] == "addon"
        assert result["data"]["scheduled"] is True
