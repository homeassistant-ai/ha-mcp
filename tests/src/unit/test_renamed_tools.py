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

from ha_mcp.llm_exposure import effective_llm_api_exposed
from ha_mcp.policy.evaluator import find_matching_rule
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

    def test_an_llm_api_override_follows_the_rename(self, tmp_path: Path) -> None:
        """The second name-keyed map in the same file, and the riskier one.

        ``ha_manage_app`` is exposed to conversation agents by default, so an
        orphaned override does not merely lose its setting — it re-exposes app
        install/uninstall/restart to a voice assistant, and the next Tools-tab
        save writes the map back without the record.
        """
        config_path = tmp_path / "tool_config.json"
        config_path.write_text(
            json.dumps({"llm_api": {"ha_manage_addon": False}}), encoding="utf-8"
        )

        with patch(
            "ha_mcp.settings_ui._persistence._get_config_path", return_value=config_path
        ):
            loaded = load_tool_config()

        assert loaded["llm_api"] == {"ha_manage_app": False}
        assert not effective_llm_api_exposed("ha_manage_app", [], loaded["llm_api"])

    def test_an_env_pin_follows_the_rename(self) -> None:
        settings = SimpleNamespace(
            disabled_tools="ha_manage_addon", pinned_tools="ha_get_addon"
        )

        assert env_pinned_tools(settings) == {
            "ha_manage_app": "disabled",
            "ha_get_app": "pinned",
        }

    def test_the_seeds_tie_rule_survives_the_rename(self, tmp_path: Path) -> None:
        """A retired name on one env var and the current one on the other.

        The seed's documented rule is that PINNED_TOOLS wins a tie only where
        the tool is not already disabled, so naming one spelling in each var
        has to leave the tool disabled — losing that is how a write tool the
        user switched off comes back on.
        """
        with patch(
            "ha_mcp.settings_ui._persistence._get_config_path",
            return_value=tmp_path / "tool_config.json",
        ):
            config = load_tool_config(
                SimpleNamespace(
                    disabled_tools="ha_manage_addon", pinned_tools="ha_manage_app"
                )
            )

        assert config["tools"] == {"ha_manage_app": "disabled"}

    def test_the_env_overlays_tie_rule_survives_the_rename(self) -> None:
        """``env_pinned_tools`` documents the opposite tie: pinned wins."""
        settings = SimpleNamespace(
            disabled_tools="ha_manage_addon", pinned_tools="ha_manage_app"
        )

        assert env_pinned_tools(settings) == {"ha_manage_app": "pinned"}


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

    def test_a_rule_authored_on_the_current_name_supplies_remember_minutes(
        self, tmp_path: Path
    ) -> None:
        """Both rules stay — gating is additive — but order decides one field.

        ``find_matching_rule`` returns the first match and the middleware reads
        ``remember_minutes`` off it, so the rule the user authored against the
        tool as it is called today has to come first; an inherited one would
        otherwise silently supply the approval-memory duration.
        """
        (tmp_path / "tool_policy.json").write_text(
            json.dumps(
                {
                    "rules": [
                        {"tool_name": "ha_manage_addon", "remember_minutes": 60},
                        {"tool_name": "ha_manage_app", "remember_minutes": 0},
                    ]
                }
            ),
            encoding="utf-8",
        )

        policy = load_policy(tmp_path)

        assert [rule.tool_name for rule in policy.rules] == [
            "ha_manage_app",
            "ha_manage_app",
        ]
        first = find_matching_rule("ha_manage_app", {}, policy)
        assert first is not None and first.remember_minutes == 0

    def test_an_inherited_rule_alone_keeps_its_position(self, tmp_path: Path) -> None:
        """Only a same-tool rule displaces an inherited one.

        A wildcard rule matches the same call and supplies ``remember_minutes``
        when it comes first, so sinking every inherited rule to the end would
        hand a tool that only ever had one rule to the wildcard behind it.
        """
        (tmp_path / "tool_policy.json").write_text(
            json.dumps(
                {
                    "rules": [
                        {"tool_name": "ha_manage_addon", "remember_minutes": 60},
                        {"tool_name": "*", "remember_minutes": 5},
                    ]
                }
            ),
            encoding="utf-8",
        )

        policy = load_policy(tmp_path)

        assert [rule.tool_name for rule in policy.rules] == ["ha_manage_app", "*"]
        first = find_matching_rule("ha_manage_app", {}, policy)
        assert first is not None and first.remember_minutes == 60

    def test_a_policy_without_retired_names_is_returned_as_is(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "tool_policy.json").write_text(
            json.dumps({"rules": [{"tool_name": "ha_call_service"}]}), encoding="utf-8"
        )

        policy = load_policy(tmp_path)

        assert [rule.tool_name for rule in policy.rules] == ["ha_call_service"]
        assert not set(RENAMED_TOOLS) & {rule.tool_name for rule in policy.rules}
