"""Unit tests for tools_dev (developer-mode tools, issue #1775)."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.config import get_global_settings, reset_global_settings
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
    reset_global_settings()
    yield
    get_data_dir.cache_clear()
    reset_global_settings()


def _override_file_path():
    from ha_mcp.config import _FEATURE_FLAG_OVERRIDE_FILENAME

    return get_data_dir() / _FEATURE_FLAG_OVERRIDE_FILENAME


async def _drain_background_tasks():
    """Await any fire-and-forget tasks the tool spawned."""
    tasks = list(tools_dev._BACKGROUND_TASKS)
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks)
    assert not pending
    for task in done:
        task.result()  # re-raise anything the background task threw


class TestRegistrationGating:
    """Dev tools must not exist at all unless the flag is on."""

    def test_flag_disabled_by_default(self):
        assert is_dev_mode_enabled() is False

    def test_flag_enabled_via_env(self, monkeypatch):
        monkeypatch.setenv(FEATURE_FLAG, "true")
        reset_global_settings()
        assert is_dev_mode_enabled() is True

    def test_register_noop_when_disabled(self):
        mcp = MagicMock()
        register_dev_tools(mcp, MagicMock())
        mcp.add_tool.assert_not_called()

    def test_register_adds_tools_when_enabled(self, monkeypatch):
        monkeypatch.setenv(FEATURE_FLAG, "true")
        reset_global_settings()
        mcp = MagicMock()
        register_dev_tools(mcp, MagicMock())
        registered = {call.args[0].__name__ for call in mcp.add_tool.call_args_list}
        assert registered == {"ha_dev_manage_server", "ha_dev_manage_settings"}

    def test_flag_persisted_via_override_file(self):
        """The web-UI toggle path: value in feature_flags.json, no env var."""
        _override_file_path().write_text(json.dumps({"enable_dev_mode": True}))
        reset_global_settings()
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
        reset_global_settings()
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
        reset_global_settings()
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
        reset_global_settings()
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

# Server flow with URL / secret overrides set. The optional text fields carry
# their persisted value in ``description.suggested_value`` (not as a schema
# ``default``), exactly as the component renders them so they stay clearable.
_SERVER_FLOW_WITH_OVERRIDES = {
    "type": "form",
    "flow_id": "flow-1",
    "data_schema": [
        {"name": "channel", "default": "dev"},
        {"name": "pip_spec", "description": {"suggested_value": "ha-mcp==9.9.9"}},
        {
            "name": "server_url",
            "description": {"suggested_value": "http://ha.local:8123"},
        },
        {
            "name": "external_url",
            "description": {"suggested_value": "https://ext.example.com"},
        },
        {
            "name": "webhook_id_override",
            "description": {"suggested_value": "hook123"},
        },
        {
            "name": "secret_path_override",
            "description": {"suggested_value": "/secret"},
        },
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
            flows=[
                {"type": "form", "step_id": "tools_info", "data_schema": []},
                dict(_SERVER_FLOW),
            ],
        )
        result = await DevTools(client).ha_dev_manage_server(action="info")
        entry = result["data"]["component_server_entry"]
        assert entry["entry_id"] == "server-e"
        assert entry["channel"] == "stable"
        assert entry["pip_spec"] == ""
        client.abort_options_flow.assert_awaited_with("flow-1")

    async def test_info_quietly_aborts_the_tools_entry_info_form(self):
        # The tools entry's options flow is an OPEN informational form since
        # #1853 (it used to abort server-side); the probe must close it after
        # rejecting it, or every info/update/restart probe leaks a flow.
        client = _mock_client(
            entries=[{"entry_id": "tools-e"}, {"entry_id": "server-e"}],
            flows=[
                {
                    "type": "form",
                    "flow_id": "tools-flow",
                    "step_id": "tools_info",
                    "data_schema": [],
                },
                dict(_SERVER_FLOW),
            ],
        )
        result = await DevTools(client).ha_dev_manage_server(action="info")
        assert result["data"]["component_server_entry"]["entry_id"] == "server-e"
        client.abort_options_flow.assert_any_await("tools-flow")

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

    async def test_update_source_preserves_url_and_secret_overrides(self):
        # update_source drives the component's options flow, whose optional text
        # fields pre-fill via suggested_value so the UI can clear them. A sparse
        # submit would blank the user's server-URL / connect-secret overrides
        # (an omitted optional reads as "cleared"), so update_source must resend
        # them — harvested from description.suggested_value, not a schema default.
        client = _mock_client(
            entries=[{"entry_id": "server-e"}],
            flows=[dict(_SERVER_FLOW_WITH_OVERRIDES)],
        )
        await DevTools(client).ha_dev_manage_server(
            action="update_source", channel="stable"
        )
        client.submit_options_flow_step.assert_awaited_once_with(
            "flow-1",
            {
                "pip_spec": "ha-mcp==9.9.9",
                "server_url": "http://ha.local:8123",
                "external_url": "https://ext.example.com",
                "webhook_id_override": "hook123",
                "secret_path_override": "/secret",
                "channel": "stable",
            },
        )

    async def test_update_source_new_pip_spec_wins_over_preserved(self):
        # A caller-supplied pip_spec must override the preserved (suggested_value)
        # pin, not be clobbered by it: the preserve dict is seeded first, then the
        # explicit pip_spec overwrites it.
        client = _mock_client(
            entries=[{"entry_id": "server-e"}],
            flows=[dict(_SERVER_FLOW_WITH_OVERRIDES)],
        )
        await DevTools(client).ha_dev_manage_server(
            action="update_source", pip_spec="ha-mcp==2.0.0"
        )
        client.submit_options_flow_step.assert_awaited_once_with(
            "flow-1",
            {
                "pip_spec": "ha-mcp==2.0.0",
                "server_url": "http://ha.local:8123",
                "external_url": "https://ext.example.com",
                "webhook_id_override": "hook123",
                "secret_path_override": "/secret",
            },
        )

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

    async def test_update_source_result_names_component_target(self):
        """The non-embedded success payload must say WHICH server changed.

        Regression: an agent connected to the ADD-ON called update_source,
        read success:true plus a generic "the component is reloading" note,
        and concluded the server it was talking to would change version. It
        never does — only the ha_mcp_tools component's separate in-process
        entry is updated, and success means the entry options were applied,
        not that the background install finished.
        """
        client = _mock_client(
            entries=[{"entry_id": "server-e"}], flows=[dict(_SERVER_FLOW)]
        )
        result = await DevTools(client).ha_dev_manage_server(
            action="update_source", channel="dev"
        )
        data = result["data"]
        assert data["target"] == "ha_mcp_tools in-process server entry"
        assert "NOT" in data["note"]
        assert "this connection" in data["note"]
        assert "background" in data["note"]
        assert "fail" in data["note"]

    async def test_update_source_embedded_target_says_this_server(self, monkeypatch):
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        monkeypatch.setattr(tools_dev, "_SELF_ACTION_FLUSH_DELAY_S", 0)
        client = _mock_client(
            entries=[{"entry_id": "server-e"}], flows=[dict(_SERVER_FLOW)]
        )
        result = await DevTools(client).ha_dev_manage_server(
            action="update_source", channel="dev"
        )
        assert "this server" in result["data"]["target"]
        await _drain_background_tasks()

    async def test_info_distinguishes_serving_server_from_component_entry(self):
        """info must label the component entry as a separate server, so its
        channel/pip_spec can't be conflated with the serving server's
        version (deployment_mode 'standalone'/'addon')."""
        client = _mock_client(
            entries=[{"entry_id": "server-e"}], flows=[dict(_SERVER_FLOW)]
        )
        result = await DevTools(client).ha_dev_manage_server(action="info")
        entry = result["data"]["component_server_entry"]
        assert "separate" in entry["role"]
        assert "update_source" in entry["role"]

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
        reset_global_settings()
        with patch(
            "ha_mcp.settings_ui._supervisor._schedule_supervisor_self_restart"
        ) as sched:
            result = await DevTools(_mock_client()).ha_dev_manage_server(
                action="restart"
            )
        sched.assert_called_once()
        assert result["data"]["mode"] == "addon"
        assert result["data"]["scheduled"] is True


class TestMergeFileOverrideGuards:
    """The settings-write path must refuse to clobber unreadable state."""

    async def test_set_refuses_corrupt_override_file(self):
        _override_file_path().write_text("not json {{{")
        with pytest.raises(ToolError, match="not valid"):
            await DevTools(MagicMock()).ha_dev_manage_settings(
                action="set", setting="log_level", value="DEBUG"
            )
        # The corrupt file is preserved for inspection, not overwritten.
        assert _override_file_path().read_text() == "not json {{{"

    async def test_set_refuses_unreadable_override_file(self):
        _override_file_path().write_text("{}")
        from pathlib import Path

        with (
            patch.object(Path, "read_text", side_effect=OSError("boom")),
            pytest.raises(ToolError, match="refusing to overwrite"),
        ):
            await DevTools(MagicMock()).ha_dev_manage_settings(
                action="set", setting="log_level", value="DEBUG"
            )


class TestManageSettingsAddonOrigin:
    """Add-on-origin fields route through Supervisor, not the file."""

    @pytest.fixture(autouse=True)
    def _addon_mode(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
        reset_global_settings()

    async def test_set_routes_via_supervisor(self, monkeypatch):
        from ha_mcp import settings_ui

        calls: list[dict] = []

        async def _fake_merge(verify_ssl, changes):
            calls.append(changes)
            return True, None

        monkeypatch.setattr(
            settings_ui._supervisor, "_supervisor_merge_and_post_options", _fake_merge
        )
        result = await DevTools(MagicMock()).ha_dev_manage_settings(
            action="set", setting="enable_tool_search", value=True
        )
        assert result["data"]["mode"] == "addon"
        assert calls == [{"enable_tool_search": True}]
        # Nothing lands in the override file on the addon path.
        assert not _override_file_path().exists()

    async def test_set_surfaces_supervisor_rejection(self, monkeypatch):
        from types import SimpleNamespace

        from ha_mcp import settings_ui

        async def _fake_merge(verify_ssl, changes):
            return False, SimpleNamespace(message="schema says no")

        monkeypatch.setattr(
            settings_ui._supervisor, "_supervisor_merge_and_post_options", _fake_merge
        )
        with pytest.raises(ToolError, match="Supervisor rejected"):
            await DevTools(MagicMock()).ha_dev_manage_settings(
                action="set", setting="enable_tool_search", value=True
            )

    async def test_reset_rejects_addon_managed(self):
        with pytest.raises(ToolError, match="add-on"):
            await DevTools(MagicMock()).ha_dev_manage_settings(
                action="reset", setting="enable_tool_search"
            )


class TestManageSettingsValueEdgeCases:
    async def test_set_rejects_null_byte_string(self):
        with pytest.raises(ToolError, match="null byte"):
            await DevTools(MagicMock()).ha_dev_manage_settings(
                action="set", setting="mcp_server_name", value="a\x00b"
            )


class TestServerEntryDiscoveryErrors:
    """Probe failures must surface as what they are (issue #1780 review)."""

    async def test_ws_failure_raises_tool_error(self):
        client = _mock_client()
        client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": "ws down"}
        )
        with pytest.raises(ToolError, match="config_entries/get failed"):
            await DevTools(client).ha_dev_manage_server(
                action="update_source", channel="dev"
            )

    async def test_connection_error_propagates_not_masked(self):
        # A dead connection must NOT read as "no server entry exists" —
        # that told users to reinstall a component that was running.
        from ha_mcp.client.rest_client import HomeAssistantConnectionError

        client = _mock_client(entries=[{"entry_id": "server-e"}])
        client.start_options_flow = AsyncMock(
            side_effect=HomeAssistantConnectionError("conn refused")
        )
        with pytest.raises(ToolError) as excinfo:
            await DevTools(client).ha_dev_manage_server(
                action="update_source", channel="dev"
            )
        assert "server entry" not in str(excinfo.value)

    async def test_api_error_skips_entry_and_keeps_probing(self):
        from ha_mcp.client.rest_client import HomeAssistantAPIError

        client = _mock_client(
            entries=[{"entry_id": "tools-e"}, {"entry_id": "server-e"}],
            flows=[HomeAssistantAPIError("no options"), dict(_SERVER_FLOW)],
        )
        result = await DevTools(client).ha_dev_manage_server(action="info")
        entry = result["data"]["component_server_entry"]
        assert entry and entry["entry_id"] == "server-e"

    async def test_restart_embedded_errors_without_entry(self, monkeypatch):
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        client = _mock_client(entries=[])
        with pytest.raises(ToolError, match="server entry"):
            await DevTools(client).ha_dev_manage_server(action="restart")


