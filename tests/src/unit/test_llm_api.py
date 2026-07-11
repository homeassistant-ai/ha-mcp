"""Unit tests for the conversation-agent LLM API (issue #1745).

``llm_api`` exposes the in-process server's toolset as Home Assistant LLM
API(s) so conversation agents (and through them the Assist chat UI and voice)
can drive ha-mcp. These tests cover the registration lifecycle (exposure
modes, failure containment), the per-turn tool-list fetch with the server's
exposure stamp, the tool-search meta-tools (search + call-time enforcement),
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
    EXPOSURE_BOTH,
    EXPOSURE_FULL,
    EXPOSURE_TOOL_SEARCH,
    OPT_LLM_API_EXPOSURE,
)

_FULL_ID = f"{DOMAIN}-entry-1745"
_SEARCH_ID = f"{DOMAIN}-entry-1745-toolsearch"


def _make_hass() -> MagicMock:
    hass = MagicMock(name="hass")
    hass.data = {}

    async def _executor(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_executor)
    return hass


def _make_entry(options: dict[str, Any] | None = None) -> MagicMock:
    entry = MagicMock(name="entry")
    entry.entry_id = "entry-1745"
    entry.title = "HA-MCP Server"
    entry.options = options or {}
    return entry


def _tool_entry(
    name: str = "ha_search",
    *,
    exposed: bool = True,
    pinned: bool = False,
    stamped: bool = True,
    description: str | None = None,
) -> SimpleNamespace:
    meta = (
        {"ha_mcp": {"llm_api_exposed": exposed, "pinned": pinned}} if stamped else None
    )
    return SimpleNamespace(
        name=name,
        description=description if description is not None else f"{name} description",
        inputSchema={"type": "object", "properties": {"query": {"type": "string"}}},
        meta=meta,
    )


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


def _make_api(hass, mode: str = EXPOSURE_FULL) -> Any:
    return llm_api.HaMcpLlmApi(
        hass=hass,
        id=_FULL_ID,
        name="HA-MCP Server",
        server_url="http://127.0.0.1:9584/private_x",
        mode=mode,
    )


class TestRegistrationLifecycle:
    async def test_default_exposure_registers_tool_search_api(self, monkeypatch):
        monkeypatch.setattr(llm_api, "_import_mcp_sdk", lambda: None)
        hass = _make_hass()

        await llm_api.async_register_llm_api(
            hass, _make_entry(), port=9584, secret_path="/private_x"
        )

        apis = fake_llm_apis(hass)
        assert set(apis) == {_SEARCH_ID}
        api = apis[_SEARCH_ID]
        assert api.name == "HA-MCP Server (tool search)"
        assert api.mode == EXPOSURE_TOOL_SEARCH
        assert api.server_url == "http://127.0.0.1:9584/private_x"
        unsubs = hass.data[DOMAIN][DATA_LLM_API_UNSUB]
        assert len(unsubs) == 1 and all(callable(u) for u in unsubs)

    async def test_full_exposure_registers_full_api(self, monkeypatch):
        monkeypatch.setattr(llm_api, "_import_mcp_sdk", lambda: None)
        hass = _make_hass()
        entry = _make_entry({OPT_LLM_API_EXPOSURE: EXPOSURE_FULL})

        await llm_api.async_register_llm_api(
            hass, entry, port=9584, secret_path="/private_x"
        )

        apis = fake_llm_apis(hass)
        assert set(apis) == {_FULL_ID}
        assert apis[_FULL_ID].mode == EXPOSURE_FULL
        assert apis[_FULL_ID].name == "HA-MCP Server"

    async def test_both_exposure_registers_two_apis_one_server(self, monkeypatch):
        monkeypatch.setattr(llm_api, "_import_mcp_sdk", lambda: None)
        hass = _make_hass()
        entry = _make_entry({OPT_LLM_API_EXPOSURE: EXPOSURE_BOTH})

        await llm_api.async_register_llm_api(
            hass, entry, port=9584, secret_path="/private_x"
        )

        apis = fake_llm_apis(hass)
        assert set(apis) == {_FULL_ID, _SEARCH_ID}
        # One server: both registrations point at the same loopback URL.
        assert {a.server_url for a in apis.values()} == {
            "http://127.0.0.1:9584/private_x"
        }
        assert len(hass.data[DOMAIN][DATA_LLM_API_UNSUB]) == 2

    async def test_unknown_stored_mode_degrades_to_default(self, monkeypatch):
        monkeypatch.setattr(llm_api, "_import_mcp_sdk", lambda: None)
        hass = _make_hass()
        entry = _make_entry({OPT_LLM_API_EXPOSURE: "bogus"})

        await llm_api.async_register_llm_api(
            hass, entry, port=9584, secret_path="/private_x"
        )

        assert set(fake_llm_apis(hass)) == {_SEARCH_ID}

    async def test_unregister_removes_apis_and_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(llm_api, "_import_mcp_sdk", lambda: None)
        hass = _make_hass()
        entry = _make_entry({OPT_LLM_API_EXPOSURE: EXPOSURE_BOTH})

        await llm_api.async_register_llm_api(
            hass, entry, port=9584, secret_path="/private_x"
        )
        llm_api.async_unregister_llm_api(hass)

        assert fake_llm_apis(hass) == {}
        assert DATA_LLM_API_UNSUB not in hass.data[DOMAIN]
        # Second teardown (reload paths run it again) must be a no-op.
        llm_api.async_unregister_llm_api(hass)

    async def test_reregistration_replaces_stale_apis(self, monkeypatch):
        # A bring-up after a teardown that never ran (e.g. a crashed reload)
        # must replace the stale registrations instead of failing on the
        # duplicate ids.
        monkeypatch.setattr(llm_api, "_import_mcp_sdk", lambda: None)
        hass = _make_hass()

        await llm_api.async_register_llm_api(
            hass, _make_entry(), port=9584, secret_path="/private_x"
        )
        await llm_api.async_register_llm_api(
            hass, _make_entry(), port=9999, secret_path="/private_y"
        )

        apis = fake_llm_apis(hass)
        assert len(apis) == 1
        assert apis[_SEARCH_ID].server_url == "http://127.0.0.1:9999/private_y"

    @pytest.mark.parametrize(
        "exc_factory",
        [
            lambda: llm_api.HomeAssistantError("duplicate id"),
            lambda: RuntimeError("unexpected"),
        ],
        ids=["homeassistanterror", "unexpected-exception"],
    )
    async def test_registration_failure_is_contained(
        self, monkeypatch, caplog, exc_factory
    ):
        # "Never raises" must be literal: anything escaping this function
        # lands in the bring-up's outer `except Exception`, which tears the
        # ALREADY-RUNNING server down and files a "start" repair issue for a
        # cosmetic failure (review findings on #1782). Both the expected
        # HomeAssistantError and an arbitrary exception must be contained.
        monkeypatch.setattr(llm_api, "_import_mcp_sdk", lambda: None)
        hass = _make_hass()
        exc = exc_factory()

        def _raise(*_args):
            raise exc

        monkeypatch.setattr(llm_api.llm, "async_register_api", _raise)

        with caplog.at_level(logging.WARNING):
            await llm_api.async_register_llm_api(
                hass, _make_entry(), port=9584, secret_path="/private_x"
            )

        assert DATA_LLM_API_UNSUB not in hass.data.get(DOMAIN, {})
        assert "Could not register the HA-MCP LLM API" in caplog.text

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


class TestFullModeInstance:
    async def test_lists_exposed_tools_with_converted_schemas_and_prompt(
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
        # The server's own initialize instructions become the API prompt,
        # WITHOUT the tool-search discovery section in full mode.
        assert instance.api_prompt == "Use the skills-first workflow."

    async def test_hidden_tools_are_filtered_out(self, monkeypatch):
        # The server stamp is the exposure decision: a tool stamped
        # llm_api_exposed=False must be invisible to the agent even though it
        # is present on the raw MCP surface.
        hass = _make_hass()
        _fake_session(
            monkeypatch,
            tools=[
                _tool_entry("ha_get_state"),
                _tool_entry("ha_restart", exposed=False),
            ],
        )

        instance = await _make_api(hass).async_get_api_instance(
            llm_api.llm.LLMContext()
        )

        assert [t.name for t in instance.tools] == ["ha_get_state"]

    async def test_unstamped_server_falls_back_to_deny_list(self, monkeypatch, caplog):
        # Older server packages don't stamp exposure: the component applies
        # its conservative built-in deny-list instead, loudly.
        hass = _make_hass()
        _fake_session(
            monkeypatch,
            tools=[
                _tool_entry("ha_get_state", stamped=False),
                _tool_entry("ha_restart", stamped=False),
                _tool_entry("ha_write_file", stamped=False),
                _tool_entry("ha_dev_manage_server", stamped=False),
            ],
        )

        with caplog.at_level(logging.WARNING):
            instance = await _make_api(hass).async_get_api_instance(
                llm_api.llm.LLMContext()
            )

        assert [t.name for t in instance.tools] == ["ha_get_state"]
        assert "does not stamp LLM-API exposure metadata" in caplog.text

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

    async def test_group_wrapped_bug_propagates_from_list(self, monkeypatch):
        # The SDK's task groups wrap in-session failures indiscriminately —
        # a group carrying a genuine bug must NOT be relabeled as "could not
        # reach the server" (review finding on #1782).
        hass = _make_hass()
        _fake_session(
            monkeypatch,
            raise_on_open=ExceptionGroup("boom", [ValueError("a bug")]),
        )

        with pytest.raises(ExceptionGroup):
            await _make_api(hass).async_get_api_instance(llm_api.llm.LLMContext())


class TestToolSearchModeInstance:
    def _tools(self) -> list[SimpleNamespace]:
        return [
            _tool_entry("ha_search", pinned=True),
            _tool_entry("ha_get_state", description="Get entity state"),
            _tool_entry("ha_config_set_automation", description="Create automation"),
            _tool_entry("ha_restart", exposed=False),
        ]

    async def _instance(self, monkeypatch, tools=None):
        hass = _make_hass()
        _fake_session(monkeypatch, tools=tools or self._tools())
        return await _make_api(hass, mode=EXPOSURE_TOOL_SEARCH).async_get_api_instance(
            llm_api.llm.LLMContext()
        )

    async def test_compact_catalog_shape(self, monkeypatch):
        instance = await self._instance(monkeypatch)

        names = [t.name for t in instance.tools]
        # Pinned exposed tool mirrored directly; hidden + unpinned ones are
        # only reachable through the meta-tools.
        assert names == ["ha_search", "ha_search_tools", "ha_call_tool"]
        assert "Tool Discovery" in instance.api_prompt

    async def test_search_finds_exposed_tools_only(self, monkeypatch):
        instance = await self._instance(monkeypatch)
        search = next(t for t in instance.tools if t.name == "ha_search_tools")

        result = await search.async_call(
            _make_hass(),
            llm_api.llm.ToolInput("ha_search_tools", {"query": "create automation"}),
            llm_api.llm.LLMContext(),
        )

        names = [r["name"] for r in result["results"]]
        assert "ha_config_set_automation" in names
        # Hidden tools never appear in search results.
        assert "ha_restart" not in names
        # Results carry the schema the agent needs for ha_call_tool.
        assert all("input_schema" in r for r in result["results"])

    async def test_search_with_no_match_guides_retry(self, monkeypatch):
        instance = await self._instance(monkeypatch)
        search = next(t for t in instance.tools if t.name == "ha_search_tools")

        result = await search.async_call(
            _make_hass(),
            llm_api.llm.ToolInput("ha_search_tools", {"query": "zzzznothing"}),
            llm_api.llm.LLMContext(),
        )

        assert result["results"] == []
        assert "message" in result

    async def test_call_tool_forwards_exposed_tool(self, monkeypatch):
        instance = await self._instance(monkeypatch)
        call = next(t for t in instance.tools if t.name == "ha_call_tool")
        forward = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        monkeypatch.setattr(llm_api, "_forward_tool_call", forward)

        result = await call.async_call(
            _make_hass(),
            llm_api.llm.ToolInput(
                "ha_call_tool",
                {"name": "ha_get_state", "arguments": {"entity_id": "sun.sun"}},
            ),
            llm_api.llm.LLMContext(),
        )

        forward.assert_awaited_once_with(
            "http://127.0.0.1:9584/private_x",
            "ha_get_state",
            {"entity_id": "sun.sun"},
        )
        assert result == {"content": [{"type": "text", "text": "ok"}]}

    @pytest.mark.parametrize(
        "target", ["ha_restart", "ha_totally_made_up"], ids=["hidden", "nonexistent"]
    )
    async def test_call_tool_unknown_for_hidden_and_nonexistent(
        self, monkeypatch, target
    ):
        # Call-time enforcement: a hidden tool gets EXACTLY the same
        # unknown-tool answer a nonexistent tool gets — hiding without
        # enforcement would let a model that guesses names skirt the
        # exposure settings, and a distinct error would leak existence.
        instance = await self._instance(monkeypatch)
        call = next(t for t in instance.tools if t.name == "ha_call_tool")
        forward = AsyncMock()
        monkeypatch.setattr(llm_api, "_forward_tool_call", forward)

        result = await call.async_call(
            _make_hass(),
            llm_api.llm.ToolInput("ha_call_tool", {"name": target, "arguments": {}}),
            llm_api.llm.LLMContext(),
        )

        forward.assert_not_awaited()
        assert result["error"] == f"Unknown tool '{target}'."

    async def test_server_side_search_tool_name_is_excluded(self, monkeypatch):
        # A server running its own ENABLE_TOOL_SEARCH registers a real
        # ha_search_tools — never mirror/search it alongside the synthesized
        # one: one name, one behavior.
        tools = [*self._tools(), _tool_entry("ha_search_tools", pinned=True)]
        instance = await self._instance(monkeypatch, tools=tools)

        search = next(t for t in instance.tools if t.name == "ha_search_tools")
        assert isinstance(search, llm_api.HaMcpSearchTool)
        result = await search.async_call(
            _make_hass(),
            llm_api.llm.ToolInput("ha_search_tools", {"query": "search tools"}),
            llm_api.llm.LLMContext(),
        )
        assert "ha_search_tools" not in [r["name"] for r in result["results"]]


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

    async def test_unwrapped_httpx_error_is_mapped(self, monkeypatch):
        # httpx errors do NOT inherit from OSError and can escape a session
        # call unwrapped (Gemini review finding on #1782); they must map to
        # HomeAssistantError like every other transport failure.
        import httpx

        _fake_session(monkeypatch, raise_on_open=httpx.ConnectError("refused"))

        with pytest.raises(llm_api.HomeAssistantError, match="ha_search"):
            await self._tool().async_call(
                _make_hass(),
                llm_api.llm.ToolInput("ha_search", {}),
                llm_api.llm.LLMContext(),
            )

    async def test_protocol_mcperror_is_mapped(self, monkeypatch):
        # Protocol-level JSON-RPC errors surface as McpError (HA core's mcp
        # integration maps these the same way).
        from mcp import McpError
        from mcp.types import ErrorData

        _fake_session(
            monkeypatch,
            raise_on_open=McpError(ErrorData(code=-32000, message="boom")),
        )

        with pytest.raises(llm_api.HomeAssistantError, match="ha_search"):
            await self._tool().async_call(
                _make_hass(),
                llm_api.llm.ToolInput("ha_search", {}),
                llm_api.llm.LLMContext(),
            )

    async def test_non_transport_bug_propagates(self, monkeypatch):
        # A genuine bug (TypeError, ValueError, ...) must NOT be swallowed
        # into a friendly transport message — it should surface as itself.
        _fake_session(monkeypatch, raise_on_open=ValueError("a bug"))

        with pytest.raises(ValueError, match="a bug"):
            await self._tool().async_call(
                _make_hass(),
                llm_api.llm.ToolInput("ha_search", {}),
                llm_api.llm.LLMContext(),
            )

    @pytest.mark.parametrize(
        "group",
        [
            lambda: ExceptionGroup("boom", [ValueError("a bug")]),
            lambda: ExceptionGroup("boom", [OSError("refused"), ValueError("a bug")]),
            lambda: ExceptionGroup(
                "outer", [ExceptionGroup("inner", [TypeError("a bug")])]
            ),
        ],
        ids=["bug-only", "mixed-transport-and-bug", "nested-group-bug"],
    )
    async def test_group_wrapped_bug_propagates(self, monkeypatch, group):
        # anyio task groups wrap whatever failed inside them — a group is a
        # transport failure only when EVERY leaf is one. Any genuine bug in
        # the group (even nested, even alongside real transport errors) must
        # propagate with its traceback instead of being remapped (review
        # finding on #1782).
        _fake_session(monkeypatch, raise_on_open=group())

        with pytest.raises(ExceptionGroup):
            await self._tool().async_call(
                _make_hass(),
                llm_api.llm.ToolInput("ha_search", {}),
                llm_api.llm.LLMContext(),
            )

    async def test_slow_tool_call_times_out_as_homeassistanterror(self, monkeypatch):
        _fake_session(monkeypatch, call_result=MagicMock(), delay=0.2)
        monkeypatch.setattr(llm_api, "_CALL_TOOL_TIMEOUT_SECONDS", 0.01)

        with pytest.raises(llm_api.HomeAssistantError, match="ha_search"):
            await self._tool().async_call(
                _make_hass(),
                llm_api.llm.ToolInput("ha_search", {}),
                llm_api.llm.LLMContext(),
            )


class TestPreRenameSdkFallback:
    async def test_falls_back_to_deprecated_client_name(self, monkeypatch):
        # A pip-spec override can install an older ha-mcp whose fastmcp pins
        # a pre-rename mcp SDK: mcp.client.streamable_http then exposes only
        # streamablehttp_client. _mcp_session must import-fall-back to it and
        # wire the session identically. Faked at the sys.modules level so the
        # REAL import selection in _mcp_session runs (the other tests patch
        # _mcp_session wholesale and never exercise it).
        import sys
        from types import ModuleType

        opened: dict[str, Any] = {}

        @asynccontextmanager
        async def _old_name_client(url):
            opened["url"] = url
            yield "read-stream", "write-stream", lambda: None

        fake_transport = ModuleType("mcp.client.streamable_http")
        fake_transport.streamablehttp_client = _old_name_client  # type: ignore[attr-defined]
        # Deliberately NO streamable_http_client attribute.

        class _FakeClientSession:
            def __init__(self, read_stream, write_stream):
                opened["streams"] = (read_stream, write_stream)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def initialize(self):
                return SimpleNamespace(instructions="from old SDK")

        fake_session_mod = ModuleType("mcp.client.session")
        fake_session_mod.ClientSession = _FakeClientSession  # type: ignore[attr-defined]

        monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", fake_transport)
        monkeypatch.setitem(sys.modules, "mcp.client.session", fake_session_mod)

        async with llm_api._mcp_session("http://127.0.0.1:9584/private_x") as (
            session,
            init,
        ):
            assert isinstance(session, _FakeClientSession)
            assert init.instructions == "from old SDK"

        assert opened["url"] == "http://127.0.0.1:9584/private_x"
        assert opened["streams"] == ("read-stream", "write-stream")
