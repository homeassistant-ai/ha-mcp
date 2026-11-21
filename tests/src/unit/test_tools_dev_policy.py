"""The dev tools and the tool security policies gating the agent using them.

get_policy / set_policy edit the stored policy, approve / deny decide a call in
the server's approval queue, and ``dev_tools_security_policy_access`` (#2141)
guards every one of those writes. The dev tools run on the real server
(``_tools_dev_core.serve``), whose queue exists when policies were on at start.
"""

import pytest

from ha_mcp._vendor.fastmcp import Client
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import get_global_settings, reset_global_settings
from ha_mcp.policy.persistence import POLICY_FILENAME
from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.tools.tools_dev import DevTools
from ha_mcp.utils.data_paths import get_data_dir

from ._dev_tool_env import POLICY_ACCESS_FLAG
from ._dev_tool_env import _isolated_env as _isolated_env
from ._dev_tool_env import _policy_access_on as _policy_access_on
from ._tools_dev_core import dev_tools as dev_tools
from ._tools_dev_core import (
    pending_call,
    policy_access,
    policy_rules,
    serve,
    stored_overrides,
    tool_config,
    write_overrides,
)
from ._tools_dev_core import served as served

GATE = {"rules": [{"tool_name": "ha_call_service"}], "version": 0}