class TestServerInfoDegradation:
    """info is best-effort: probe failures become warnings, not failures."""

    async def test_warns_when_ha_version_unavailable(self):
        client = _mock_client()
        client.get_config = AsyncMock(side_effect=Exception("api down"))
        result = await DevTools(client).ha_dev_manage_server(action="info")
        assert result["success"] is True
        assert any("HA version" in w for w in result["warnings"])

    async def test_warns_when_entry_probe_fails(self):
        client = _mock_client()
        client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": "ws down"}
        )
        result = await DevTools(client).ha_dev_manage_server(action="info")
        assert result["success"] is True
        assert any("component server entry" in w for w in result["warnings"])
        assert result["data"]["server_version"]


class TestConcurrentSettingsWrites:
    async def test_concurrent_sets_serialize_under_the_lock(self):
        # Two overlapping set calls must both land — the shared override-file
        # lock serializes the read-merge-write so neither clobbers the other.
        dev_tools = DevTools(MagicMock())
        await asyncio.gather(
            dev_tools.ha_dev_manage_settings(
                action="set", setting="log_level", value="DEBUG"
            ),
            dev_tools.ha_dev_manage_settings(action="set", setting="debug", value=True),
        )
        persisted = json.loads(_override_file_path().read_text())
        assert persisted == {"log_level": "DEBUG", "debug": True}


