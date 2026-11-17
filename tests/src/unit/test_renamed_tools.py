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

import pytest

from ha_mcp.config import Settings
from ha_mcp.llm_exposure import effective_llm_api_exposed
from ha_mcp.policy.evaluator import find_matching_rule
from ha_mcp.policy.persistence import load_policy
from ha_mcp.renamed_tools import (
    RENAMED_TOOLS,
    adapt_retired_arguments,
    current_tool_name,
    rename_retired_keys,
)
from ha_mcp.settings_ui import env_pinned_tools, load_tool_config
from ha_mcp.utils.data_paths import get_data_dir


@pytest.fixture
def config_path() -> Path:
    """Where the settings UI keeps ``tool_config.json`` (this test's own
    config directory, which the unit conftest sets)."""
    return get_data_dir() / "tool_config.json"


def env(disabled: str, pinned: str) -> Settings:
    """Settings as read from ``DISABLED_TOOLS`` and ``PINNED_TOOLS``."""
    return Settings(_env_file=None, DISABLED_TOOLS=disabled, PINNED_TOOLS=pinned)


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
        self, config_path: Path
    ) -> None:
        config_path.write_text(
            json.dumps({"tools": {"ha_manage_addon": "disabled"}}), encoding="utf-8"
        )

        loaded = load_tool_config()

        assert loaded["tools"] == {"ha_manage_app": "disabled"}

    def test_an_llm_api_override_follows_the_rename(self, config_path: Path) -> None:
        """The second name-keyed map in the same file, and the riskier one.

        ``ha_manage_app`` is exposed to conversation agents by default, so an
        orphaned override does not merely lose its setting — it re-exposes app
        install/uninstall/restart to a voice assistant, and the next Tools-tab
        save writes the map back without the record.
        """
        config_path.write_text(
            json.dumps({"llm_api": {"ha_manage_addon": False}}), encoding="utf-8"
        )

        loaded = load_tool_config()

        assert loaded["llm_api"] == {"ha_manage_app": False}
        assert not effective_llm_api_exposed("ha_manage_app", [], loaded["llm_api"])

    def test_an_env_pin_follows_the_rename(self) -> None:
        assert env_pinned_tools(env("ha_manage_addon", "ha_get_addon")) == {
            "ha_manage_app": "disabled",
            "ha_get_app": "pinned",
        }

    @pytest.mark.usefixtures("config_path")
    def test_the_seeds_tie_rule_survives_the_rename(self) -> None:
        """A retired name on one env var and the current one on the other.

        The seed's documented rule is that PINNED_TOOLS wins a tie only where
        the tool is not already disabled, so naming one spelling in each var
        has to leave the tool disabled — losing that is how a write tool the
        user switched off comes back on.
        """
        config = load_tool_config(env("ha_manage_addon", "ha_manage_app"))

        assert config["tools"] == {"ha_manage_app": "disabled"}

    def test_the_env_tie_rule_still_applies_to_one_name_in_both_vars(self) -> None:
        """PINNED_TOOLS wins when the SAME tool is named in both vars."""
        assert env_pinned_tools(env("ha_manage_app", "ha_manage_app")) == {
            "ha_manage_app": "pinned"
        }

    def test_two_retired_names_colliding_keep_the_restriction(self) -> None:
        """A disabled half must not come back enabled via its pinned sibling.

        ``rename_retired_keys`` states that principle for the STORED config;
        the env overlay always wins over stored state, so letting "pinned" win
        a consolidation collision here would hand the operator back an enabled
        write-capable tool they had explicitly disabled.
        """
        assert env_pinned_tools(env("ha_manage_addon", "ha_manage_app")) == {
            "ha_manage_app": "disabled"
        }

    def test_a_repeated_pinned_alias_cannot_undo_the_restriction(self) -> None:
        """Naming the disabled spelling in PINNED_TOOLS too must not free the
        OTHER spelling's restriction.

        Resolving alias by alias let the same-name tie fire first and leave
        "pinned" in place, so the second alias then merged against "pinned"
        instead of "disabled" and the restriction vanished.
        """
        settings = env("ha_get_blueprint", "ha_get_blueprint,ha_import_blueprint")

        assert env_pinned_tools(settings) == {"ha_manage_blueprints": "pinned"}

    def test_a_disabled_alias_never_named_in_pinned_keeps_the_restriction(
        self,
    ) -> None:
        """Order must not matter either: the disabled spelling is not pinned."""
        assert env_pinned_tools(env("ha_import_blueprint", "ha_get_blueprint")) == {
            "ha_manage_blueprints": "disabled"
        }

    def test_a_disabled_blueprint_alias_survives_a_pinned_sibling(self) -> None:
        """The pair this PR consolidates, through the same path."""
        assert env_pinned_tools(env("ha_get_blueprint", "ha_import_blueprint")) == {
            "ha_manage_blueprints": "disabled"
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


class TestBlueprintConsolidation:
    """#2329 folded two tools into one: both retired names must still resolve,
    their stored states must merge restrictively, and a call on either old
    signature must gain the ``action`` the merged tool dispatches on."""

    def test_both_retired_blueprint_names_resolve(self) -> None:
        assert current_tool_name("ha_get_blueprint") == "ha_manage_blueprints"
        assert current_tool_name("ha_import_blueprint") == "ha_manage_blueprints"

    def test_disagreeing_retired_states_take_the_restrictive_one(self) -> None:
        def restrictive(first: str, second: str) -> str:
            return "disabled" if "disabled" in (first, second) else first

        states = rename_retired_keys(
            {"ha_get_blueprint": "pinned", "ha_import_blueprint": "disabled"},
            prefer=restrictive,
        )
        assert states == {"ha_manage_blueprints": "disabled"}

        states = rename_retired_keys(
            {"ha_import_blueprint": "disabled", "ha_get_blueprint": "pinned"},
            prefer=restrictive,
        )
        assert states == {"ha_manage_blueprints": "disabled"}

    def test_a_disabled_retired_blueprint_tool_disables_the_merged_one(
        self, config_path: Path
    ) -> None:
        config_path.write_text(
            json.dumps(
                {
                    "tools": {
                        "ha_get_blueprint": "pinned",
                        "ha_import_blueprint": "disabled",
                    },
                    "llm_api": {"ha_get_blueprint": True, "ha_import_blueprint": False},
                }
            ),
            encoding="utf-8",
        )
        config = load_tool_config(env("", ""))

        assert config["tools"] == {"ha_manage_blueprints": "disabled"}
        assert config["llm_api"] == {"ha_manage_blueprints": False}

    def test_get_blueprint_arguments_gain_list_or_get(self) -> None:
        assert adapt_retired_arguments("ha_get_blueprint", {"domain": "script"}) == {
            "action": "list",
            "domain": "script",
        }
        assert adapt_retired_arguments(
            "ha_get_blueprint", {"path": "user/motion.yaml", "domain": "automation"}
        ) == {"action": "get", "path": "user/motion.yaml", "domain": "automation"}

    def test_import_blueprint_arguments_gain_import(self) -> None:
        assert adapt_retired_arguments(
            "ha_import_blueprint", {"url": "https://example.com/bp.yaml"}
        ) == {"action": "import", "url": "https://example.com/bp.yaml"}

    def test_entity_exposure_arguments_map_onto_ha_get_entity(self) -> None:
        """The assistant filter becomes exposed_to only for the listing; with
        an entity_id the adapter drops it, after adapt_retired_call had Core
        check the name."""
        assert adapt_retired_arguments(
            "ha_get_entity_exposure", {"assistant": "cloud.alexa"}
        ) == {"exposed_to": "cloud.alexa"}
        assert adapt_retired_arguments(
            "ha_get_entity_exposure",
            {"entity_id": "light.lamp", "assistant": "cloud.alexa"},
        ) == {"entity_id": "light.lamp"}
        assert adapt_retired_arguments("ha_get_entity_exposure", {}) == {}

    def test_an_explicit_action_survives_the_adapter(self) -> None:
        """The docstring promises it, and the dict spread is what delivers it.

        A caller reaching the consolidated tool through a retired name can
        still ask for any action; the adapter only supplies the default the
        old signature implied.
        """
        assert adapt_retired_arguments(
            "ha_get_blueprint",
            {"action": "delete", "path": "user/motion.yaml", "confirm": True},
        ) == {"action": "delete", "path": "user/motion.yaml", "confirm": True}
        assert adapt_retired_arguments(
            "ha_import_blueprint", {"action": "list", "domain": "script"}
        ) == {"action": "list", "domain": "script"}

    def test_names_without_an_adapter_pass_arguments_through(self) -> None:
        assert adapt_retired_arguments("ha_manage_addon", {"slug": "x"}) == {
            "slug": "x"
        }
        assert adapt_retired_arguments("ha_get_state", None) is None
