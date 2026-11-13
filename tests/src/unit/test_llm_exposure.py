"""Unit tests for the conversation-agent LLM API exposure layer (#1745).

``ha_mcp.llm_exposure`` decides which tools the ha_mcp_tools custom component
may offer to Home Assistant conversation agents, and stamps that decision
into every ``tools/list`` entry as ``_meta.ha_mcp``. These tests cover the
deny-by-default policy (beta tag / dev prefix / restart-reload-backup), the
override semantics, and the stamping middleware — including its must-never-
break-tools/list containment.

The settings the middleware reads are real ``tool_config.json`` and policy
files in the per-test data directory, and the stamp is read off ``tools/list``
the way the component reads it: over an MCP client session.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory

from ha_mcp import llm_exposure
from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp.llm_exposure import (
    LLM_API_CONFIG_KEY,
    META_EXPOSED_KEY,
    META_NAMESPACE,
    META_PARAMS_KEY,
    META_PINNED_KEY,
    META_POLICY_KEY,
    LlmExposureMiddleware,
    default_llm_api_exposed,
    effective_llm_api_exposed,
    load_llm_api_overrides,
)
from ha_mcp.policy.model import Policy, Rule
from ha_mcp.policy.persistence import save_policy
from ha_mcp.settings_ui import save_tool_config
from ha_mcp.transforms import DEFAULT_PINNED_TOOLS
from ha_mcp.utils.data_paths import get_data_dir

# Longer than any coalescing window the middleware may use for its settings
# reads, so the next listing must read the files again.
_PAST_THE_CACHE = timedelta(minutes=5)


class TestDefaults:
    @pytest.mark.parametrize(
        ("name", "tags", "expected"),
        [
            ("ha_get_state", set(), True),
            ("ha_config_set_automation", {"Config"}, True),
            ("ha_write_file", {"Files", "beta"}, False),
            ("ha_dev_manage_server", set(), False),
            ("ha_dev_future_tool", set(), False),
            ("ha_restart", set(), False),
            ("ha_reload_core", set(), False),
            ("ha_manage_backup", set(), False),
            ("ha_manage_security_policy", {"System"}, False),
        ],
    )
    def test_default_policy(self, name: str, tags: set[str], expected: bool) -> None:
        assert default_llm_api_exposed(name, tags) is expected

    def test_override_wins_both_ways(self) -> None:
        # A user can expose a default-hidden tool and hide a default-exposed
        # one; non-bool garbage in the store is ignored, not honored.
        assert effective_llm_api_exposed("ha_restart", set(), {"ha_restart": True})
        assert not effective_llm_api_exposed(
            "ha_get_state", set(), {"ha_get_state": False}
        )
        assert not effective_llm_api_exposed("ha_restart", set(), {"ha_restart": "yes"})


class TestOverridesLoading:
    def test_loads_only_bool_values(self) -> None:
        assert save_tool_config(
            {
                LLM_API_CONFIG_KEY: {
                    "ha_restart": True,
                    "ha_get_state": False,
                    "ha_bad": "nope",
                }
            }
        )
        assert load_llm_api_overrides() == {
            "ha_restart": True,
            "ha_get_state": False,
        }

    @pytest.mark.parametrize(
        "stored",
        [
            pytest.param(None, id="no-config-file"),
            pytest.param({}, id="no-llm-api-key"),
            pytest.param({LLM_API_CONFIG_KEY: ["x"]}, id="llm-api-not-a-mapping"),
        ],
    )
    def test_missing_or_malformed_key_is_empty(
        self, stored: dict[str, Any] | None
    ) -> None:
        if stored is not None:
            assert save_tool_config(stored)
        assert load_llm_api_overrides() == {}


def _tool(name: str, tags: set[str] | None = None, **kwargs: Any) -> Tool:
    def _noop() -> None:
        return None

    return Tool.from_function(_noop, name=name, tags=tags or set(), **kwargs)


def _server(middleware: LlmExposureMiddleware, *tools: Tool) -> FastMCP:
    """A FastMCP server listing ``tools`` through ``middleware``."""
    mcp = FastMCP("llm-exposure")
    for tool in tools:
        mcp.add_tool(tool)
    mcp.add_middleware(middleware)
    return mcp


async def _listed_meta(mcp: FastMCP) -> dict[str, dict[str, Any]]:
    """Each listed tool's ``_meta`` as an MCP client receives it."""
    async with Client(mcp) as client:
        tools = await client.list_tools()
    return {tool.name: dict(tool.meta or {}) for tool in tools}


async def _stamps(mcp: FastMCP) -> dict[str, dict[str, Any]]:
    return {
        name: meta[META_NAMESPACE] for name, meta in (await _listed_meta(mcp)).items()
    }


class TestPinnedStamp:
    async def test_mirrors_server_tool_search_semantics(self) -> None:
        """A user pin and a user unpin of a default both reach the stamp."""
        unpinned, still_pinned = DEFAULT_PINNED_TOOLS[:2]
        assert save_tool_config(
            {
                "tools": {
                    "ha_custom_pin": "pinned",
                    unpinned: "enabled",  # explicit unpin of a default
                }
            }
        )
        mcp = _server(
            LlmExposureMiddleware(),
            _tool("ha_custom_pin"),
            _tool(unpinned),
            _tool(still_pinned),
            _tool("ha_plain"),
        )

        stamps = await _stamps(mcp)

        assert stamps["ha_custom_pin"][META_PINNED_KEY] is True
        assert stamps[unpinned][META_PINNED_KEY] is False
        # Untouched defaults stay pinned.
        assert stamps[still_pinned][META_PINNED_KEY] is True
        assert stamps["ha_plain"][META_PINNED_KEY] is False


