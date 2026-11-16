"""The per-tool states the settings page saves, and how the server applies them.

``tool_config.json`` lives in the data directory; ``DISABLED_TOOLS`` and
``PINNED_TOOLS`` seed it and override it per tool. ``apply_tool_visibility``
turns the saved states into what the server's FastMCP shows.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ha_mcp.config import Settings
from ha_mcp.settings_ui import (
    MANDATORY_TOOLS,
    apply_tool_visibility,
    effective_tool_config,
    env_pinned_tools,
    load_tool_config,
    save_tool_config,
)
from ha_mcp.utils.data_paths import get_data_dir

from ._settings_ui_app import settings_world as settings_world
from ._settings_ui_app import with_env


def _yaml_editing_on(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    return with_env(
        monkeypatch,
        ENABLE_BETA_FEATURES="true",
        ENABLE_YAML_CONFIG_EDITING="true",
        **env,
    )


def _hidden(mcp: MagicMock) -> set[str]:
    """The tools ``apply_tool_visibility`` told FastMCP to hide."""
    return set(mcp.disable.call_args.kwargs["names"]) if mcp.disable.called else set()


class TestSavedStates:
    def test_saved_states_read_back_as_saved(self) -> None:
        config = {"tools": {"ha_get_hacs_info": "disabled", "ha_restart": "pinned"}}

        assert save_tool_config(config) is True
        assert load_tool_config() == config

    def test_no_file_is_no_config(self) -> None:
        assert load_tool_config() == {}

    def test_a_corrupt_file_is_no_config(self, tmp_path: Path) -> None:
        (tmp_path / "tool_config.json").write_text("not json {{{")

        assert load_tool_config() == {}

    def test_the_first_start_seeds_the_file_from_the_env_vars(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        settings = with_env(
            monkeypatch,
            DISABLED_TOOLS="ha_get_hacs_info,ha_manage_hacs",
            PINNED_TOOLS="ha_restart",
        )

        config = load_tool_config(settings)

        assert config["tools"] == {
            "ha_get_hacs_info": "disabled",
            "ha_manage_hacs": "disabled",
            "ha_restart": "pinned",
        }
        assert json.loads((tmp_path / "tool_config.json").read_text()) == config

    def test_without_a_config_dir_the_file_is_kept_under_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("HA_MCP_CONFIG_DIR")
        monkeypatch.setenv("HOME", str(tmp_path))
        get_data_dir.cache_clear()

        assert save_tool_config({"tools": {"ha_restart": "pinned"}}) is True

        assert (tmp_path / ".ha-mcp" / "tool_config.json").is_file()

    def test_an_unreadable_file_is_no_config_instead_of_a_crash(
        self, tmp_path: Path
    ) -> None:
        """#1125: a config path the server cannot read must not stop it starting."""
        (tmp_path / "tool_config.json").mkdir()

        assert load_tool_config() == {}

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="chmod 0o000 doesn't model POSIX EACCES on Windows",
    )
    def test_a_config_dir_the_server_cannot_enter_is_no_config(
        self, tmp_path: Path
    ) -> None:
        """#1125 with a real permission boundary: the file sits in a 0o000 dir."""
        (tmp_path / "tool_config.json").write_text("{}")
        get_data_dir()
        os.chmod(tmp_path, 0o000)
        try:
            assert load_tool_config() == {}
        finally:
            os.chmod(tmp_path, 0o755)

    def test_a_save_that_cannot_be_written_reports_failure(
        self, tmp_path: Path
    ) -> None:
        """The page shows "Save failed" instead of a save that was lost."""
        (tmp_path / "tool_config.json").mkdir()

        assert save_tool_config({"tools": {"x": "disabled"}}) is False