def _tool_config() -> dict:
    """Read the persisted tool_config.json (or {} if absent)."""
    path = get_data_dir() / "tool_config.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _seed_metadata(rows: list[dict]) -> None:
    """Seed the sidecar tool-metadata cache so server-less list_tools works."""
    from ha_mcp.settings_ui._persistence import dump_tool_metadata_cache

    dump_tool_metadata_cache(rows)


class TestManageToolsState:
    """set_tool drives the Tools-tab enable/disable/pin + LLM-API toggles."""

    @pytest.fixture
    def dev_tools(self):
        return DevTools(MagicMock())

    async def test_set_state_disabled_writes_config(self, dev_tools):
        result = await dev_tools.ha_dev_manage_settings(
            action="set_tool", tool="ha_write_file", state="disabled"
        )
        assert result["data"]["state"] == "disabled"
        assert result["data"]["restart_required"] is True
        assert _tool_config()["tools"]["ha_write_file"] == "disabled"

    async def test_set_state_pinned_writes_config(self, dev_tools):
        await dev_tools.ha_dev_manage_settings(
            action="set_tool", tool="ha_get_history", state="pinned"
        )
        assert _tool_config()["tools"]["ha_get_history"] == "pinned"

    async def test_set_state_rejects_invalid_state(self, dev_tools):
        with pytest.raises(ToolError, match="state must be one of"):
            await dev_tools.ha_dev_manage_settings(
                action="set_tool", tool="ha_get_history", state="bogus"
            )

    async def test_set_state_rejects_env_pinned_flip(self, dev_tools, monkeypatch):
        monkeypatch.setenv("DISABLED_TOOLS", "ha_foo")
        reset_global_settings()
        with pytest.raises(ToolError, match="unset the env var"):
            await dev_tools.ha_dev_manage_settings(
                action="set_tool", tool="ha_foo", state="pinned"
            )

    async def test_set_state_rejects_bps_locked_disable(self, dev_tools):
        # enable_mandatory_bps + enable_strict_mandatory_bps default on, so
        # ha_get_skill_guide is locked enabled.
        with pytest.raises(ToolError, match="strict best-practices"):
            await dev_tools.ha_dev_manage_settings(
                action="set_tool", tool="ha_get_skill_guide", state="disabled"
            )

    async def test_set_state_rejects_mandatory_disable(self, dev_tools):
        # ha_search is an unconditional MANDATORY_TOOLS member; the headless
        # path must refuse (the web UI locks the row, and apply_tool_visibility
        # would silently re-enable it at startup — a misleading "success").
        with pytest.raises(ToolError, match="mandatory"):
            await dev_tools.ha_dev_manage_settings(
                action="set_tool", tool="ha_search", state="disabled"
            )

    async def test_set_tool_rejects_wildcard(self, dev_tools):
        # tool='*' is a policy wildcard; gating it would gate every tool,
        # including the developer recovery actions.
        with pytest.raises(ToolError, match="wildcard"):
            await dev_tools.ha_dev_manage_settings(
                action="set_tool", tool="*", gated=True
            )

    async def test_set_llm_api_writes_override(self, dev_tools):
        result = await dev_tools.ha_dev_manage_settings(
            action="set_tool", tool="ha_search", llm_api=False
        )
        assert result["data"]["llm_api"] is False
        assert _tool_config()["llm_api"]["ha_search"] is False
        # Not embedded in the test env → a no-effect warning is surfaced.
        assert any("embedded" in w for w in result["warnings"])

    async def test_set_state_and_llm_api_together(self, dev_tools):
        await dev_tools.ha_dev_manage_settings(
            action="set_tool", tool="ha_search", state="pinned", llm_api=True
        )
        config = _tool_config()
        assert config["tools"]["ha_search"] == "pinned"
        assert config["llm_api"]["ha_search"] is True

    async def test_set_tool_requires_tool(self, dev_tools):
        with pytest.raises(ToolError, match="'tool' is required"):
            await dev_tools.ha_dev_manage_settings(action="set_tool", state="disabled")

    async def test_set_tool_requires_a_field(self, dev_tools):
        with pytest.raises(ToolError, match="at least one"):
            await dev_tools.ha_dev_manage_settings(action="set_tool", tool="ha_search")


