"""Unit tests for tools_security_policy (issue #2148).

``ha_manage_security_policy`` edits the same ``tool_policy.json`` the
developer-mode settings tool does, so these mirror the assertions in
``test_tools_dev.py::TestManagePolicy`` — the two surfaces share
``policy.editing`` and must not drift.
"""

import asyncio
import inspect
import json
from types import SimpleNamespace
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


def _queue(remembered: bool = False):
    """A real ApprovalQueue, optionally holding a remembered approval."""
    from ha_mcp.policy.approval_queue import ApprovalQueue

    queue = ApprovalQueue()
    if remembered:
        queue.remember("ha_call_service", "argshash", minutes=10)
        assert queue.is_remembered("ha_call_service", "argshash")
    return queue


class TestRegistrationGating:
    """The tool must not exist at all unless the flag is on."""

    def test_flag_disabled_by_default(self):
        from ha_mcp.config import get_global_settings

        assert get_global_settings().enable_security_policy_tool is False

    def test_flag_empty_string_is_false(self, monkeypatch):
        """An empty env value must read as off, not crash startup."""
        from ha_mcp.config import get_global_settings

        monkeypatch.setenv(FEATURE_FLAG, "")
        reset_global_settings()
        assert get_global_settings().enable_security_policy_tool is False

    def test_register_noop_when_disabled(self):
        mcp = MagicMock()
        register_security_policy_tools(mcp, MagicMock(), server=None)
        mcp.add_tool.assert_not_called()

    def test_register_adds_tool_when_enabled(self, monkeypatch):
        monkeypatch.setenv(FEATURE_FLAG, "true")
        reset_global_settings()
        mcp = MagicMock()
        register_security_policy_tools(mcp, MagicMock(), server=None)
        registered = {call.args[0].__name__ for call in mcp.add_tool.call_args_list}
        assert registered == {"ha_manage_security_policy"}

    def test_register_requires_the_server_kwarg(self, monkeypatch):
        """The registry always passes ``server``; a call site that forgets
        must fail loudly rather than silently lose the liveness report."""
        monkeypatch.setenv(FEATURE_FLAG, "true")
        reset_global_settings()
        with pytest.raises(KeyError):
            register_security_policy_tools(MagicMock(), MagicMock())

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
        return SecurityPolicyTools(MagicMock(), None)

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
        server = SimpleNamespace(approval_queue=_queue())
        result = await SecurityPolicyTools(
            MagicMock(), server
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
        with pytest.raises(ToolError, match="version mismatch") as exc:
            await policy_tools.ha_manage_security_policy(
                action="set",
                policy={"rules": []},
                expected_version=0,
            )
        # The reload hint names THIS tool's action, not the dev tool's.
        assert "ha_manage_security_policy('get')" in str(exc.value)

    async def test_set_invalid_schema_rejected(self, policy_tools):
        # wait_seconds must be < approval_ttl_minutes * 60.
        with pytest.raises(ToolError, match="schema validation"):
            await policy_tools.ha_manage_security_policy(
                action="set",
                policy={"wait_seconds": 599, "approval_ttl_minutes": 1, "rules": []},
            )

    async def test_set_requires_policy_object(self, policy_tools):
        with pytest.raises(ToolError, match="'policy'") as exc:
            await policy_tools.ha_manage_security_policy(action="set")
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
        queue = _queue(remembered=True)
        await SecurityPolicyTools(
            MagicMock(), SimpleNamespace(approval_queue=queue)
        ).ha_manage_security_policy(
            action="set",
            policy={"rules": [{"tool_name": "ha_call_service"}]},
        )
        assert not queue.is_remembered("ha_call_service", "argshash")


class TestMissingRulesGuard:
    """A payload without ``rules`` used to validate (the model defaults it
    to []) and silently delete every approval gate."""

    @pytest.fixture
    def policy_tools(self):
        return SecurityPolicyTools(MagicMock(), None)

    async def test_set_without_rules_key_is_rejected(self, policy_tools):
        await policy_tools.ha_manage_security_policy(
            action="set", policy={"rules": [{"tool_name": "ha_call_service"}]}
        )
        with pytest.raises(ToolError, match="'rules' is missing"):
            await policy_tools.ha_manage_security_policy(
                action="set", policy={"wait_seconds": 45}
            )
        # The existing gate survived the rejected write.
        got = await policy_tools.ha_manage_security_policy(action="get")
        assert got["data"]["policy"]["rules"][0]["tool_name"] == "ha_call_service"

    async def test_rejection_explains_how_to_clear_deliberately(self, policy_tools):
        with pytest.raises(ToolError) as exc:
            await policy_tools.ha_manage_security_policy(
                action="set", policy={"wait_seconds": 45}
            )
        # Parse rather than substring-match the serialized ToolError: the
        # quotes around "rules" are backslash-escaped in the JSON payload.
        payload = json.loads(str(exc.value))
        assert '"rules": []' in payload["error"]["message"]
        assert "ha_manage_security_policy('get')" in payload["error"]["suggestion"]

    async def test_empty_rules_list_is_accepted(self, policy_tools):
        """The deliberate clear must still work."""
        result = await policy_tools.ha_manage_security_policy(
            action="set", policy={"rules": [], "version": 0}
        )
        assert result["data"]["version"] == 1

    async def test_removing_rules_warns_with_names_and_count(self, policy_tools):
        await policy_tools.ha_manage_security_policy(
            action="set",
            policy={
                "rules": [
                    {"tool_name": "ha_call_service"},
                    {"tool_name": "ha_restart"},
                ],
                "version": 0,
            },
        )
        result = await policy_tools.ha_manage_security_policy(
            action="set", policy={"rules": [], "version": 1}
        )
        removed = [w for w in result["warnings"] if "removed 2 existing rule" in w]
        assert removed, result["warnings"]
        assert "ha_call_service" in removed[0]
        assert "ha_restart" in removed[0]

    async def test_no_removal_warning_when_rules_are_kept(self, policy_tools):
        await policy_tools.ha_manage_security_policy(
            action="set",
            policy={"rules": [{"tool_name": "ha_call_service"}], "version": 0},
        )
        result = await policy_tools.ha_manage_security_policy(
            action="set",
            policy={
                "wait_seconds": 45,
                "rules": [{"tool_name": "ha_call_service"}],
                "version": 1,
            },
        )
        assert not [w for w in result.get("warnings", []) if "removed" in w]


class TestEngineLivenessWarning:
    """Policies enabled but no queue in this process = nothing is gated."""

    @pytest.fixture(autouse=True)
    def _policies_on(self, monkeypatch):
        monkeypatch.setenv("ENABLE_TOOL_SECURITY_POLICIES", "true")
        reset_global_settings()

    async def test_warns_when_engine_is_not_running(self):
        result = await SecurityPolicyTools(MagicMock(), None).ha_manage_security_policy(
            action="set",
            policy={"rules": [{"tool_name": "ha_call_service"}], "version": 0},
        )
        assert any("approval engine is NOT running" in w for w in result["warnings"])

    async def test_no_warning_when_engine_is_live(self):
        server = SimpleNamespace(approval_queue=_queue())
        result = await SecurityPolicyTools(
            MagicMock(), server
        ).ha_manage_security_policy(
            action="set",
            policy={"rules": [{"tool_name": "ha_call_service"}], "version": 0},
        )
        assert not [
            w for w in result.get("warnings", []) if "approval engine is NOT" in w
        ]

    async def test_no_engine_warning_for_an_empty_rule_set(self):
        """Nothing to gate, so nothing to warn about."""
        result = await SecurityPolicyTools(MagicMock(), None).ha_manage_security_policy(
            action="set", policy={"rules": [], "version": 0}
        )
        assert not [
            w for w in result.get("warnings", []) if "approval engine is NOT" in w
        ]


class TestPostSaveTailRobustness:
    """The write has landed by the time the remember-cache is cleared, so a
    failure there must degrade to a warning, never an INTERNAL_ERROR the
    caller would retry into a spurious version mismatch."""

    async def test_cache_clear_failure_still_reports_success(self):
        class _Exploding:
            def clear_remember_cache(self):
                raise RuntimeError("queue is wedged")

        server = SimpleNamespace(approval_queue=_Exploding())
        tools = SecurityPolicyTools(MagicMock(), server)
        result = await tools.ha_manage_security_policy(
            action="set",
            policy={"rules": [{"tool_name": "ha_call_service"}], "version": 0},
        )
        assert result["success"] is True
        assert result["data"]["version"] == 1
        assert any("could not be cleared" in w for w in result["warnings"])
        # And the write really did land.
        got = await tools.ha_manage_security_policy(action="get")
        assert got["data"]["policy"]["rules"][0]["tool_name"] == "ha_call_service"


class TestCorruptPolicyFile:
    @pytest.fixture
    def policy_tools(self):
        return SecurityPolicyTools(MagicMock(), None)

    @pytest.fixture(autouse=True)
    def _corrupt(self):
        (get_data_dir() / "tool_policy.json").write_text("{not json", encoding="utf-8")

    async def test_get_reports_config_invalid(self, policy_tools):
        with pytest.raises(ToolError, match=r"tool_policy\.json is invalid") as exc:
            await policy_tools.ha_manage_security_policy(action="get")
        assert "CONFIG_INVALID" in str(exc.value)

    async def test_set_reports_config_invalid(self, policy_tools):
        with pytest.raises(ToolError, match=r"tool_policy\.json is invalid") as exc:
            await policy_tools.ha_manage_security_policy(
                action="set", policy={"rules": []}
            )
        assert "CONFIG_INVALID" in str(exc.value)


class TestCrossSurfaceInterop:
    """The dev tool and this tool are two doors onto one document."""

    @pytest.fixture(autouse=True)
    def _dev_policy_access(self, monkeypatch):
        # The dev-side write path is gated by #2141's access toggle
        # (default OFF); these tests exercise the write itself, not the
        # gate, so turn it on. The new tool's own path is never gated
        # by this flag.
        monkeypatch.setenv("HAMCP_DEV_SECURITY_POLICY_ACCESS", "true")
        reset_global_settings()
        yield

    async def test_dev_write_is_visible_to_the_new_tool(self):
        from ha_mcp.tools.tools_dev import DevTools

        await DevTools(MagicMock()).ha_dev_manage_settings(
            action="set_policy",
            policy={
                "wait_seconds": 30,
                "rules": [{"tool_name": "ha_restart"}],
                "version": 0,
            },
        )
        got = await SecurityPolicyTools(MagicMock(), None).ha_manage_security_policy(
            action="get"
        )
        policy = got["data"]["policy"]
        assert policy["version"] == 1
        assert policy["wait_seconds"] == 30
        assert policy["rules"][0]["tool_name"] == "ha_restart"

    async def test_new_tool_write_is_visible_to_the_dev_tool(self):
        from ha_mcp.tools.tools_dev import DevTools

        await SecurityPolicyTools(MagicMock(), None).ha_manage_security_policy(
            action="set",
            policy={"rules": [{"tool_name": "ha_call_service"}], "version": 0},
        )
        got = await DevTools(MagicMock()).ha_dev_manage_settings(action="get_policy")
        assert got["data"]["policy"]["rules"][0]["tool_name"] == "ha_call_service"

    async def test_concurrent_writes_serialize_without_corruption(self):
        """Both surfaces take the same write locks, so two unversioned
        writes land one after the other (version 2), not on top of each
        other."""
        from ha_mcp.tools.tools_dev import DevTools

        dev = DevTools(MagicMock())
        new = SecurityPolicyTools(MagicMock(), None)
        results = await asyncio.gather(
            dev.ha_dev_manage_settings(
                action="set_policy", policy={"rules": [{"tool_name": "ha_restart"}]}
            ),
            new.ha_manage_security_policy(
                action="set",
                policy={"rules": [{"tool_name": "ha_call_service"}]},
            ),
        )
        assert {r["data"]["version"] for r in results} == {1, 2}
        got = await new.ha_manage_security_policy(action="get")
        policy = got["data"]["policy"]
        assert policy["version"] == 2
        # Whichever landed last is intact and readable — no interleaved write.
        assert len(policy["rules"]) == 1
        assert policy["rules"][0]["tool_name"] in {"ha_restart", "ha_call_service"}
        raw = json.loads((get_data_dir() / "tool_policy.json").read_text("utf-8"))
        assert raw["version"] == 2
