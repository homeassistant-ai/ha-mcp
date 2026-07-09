"""Unit tests for the conversation-agent LLM API (issue #1745).

``llm_api`` exposes the in-process server's toolset as a Home Assistant LLM
API so conversation agents (and through them the Assist chat UI and voice)
can drive ha-mcp. These tests cover the registration lifecycle the bring-up /
teardown paths rely on, the per-turn tool-list fetch and schema conversion,
and the loopback transport error mapping — all hermetically: Home Assistant
is stubbed via ``_embedded_stubs`` and the MCP client session is faked at the
``_mcp_session`` seam (the SDK itself is exercised by the embedded e2e test).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import fake_llm_apis, install

install()

import custom_components.ha_mcp_tools.llm_api as llm_api  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DATA_LLM_API_UNSUB,
    DOMAIN,
)


def _make_hass() -> MagicMock:
    hass = MagicMock(name="hass")
    hass.data = {}

    async def _executor(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_executor)
    return hass


def _make_entry() -> MagicMock:
    entry = MagicMock(name="entry")
    entry.entry_id = "entry-1745"
    entry.title = "HA-MCP Server"
    return entry


def _fake_session(
    monkeypatch,
    *,
    tools: list[Any] | None = None,
    instructions: str | None = "Use the skills-first workflow.",
    call_result: Any = None,
    raise_on_open: BaseException | None = None,
    delay: float = 0.0,
) -> SimpleNamespace:
    """Patch ``llm_api._mcp_session`` with a fake and return the session."""
    session = SimpleNamespace(
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=tools or [])),
        call_tool=AsyncMock(return_value=call_result),
    )
    init_result = SimpleNamespace(instructions=instructions)

    @asynccontextmanager
    async def fake_mcp_session(url):
        session.url = url
        if raise_on_open is not None:
            raise raise_on_open
        if delay:
            await asyncio.sleep(delay)
        yield session, init_result

    monkeypatch.setattr(llm_api, "_mcp_session", fake_mcp_session)
    return session


def _tool_entry(name: str = "ha_search") -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description=f"{name} description",
        inputSchema={"type": "object", "properties": {"query": {"type": "string"}}},
    )


class TestRegistrationLifecycle:
    async def test_register_stores_unsub_and_registers_api(self, monkeypatch):
        monkeypatch.setattr(llm_api, "_import_mcp_sdk", lambda: None)
        hass = _make_hass()
        entry = _make_entry()

        await llm_api.async_register_llm_api(
            hass, entry, port=9584, secret_path="/private_x"
        )

        apis = fake_llm_apis(hass)
        assert set(apis) == {f"{DOMAIN}-entry-1745"}
        api = apis[f"{DOMAIN}-entry-1745"]
        assert api.name == "HA-MCP Server"
        assert api.server_url == "http://127.0.0.1:9584/private_x"
        assert callable(hass.data[DOMAIN][DATA_LLM_API_UNSUB])

    async def test_unregister_removes_api_and_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(llm_api, "_import_mcp_sdk", lambda: None)
        hass = _make_hass()

        await llm_api.async_register_llm_api(
            hass, _make_entry(), port=9584, secret_path="/private_x"
        )
        llm_api.async_unregister_llm_api(hass)

        assert fake_llm_apis(hass) == {}
        assert DATA_LLM_API_UNSUB not in hass.data[DOMAIN]
        # Second teardown (reload paths run it again) must be a no-op.
        llm_api.async_unregister_llm_api(hass)

    async def test_reregistration_replaces_stale_api(self, monkeypatch):
        # A bring-up after a teardown that never ran (e.g. a crashed reload)
        # must replace the stale registration instead of failing on the
        # duplicate id.
        monkeypatch.setattr(llm_api, "_import_mcp_sdk", lambda: None)
        hass = _make_hass()
        entry = _make_entry()

        await llm_api.async_register_llm_api(
            hass, entry, port=9584, secret_path="/private_x"
        )
        await llm_api.async_register_llm_api(
            hass, entry, port=9999, secret_path="/private_y"
        )

        apis = fake_llm_apis(hass)
        assert len(apis) == 1
        assert apis[f"{DOMAIN}-entry-1745"].server_url == (
            "http://127.0.0.1:9999/private_y"
        )

    async def test_missing_sdk_skips_registration(self, monkeypatch, caplog):
        # The mcp client SDK arrives with the runtime-installed server
        # package; a build without it must skip the feature with a warning,
        # never fail the (already running) server bring-up.
        def _boom() -> None:
            raise ImportError("No module named 'mcp'")

        monkeypatch.setattr(llm_api, "_import_mcp_sdk", _boom)
        hass = _make_hass()

        with caplog.at_level(logging.WARNING):
            await llm_api.async_register_llm_api(
                hass, _make_entry(), port=9584, secret_path="/private_x"
            )

        assert fake_llm_apis(hass) == {}
        assert DATA_LLM_API_UNSUB not in hass.data.get(DOMAIN, {})
        assert "no importable 'mcp' client SDK" in caplog.text


def _make_api(hass) -> Any:
    return llm_api.HaMcpLlmApi(
        hass=hass,
        id=f"{DOMAIN}-entry-1745",
        name="HA-MCP Server",
        server_url="http://127.0.0.1:9584/private_x",
    )


class TestApiInstance:
    async def test_lists_tools_with_converted_schemas_and_server_prompt(
        self, monkeypatch
    ):
        hass = _make_hass()
        session = _fake_session(
            monkeypatch,
            tools=[_tool_entry("ha_search"), _tool_entry("ha_get_state")],
            instructions="Use the skills-first workflow.",
        )

        instance = await _make_api(hass).async_get_api_instance(
            llm_api.llm.LLMContext()
        )

        assert session.url == "http://127.0.0.1:9584/private_x"
        assert [t.name for t in instance.tools] == ["ha_search", "ha_get_state"]
        assert instance.tools[0].description == "ha_search description"
        # The stubbed convert_to_voluptuous wraps the input schema verbatim.
        assert instance.tools[0].parameters == {
            "_converted": _tool_entry("ha_search").inputSchema
        }
        # The server's own initialize instructions become the API prompt.
        assert instance.api_prompt == "Use the skills-first workflow."

    async def test_prompt_falls_back_when_server_has_no_instructions(self, monkeypatch):
        hass = _make_hass()
        _fake_session(monkeypatch, tools=[_tool_entry()], instructions=None)

        instance = await _make_api(hass).async_get_api_instance(
            llm_api.llm.LLMContext()
        )

        assert instance.api_prompt == llm_api._FALLBACK_API_PROMPT

    async def test_unconvertible_schema_skips_that_tool_only(self, monkeypatch, caplog):
        hass = _make_hass()
        _fake_session(
            monkeypatch, tools=[_tool_entry("ha_bad"), _tool_entry("ha_good")]
        )

        calls = {"n": 0}

        def _convert_first_fails(schema):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("unsupported schema")
            return {"_converted": schema}

        monkeypatch.setattr(llm_api, "convert_to_voluptuous", _convert_first_fails)

        with caplog.at_level(logging.WARNING):
            instance = await _make_api(hass).async_get_api_instance(
                llm_api.llm.LLMContext()
            )

        assert [t.name for t in instance.tools] == ["ha_good"]
        assert "Skipping tool ha_bad" in caplog.text

    async def test_server_unreachable_raises_homeassistanterror(self, monkeypatch):
        hass = _make_hass()
        _fake_session(monkeypatch, raise_on_open=OSError("connection refused"))

        with pytest.raises(llm_api.HomeAssistantError, match="Could not reach"):
            await _make_api(hass).async_get_api_instance(llm_api.llm.LLMContext())

    async def test_slow_server_times_out_as_homeassistanterror(self, monkeypatch):
        hass = _make_hass()
        _fake_session(monkeypatch, tools=[_tool_entry()], delay=0.2)
        monkeypatch.setattr(llm_api, "_LIST_TOOLS_TIMEOUT_SECONDS", 0.01)

        with pytest.raises(llm_api.HomeAssistantError, match="Could not reach"):
            await _make_api(hass).async_get_api_instance(llm_api.llm.LLMContext())


class TestToolCall:
    def _tool(self) -> Any:
        return llm_api.HaMcpTool(
            "ha_search",
            "Search",
            {"_converted": {}},
            "http://127.0.0.1:9584/private_x",
        )

    async def test_call_passes_args_and_returns_model_dump(self, monkeypatch):
        result = MagicMock(name="call_result")
        result.model_dump.return_value = {"content": [{"type": "text", "text": "ok"}]}
        session = _fake_session(monkeypatch, call_result=result)

        out = await self._tool().async_call(
            _make_hass(),
            llm_api.llm.ToolInput("ha_search", {"query": "kitchen light"}),
            llm_api.llm.LLMContext(),
        )

        session.call_tool.assert_awaited_once_with(
            "ha_search", {"query": "kitchen light"}
        )
        result.model_dump.assert_called_once_with(exclude_unset=True, exclude_none=True)
        assert out == {"content": [{"type": "text", "text": "ok"}]}

    async def test_transport_error_raises_homeassistanterror(self, monkeypatch):
        _fake_session(monkeypatch, raise_on_open=OSError("connection refused"))

        with pytest.raises(llm_api.HomeAssistantError, match="ha_search"):
            await self._tool().async_call(
                _make_hass(),
                llm_api.llm.ToolInput("ha_search", {}),
                llm_api.llm.LLMContext(),
            )

    async def test_exception_group_from_transport_is_mapped(self, monkeypatch):
        # The SDK's anyio task groups surface failures as ExceptionGroup.
        _fake_session(
            monkeypatch,
            raise_on_open=ExceptionGroup("boom", [OSError("refused")]),
        )

        with pytest.raises(llm_api.HomeAssistantError, match="ha_search"):
            await self._tool().async_call(
                _make_hass(),
                llm_api.llm.ToolInput("ha_search", {}),
                llm_api.llm.LLMContext(),
            )