@pytest.fixture
def gating_server(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> HomeAssistantSmartMCPServer:
    """The real server with policies on at start: its approval queue is wired."""
    return serve(monkeypatch, core_client, policies=True)


@pytest.fixture
def gating_tools(
    core_client: HomeAssistantClient, gating_server: HomeAssistantSmartMCPServer
) -> DevTools:
    return DevTools(core_client, server=gating_server)


@pytest.mark.usefixtures("_policy_access_on")
class TestPolicyDocument:
    async def test_a_fresh_install_has_no_rules(self, served: DevTools) -> None:
        result = await served.ha_dev_manage_settings(action="get_policy")
        policy = result["data"]["policy"]
        assert policy["rules"] == []
        assert policy["version"] == 0
        assert result["data"]["policies_enabled"] is False

    async def test_a_saved_policy_reads_back_with_the_next_version(
        self, served: DevTools
    ) -> None:
        result = await served.ha_dev_manage_settings(
            action="set_policy",
            policy={"wait_seconds": 30, "approval_ttl_minutes": 5, **GATE},
        )
        assert result["data"]["version"] == 1
        assert result["data"]["rules_changed"] is True
        got = (await served.ha_dev_manage_settings(action="get_policy"))["data"]
        assert got["policy"]["version"] == 1
        assert got["policy"]["wait_seconds"] == 30
        assert got["policy"]["rules"][0]["tool_name"] == "ha_call_service"

    async def test_a_write_based_on_a_stale_read_is_refused(
        self, served: DevTools
    ) -> None:
        await served.ha_dev_manage_settings(
            action="set_policy", policy={"rules": [], "version": 0}
        )
        with pytest.raises(ToolError, match="version mismatch"):
            await served.ha_dev_manage_settings(
                action="set_policy", policy={"rules": []}, expected_version=0
            )

    async def test_a_policy_its_own_schema_refuses_is_not_saved(
        self, served: DevTools
    ) -> None:
        """wait_seconds must stay under the approval TTL."""
        with pytest.raises(ToolError, match="schema validation"):
            await served.ha_dev_manage_settings(
                action="set_policy",
                policy={"wait_seconds": 599, "approval_ttl_minutes": 1, "rules": []},
            )
        assert not (get_data_dir() / POLICY_FILENAME).exists()

    async def test_a_policy_without_rules_is_refused(self, served: DevTools) -> None:
        """set_policy replaces the whole document, so an omitted 'rules' would
        silently delete every approval gate (#2148 review)."""
        await served.ha_dev_manage_settings(action="set_policy", policy=GATE)
        with pytest.raises(ToolError, match="'rules' is missing") as raised:
            await served.ha_dev_manage_settings(
                action="set_policy", policy={"wait_seconds": 45}
            )
        assert "ha_dev_manage_settings('get_policy')" in str(raised.value)
        assert [r.tool_name for r in policy_rules()] == ["ha_call_service"]

    async def test_removing_rules_names_them_in_a_warning(
        self, served: DevTools
    ) -> None:
        await served.ha_dev_manage_settings(action="set_policy", policy=GATE)
        result = await served.ha_dev_manage_settings(
            action="set_policy", policy={"rules": [], "version": 1}
        )
        removed = [w for w in result["warnings"] if "removed 1 existing rule" in w]
        assert removed, result["warnings"]
        assert "ha_call_service" in removed[0]

    async def test_the_policy_is_required(self, served: DevTools) -> None:
        with pytest.raises(ToolError, match="'policy'"):
            await served.ha_dev_manage_settings(action="set_policy")

    async def test_a_write_without_a_version_warns(self, served: DevTools) -> None:
        result = await served.ha_dev_manage_settings(
            action="set_policy", policy={"rules": []}
        )
        assert any("without an optimistic-concurrency" in w for w in result["warnings"])


@pytest.mark.usefixtures("_policy_access_on")
class TestApprovals:
    async def test_list_pending_shows_the_waiting_call(
        self, gating_tools: DevTools, gating_server: HomeAssistantSmartMCPServer
    ) -> None:
        token = pending_call(gating_server)
        result = await gating_tools.ha_dev_manage_server(action="list_pending")
        assert result["data"]["count"] == 1
        assert result["data"]["pending"][0]["token"] == token
        assert result["data"]["pending"][0]["tool_name"] == "ha_call_service"

    async def test_list_pending_without_policies_at_start_is_empty(
        self, served: DevTools
    ) -> None:
        result = await served.ha_dev_manage_server(action="list_pending")
        assert result["data"]["pending"] == []
        assert "No active approval queue" in result["data"]["note"]

    @pytest.mark.parametrize(
        ("action", "decision"), [("approve", "approved"), ("deny", "denied")]
    )
    async def test_a_decision_reaches_the_waiting_call(
        self,
        gating_tools: DevTools,
        gating_server: HomeAssistantSmartMCPServer,
        action: str,
        decision: str,
    ) -> None:
        token = pending_call(gating_server)
        result = await gating_tools.ha_dev_manage_server(action=action, token=token)
        assert result["data"]["decision"] == decision
        entry = gating_server.approval_queue.get(token)
        assert entry is not None
        assert entry.decision == decision

    async def test_an_unknown_token_is_refused(
        self, gating_tools: DevTools, gating_server: HomeAssistantSmartMCPServer
    ) -> None:
        pending_call(gating_server)
        with pytest.raises(ToolError, match="Unknown or expired"):
            await gating_tools.ha_dev_manage_server(
                action="approve", token="not-a-real-token"
            )

    async def test_a_call_is_decided_once(
        self, gating_tools: DevTools, gating_server: HomeAssistantSmartMCPServer
    ) -> None:
        token = pending_call(gating_server)
        await gating_tools.ha_dev_manage_server(action="approve", token=token)
        with pytest.raises(ToolError, match="already decided"):
            await gating_tools.ha_dev_manage_server(action="deny", token=token)

    async def test_the_token_is_required(self, gating_tools: DevTools) -> None:
        with pytest.raises(ToolError, match="'token' is required"):
            await gating_tools.ha_dev_manage_server(action="approve")

    async def test_without_policies_at_start_there_is_nothing_to_decide(
        self, served: DevTools
    ) -> None:
        with pytest.raises(ToolError, match="No active approval queue"):
            await served.ha_dev_manage_server(action="approve", token="x")

    async def test_the_registered_tools_decide_on_the_servers_own_queue(
        self,
        gating_server: HomeAssistantSmartMCPServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """What an MCP client reaches is the server's registration, which
        hands the dev tools the server's live queue."""
        token = pending_call(gating_server)
        async with Client(gating_server.mcp) as session:
            await session.call_tool(
                "ha_dev_manage_server", {"action": "approve", "token": token}
            )
        entry = gating_server.approval_queue.get(token)
        assert entry is not None
        assert entry.decision == "approved"


@pytest.mark.usefixtures("_policy_access_on")
class TestRememberedApprovalsAreForgotten:
    """A rule change must not leave a remembered approval standing (security)."""

    @pytest.fixture(autouse=True)
    def _remembered(self, gating_server: HomeAssistantSmartMCPServer) -> None:
        gating_server.approval_queue.remember("ha_call_service", "argshash", minutes=10)
        assert gating_server.approval_queue.is_remembered("ha_call_service", "argshash")

    async def test_gating_a_tool_forgets_them(
        self, gating_tools: DevTools, gating_server: HomeAssistantSmartMCPServer
    ) -> None:
        await gating_tools.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", gated=True
        )
        assert not gating_server.approval_queue.is_remembered(
            "ha_call_service", "argshash"
        )

    async def test_a_new_policy_forgets_them(
        self, gating_tools: DevTools, gating_server: HomeAssistantSmartMCPServer
    ) -> None:
        await gating_tools.ha_dev_manage_settings(action="set_policy", policy=GATE)
        assert not gating_server.approval_queue.is_remembered(
            "ha_call_service", "argshash"
        )


class TestPolicyAccessGuard:
    """Dev mode alone must not let an agent rewrite the policies gating it
    or decide its own approvals (#2141). Access is off unless a test asks
    for ``_policy_access_on``."""

    async def test_set_policy_is_refused_and_nothing_is_saved(
        self, dev_tools: DevTools
    ) -> None:
        assert await policy_access(dev_tools) is False
        assert not (get_data_dir() / POLICY_FILENAME).exists()

    async def test_a_gate_is_refused_while_a_state_change_still_works(
        self, served: DevTools
    ) -> None:
        with pytest.raises(ToolError, match="dev_tools_security_policy_access"):
            await served.ha_dev_manage_settings(
                action="set_tool", tool="ha_call_service", gated=True
            )
        assert policy_rules() == []
        result = await served.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", state="disabled"
        )
        assert result["data"]["state"] == "disabled"
        assert tool_config()["tools"]["ha_call_service"] == "disabled"

    async def test_a_state_change_with_a_gate_is_refused_whole(
        self, served: DevTools
    ) -> None:
        with pytest.raises(ToolError, match="security-policy access is disabled"):
            await served.ha_dev_manage_settings(
                action="set_tool", tool="ha_get_history", state="pinned", gated=True
            )
        assert tool_config() == {}
        assert policy_rules() == []

    async def test_ungating_is_refused_too(
        self, served: DevTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gated=False removes an operator's approval gate: the escalation
        #2141 blocks, so a falsy value is no way around the guard."""
        monkeypatch.setenv(POLICY_ACCESS_FLAG, "true")
        await served.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", gated=True
        )
        monkeypatch.delenv(POLICY_ACCESS_FLAG)
        with pytest.raises(ToolError, match="AUTH_INSUFFICIENT_PERMISSIONS"):
            await served.ha_dev_manage_settings(
                action="set_tool", tool="ha_call_service", gated=False
            )
        assert [r.tool_name for r in policy_rules()] == ["ha_call_service"]

    @pytest.mark.parametrize("action", ["approve", "deny"])
    async def test_an_approval_decision_is_refused(
        self,
        gating_tools: DevTools,
        gating_server: HomeAssistantSmartMCPServer,
        action: str,
    ) -> None:
        token = pending_call(gating_server)
        with pytest.raises(ToolError, match="AUTH_INSUFFICIENT_PERMISSIONS"):
            await gating_tools.ha_dev_manage_server(action=action, token=token)
        entry = gating_server.approval_queue.get(token)
        assert entry is not None
        assert entry.decision == "pending"

    async def test_reads_stay_available(
        self, gating_tools: DevTools, gating_server: HomeAssistantSmartMCPServer
    ) -> None:
        policy = await gating_tools.ha_dev_manage_settings(action="get_policy")
        assert policy["data"]["policy"]["rules"] == []
        pending_call(gating_server)
        pending = await gating_tools.ha_dev_manage_server(action="list_pending")
        assert pending["data"]["count"] == 1

    @pytest.mark.usefixtures("_policy_access_on")
    async def test_every_guarded_write_works_with_access_on(
        self, gating_tools: DevTools, gating_server: HomeAssistantSmartMCPServer
    ) -> None:
        token = pending_call(gating_server)
        written = await gating_tools.ha_dev_manage_settings(
            action="set_policy", policy={"rules": [], "version": 0}
        )
        assert written["data"]["version"] == 1
        gate = await gating_tools.ha_dev_manage_settings(
            action="set_tool", tool="ha_call_service", gated=True
        )
        assert gate["data"]["gated"] is True
        decided = await gating_tools.ha_dev_manage_server(action="approve", token=token)
        assert decided["data"]["decision"] == "approved"

    async def test_switching_the_policy_engine_is_refused(
        self, dev_tools: DevTools
    ) -> None:
        """Turning the engine off drops every gate in one write."""
        with pytest.raises(ToolError, match="AUTH_INSUFFICIENT_PERMISSIONS"):
            await dev_tools.ha_dev_manage_settings(
                action="set", setting="enable_tool_security_policies", value=False
            )

    async def test_resetting_the_policy_engine_is_refused(
        self, dev_tools: DevTools
    ) -> None:
        """A reset returns it to off, the same as setting it off."""
        write_overrides({"enable_tool_security_policies": True})
        with pytest.raises(ToolError, match="AUTH_INSUFFICIENT_PERMISSIONS"):
            await dev_tools.ha_dev_manage_settings(
                action="reset", setting="enable_tool_security_policies"
            )
        assert stored_overrides()["enable_tool_security_policies"] is True

    @pytest.mark.usefixtures("_policy_access_on")
    async def test_switching_the_policy_engine_works_with_access_on(
        self, dev_tools: DevTools
    ) -> None:
        result = await dev_tools.ha_dev_manage_settings(
            action="set", setting="enable_tool_security_policies", value=True
        )
        assert result["data"]["value"] is True

    @pytest.mark.parametrize("access", [False, True])
    async def test_the_policy_tool_flag_is_never_set_by_dev_tools(
        self, dev_tools: DevTools, monkeypatch: pytest.MonkeyPatch, access: bool
    ) -> None:
        """Enabling it registers ha_manage_security_policy, a shorter route to
        dropping gates than set_policy: a human decision, even with access."""
        monkeypatch.setenv(POLICY_ACCESS_FLAG, str(access).lower())
        with pytest.raises(ToolError, match="enable_security_policy_tool"):
            await dev_tools.ha_dev_manage_settings(
                action="set", setting="enable_security_policy_tool", value=True
            )

    @pytest.mark.parametrize("action", ["set", "reset"])
    async def test_the_access_toggle_is_never_changed_by_dev_tools(
        self, dev_tools: DevTools, action: str
    ) -> None:
        """The agent must not flip its own leash either way. Access is granted
        in the override file, so the field is editable by origin and only the
        guard can refuse."""
        write_overrides({"dev_tools_security_policy_access": True})
        reset_global_settings()
        with pytest.raises(ToolError, match="cannot be changed by dev tools"):
            await dev_tools.ha_dev_manage_settings(
                action=action, setting="dev_tools_security_policy_access", value=False
            )
        assert get_global_settings().dev_tools_security_policy_access is True

    async def test_access_granted_by_env_var_applies_without_a_restart(
        self, dev_tools: DevTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert await policy_access(dev_tools) is False
        monkeypatch.setenv(POLICY_ACCESS_FLAG, "true")
        assert await policy_access(dev_tools) is True

    async def test_access_granted_in_the_web_ui_applies_without_a_restart(
        self, dev_tools: DevTools
    ) -> None:
        """In stdio mode the settings UI is a separate process whose save
        resets only its own cached settings: this process's are stale, and
        the guard must read the file anyway."""
        assert get_global_settings().dev_tools_security_policy_access is False
        write_overrides({"dev_tools_security_policy_access": True})
        assert await policy_access(dev_tools) is True

    async def test_access_revoked_in_the_web_ui_applies_without_a_restart(
        self, dev_tools: DevTools
    ) -> None:
        write_overrides({"dev_tools_security_policy_access": True})
        reset_global_settings()
        assert get_global_settings().dev_tools_security_policy_access is True
        write_overrides({"dev_tools_security_policy_access": False})
        assert await policy_access(dev_tools) is False

    @pytest.mark.parametrize("env_value", ["on", "TRUE"])
    async def test_env_values_settings_read_as_true_grant_access(
        self, dev_tools: DevTools, monkeypatch: pytest.MonkeyPatch, env_value: str
    ) -> None:
        monkeypatch.setenv(POLICY_ACCESS_FLAG, env_value)
        assert await policy_access(dev_tools) is True

    async def test_an_env_value_settings_read_as_false_leaves_access_off(
        self, dev_tools: DevTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A set variable is not a grant by being set."""
        monkeypatch.setenv(POLICY_ACCESS_FLAG, "off")
        assert await policy_access(dev_tools) is False

    async def test_the_env_var_wins_over_the_web_ui(
        self, dev_tools: DevTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same env-over-file order the settings loader applies."""
        write_overrides({"dev_tools_security_policy_access": True})
        monkeypatch.setenv(POLICY_ACCESS_FLAG, "false")
        assert await policy_access(dev_tools) is False

    @pytest.mark.parametrize(("saved", "granted"), [("true", False), (1, True)])
    async def test_a_hand_edited_value_grants_what_the_web_ui_shows(
        self, dev_tools: DevTools, saved: object, granted: bool
    ) -> None:
        """The settings loader takes a bool or an int for this field and
        shows a string as off; the guard must agree, never grant access no
        surface displays."""
        write_overrides({"dev_tools_security_policy_access": saved})
        assert await policy_access(dev_tools) is granted

    async def test_a_saved_value_of_the_wrong_shape_leaves_access_off(
        self, dev_tools: DevTools
    ) -> None:
        write_overrides({"dev_tools_security_policy_access": ["true"]})
        assert await policy_access(dev_tools) is False


class TestSettingsListLocks:
    """The settings list must not offer as editable what the guard refuses."""

    async def test_without_access_the_guarded_rows_are_locked(
        self, dev_tools: DevTools
    ) -> None:
        rows = await _settings(dev_tools)
        engine = rows["enable_tool_security_policies"]
        assert (engine["editable"], engine["locked_reason"]) == (
            False,
            "policy_access_required",
        )
        toggle = rows["dev_tools_security_policy_access"]
        assert (toggle["editable"], toggle["locked_reason"]) == (
            False,
            "web_ui_or_env_only",
        )
        assert toggle["value"] is False
        tool_flag = rows["enable_security_policy_tool"]
        assert (tool_flag["editable"], tool_flag["locked_reason"]) == (
            False,
            "web_ui_or_env_only",
        )

    async def test_access_unlocks_only_the_engine_row(
        self, dev_tools: DevTools
    ) -> None:
        """Granted in the web UI (an env grant would env-pin the toggle row);
        the toggle shows the fresh file value, not the cached settings."""
        write_overrides({"dev_tools_security_policy_access": True})
        rows = await _settings(dev_tools)
        engine = rows["enable_tool_security_policies"]
        assert engine["editable"] is True
        assert "locked_reason" not in engine
        toggle = rows["dev_tools_security_policy_access"]
        assert (toggle["editable"], toggle["value"]) == (False, True)
        assert toggle["locked_reason"] == "web_ui_or_env_only"
        assert rows["enable_security_policy_tool"]["editable"] is False

    async def test_an_env_pinned_engine_row_keeps_the_env_story(
        self, dev_tools: DevTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pin refuses the write before the guard would; naming the guard
        would send the operator to the wrong lock."""
        monkeypatch.setenv("ENABLE_TOOL_SECURITY_POLICIES", "true")
        reset_global_settings()
        engine = (await _settings(dev_tools))["enable_tool_security_policies"]
        assert engine["origin"] == "env"
        assert engine["editable"] is False
        assert "locked_reason" not in engine


async def _settings(dev_tools: DevTools) -> dict[str, dict[str, object]]:
    result = await dev_tools.ha_dev_manage_settings(action="list")
    return {row["setting"]: row for row in result["data"]["settings"]}
