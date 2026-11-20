"""``ha_dev_manage_settings``: the server-settings matrix and backup config.

The dev tools run on the server's client connected to a real ``hass``; what
they change lands in the per-test data dir (``_dev_tool_env``), or, in an
add-on, at the Supervisor upstream (``_tools_dev_core.supervisor``).
"""

import asyncio
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp import Client
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import (
    BACKUP_OVERRIDE_FIELDS,
    get_global_settings,
    reset_global_settings,
)
from ha_mcp.tools.tools_dev import FEATURE_FLAG, DevTools

from ._dev_tool_env import _isolated_env as _isolated_env
from ._real_server import build_server
from ._tools_dev_core import (
    Supervisor,
    override_path,
    serve,
    stored_overrides,
    write_overrides,
)
from ._tools_dev_core import dev_tools as dev_tools
from ._tools_dev_core import supervisor as supervisor

DEV_TOOLS = {"ha_dev_manage_server", "ha_dev_manage_settings"}


@pytest.mark.parametrize(
    ("switch", "registered"), [("off", False), ("env", True), ("web_ui", True)]
)
async def test_dev_tools_exist_only_once_dev_mode_is_switched_on(
    switch: str,
    registered: bool,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off by default; on through the env var or the web UI's saved toggle."""
    if switch == "env":
        monkeypatch.setenv(FEATURE_FLAG, "true")
    elif switch == "web_ui":
        write_overrides({"enable_dev_mode": True})
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "false")
    server = build_server(monkeypatch, core_client)

    async with Client(server.mcp) as session:
        listed = {tool.name for tool in await session.list_tools()}

    assert DEV_TOOLS & listed == (DEV_TOOLS if registered else set())


