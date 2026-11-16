"""``ha_dev_manage_settings`` list_tools / set_tool: the web UI's Tools tab.

The dev tools run on the real server (``_tools_dev_core.served``), so the tool
names they accept and list come from the server's own registry, and what they
save lands in the per-test data dir.
"""

from typing import Literal

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.policy import persistence
from ha_mcp.policy.model import Policy, Predicate, Rule
from ha_mcp.policy.persistence import save_policy
from ha_mcp.tools.tools_dev import DevTools
from ha_mcp.utils.data_paths import get_data_dir

from ._dev_tool_env import _isolated_env as _isolated_env
from ._dev_tool_env import _policy_access_on as _policy_access_on
from ._tools_dev_core import policy_rules, serve, tool_config
from ._tools_dev_core import served as served


def _store_policy(
    *rules: Rule, rule_effect: Literal["require_approval", "allow"] = "require_approval"
) -> None:
    save_policy(get_data_dir(), Policy(rule_effect=rule_effect, rules=list(rules)))


class TestToolState:
    async def test_disabling_a_tool_is_saved_for_the_next_start(
        self, served: DevTools
    ) -> None:
        result = await served.ha_dev_manage_settings(
            action="set_tool", tool="ha_write_file", state="disabled"
        )
        assert result["data"]["state"] == "disabled"
        assert result["data"]["restart_required"] is True
        assert tool_config()["tools"]["ha_write_file"] == "disabled"

    async def test_state_and_llm_api_are_saved_together(self, served: DevTools) -> None:
        await served.ha_dev_manage_settings(
            action="set_tool", tool="ha_search", state="pinned", llm_api=True
        )
        config = tool_config()
        assert config["tools"]["ha_search"] == "pinned"
        assert config["llm_api"]["ha_search"] is True

    async def test_a_tool_called_by_its_retired_name_is_found(
        self, served: DevTools
    ) -> None:
        """A stale catalog names the tool the way it was called before; the
        alias middleware runs a call on that name, so the same name arriving
        as data has to reach the same tool."""
        result = await served.ha_dev_manage_settings(
            action="set_tool", tool="ha_manage_addon", state="disabled"
        )
        assert result["data"]["tool"] == "ha_manage_app"
        assert tool_config()["tools"] == {"ha_manage_app": "disabled"}

    async def test_a_tool_pinned_by_env_var_cannot_be_flipped(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DISABLED_TOOLS", "ha_get_history")
        dev = DevTools(core_client, server=serve(monkeypatch, core_client))
        saved = tool_config()
        with pytest.raises(ToolError, match="unset the env var"):
            await dev.ha_dev_manage_settings(
                action="set_tool", tool="ha_get_history", state="pinned"
            )
        assert tool_config() == saved

    async def test_a_best_practices_tool_stays_on_in_strict_mode(
        self, served: DevTools
    ) -> None:
        """Strict best-practices mode is on by default."""
        with pytest.raises(ToolError, match="strict best-practices"):
            await served.ha_dev_manage_settings(
                action="set_tool", tool="ha_get_skill_guide", state="disabled"
            )

    async def test_a_mandatory_tool_cannot_be_disabled(self, served: DevTools) -> None:
        """The web UI locks its row, and startup would silently re-enable it:
        a saved disable would be a misleading success."""
        with pytest.raises(ToolError, match="mandatory"):
            await served.ha_dev_manage_settings(
                action="set_tool", tool="ha_search", state="disabled"
            )
        assert tool_config() == {}

    async def test_llm_api_exposure_says_it_needs_the_embedded_server(
        self, served: DevTools
    ) -> None:
        result = await served.ha_dev_manage_settings(
            action="set_tool", tool="ha_search", llm_api=False
        )
        assert result["data"]["llm_api"] is False
        assert tool_config()["llm_api"]["ha_search"] is False
        assert any("embedded" in w for w in result["warnings"])

    async def test_the_tool_is_required(self, served: DevTools) -> None:
        with pytest.raises(ToolError, match="'tool' is required"):
            await served.ha_dev_manage_settings(action="set_tool", state="disabled")

    async def test_something_to_change_is_required(self, served: DevTools) -> None:
        with pytest.raises(ToolError, match="at least one"):
            await served.ha_dev_manage_settings(action="set_tool", tool="ha_search")

    async def test_a_wildcard_is_not_a_tool(self, served: DevTools) -> None:
        """Gating '*' would gate every tool, the developer recovery actions too."""
        with pytest.raises(ToolError, match="wildcard"):
            await served.ha_dev_manage_settings(action="set_tool", tool="*", gated=True)

    async def test_a_misspelt_tool_is_refused_before_anything_is_saved(
        self, served: DevTools
    ) -> None:
        """A typo'd gate used to save a rule for no tool and report success
        while the intended tool stayed ungated (Codex #1993 round 3)."""
        with pytest.raises(ToolError, match="Unknown tool"):
            await served.ha_dev_manage_settings(
                action="set_tool", tool="ha_call_servce", state="disabled"
            )
        assert tool_config() == {}

    async def test_a_tool_behind_an_off_feature_flag_can_still_be_set(
        self, served: DevTools
    ) -> None:
        result = await served.ha_dev_manage_settings(
            action="set_tool", tool="ha_write_file", state="pinned"
        )
        assert result["data"]["state"] == "pinned"


@pytest.mark.usefixtures("_policy_access_on")
class TestSecurityGate:
    """``gated`` adds or removes the tool's unconditional policy rule."""

    async def test_gating_adds_a_bare_rule_and_says_policies_are_off(
        self, served: DevTools
    ) -> None:
        result = await served.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", gated=True
        )
        assert result["data"]["gated"] is True
        assert result["data"]["policy_rules_changed"] is True
        assert [(r.tool_name, r.when) for r in policy_rules()] == [
            ("ha_call_service", [])
        ]
        assert any("enable_tool_security_policies" in w for w in result["warnings"])

    async def test_ungating_removes_the_rule(self, served: DevTools) -> None:
        await served.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", gated=True
        )
        result = await served.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", gated=False
        )
        assert result["data"]["policy_rules_changed"] is True
        assert policy_rules() == []

    async def test_gating_twice_adds_one_rule(self, served: DevTools) -> None:
        await served.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", gated=True
        )
        result = await served.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", gated=True
        )
        assert result["data"]["policy_rules_changed"] is False
        assert len(policy_rules()) == 1

    async def test_a_conditional_rule_survives_gating_and_ungating(
        self, served: DevTools
    ) -> None:
        """Only the bare gate is the toggle's to add and remove (Codex #1993 P1)."""
        conditional = Rule(
            tool_name="ha_call_service",
            when=[Predicate(path="args.domain", op="eq", value="lock")],
        )
        _store_policy(conditional)
        await served.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", gated=True
        )
        assert sorted(bool(r.when) for r in policy_rules()) == [False, True]

        await served.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", gated=False
        )
        assert [r.when for r in policy_rules()] == [conditional.when]

    async def test_a_new_gate_goes_before_a_wildcard_rule(
        self, served: DevTools
    ) -> None:
        """The first matching rule wins, as in the web UI: a gate after '*'
        would take the wildcard's remember window."""
        _store_policy(Rule(tool_name="*", remember_minutes=60))
        await served.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", gated=True
        )
        assert [r.tool_name for r in policy_rules()] == ["ha_call_service", "*"]

    async def test_a_gate_under_a_gate_everything_rule_can_be_removed(
        self, served: DevTools
    ) -> None:
        """The tool's own rule carries its own remember window, so adding and
        removing it are real changes even though '*' gates it anyway."""
        _store_policy(Rule(tool_name="*", remember_minutes=60))
        for gated in (True, False):
            await served.ha_dev_manage_settings(
                action="set_tool", tool="ha_call_service", gated=gated
            )
        assert [r.tool_name for r in policy_rules()] == ["*"]

    async def test_a_refused_state_change_saves_no_gate(self, served: DevTools) -> None:
        """Every field is checked before anything is written."""
        with pytest.raises(ToolError, match="strict best-practices"):
            await served.ha_dev_manage_settings(
                action="set_tool",
                tool="ha_get_skill_guide",
                state="disabled",
                gated=True,
            )
        assert policy_rules() == []

    async def test_a_failed_gate_write_says_the_state_was_already_saved(
        self, served: DevTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def disk_full(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(persistence, "save_policy", disk_full)
        with pytest.raises(ToolError, match="already saved"):
            await served.ha_dev_manage_settings(
                action="set_tool", tool="ha_get_history", state="pinned", gated=True
            )
        assert tool_config()["tools"]["ha_get_history"] == "pinned"


@pytest.mark.usefixtures("_policy_access_on")
class TestSecurityGateUnderAllowList:
    """In an allow list the bare rule approves its tool (issue #2540)."""

    @pytest.fixture(autouse=True)
    def _allow_list(self) -> None:
        _store_policy(Rule(tool_name="ha_get_state"), rule_effect="allow")

    async def test_gating_removes_the_approval(self, served: DevTools) -> None:
        result = await served.ha_dev_manage_settings(
            action="set_tool", tool="ha_get_state", gated=True
        )
        assert result["data"]["policy_rules_changed"] is True
        assert policy_rules() == []

    async def test_ungating_adds_an_approval(self, served: DevTools) -> None:
        await served.ha_dev_manage_settings(
            action="set_tool", tool="ha_search", gated=False
        )
        assert sorted(r.tool_name for r in policy_rules()) == [
            "ha_get_state",
            "ha_search",
        ]

    async def test_gating_under_an_approve_everything_rule_is_refused(
        self, served: DevTools
    ) -> None:
        _store_policy(
            Rule(tool_name="ha_get_state"), Rule(tool_name="*"), rule_effect="allow"
        )
        with pytest.raises(
            ToolError, match="unconditional '\\*' rule in this allow list approves"
        ):
            await served.ha_dev_manage_settings(
                action="set_tool", tool="ha_get_state", gated=True
            )
        assert [r.tool_name for r in policy_rules()] == ["ha_get_state", "*"]

    async def test_list_shows_unapproved_tools_as_gated(self, served: DevTools) -> None:
        rows = await _listed(served)
        assert rows["ha_get_state"]["gated"] is False
        assert rows["ha_call_service"]["gated"] is True

    async def test_list_shows_nothing_gated_under_an_approve_everything_rule(
        self, served: DevTools
    ) -> None:
        _store_policy(Rule(tool_name="*"), rule_effect="allow")
        rows = await _listed(served)
        assert rows["ha_call_service"]["gated"] is False


async def _listed(dev_tools: DevTools) -> dict[str, dict[str, object]]:
    result = await dev_tools.ha_dev_manage_settings(action="list_tools")
    return {row["name"]: row for row in result["data"]["tools"]}


class TestListTools:
    @pytest.mark.usefixtures("_policy_access_on")
    async def test_list_shows_state_and_gate(self, served: DevTools) -> None:
        await served.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", state="disabled", gated=True
        )
        result = await served.ha_dev_manage_settings(action="list_tools")
        rows = {row["name"]: row for row in result["data"]["tools"]}
        assert rows["ha_call_service"]["state"] == "disabled"
        assert rows["ha_call_service"]["gated"] is True
        assert rows["ha_search"]["gated"] is False
        assert result["data"]["policies_enabled"] is False

    async def test_list_marks_the_locked_rows(self, served: DevTools) -> None:
        rows = await _listed(served)
        assert rows["ha_search"]["mandatory"] is True
        assert rows["ha_get_skill_guide"]["bps_locked"] is True

    async def test_list_warns_when_the_policy_file_is_corrupt(
        self, served: DevTools
    ) -> None:
        """It must not read as a clean policy with nothing gated."""
        (get_data_dir() / "tool_policy.json").write_text("{not json")
        result = await served.ha_dev_manage_settings(action="list_tools")
        rows = {row["name"]: row for row in result["data"]["tools"]}
        assert rows["ha_search"]["gated"] is False
        assert any("tool_policy.json is invalid" in w for w in result["warnings"])

    async def test_list_marks_tools_behind_an_off_feature_flag(
        self, served: DevTools
    ) -> None:
        rows = await _listed(served)
        assert rows["ha_search"]["available"] is True
        assert rows["ha_search"]["disabled_by"] is None
        assert rows["ha_write_file"]["available"] is False
        assert rows["ha_write_file"]["disabled_by"] == "enable_filesystem_tools"

    @pytest.mark.parametrize("policies", [True, False])
    async def test_list_says_whether_the_policy_gate_is_running(
        self,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        policies: bool,
    ) -> None:
        """The gate is wired at startup only, whatever the saved flag says now."""
        server = serve(monkeypatch, core_client, policies=policies)
        result = await DevTools(core_client, server=server).ha_dev_manage_settings(
            action="list_tools"
        )
        assert result["data"]["policies_live"] is policies