class TestManageToolsGate:
    """The per-tool security gate adds/removes an unconditional policy rule."""

    @pytest.fixture
    def dev_tools(self):
        return DevTools(MagicMock())

    def _rules(self):
        from ha_mcp.policy.persistence import load_policy

        return load_policy(get_data_dir()).rules

    async def test_gate_on_adds_rule(self, dev_tools):
        result = await dev_tools.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", gated=True
        )
        assert result["data"]["gated"] is True
        assert result["data"]["policy_rules_changed"] is True
        rules = self._rules()
        assert [r.tool_name for r in rules] == ["ha_call_service"]
        assert rules[0].when == []
        # Policies default OFF → warning that the gate won't enforce yet.
        assert any("enable_tool_security_policies" in w for w in result["warnings"])

    async def test_gate_off_removes_rule(self, dev_tools):
        await dev_tools.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", gated=True
        )
        result = await dev_tools.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", gated=False
        )
        assert result["data"]["policy_rules_changed"] is True
        assert self._rules() == []

    async def test_gate_on_is_idempotent(self, dev_tools):
        await dev_tools.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", gated=True
        )
        result = await dev_tools.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", gated=True
        )
        assert result["data"]["policy_rules_changed"] is False
        assert len(self._rules()) == 1

    async def test_gate_preserves_conditional_rules(self, dev_tools):
        # A predicate-bearing rule the user authored must survive gate toggles:
        # gated=True adds the bare gate alongside it; gated=False removes ONLY
        # the bare gate, never the conditional rule (Codex #1993 P1).
        from ha_mcp.policy.model import Policy, Predicate, Rule
        from ha_mcp.policy.persistence import save_policy

        save_policy(
            get_data_dir(),
            Policy(
                rules=[
                    Rule(
                        tool_name="ha_call_service",
                        when=[Predicate(path="args.domain", op="eq", value="lock")],
                    )
                ]
            ),
        )
        await dev_tools.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", gated=True
        )
        rules = self._rules()
        assert len(rules) == 2
        assert any(r.tool_name == "ha_call_service" and not r.when for r in rules)
        assert any(r.when for r in rules)  # conditional preserved

        await dev_tools.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", gated=False
        )
        rules = self._rules()
        assert len(rules) == 1
        assert rules[0].when  # the conditional rule remains

    async def test_combined_set_tool_is_atomic_on_validation_failure(self, dev_tools):
        # A combined state+gate request whose state fails validation must NOT
        # have persisted the gate rule — preflight validates before any write.
        with pytest.raises(ToolError, match="strict best-practices"):
            await dev_tools.ha_dev_manage_settings(
                action="set_tool",
                tool="ha_get_skill_guide",
                state="disabled",
                gated=True,
            )
        assert self._rules() == []  # the gate was never written