class TestMiddleware:
    async def test_stamps_exposure_and_pinned(self) -> None:
        assert save_tool_config({LLM_API_CONFIG_KEY: {"ha_restart": True}})
        mcp = _server(
            LlmExposureMiddleware(),
            _tool("ha_search"),
            _tool("ha_restart"),
            _tool("ha_write_file", tags={"beta"}),
        )

        by_name = await _stamps(mcp)

        assert by_name["ha_search"][META_EXPOSED_KEY] is True
        assert by_name["ha_search"][META_PINNED_KEY] is True
        # The component's compact search hits show this line (#2633).
        assert by_name["ha_search"][META_PARAMS_KEY] == "none"
        # The serving-server policy block rides the same namespace (#1990).
        assert META_POLICY_KEY in by_name["ha_search"]
        # Override exposes the default-hidden restart tool.
        assert by_name["ha_restart"][META_EXPOSED_KEY] is True
        assert by_name["ha_restart"][META_PINNED_KEY] is False
        # Beta tag stays hidden without an override.
        assert by_name["ha_write_file"][META_EXPOSED_KEY] is False

    async def test_preserves_existing_meta(self) -> None:
        mcp = _server(
            LlmExposureMiddleware(),
            _tool("ha_get_state", meta={"other": {"k": 1}}),
        )

        meta = (await _listed_meta(mcp))["ha_get_state"]

        assert meta["other"] == {"k": 1}
        assert meta[META_NAMESPACE][META_EXPOSED_KEY] is True

    async def test_settings_read_failure_stamps_defaults(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Stamping must never break tools/list — a failed settings read
        # falls back to pure defaults with a visible warning.
        def _boom() -> dict[str, bool]:
            raise OSError("disk gone")

        monkeypatch.setattr(llm_exposure, "load_llm_api_overrides", _boom)
        mcp = _server(
            LlmExposureMiddleware(), _tool("ha_get_state"), _tool("ha_restart")
        )

        by_name = await _stamps(mcp)

        assert by_name["ha_get_state"][META_EXPOSED_KEY] is True
        assert by_name["ha_restart"][META_EXPOSED_KEY] is False
        assert "Could not read LLM-API exposure settings" in caplog.text

    async def test_settings_change_applies_once_the_cached_read_expires(
        self, freezer: FrozenDateTimeFactory
    ) -> None:
        """Listings in one moment share a read; a later one sees the edit."""
        assert save_tool_config({LLM_API_CONFIG_KEY: {"ha_get_state": False}})
        mcp = _server(LlmExposureMiddleware(), _tool("ha_get_state"))

        with patch.object(
            llm_exposure,
            "load_llm_api_overrides",
            wraps=llm_exposure.load_llm_api_overrides,
        ) as reads:
            first = await _stamps(mcp)
            assert save_tool_config({LLM_API_CONFIG_KEY: {"ha_get_state": True}})
            coalesced = await _stamps(mcp)
            assert reads.call_count == 1

            freezer.tick(_PAST_THE_CACHE)
            later = await _stamps(mcp)
            assert reads.call_count == 2

        assert first["ha_get_state"][META_EXPOSED_KEY] is False
        assert coalesced["ha_get_state"][META_EXPOSED_KEY] is False
        assert later["ha_get_state"][META_EXPOSED_KEY] is True

    async def test_settings_read_failure_serves_last_known_good(
        self, monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory
    ) -> None:
        # Fail-direction guard (review finding): a failed settings read must
        # NOT re-expose tools the user explicitly hid — the middleware keeps
        # serving the last successful read instead of pure defaults.
        assert save_tool_config({LLM_API_CONFIG_KEY: {"ha_get_state": False}})
        mcp = _server(LlmExposureMiddleware(), _tool("ha_get_state"))
        first = await _stamps(mcp)
        assert first["ha_get_state"][META_EXPOSED_KEY] is False

        def _boom() -> dict[str, bool]:
            raise OSError("disk gone")

        monkeypatch.setattr(llm_exposure, "load_llm_api_overrides", _boom)
        freezer.tick(_PAST_THE_CACHE)
        second = await _stamps(mcp)

        # The user-hidden tool STAYS hidden on the stale data.
        assert second["ha_get_state"][META_EXPOSED_KEY] is False


class TestPolicyStamp:
    """The serving-server policy/identity block in _meta.ha_mcp (#1990).

    A client pointed at a different server than the one the user configured
    rules on previously failed SILENTLY — calls executed ungated with nothing
    on the wire saying "this server has zero rules". The stamp makes the
    serving server's actual gating state visible on every tools/list.
    """

    async def test_policy_block_reflects_rules_and_live_state(self) -> None:
        save_policy(get_data_dir(), Policy(rules=[Rule(tool_name="ha_call_service")]))
        mcp = _server(
            LlmExposureMiddleware(policy_live=lambda: True), _tool("ha_get_state")
        )

        block = (await _stamps(mcp))["ha_get_state"][META_POLICY_KEY]

        assert block["rules"] == 1
        assert block["live"] is True
        assert block["deployment"] == "standalone"

    async def test_policy_block_defaults_without_queue_or_file(self) -> None:
        mcp = _server(LlmExposureMiddleware(), _tool("ha_get_state"))  # no policy_live

        block = (await _stamps(mcp))["ha_get_state"][META_POLICY_KEY]

        assert block["live"] is False
        assert block["rules"] == 0
