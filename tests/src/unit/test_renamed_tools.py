"""State stored under a retired tool name keeps applying after the rename.

The alias middleware covers callers. These cover the other half: a setting the
user made against ``ha_manage_addon`` — disabled in the settings UI, pinned
through the environment, put behind a policy gate — has to keep applying to
the tool now called ``ha_manage_app``. Falling back to the default is the
dangerous direction in each case: a disabled write tool comes back on, and a
gated one runs ungated.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ha_mcp.policy.persistence import load_policy
from ha_mcp.renamed_tools import (
    RENAMED_TOOLS,
    current_tool_name,
    rename_retired_keys,
)
from ha_mcp.settings_ui._persistence import env_pinned_tools, load_tool_config


class TestRenameMapping:
    def test_a_retired_name_resolves_to_the_current_one(self) -> None:
        assert current_tool_name("ha_manage_addon") == "ha_manage_app"

    def test_an_unrelated_name_is_returned_unchanged(self) -> None:
        assert current_tool_name("ha_call_service") == "ha_call_service"

    def test_a_state_set_against_the_current_name_wins(self) -> None:
        """The retired key is inherited; the current one was set deliberately."""
        states = rename_retired_keys(
            {"ha_manage_addon": "disabled", "ha_manage_app": "pinned"}
        )

        assert states == {"ha_manage_app": "pinned"}

    def test_untouched_tools_keep_their_state(self) -> None:
        states = rename_retired_keys(
            {"ha_manage_addon": "disabled", "ha_get_entity": "pinned"}
        )

        assert states == {"ha_manage_app": "disabled", "ha_get_entity": "pinned"}


class TestStoredToolConfig:
    def test_a_disabled_tool_stays_disabled_through_the_rename(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "tool_config.json"
        config_path.write_text(
            json.dumps({"tools": {"ha_manage_addon": "disabled"}}), encoding="utf-8"
        )

        with patch(
            "ha_mcp.settings_ui._persistence._get_config_path", return_value=config_path
        ):
            loaded = load_tool_config()

        assert loaded["tools"] == {"ha_manage_app": "disabled"}

    def test_an_env_pin_follows_the_rename(self) -> None:
        settings = SimpleNamespace(
            disabled_tools="ha_manage_addon", pinned_tools="ha_get_addon"
        )

        assert env_pinned_tools(settings) == {
            "ha_manage_app": "disabled",
            "ha_get_app": "pinned",
        }


class TestStoredPolicy:
    def test_a_rule_naming_a_retired_tool_gates_the_current_one(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "tool_policy.json").write_text(
            json.dumps(
                {
                    "rules": [
                        {"tool_name": "ha_manage_addon"},
                        {"tool_name": "ha_call_service"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        policy = load_policy(tmp_path)

        assert [rule.tool_name for rule in policy.rules] == [
            "ha_manage_app",
            "ha_call_service",
        ]

    def test_a_policy_without_retired_names_is_returned_as_is(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "tool_policy.json").write_text(
            json.dumps({"rules": [{"tool_name": "ha_call_service"}]}), encoding="utf-8"
        )

        policy = load_policy(tmp_path)

        assert [rule.tool_name for rule in policy.rules] == ["ha_call_service"]
        assert not set(RENAMED_TOOLS) & {rule.tool_name for rule in policy.rules}