class TestListToolStates:
    async def test_list_reflects_state_llm_and_gate(self):
        _seed_metadata(
            [
                {"name": "ha_search", "tags": ["Search"], "category": "read"},
                {"name": "ha_call_service", "tags": ["Control"], "category": "write"},
            ]
        )
        dev = DevTools(MagicMock())
        await dev.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", state="disabled", gated=True
        )
        result = await dev.ha_dev_manage_settings(action="list_tools")
        rows = {r["name"]: r for r in result["data"]["tools"]}
        assert rows["ha_call_service"]["state"] == "disabled"
        assert rows["ha_call_service"]["gated"] is True
        assert rows["ha_search"]["gated"] is False
        assert result["data"]["policies_enabled"] is False
        assert result["data"]["count"] == 2

    async def test_list_marks_mandatory_and_bps(self):
        _seed_metadata(
            [
                {"name": "ha_search", "tags": ["Search"]},
                {"name": "ha_get_skill_guide", "tags": ["System"]},
            ]
        )
        result = await DevTools(MagicMock()).ha_dev_manage_settings(action="list_tools")
        rows = {r["name"]: r for r in result["data"]["tools"]}
        assert rows["ha_search"]["mandatory"] is True
        assert rows["ha_get_skill_guide"]["bps_locked"] is True