class TestSettingsMatrix:
    async def test_list_shows_the_dev_toggle_in_the_developer_section(
        self, dev_tools: DevTools
    ) -> None:
        result = await dev_tools.ha_dev_manage_settings(action="list")
        rows = {r["setting"]: r for r in result["data"]["settings"]}
        row = rows["enable_dev_mode"]
        assert row["registry"] == "advanced"
        assert row["section"] == "developer"
        assert row["value"] is False
        assert rows["log_level"]["editable"] is True

    async def test_list_never_shows_the_long_lived_token(
        self, dev_tools: DevTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOMEASSISTANT_TOKEN", "super-secret-token")
        reset_global_settings()
        result = await dev_tools.ha_dev_manage_settings(action="list")
        rows = {r["setting"]: r for r in result["data"]["settings"]}
        assert rows["homeassistant_token"]["value"] == "*****"
        assert "super-secret-token" not in str(result)

    async def test_set_saves_the_value_where_the_web_ui_saves_it(
        self, dev_tools: DevTools
    ) -> None:
        shared_lock = override_path().with_name(".config_write.lock")
        assert not shared_lock.exists()
        result = await dev_tools.ha_dev_manage_settings(
            action="set", setting="log_level", value="DEBUG"
        )
        assert result["data"]["restart_required"] is True
        assert result["data"]["mode"] == "file"
        assert stored_overrides() == {"log_level": "DEBUG"}
        assert shared_lock.is_file()
        assert get_global_settings().log_level == "DEBUG"

    async def test_set_keeps_the_other_saved_settings(
        self, dev_tools: DevTools
    ) -> None:
        write_overrides({"debug": True})
        await dev_tools.ha_dev_manage_settings(
            action="set", setting="log_level", value="ERROR"
        )
        assert stored_overrides() == {"debug": True, "log_level": "ERROR"}

    async def test_set_refuses_a_setting_pinned_by_its_env_var(
        self, dev_tools: DevTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        reset_global_settings()
        with pytest.raises(ToolError, match="locked by env"):
            await dev_tools.ha_dev_manage_settings(
                action="set", setting="log_level", value="DEBUG"
            )

    async def test_set_refuses_an_unknown_setting(self, dev_tools: DevTools) -> None:
        with pytest.raises(ToolError, match="Unknown setting"):
            await dev_tools.ha_dev_manage_settings(
                action="set", setting="no_such_setting", value=1
            )

    async def test_set_refuses_a_display_only_setting(
        self, dev_tools: DevTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Set in the environment, the setting is refused as pinned instead.
        monkeypatch.delenv("HOMEASSISTANT_URL", raising=False)
        with pytest.raises(ToolError, match="display-only"):
            await dev_tools.ha_dev_manage_settings(
                action="set", setting="homeassistant_url", value="http://x"
            )

    @pytest.mark.parametrize(
        ("setting", "value", "refusal"),
        [
            ("fuzzy_threshold", 500, "between"),
            ("log_level", "VERBOSE", "one of"),
            ("debug", 3.14, "must be of type"),
            ("mcp_server_name", "a\x00b", "null byte"),
        ],
    )
    async def test_set_refuses_a_value_the_setting_cannot_hold(
        self, dev_tools: DevTools, setting: str, value: Any, refusal: str
    ) -> None:
        with pytest.raises(ToolError, match=refusal):
            await dev_tools.ha_dev_manage_settings(
                action="set", setting=setting, value=value
            )
        assert not override_path().exists()

    async def test_set_accepts_a_stringified_bool(self, dev_tools: DevTools) -> None:
        """Some MCP clients send booleans as strings."""
        await dev_tools.ha_dev_manage_settings(
            action="set", setting="debug", value="true"
        )
        assert stored_overrides() == {"debug": True}

    async def test_a_beta_sub_flag_needs_the_beta_master_first(
        self, dev_tools: DevTools
    ) -> None:
        with pytest.raises(ToolError, match="beta"):
            await dev_tools.ha_dev_manage_settings(
                action="set", setting="enable_filesystem_tools", value=True
            )
        await dev_tools.ha_dev_manage_settings(
            action="set", setting="enable_beta_features", value=True
        )
        result = await dev_tools.ha_dev_manage_settings(
            action="set", setting="enable_filesystem_tools", value=True
        )
        assert result["data"]["value"] is True

    async def test_set_names_the_missing_argument(self, dev_tools: DevTools) -> None:
        with pytest.raises(ToolError, match="'setting' is required"):
            await dev_tools.ha_dev_manage_settings(action="set")
        with pytest.raises(ToolError, match="'value' is required"):
            await dev_tools.ha_dev_manage_settings(action="set", setting="debug")

    async def test_reset_returns_the_setting_to_its_default(
        self, dev_tools: DevTools
    ) -> None:
        await dev_tools.ha_dev_manage_settings(
            action="set", setting="log_level", value="DEBUG"
        )
        result = await dev_tools.ha_dev_manage_settings(
            action="reset", setting="log_level"
        )
        assert result["data"]["removed_override"] is True
        assert "log_level" not in stored_overrides()
        assert get_global_settings().log_level == "INFO"

    async def test_reset_of_a_setting_never_saved_changes_nothing(
        self, dev_tools: DevTools
    ) -> None:
        result = await dev_tools.ha_dev_manage_settings(
            action="reset", setting="log_level"
        )
        assert result["data"]["removed_override"] is False
        assert not override_path().exists()

    async def test_reset_points_at_the_env_var_that_pins_the_setting(
        self, dev_tools: DevTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        reset_global_settings()
        with pytest.raises(ToolError, match="env var"):
            await dev_tools.ha_dev_manage_settings(action="reset", setting="log_level")

    async def test_overlapping_sets_both_land(self, dev_tools: DevTools) -> None:
        """The shared override-file lock keeps one write from clobbering the other."""
        await asyncio.gather(
            dev_tools.ha_dev_manage_settings(
                action="set", setting="log_level", value="DEBUG"
            ),
            dev_tools.ha_dev_manage_settings(action="set", setting="debug", value=True),
        )
        assert stored_overrides() == {"log_level": "DEBUG", "debug": True}


class TestUnreadableOverrideFile:
    """A set must never overwrite saved settings it could not read."""

    async def test_a_corrupt_file_is_kept_for_inspection(
        self, dev_tools: DevTools
    ) -> None:
        override_path().write_text("not json {{{")
        with pytest.raises(ToolError, match="not valid"):
            await dev_tools.ha_dev_manage_settings(
                action="set", setting="log_level", value="DEBUG"
            )
        assert override_path().read_text() == "not json {{{"

    async def test_a_file_that_cannot_be_read_is_not_replaced(
        self, dev_tools: DevTools
    ) -> None:
        override_path().mkdir()
        with pytest.raises(ToolError, match="refusing to overwrite"):
            await dev_tools.ha_dev_manage_settings(
                action="set", setting="log_level", value="DEBUG"
            )
        assert override_path().is_dir()


class TestAddonSettings:
    """In an add-on, add-on options are saved through the Supervisor."""

    async def test_set_saves_the_option_and_keeps_the_others(
        self, dev_tools: DevTools, supervisor: Supervisor
    ) -> None:
        supervisor.options = {"enable_tool_search": False, "log_level": "info"}
        result = await dev_tools.ha_dev_manage_settings(
            action="set", setting="enable_tool_search", value=True
        )
        assert result["data"]["mode"] == "addon"
        assert supervisor.saved == [{"enable_tool_search": True, "log_level": "info"}]
        assert not override_path().exists()

    async def test_set_reports_the_supervisors_refusal(
        self, dev_tools: DevTools, supervisor: Supervisor
    ) -> None:
        supervisor.refusal = "schema says no"
        with pytest.raises(ToolError, match="Supervisor rejected"):
            await dev_tools.ha_dev_manage_settings(
                action="set", setting="enable_tool_search", value=True
            )

    async def test_reset_points_at_the_add_on_configuration(
        self, dev_tools: DevTools, supervisor: Supervisor
    ) -> None:
        with pytest.raises(ToolError, match="add-on"):
            await dev_tools.ha_dev_manage_settings(
                action="reset", setting="enable_tool_search"
            )
        assert supervisor.saved == []


class TestBackupConfig:
    @pytest.fixture(autouse=True)
    def _no_backup_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Clear auto-backup env vars (e.g. from tests/.env.test), which pin
        the fields they name."""
        for _field, env_name, _ftype in BACKUP_OVERRIDE_FIELDS:
            monkeypatch.delenv(env_name, raising=False)
        reset_global_settings()

    async def test_get_lists_the_editable_fields(self, dev_tools: DevTools) -> None:
        result = await dev_tools.ha_dev_manage_settings(action="get_backup_config")
        fields = {f["field"]: f for f in result["data"]["fields"]}
        assert fields["enable_auto_backup"]["editable"] is True

    async def test_set_applies_live_without_a_restart(
        self, dev_tools: DevTools
    ) -> None:
        result = await dev_tools.ha_dev_manage_settings(
            action="set_backup_config", backup={"enable_auto_backup": False}
        )
        assert result["data"]["applied"] == {"enable_auto_backup": False}
        assert result["data"]["restart_required"] is False
        assert get_global_settings().enable_auto_backup is False

    async def test_set_refuses_a_field_pinned_by_its_env_var(
        self, dev_tools: DevTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")
        reset_global_settings()
        with pytest.raises(ToolError, match="environment variable"):
            await dev_tools.ha_dev_manage_settings(
                action="set_backup_config", backup={"enable_auto_backup": False}
            )

    async def test_set_refuses_an_out_of_range_value(self, dev_tools: DevTools) -> None:
        with pytest.raises(ToolError, match="must be"):
            await dev_tools.ha_dev_manage_settings(
                action="set_backup_config",
                backup={"auto_backup_throttle_minutes": 99999},
            )

    async def test_set_needs_the_fields_to_change(self, dev_tools: DevTools) -> None:
        with pytest.raises(ToolError, match="'backup'"):
            await dev_tools.ha_dev_manage_settings(action="set_backup_config")

    async def test_in_an_add_on_set_saves_through_the_supervisor(
        self,
        core_client: HomeAssistantClient,
        supervisor: Supervisor,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        server = serve(monkeypatch, core_client)
        result = await DevTools(core_client, server=server).ha_dev_manage_settings(
            action="set_backup_config", backup={"enable_auto_backup": False}
        )
        assert supervisor.saved == [{"enable_auto_backup": False}]
        assert result["data"]["mode"] == "addon"
        assert result["data"]["restart_required"] is True