class TestEnvPinnedTools:
    def test_each_env_var_pins_its_tools(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with_env(
            monkeypatch, DISABLED_TOOLS="ha_foo, ha_bar", PINNED_TOOLS="ha_baz, ha_qux"
        )

        assert env_pinned_tools() == {
            "ha_foo": "disabled",
            "ha_bar": "disabled",
            "ha_baz": "pinned",
            "ha_qux": "pinned",
        }

    def test_a_tool_in_both_env_vars_is_pinned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with_env(monkeypatch, DISABLED_TOOLS="ha_foo", PINNED_TOOLS="ha_foo")

        assert env_pinned_tools() == {"ha_foo": "pinned"}

    def test_disabled_tools_wins_over_a_saved_enable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "tool_config.json").write_text(
            json.dumps({"tools": {"ha_foo": "enabled"}})
        )
        with_env(monkeypatch, DISABLED_TOOLS="ha_foo")

        assert effective_tool_config()["tools"]["ha_foo"] == "disabled"

    def test_pinned_tools_wins_over_a_saved_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "tool_config.json").write_text(
            json.dumps({"tools": {"ha_bar": ""}})
        )
        with_env(monkeypatch, PINNED_TOOLS="ha_bar")

        assert effective_tool_config()["tools"]["ha_bar"] == "pinned"

    def test_a_tool_no_env_var_names_keeps_its_saved_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "tool_config.json").write_text(
            json.dumps({"tools": {"ha_other": "disabled"}})
        )
        with_env(monkeypatch, DISABLED_TOOLS="ha_foo")

        tools = effective_tool_config()["tools"]

        assert tools["ha_foo"] == "disabled"
        assert tools["ha_other"] == "disabled"


class TestApplyToolVisibility:
    def test_a_disabled_tool_is_hidden_and_an_enabled_one_is_not(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = MagicMock()
        config = {"tools": {"ha_get_hacs_info": "disabled", "ha_restart": "enabled"}}

        apply_tool_visibility(mcp, config, _yaml_editing_on(monkeypatch))

        assert _hidden(mcp) == {"ha_get_hacs_info"}

    def test_a_mandatory_tool_cannot_be_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = MagicMock()
        config = {"tools": dict.fromkeys(MANDATORY_TOOLS, "disabled")}

        apply_tool_visibility(mcp, config, with_env(monkeypatch))

        assert _hidden(mcp) == {"ha_config_set_yaml"}

    def test_the_skill_guide_can_be_disabled_outside_strict_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#1886: with strict best-practices mode off a disable of
        ha_get_skill_guide holds and the tool is not forced on."""
        mcp = MagicMock()
        settings = _yaml_editing_on(monkeypatch, ENABLE_STRICT_MANDATORY_BPS="false")

        apply_tool_visibility(
            mcp, {"tools": {"ha_get_skill_guide": "disabled"}}, settings
        )

        assert "ha_get_skill_guide" in _hidden(mcp)
        assert "ha_get_skill_guide" not in mcp.enable.call_args.kwargs["names"]

    def test_strict_mode_keeps_the_skill_guide_on_whatever_was_saved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#1886: the strict gate publishes its acknowledgment key only
        through ha_get_skill_guide, so a saved disable is dropped."""
        mcp = MagicMock()
        settings = _yaml_editing_on(monkeypatch, ENABLE_STRICT_MANDATORY_BPS="true")

        apply_tool_visibility(
            mcp, {"tools": {"ha_get_skill_guide": "disabled"}}, settings
        )

        assert "ha_get_skill_guide" not in _hidden(mcp)
        assert "ha_get_skill_guide" in mcp.enable.call_args.kwargs["names"]

    def test_yaml_editing_off_hides_the_yaml_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = MagicMock()

        apply_tool_visibility(mcp, {"tools": {}}, with_env(monkeypatch))

        assert _hidden(mcp) == {"ha_config_set_yaml"}

    def test_yaml_editing_on_still_honours_a_saved_disable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The safety toggle and the page's switch both have to allow the tool."""
        mcp = MagicMock()
        config = {"tools": {"ha_config_set_yaml": "disabled"}}

        apply_tool_visibility(mcp, config, _yaml_editing_on(monkeypatch))

        assert _hidden(mcp) == {"ha_config_set_yaml"}

    def test_nothing_is_hidden_when_nothing_was_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = MagicMock()
        config: dict[str, Any] = {}

        apply_tool_visibility(mcp, config, _yaml_editing_on(monkeypatch))

        mcp.disable.assert_not_called()

    def test_pinned_tools_are_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = {"tools": {"ha_restart": "pinned", "ha_get_hacs_info": "enabled"}}

        result = apply_tool_visibility(MagicMock(), config, with_env(monkeypatch))

        assert result.pinned_names == frozenset({"ha_restart"})

    def test_explicitly_enabled_tools_are_returned_to_unpin_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#966: the server unpins a default-pinned tool the user set to enabled."""
        config = {
            "tools": {
                "ha_manage_backup": "enabled",
                "ha_restart": "pinned",
                "ha_get_hacs_info": "disabled",
            }
        }

        result = apply_tool_visibility(MagicMock(), config, with_env(monkeypatch))

        assert result.enabled_names == frozenset({"ha_manage_backup"})
