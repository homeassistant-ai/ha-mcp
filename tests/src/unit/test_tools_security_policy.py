"""Unit tests for tools_security_policy (issue #2148).

``ha_manage_security_policy`` edits the same ``tool_policy.json`` the
developer-mode settings tool does, so these mirror the assertions in
``test_tools_dev.py::TestManagePolicy`` — the two surfaces share
``policy.editing`` and must not drift.
"""

import inspect
from typing import get_args
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.config import reset_global_settings
from ha_mcp.tools.tools_security_policy import (
    FEATURE_FLAG,
    SecurityPolicyTools,
    register_security_policy_tools,
)
from ha_mcp.utils.data_paths import get_data_dir


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Isolate the data dir and the Settings singleton per test.

    Registration reads through ``get_global_settings()`` (cached) and the
    policy persists under ``get_data_dir()`` (memoized) — both must be
    reset so tests can't see each other's state or the real user data dir.
    """
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv(FEATURE_FLAG, raising=False)
    monkeypatch.delenv("ENABLE_TOOL_SECURITY_POLICIES", raising=False)
    get_data_dir.cache_clear()
    reset_global_settings()
    yield
    get_data_dir.cache_clear()
    reset_global_settings()


class TestRegistrationGating:
    """The tool must not exist at all unless the flag is on."""

    def test_flag_disabled_by_default(self):
        from ha_mcp.config import get_global_settings

        assert get_global_settings().enable_security_policy_tool is False

    def test_register_noop_when_disabled(self):
        mcp = MagicMock()
        register_security_policy_tools(mcp, MagicMock())
        mcp.add_tool.assert_not_called()

    def test_register_adds_tool_when_enabled(self, monkeypatch):
        monkeypatch.setenv(FEATURE_FLAG, "true")
        reset_global_settings()
        mcp = MagicMock()
        register_security_policy_tools(mcp, MagicMock())
        registered = {call.args[0].__name__ for call in mcp.add_tool.call_args_list}
        assert registered == {"ha_manage_security_policy"}

    def test_only_get_and_set_are_reachable(self):
        """The approval queue must stay out of this tool's surface."""
        annotation = (
            inspect.signature(SecurityPolicyTools.ha_manage_security_policy)
            .parameters["action"]
            .annotation
        )
        literal = get_args(annotation)[0]
        assert set(get_args(literal)) == {"get", "set"}


class TestManagePolicy:
    @pytest.fixture
    def policy_tools(self):
        return SecurityPolicyTools(MagicMock())

    async def test_get_returns_default_policy(self, policy_tools):
        result = await policy_tools.ha_manage_security_policy(action="get")
        policy = result["data"]["policy"]
        assert policy["wait_seconds"] == 60
        assert policy["rules"] == []
        assert policy["version"] == 0
        assert result["data"]["policies_enabled"] is False

    async def test_get_reports_status_without_queue_contents(self, policy_tools):
        """Status metadata only — no pending-approval data may leak here."""
        result = await policy_tools.ha_manage_security_policy(action="get")
        assert set(result["data"]) == {"policy", "policies_enabled", "policies_live"}
        assert result["data"]["policies_live"] is False

    async def test_get_reports_live_engine(self):
        from types import SimpleNamespace

        from ha_mcp.policy.approval_queue import ApprovalQueue

        server = SimpleNamespace(approval_queue=ApprovalQueue())
        result = await SecurityPolicyTools(
            MagicMock(), server=server
        ).ha_manage_security_policy(action="get")
        assert result["data"]["policies_live"] is True

    async def test_set_roundtrip_bumps_version(self, policy_tools):
        result = await policy_tools.ha_manage_security_policy(
            action="set",
            policy={
                "wait_seconds": 30,
                "approval_ttl_minutes": 5,
                "rules": [{"tool_name": "ha_call_service"}],
                "version": 0,
            },
        )
        assert result["data"]["version"] == 1
        assert result["data"]["rules_changed"] is True
        got = await policy_tools.ha_manage_security_policy(action="get")
        assert got["data"]["policy"]["version"] == 1
        assert got["data"]["policy"]["rules"][0]["tool_name"] == "ha_call_service"

    async def test_set_version_mismatch_rejected(self, policy_tools):
        await policy_tools.ha_manage_security_policy(
            action="set", policy={"rules": [], "version": 0}
        )
        # On-disk version is now 1; a stale expected_version=0 must be rejected.
        with pytest.raises(ToolError, match="version mismatch"):
            await policy_tools.ha_manage_security_policy(
                action="set",
                policy={"rules": []},
                expected_version=0,
            )

    async def test_set_invalid_schema_rejected(self, policy_tools):
        # wait_seconds must be < approval_ttl_minutes * 60.
        with pytest.raises(ToolError, match="schema validation"):
            await policy_tools.ha_manage_security_policy(
                action="set",
                policy={"wait_seconds": 599, "approval_ttl_minutes": 1},
            )

    async def test_set_requires_policy_object(self, policy_tools):
        with pytest.raises(ToolError, match="'policy'") as exc:
            await policy_tools.ha_manage_security_policy(action="set")
        # The reload hint must name THIS tool's action, not the dev tool's.
        assert "ha_manage_security_policy('get')" in str(exc.value)

    async def test_set_without_version_warns(self, policy_tools):
        result = await policy_tools.ha_manage_security_policy(
            action="set", policy={"rules": []}
        )
        assert any("without an optimistic-concurrency" in w for w in result["warnings"])

    async def test_set_warns_while_policies_disabled(self, policy_tools):
        result = await policy_tools.ha_manage_security_policy(
            action="set",
            policy={"rules": [{"tool_name": "ha_call_service"}], "version": 0},
        )
        assert any("won't enforce" in w for w in result["warnings"])

    async def test_set_clears_remember_cache_on_rule_change(self):
        from types import SimpleNamespace

        from ha_mcp.policy.approval_queue import ApprovalQueue

        queue = ApprovalQueue()
        queue.remember("ha_call_service", "argshash", minutes=10)
        assert queue.is_remembered("ha_call_service", "argshash")
        server = SimpleNamespace(approval_queue=queue)
        await SecurityPolicyTools(MagicMock(), server=server).ha_manage_security_policy(
            action="set",
            policy={"rules": [{"tool_name": "ha_call_service"}]},
        )
        assert not queue.is_remembered("ha_call_service", "argshash")