class TestManagePolicy:
    @pytest.fixture
    def dev_tools(self):
        return DevTools(MagicMock())

    async def test_get_policy_default(self, dev_tools):
        result = await dev_tools.ha_dev_manage_settings(action="get_policy")
        policy = result["data"]["policy"]
        assert policy["wait_seconds"] == 60
        assert policy["rules"] == []
        assert policy["version"] == 0
        assert result["data"]["policies_enabled"] is False

    async def test_set_policy_roundtrip_bumps_version(self, dev_tools):
        result = await dev_tools.ha_dev_manage_settings(
            action="set_policy",
            policy={
                "wait_seconds": 30,
                "approval_ttl_minutes": 5,
                "rules": [{"tool_name": "ha_call_service"}],
                "version": 0,
            },
        )
        assert result["data"]["version"] == 1
        assert result["data"]["rules_changed"] is True
        got = await dev_tools.ha_dev_manage_settings(action="get_policy")
        assert got["data"]["policy"]["version"] == 1
        assert got["data"]["policy"]["rules"][0]["tool_name"] == "ha_call_service"

    async def test_set_policy_version_mismatch_rejected(self, dev_tools):
        await dev_tools.ha_dev_manage_settings(
            action="set_policy", policy={"rules": [], "version": 0}
        )
        # On-disk version is now 1; a stale expected_version=0 must be rejected.
        with pytest.raises(ToolError, match="version mismatch"):
            await dev_tools.ha_dev_manage_settings(
                action="set_policy",
                policy={"rules": []},
                expected_version=0,
            )

    async def test_set_policy_invalid_schema_rejected(self, dev_tools):
        # wait_seconds must be < approval_ttl_minutes * 60.
        with pytest.raises(ToolError, match="schema validation"):
            await dev_tools.ha_dev_manage_settings(
                action="set_policy",
                policy={"wait_seconds": 599, "approval_ttl_minutes": 1},
            )

    async def test_set_policy_requires_object(self, dev_tools):
        with pytest.raises(ToolError, match="'policy'"):
            await dev_tools.ha_dev_manage_settings(action="set_policy")

    async def test_set_policy_without_version_warns(self, dev_tools):
        result = await dev_tools.ha_dev_manage_settings(
            action="set_policy", policy={"rules": []}
        )
        assert any("without an optimistic-concurrency" in w for w in result["warnings"])


class TestBackupConfig:
    @pytest.fixture(autouse=True)
    def _clean_backup_env(self, monkeypatch):
        """Neutralize ambient auto-backup env vars (e.g. from tests/.env.test)
        so file-mode edits aren't spuriously rejected as env-pinned."""
        from ha_mcp.config import BACKUP_OVERRIDE_FIELDS

        for _field, env_name, _ftype in BACKUP_OVERRIDE_FIELDS:
            monkeypatch.delenv(env_name, raising=False)
        reset_global_settings()

    @pytest.fixture
    def dev_tools(self):
        return DevTools(MagicMock())

    async def test_get_backup_config_lists_fields(self, dev_tools):
        result = await dev_tools.ha_dev_manage_settings(action="get_backup_config")
        fields = {f["field"]: f for f in result["data"]["fields"]}
        assert "enable_auto_backup" in fields
        assert fields["enable_auto_backup"]["editable"] is True

    async def test_set_backup_config_file_mode_persists(self, dev_tools):
        result = await dev_tools.ha_dev_manage_settings(
            action="set_backup_config", backup={"enable_auto_backup": False}
        )
        assert result["data"]["applied"] == {"enable_auto_backup": False}
        assert result["data"]["restart_required"] is False
        assert get_global_settings().enable_auto_backup is False

    async def test_set_backup_config_rejects_env_pinned(self, dev_tools, monkeypatch):
        monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")
        reset_global_settings()
        with pytest.raises(ToolError, match="environment variable"):
            await dev_tools.ha_dev_manage_settings(
                action="set_backup_config", backup={"enable_auto_backup": False}
            )

    async def test_set_backup_config_rejects_out_of_bounds(self, dev_tools):
        with pytest.raises(ToolError, match="must be"):
            await dev_tools.ha_dev_manage_settings(
                action="set_backup_config",
                backup={"auto_backup_throttle_minutes": 99999},
            )

    async def test_set_backup_config_requires_object(self, dev_tools):
        with pytest.raises(ToolError, match="'backup'"):
            await dev_tools.ha_dev_manage_settings(action="set_backup_config")


def _queue_with_entry(**args):
    """A real ApprovalQueue holding one pending entry; returns (server, token)."""
    from types import SimpleNamespace

    from ha_mcp.policy.approval_queue import ApprovalQueue, compute_args_hash

    queue = ApprovalQueue()
    entry = queue.create(
        "ha_call_service",
        compute_args_hash(args),
        args,
        ttl_minutes=5,
    )
    return SimpleNamespace(approval_queue=queue), entry.token, queue


