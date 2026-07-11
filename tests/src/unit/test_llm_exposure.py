"""Unit tests for the conversation-agent LLM API exposure layer (#1745).

``ha_mcp.llm_exposure`` decides which tools the ha_mcp_tools custom component
may offer to Home Assistant conversation agents, and stamps that decision
into every ``tools/list`` entry as ``_meta.ha_mcp``. These tests cover the
deny-by-default policy (beta tag / dev prefix / restart-reload-backup), the
override semantics, and the stamping middleware — including its must-never-
break-tools/list containment.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp.tools import Tool

from ha_mcp import llm_exposure
from ha_mcp.llm_exposure import (
    LLM_API_CONFIG_KEY,
    META_EXPOSED_KEY,
    META_NAMESPACE,
    META_PINNED_KEY,
    LlmExposureMiddleware,
    default_llm_api_exposed,
    effective_llm_api_exposed,
    load_llm_api_overrides,
)


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
        ],
    )
    def test_default_policy(self, name, tags, expected):
        assert default_llm_api_exposed(name, tags) is expected

    def test_override_wins_both_ways(self):
        # A user can expose a default-hidden tool and hide a default-exposed
        # one; non-bool garbage in the store is ignored, not honored.
        assert effective_llm_api_exposed("ha_restart", set(), {"ha_restart": True})
        assert not effective_llm_api_exposed(
            "ha_get_state", set(), {"ha_get_state": False}
        )
        assert not effective_llm_api_exposed("ha_restart", set(), {"ha_restart": "yes"})


class TestOverridesLoading:
    def test_loads_only_bool_values(self, monkeypatch):
        # load_llm_api_overrides imports load_tool_config lazily from
        # settings_ui — patch it there.
        import ha_mcp.settings_ui as settings_ui

        monkeypatch.setattr(
            settings_ui,
            "load_tool_config",
            lambda: {
                LLM_API_CONFIG_KEY: {
                    "ha_restart": True,
                    "ha_get_state": False,
                    "ha_bad": "nope",
                }
            },
        )
        assert load_llm_api_overrides() == {
            "ha_restart": True,
            "ha_get_state": False,
        }

    def test_missing_or_malformed_key_is_empty(self, monkeypatch):
        import ha_mcp.settings_ui as settings_ui

        monkeypatch.setattr(settings_ui, "load_tool_config", dict)
        assert load_llm_api_overrides() == {}
        monkeypatch.setattr(
            settings_ui, "load_tool_config", lambda: {LLM_API_CONFIG_KEY: ["x"]}
        )
        assert load_llm_api_overrides() == {}


class TestPinnedNames:
    def test_mirrors_server_tool_search_semantics(self, monkeypatch):
        import ha_mcp.settings_ui as settings_ui
        from ha_mcp.transforms import DEFAULT_PINNED_TOOLS

        default_pinned = next(iter(DEFAULT_PINNED_TOOLS))
        monkeypatch.setattr(
            settings_ui,
            "effective_tool_config",
            lambda: {
                "tools": {
                    "ha_custom_pin": "pinned",
                    default_pinned: "enabled",  # explicit unpin of a default
                }
            },
        )
        pinned = llm_exposure._pinned_tool_names()
        assert "ha_custom_pin" in pinned
        assert default_pinned not in pinned
        # Untouched defaults stay pinned.
        assert (set(DEFAULT_PINNED_TOOLS) - {default_pinned}) <= pinned


def _tool(name: str, tags: set[str] | None = None) -> Tool:
    def _noop() -> None:
        return None

    return Tool.from_function(_noop, name=name, tags=tags or set())


def _context() -> SimpleNamespace:
    return SimpleNamespace(message=SimpleNamespace())


class TestMiddleware:
    async def test_stamps_exposure_and_pinned(self, monkeypatch):
        mw = LlmExposureMiddleware()
        monkeypatch.setattr(
            llm_exposure, "load_llm_api_overrides", lambda: {"ha_restart": True}
        )
        monkeypatch.setattr(llm_exposure, "_pinned_tool_names", lambda: {"ha_search"})
        tools = [
            _tool("ha_search"),
            _tool("ha_restart"),
            _tool("ha_write_file", tags={"beta"}),
        ]
        result = await mw.on_list_tools(_context(), AsyncMock(return_value=tools))

        by_name = {t.name: t.meta[META_NAMESPACE] for t in result}
        assert by_name["ha_search"] == {META_EXPOSED_KEY: True, META_PINNED_KEY: True}
        # Override exposes the default-hidden restart tool.
        assert by_name["ha_restart"][META_EXPOSED_KEY] is True
        assert by_name["ha_restart"][META_PINNED_KEY] is False
        # Beta tag stays hidden without an override.
        assert by_name["ha_write_file"][META_EXPOSED_KEY] is False

    async def test_preserves_existing_meta(self, monkeypatch):
        mw = LlmExposureMiddleware()
        monkeypatch.setattr(llm_exposure, "load_llm_api_overrides", dict)
        monkeypatch.setattr(llm_exposure, "_pinned_tool_names", set)
        tool = _tool("ha_get_state").model_copy(update={"meta": {"other": {"k": 1}}})
        result = await mw.on_list_tools(_context(), AsyncMock(return_value=[tool]))
        assert result[0].meta["other"] == {"k": 1}
        assert result[0].meta[META_NAMESPACE][META_EXPOSED_KEY] is True

    async def test_settings_read_failure_stamps_defaults(self, monkeypatch, caplog):
        # Stamping must never break tools/list — a failed settings read
        # falls back to pure defaults with a visible warning.
        mw = LlmExposureMiddleware()

        def _boom():
            raise OSError("disk gone")

        monkeypatch.setattr(llm_exposure, "load_llm_api_overrides", _boom)
        tools = [_tool("ha_get_state"), _tool("ha_restart")]
        result = await mw.on_list_tools(_context(), AsyncMock(return_value=tools))

        by_name = {t.name: t.meta[META_NAMESPACE] for t in result}
        assert by_name["ha_get_state"][META_EXPOSED_KEY] is True
        assert by_name["ha_restart"][META_EXPOSED_KEY] is False
        assert "Could not read LLM-API exposure settings" in caplog.text

    async def test_ttl_cache_reuses_settings_reads(self, monkeypatch):
        mw = LlmExposureMiddleware()
        calls = {"n": 0}

        def _counting_overrides():
            calls["n"] += 1
            return {}

        monkeypatch.setattr(llm_exposure, "load_llm_api_overrides", _counting_overrides)
        monkeypatch.setattr(llm_exposure, "_pinned_tool_names", set)
        ctx = _context()
        await mw.on_list_tools(ctx, AsyncMock(return_value=[_tool("ha_a")]))
        await mw.on_list_tools(ctx, AsyncMock(return_value=[_tool("ha_b")]))
        assert calls["n"] == 1

    async def test_ttl_expiry_rereads_settings(self, monkeypatch):
        mw = LlmExposureMiddleware()
        calls = {"n": 0}

        def _counting_overrides():
            calls["n"] += 1
            return {}

        monkeypatch.setattr(llm_exposure, "load_llm_api_overrides", _counting_overrides)
        monkeypatch.setattr(llm_exposure, "_pinned_tool_names", set)
        monkeypatch.setattr(llm_exposure, "_OVERRIDES_TTL_SECONDS", 0.0)
        ctx = _context()
        await mw.on_list_tools(ctx, AsyncMock(return_value=[_tool("ha_a")]))
        await mw.on_list_tools(ctx, AsyncMock(return_value=[_tool("ha_b")]))
        assert calls["n"] == 2

    async def test_settings_read_failure_serves_last_known_good(self, monkeypatch):
        # Fail-direction guard (review finding): a failed settings read must
        # NOT re-expose tools the user explicitly hid — the middleware keeps
        # serving the last successful read instead of pure defaults.
        mw = LlmExposureMiddleware()
        monkeypatch.setattr(
            llm_exposure, "load_llm_api_overrides", lambda: {"ha_get_state": False}
        )
        monkeypatch.setattr(llm_exposure, "_pinned_tool_names", set)
        ctx = _context()
        first = await mw.on_list_tools(
            ctx, AsyncMock(return_value=[_tool("ha_get_state")])
        )
        assert first[0].meta[META_NAMESPACE][META_EXPOSED_KEY] is False

        def _boom():
            raise OSError("disk gone")

        monkeypatch.setattr(llm_exposure, "load_llm_api_overrides", _boom)
        monkeypatch.setattr(llm_exposure, "_OVERRIDES_TTL_SECONDS", 0.0)
        second = await mw.on_list_tools(
            ctx, AsyncMock(return_value=[_tool("ha_get_state")])
        )
        # The user-hidden tool STAYS hidden on the stale data.
        assert second[0].meta[META_NAMESPACE][META_EXPOSED_KEY] is False