class TestManageServerApprovals:
    async def test_list_pending_reports_entries(self):
        server, token, _queue = _queue_with_entry(domain="light")
        result = await DevTools(MagicMock(), server=server).ha_dev_manage_server(
            action="list_pending"
        )
        assert result["data"]["count"] == 1
        assert result["data"]["pending"][0]["token"] == token
        assert result["data"]["pending"][0]["tool_name"] == "ha_call_service"

    async def test_list_pending_without_queue_is_empty(self):
        result = await DevTools(MagicMock(), server=None).ha_dev_manage_server(
            action="list_pending"
        )
        assert result["data"]["pending"] == []
        assert "No active approval queue" in result["data"]["note"]

    async def test_approve_marks_entry_approved(self):
        server, token, queue = _queue_with_entry(domain="light")
        result = await DevTools(MagicMock(), server=server).ha_dev_manage_server(
            action="approve", token=token
        )
        assert result["data"]["decision"] == "approved"
        assert queue.get(token).decision == "approved"

    async def test_deny_marks_entry_denied(self):
        server, token, queue = _queue_with_entry(domain="light")
        await DevTools(MagicMock(), server=server).ha_dev_manage_server(
            action="deny", token=token
        )
        assert queue.get(token).decision == "denied"

    async def test_approve_unknown_token_errors(self):
        server, _token, _queue = _queue_with_entry(domain="light")
        with pytest.raises(ToolError, match="Unknown or expired"):
            await DevTools(MagicMock(), server=server).ha_dev_manage_server(
                action="approve", token="not-a-real-token"
            )

    async def test_approve_already_decided_errors(self):
        server, token, _queue = _queue_with_entry(domain="light")
        dev = DevTools(MagicMock(), server=server)
        await dev.ha_dev_manage_server(action="approve", token=token)
        with pytest.raises(ToolError, match="already decided"):
            await dev.ha_dev_manage_server(action="approve", token=token)

    async def test_approve_requires_token(self):
        server, _token, _queue = _queue_with_entry(domain="light")
        with pytest.raises(ToolError, match="'token' is required"):
            await DevTools(MagicMock(), server=server).ha_dev_manage_server(
                action="approve"
            )

    async def test_approve_without_queue_errors(self):
        with pytest.raises(ToolError, match="No active approval queue"):
            await DevTools(MagicMock(), server=None).ha_dev_manage_server(
                action="approve", token="x"
            )


class TestDevToolsServerPlumbing:
    def test_init_stores_server(self):
        sentinel = object()
        assert DevTools(MagicMock(), server=sentinel)._server is sentinel

    def test_register_passes_server_through(self, monkeypatch):
        monkeypatch.setenv(FEATURE_FLAG, "true")
        reset_global_settings()
        captured = {}

        def _capture(_mcp, dev_tools):
            captured["server"] = dev_tools._server

        monkeypatch.setattr(tools_dev, "register_tool_methods", _capture)
        sentinel = object()
        register_dev_tools(MagicMock(), MagicMock(), server=sentinel)
        assert captured["server"] is sentinel


def _fake_server_with_tools(tools, *, approval_queue=None):
    """A server whose mcp.local_provider._list_tools() returns fake tool objs,
    exercising the live-registry list_tools path (not the cache fallback)."""
    from types import SimpleNamespace

    fake_tools = [
        SimpleNamespace(
            name=name,
            tags=set(tags),
            description="desc",
            title=None,
            annotations=SimpleNamespace(
                readOnlyHint=None, destructiveHint=None, title=None
            ),
        )
        for name, tags in tools
    ]
    provider = SimpleNamespace(_list_tools=AsyncMock(return_value=fake_tools))
    return SimpleNamespace(
        mcp=SimpleNamespace(local_provider=provider),
        approval_queue=approval_queue,
    )


class TestListToolsLiveRegistry:
    """The live-registry path every real deployment uses (server present)."""

    async def test_uses_live_registry_and_marks_feature_gated(self):
        server = _fake_server_with_tools(
            [("ha_search", ["Search"]), ("ha_call_service", ["Control"])]
        )
        result = await DevTools(MagicMock(), server=server).ha_dev_manage_settings(
            action="list_tools"
        )
        rows = {r["name"]: r for r in result["data"]["tools"]}
        assert rows["ha_search"]["available"] is True
        assert rows["ha_search"]["disabled_by"] is None
        # Feature-gated stub injected by _get_tool_metadata (flag off).
        assert rows["ha_write_file"]["available"] is False
        assert rows["ha_write_file"]["disabled_by"] == "enable_filesystem_tools"

    async def test_policies_live_reflects_queue_presence(self):
        from ha_mcp.policy.approval_queue import ApprovalQueue

        with_q = _fake_server_with_tools(
            [("ha_search", ["Search"])], approval_queue=ApprovalQueue()
        )
        res_live = await DevTools(MagicMock(), server=with_q).ha_dev_manage_settings(
            action="list_tools"
        )
        assert res_live["data"]["policies_live"] is True

        without_q = _fake_server_with_tools([("ha_search", ["Search"])])
        res_off = await DevTools(MagicMock(), server=without_q).ha_dev_manage_settings(
            action="list_tools"
        )
        assert res_off["data"]["policies_live"] is False


class TestRememberCacheCleared:
    """clear_remember_cache must actually fire on a rule change (security)."""

    def _server_with_remembered(self):
        from types import SimpleNamespace

        from ha_mcp.policy.approval_queue import ApprovalQueue

        queue = ApprovalQueue()
        queue.remember("ha_call_service", "argshash", minutes=10)
        assert queue.is_remembered("ha_call_service", "argshash")
        return SimpleNamespace(approval_queue=queue), queue

    async def test_set_tool_gate_clears_cache(self):
        server, queue = self._server_with_remembered()
        await DevTools(MagicMock(), server=server).ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", gated=True
        )
        assert not queue.is_remembered("ha_call_service", "argshash")

    async def test_set_policy_clears_cache_on_rule_change(self):
        server, queue = self._server_with_remembered()
        await DevTools(MagicMock(), server=server).ha_dev_manage_settings(
            action="set_policy",
            policy={"rules": [{"tool_name": "ha_call_service"}]},
        )
        assert not queue.is_remembered("ha_call_service", "argshash")


class TestSetToolPartialCommit:
    async def test_partial_commit_surfaced_when_gate_write_fails(self, monkeypatch):
        # tool_config saves first; force the policy save to fail and assert the
        # error tells the caller the state change already persisted.
        def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr("ha_mcp.policy.persistence.save_policy", _boom)
        with pytest.raises(ToolError, match="already saved"):
            await DevTools(MagicMock()).ha_dev_manage_settings(
                action="set_tool", tool="ha_get_history", state="pinned", gated=True
            )
        assert _tool_config()["tools"]["ha_get_history"] == "pinned"


class TestBackupConfigAddon:
    @pytest.fixture(autouse=True)
    def _addon(self, monkeypatch):
        from ha_mcp.config import BACKUP_OVERRIDE_FIELDS

        monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
        for _field, env_name, _ftype in BACKUP_OVERRIDE_FIELDS:
            monkeypatch.delenv(env_name, raising=False)
        reset_global_settings()

    async def test_addon_routes_through_supervisor(self, monkeypatch):
        from types import SimpleNamespace

        from ha_mcp import settings_ui

        calls: list[dict] = []

        async def _fake_merge(verify_ssl, clean):
            calls.append(clean)
            return True, None

        monkeypatch.setattr(
            settings_ui._supervisor, "_supervisor_merge_and_post_options", _fake_merge
        )
        server = SimpleNamespace(settings=get_global_settings())
        result = await DevTools(MagicMock(), server=server).ha_dev_manage_settings(
            action="set_backup_config", backup={"enable_auto_backup": False}
        )
        assert calls == [{"enable_auto_backup": False}]
        assert result["data"]["mode"] == "addon"
        assert result["data"]["restart_required"] is True


class TestCoerceBoolBranch:
    def test_coerce_bool_or_raise_rejects_non_bool(self):
        with pytest.raises(ToolError, match="must be a boolean"):
            DevTools._coerce_bool_or_raise("not-a-bool", "llm_api")


class TestSetToolNameValidation:
    """set_tool rejects unknown tool names before persisting any guard.

    A typo'd gate ("ha_call_servce") previously saved a rule for a
    nonexistent tool and reported success while the intended tool stayed
    ungated (Codex #1993 round 3).
    """

    async def test_unknown_tool_rejected(self):
        _seed_metadata([{"name": "ha_call_service", "tags": ["Control"]}])
        with pytest.raises(ToolError, match="Unknown tool"):
            await DevTools(MagicMock()).ha_dev_manage_settings(
                action="set_tool", tool="ha_call_servce", gated=True
            )

    async def test_feature_gated_stub_accepted(self):
        # Currently-unavailable (flag-off) tools are still configurable.
        _seed_metadata(
            [
                {
                    "name": "ha_write_file",
                    "tags": ["Files"],
                    "disabled_by": "enable_filesystem_tools",
                }
            ]
        )
        result = await DevTools(MagicMock()).ha_dev_manage_settings(
            action="set_tool", tool="ha_write_file", state="disabled"
        )
        assert result["data"]["state"] == "disabled"

    async def test_validation_skipped_without_metadata(self):
        # Defensive server-less path with an empty cache: don't brick set_tool.
        result = await DevTools(MagicMock()).ha_dev_manage_settings(
            action="set_tool", tool="ha_anything", state="pinned"
        )
        assert result["data"]["state"] == "pinned"
