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
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import fake_llm_apis, install

install()

from custom_components.ha_mcp_tools import llm_api  # noqa: E402
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
        """Stand in for ``_mcp_session``: record the url, yield the fake session."""
        session.url = url
        if raise_on_open is not None:
            raise raise_on_open
        if delay:
            await asyncio.sleep(delay)
        yield session, init_result

    monkeypatch.setattr(llm_api, "_mcp_session", fake_mcp_session)
    return session


def _spy_httpx_async_client(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``httpx.AsyncClient`` to record its constructor kwargs.

    ``verify`` has no public accessor on a constructed client (it is only
    reachable via private ``_transport`` internals), so pinning it needs
    capturing the call itself rather than introspecting the result. Returns
    the dict the next construction's kwargs land in; a real subclass (not a
    bare Mock) so callers still get a fully functioning client, since
    ``AsyncExitStack`` drives real ``__aenter__``/``aclose`` on it.
    """
    import httpx

    captured: dict[str, Any] = {}

    class _SpyAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            """Record the constructor kwargs, then build a real client."""
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _SpyAsyncClient)
    return captured


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
        assert "required LLM dependency is not importable" in caplog.text


class TestSchemaConversionCompatibility:
    @pytest.fixture(autouse=True)
    def _clear_schema_converter_cache(self):
        llm_api._schema_converter.cache_clear()
        yield
        llm_api._schema_converter.cache_clear()

    def test_prefers_stable_core_converter_when_available(self, monkeypatch):
        schema = {"type": "object"}
        legacy = SimpleNamespace(
            convert_to_voluptuous=lambda value: {"voluptuous_openapi": value}
        )

        def _import_module(name):
            assert name == "voluptuous_openapi"
            return legacy

        monkeypatch.setattr(llm_api.importlib, "import_module", _import_module)

        assert llm_api.convert_to_voluptuous(schema) == {"voluptuous_openapi": schema}

    def test_falls_back_to_probatio_on_newer_core(self, monkeypatch):
        schema = {"type": "object"}
        probatio = SimpleNamespace(from_openapi=lambda value: {"probatio": value})

        def _import_module(name):
            if name == "voluptuous_openapi":
                raise ModuleNotFoundError(
                    "No module named 'voluptuous_openapi'",
                    name="voluptuous_openapi",
                )
            assert name == "probatio"
            return probatio

        monkeypatch.setattr(llm_api.importlib, "import_module", _import_module)

        assert llm_api.convert_to_voluptuous(schema) == {"probatio": schema}

    def test_the_fallback_announces_which_codec_is_converting(
        self, monkeypatch, caplog
    ):
        """Nothing else reports that the component runs on the other codec.

        A requirement that failed to install never gets here: Home Assistant
        logs that itself and abandons the integration before importing it.
        What does get here -- skip_pip, or a deps tree that lost the package
        -- produces no message anywhere else, and the conversion then
        succeeds quietly with a codec that retypes integers.
        """
        probatio = SimpleNamespace(from_openapi=lambda value: value)

        def _import_module(name):
            if name == "voluptuous_openapi":
                raise ModuleNotFoundError(
                    "No module named 'voluptuous_openapi'",
                    name="voluptuous_openapi",
                )
            return probatio

        monkeypatch.setattr(llm_api.importlib, "import_module", _import_module)

        with caplog.at_level(logging.DEBUG, logger=llm_api._LOGGER.name):
            llm_api.convert_to_voluptuous({"type": "object"})

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "voluptuous-openapi" in warnings[0].getMessage()

    def test_reraises_nested_module_not_found(self, monkeypatch):
        def _import_module(name):
            assert name == "voluptuous_openapi"
            raise ModuleNotFoundError(
                "No module named 'legacy_dependency'",
                name="legacy_dependency",
            )

        monkeypatch.setattr(llm_api.importlib, "import_module", _import_module)

        with pytest.raises(ModuleNotFoundError, match="legacy_dependency"):
            llm_api.convert_to_voluptuous({"type": "object"})

    async def test_converter_import_is_warmed_once_off_event_loop(self, monkeypatch):
        main_thread = threading.get_ident()
        imports: list[tuple[str, int]] = []
        legacy = SimpleNamespace(
            convert_to_voluptuous=lambda value: {"voluptuous_openapi": value}
        )

        def _import_module(name):
            imports.append((name, threading.get_ident()))
            if name.startswith("mcp."):
                return SimpleNamespace()
            if name == "voluptuous_openapi":
                return legacy
            if name == "probatio":
                raise ModuleNotFoundError("No module named 'probatio'", name="probatio")
            raise AssertionError(name)

        async def _executor(func, *args):
            return await asyncio.to_thread(func, *args)

        hass = _make_hass()
        hass.async_add_executor_job = AsyncMock(side_effect=_executor)
        monkeypatch.setattr(llm_api.importlib, "import_module", _import_module)

        assert await llm_api.async_probe_mcp_sdk(hass)
        schema = {"type": "object"}
        assert llm_api.convert_to_voluptuous(schema) == {"voluptuous_openapi": schema}
        assert llm_api.convert_to_voluptuous(schema) == {"voluptuous_openapi": schema}

        assert [name for name, _ in imports] == [
            "mcp.client.session",
            "mcp.client.streamable_http",
            "voluptuous_openapi",
        ]
        assert all(thread_id != main_thread for _, thread_id in imports)


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
        hass = _make_hass()

        result = await call.async_call(
            hass,
            llm_api.llm.ToolInput(
                "ha_call_tool",
                {"name": "ha_get_state", "arguments": {"entity_id": "sun.sun"}},
            ),
            llm_api.llm.LLMContext(),
        )

        forward.assert_awaited_once_with(
            hass,
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


class TestMetaKeyContract:
    def test_component_meta_keys_match_server(self):
        # The stamp keys are duplicated because the component must never
        # import ha_mcp at runtime — but the TEST tier can import both, so
        # the keep-in-sync comment is enforced mechanically here.
        from ha_mcp.llm_exposure import (
            META_EXPOSED_KEY,
            META_NAMESPACE,
            META_PINNED_KEY,
        )

        assert llm_api._META_NAMESPACE == META_NAMESPACE
        assert llm_api._META_EXPOSED_KEY == META_EXPOSED_KEY
        assert llm_api._META_PINNED_KEY == META_PINNED_KEY


class TestModeDefault:
    async def test_omitted_mode_builds_tool_search_instance(self, monkeypatch):
        # The dataclass default is the compact/safe shape (review finding:
        # a full-catalog default made an omitted mode maximally exposed).
        hass = _make_hass()
        _fake_session(monkeypatch, tools=[_tool_entry("ha_get_state")])
        api = llm_api.HaMcpLlmApi(
            hass=hass,
            id=_SEARCH_ID,
            name="HA-MCP Server (tool search)",
            server_url="http://127.0.0.1:9584/private_x",
        )

        instance = await api.async_get_api_instance(llm_api.llm.LLMContext())

        assert [t.name for t in instance.tools] == ["ha_search_tools", "ha_call_tool"]
        assert "Tool Discovery" in instance.api_prompt

    async def test_unknown_mode_degrades_to_tool_search(self, monkeypatch):
        hass = _make_hass()
        _fake_session(monkeypatch, tools=[_tool_entry("ha_get_state")])
        api = _make_api(hass, mode="bogus")

        instance = await api.async_get_api_instance(llm_api.llm.LLMContext())

        assert [t.name for t in instance.tools] == ["ha_search_tools", "ha_call_tool"]


class TestLoopbackHttpClientTimeout:
    async def test_sdk_receives_a_dedicated_client_with_generous_timeout(
        self, monkeypatch
    ):
        """_mcp_session's own client must carry the generous timeout, not httpx's default."""
        # Regression test (kpop-timeout investigation): _mcp_session used to
        # hand the SDK Home Assistant's shared httpx client
        # (helpers.httpx_client.get_async_client). HA never configures that
        # client with an explicit timeout, so it silently carried httpx's own
        # hardcoded 5-second default — and the SDK applies no timeout of its
        # own when a caller-provided client is passed, so that 5s became the
        # REAL wire-level ceiling for every tool call regardless of how
        # generous _CALL_TOOL_TIMEOUT_SECONDS looked. _mcp_session must
        # instead build its own client with an explicit, generous timeout.
        # Faked at the sys.modules level so _mcp_session's REAL wiring runs.
        import sys
        from types import ModuleType

        import httpx

        opened: dict[str, Any] = {}
        constructed_kwargs = _spy_httpx_async_client(monkeypatch)

        @asynccontextmanager
        async def _canonical_client(url, http_client=None):
            """Stand in for the SDK's own ``streamable_http_client``; record ``http_client``."""
            opened["url"] = url
            opened["http_client"] = http_client
            yield "read-stream", "write-stream", lambda: None

        fake_transport = ModuleType("mcp.client.streamable_http")
        fake_transport.streamable_http_client = _canonical_client  # type: ignore[attr-defined]

        class _FakeClientSession:
            """Stand in for ``mcp.client.session.ClientSession``."""

            def __init__(self, read_stream, write_stream):
                """No-op: this fake needs no stream state."""

            async def __aenter__(self):
                """Enter as-is; no setup needed."""
                return self

            async def __aexit__(self, *exc_info):
                """Exit without suppressing exceptions."""
                return False

            async def initialize(self):
                """Return a canned initialize result."""
                return SimpleNamespace(instructions="hi")

        fake_session_mod = ModuleType("mcp.client.session")
        fake_session_mod.ClientSession = _FakeClientSession  # type: ignore[attr-defined]

        monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", fake_transport)
        monkeypatch.setitem(sys.modules, "mcp.client.session", fake_session_mod)

        async with llm_api._mcp_session("http://127.0.0.1:9584/private_x"):
            used_client = opened["http_client"]
            assert isinstance(used_client, httpx.AsyncClient)
            assert used_client.timeout == httpx.Timeout(
                llm_api._CALL_TOOL_TIMEOUT_SECONDS
            )
            # Not httpx's hardcoded default — the exact bug being fixed.
            assert used_client.timeout != httpx.Timeout(5.0)
            # Must never consult HTTP_PROXY/NO_PROXY for a loopback call: an
            # env proxy would both misroute the request and leak url's
            # embedded secret_path to the proxy (review finding).
            assert used_client.trust_env is False
            assert not used_client.is_closed
            # verify has no public accessor on a constructed client (review
            # finding) - pin it via the AsyncClient spy's captured kwargs
            # instead of introspecting private transport internals.
            assert constructed_kwargs["verify"] is False

        # Scoped to this one session: closed when the session exits.
        assert used_client.is_closed


class TestPreRenameSdkFallback:
    async def test_falls_back_to_deprecated_client_name(self, monkeypatch):
        """On a pre-rename SDK, _mcp_session must fall back to the deprecated client name."""
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
        async def _old_name_client(url, httpx_client_factory=None):
            """Stand in for the deprecated ``streamablehttp_client``; record its args."""
            opened["url"] = url
            opened["httpx_client_factory"] = httpx_client_factory
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
        # Regression (review finding on #2276): the deprecated entry point
        # takes no http_client, but it DOES accept a factory for the client
        # it builds internally — _mcp_session must hand it one that keeps
        # this fallback off environment proxies too, not just the canonical
        # path.
        assert opened["httpx_client_factory"] is llm_api._loopback_httpx_client_factory

    async def test_loopback_factory_builds_a_client_that_ignores_env_proxies(
        self, monkeypatch
    ):
        """The fallback factory's client must also disable env-proxy trust and TLS setup."""
        constructed_kwargs = _spy_httpx_async_client(monkeypatch)

        client = llm_api._loopback_httpx_client_factory()
        try:
            assert client.trust_env is False
            # verify has no public accessor (review finding) - pin it via
            # the spy's captured kwargs instead of private transport
            # internals.
            assert constructed_kwargs["verify"] is False
        finally:
            await client.aclose()

    async def test_loopback_factory_zero_args_gets_a_real_timeout_not_none(self):
        """Calling the factory with no timeout must not build an unbounded client."""
        # Regression (review finding on #2276): the factory forwarded
        # timeout=None straight to httpx.AsyncClient, which disables EVERY
        # timeout rather than applying a sane default — unlike the
        # reference create_mcp_http_client this substitutes for, which
        # treats None as "no timeout was supplied" and fills in its own
        # default. A future caller invoking this factory with no timeout
        # (as this test does) must not get an unbounded loopback client.
        client = llm_api._loopback_httpx_client_factory()
        try:
            assert client.timeout.connect is not None
            assert client.timeout.read is not None
        finally:
            await client.aclose()

    async def test_loopback_factory_works_on_pre_1_24_sdks(self, monkeypatch):
        """The factory must not depend on constants absent from pre-1.24 SDKs."""
        # Regression (round-2 review finding on #2276): the first fix
        # imported MCP_DEFAULT_TIMEOUT/MCP_DEFAULT_SSE_READ_TIMEOUT from
        # mcp.shared._httpx_utils to mirror create_mcp_http_client's None
        # handling - but those constants don't exist before mcp 1.24, and
        # this factory only ever runs on SDKs old enough to lack
        # streamable_http_client (the canonical name), which is exactly
        # that pre-1.24 range. The unconditional import broke the fallback
        # on every real install it serves. Fake the module down to the
        # old-SDK shape (create_mcp_http_client only, no MCP_DEFAULT_*
        # constants) to prove the factory no longer needs anything beyond
        # that — the same way test_falls_back_to_deprecated_client_name
        # fakes the transport module for the equivalent old-SDK shape.
        import sys
        from types import ModuleType

        fake_httpx_utils = ModuleType("mcp.shared._httpx_utils")
        fake_httpx_utils.__all__ = ["create_mcp_http_client"]  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "mcp.shared._httpx_utils", fake_httpx_utils)

        client = llm_api._loopback_httpx_client_factory()
        try:
            assert client.timeout.connect is not None
            assert client.timeout.read is not None
        finally:
            await client.aclose()


class TestExclusiveBoundNormalisation:
    """Issue #2361: an exclusive bound must not reach Core's schema conversion.

    Why it is fatal, and why normalising here rather than only at the source,
    is written out once in ``llm_api._to_inclusive_bounds``. These tests pin
    the walk's behaviour, not the reasoning.
    """

    def test_numeric_exclusive_bounds_become_inclusive(self):
        schema = {
            "type": "object",
            "properties": {
                "budget": {
                    "anyOf": [
                        {
                            "type": "number",
                            "exclusiveMinimum": 0,
                            "exclusiveMaximum": 300,
                        },
                        {"type": "null"},
                    ]
                }
            },
        }

        branch = llm_api._to_inclusive_bounds(schema)["properties"]["budget"]["anyOf"]

        assert branch[0] == {"type": "number", "minimum": 0, "maximum": 300}
        assert branch[1] == {"type": "null"}

    def test_nested_definitions_and_array_items_are_reached(self):
        schema = {
            "$defs": {"Item": {"type": "integer", "exclusiveMinimum": 1}},
            "properties": {
                "rows": {"type": "array", "items": {"exclusiveMaximum": 9}},
            },
        }

        normalised = llm_api._to_inclusive_bounds(schema)

        # The integer node folds exactly: 1 is excluded, so 2 is the bound.
        # The untyped one widens, there being no smaller step to take.
        assert normalised["$defs"]["Item"] == {"type": "integer", "minimum": 2}
        assert normalised["properties"]["rows"]["items"] == {"maximum": 9}

    def test_an_integer_bound_folds_to_the_value_the_server_accepts(self):
        """An integer excluding 1 accepts 2, and advertising 1 would lie.

        The widening trade the untyped case makes -- advertise an edge the
        server rejects, and let the server reject it on arrival -- is not a
        trade that has to be made here: the exact equivalent exists.
        """
        assert llm_api._to_inclusive_bounds(
            {"type": "integer", "exclusiveMinimum": 1}
        ) == {"type": "integer", "minimum": 2}
        assert llm_api._to_inclusive_bounds(
            {"type": "integer", "exclusiveMaximum": 9}
        ) == {"type": "integer", "maximum": 8}
        # A fractional bound on an integer node lands on the first integer
        # inside it, not on the next one out.
        assert llm_api._to_inclusive_bounds(
            {"type": "integer", "exclusiveMinimum": 1.5}
        ) == {"type": "integer", "minimum": 2}
        # Nullable integers are still integers.
        assert llm_api._to_inclusive_bounds(
            {"type": ["integer", "null"], "exclusiveMinimum": 1}
        ) == {"type": ["integer", "null"], "minimum": 2}

    def test_a_union_that_also_admits_numbers_is_not_tightened(self):
        """1.0001 is a legal value there, so 2 would reject what the server takes."""
        assert llm_api._to_inclusive_bounds(
            {"type": ["integer", "number"], "exclusiveMinimum": 1}
        ) == {"type": ["integer", "number"], "minimum": 1}

    def test_a_discriminator_mapping_is_a_name_map(self):
        """Its keys are author-chosen tags and its values are refs, not bounds.

        Descending into it would drop the tag as a non-numeric bound and hand
        the model a discriminated union with a missing branch.
        """
        schema = {
            "oneOf": [{"$ref": "#/$defs/A"}],
            "discriminator": {
                "propertyName": "kind",
                "mapping": {"exclusiveMinimum": "#/$defs/A"},
            },
        }

        assert llm_api._to_inclusive_bounds(schema) == schema

    def test_a_specification_extension_is_opaque(self):
        """``x-`` keys hold arbitrary vendor objects, not subschemas.

        The OpenAPI specification allows any value under an ``x-`` extension,
        so a bound-like key inside one is vendor data; rewriting it would
        corrupt the extension while leaving the schema itself unchanged.
        """
        schema = {
            "type": "number",
            "x-ui": {"exclusiveMinimum": 5, "nested": {"exclusiveMaximum": True}},
        }

        assert llm_api._to_inclusive_bounds(schema) == schema

    def test_a_specification_extension_is_copied_not_aliased(self):
        """Like instance values: the result must not share the input's objects."""
        extension = {"exclusiveMinimum": 5, "nested": {"list": [1, 2]}}
        schema = {"type": "number", "x-ui": extension}

        result = llm_api._to_inclusive_bounds(schema)
        result["x-ui"]["nested"]["list"].append(3)

        assert extension["nested"]["list"] == [1, 2]
        assert result["x-ui"] is not extension

    def test_a_property_named_like_the_keyword_is_left_alone(self):
        schema = {
            "type": "object",
            "properties": {"exclusiveMinimum": {"type": "number"}},
        }

        assert llm_api._to_inclusive_bounds(schema) == schema

    def test_instance_values_are_left_alone(self):
        """default/const/enum/examples hold data, not subschemas."""
        schema = {
            "type": "object",
            "default": {"exclusiveMinimum": 5},
            "const": {"exclusiveMinimum": 5},
            "enum": [{"exclusiveMinimum": 5}],
            "examples": [{"exclusiveMinimum": 5}],
        }

        assert llm_api._to_inclusive_bounds(schema) == schema

    def test_every_non_numeric_bound_is_dropped(self):
        """Measured against probatio 0.11.4, none of these survive downstream.

        A string or a list raises SchemaError, which costs the whole tool;
        ``None`` is worse than a refusal, re-emitting a number parameter as
        ``{"type": "string"}`` with no error anywhere. The name maps and
        instance-value keywords keep foreign data out of reach before this
        runs, so a key here is in a subschema slot, where no dialect permits
        a non-numeric bound.
        """
        for schema in (
            {"exclusiveMinimum": "not-a-number"},
            {"exclusiveMinimum": None},
            {"exclusiveMaximum": ["a"]},
        ):
            assert llm_api._to_inclusive_bounds(schema) == {}

    def test_dependent_required_keys_are_property_names(self):
        schema = {"dependentRequired": {"exclusiveMinimum": ["a"]}}

        assert llm_api._to_inclusive_bounds(schema) == schema

    def test_the_openapi_singular_example_is_instance_data_too(self):
        schema = {"example": {"exclusiveMinimum": 5}}

        assert llm_api._to_inclusive_bounds(schema) == schema

    def test_draft4_boolean_form_is_dropped_and_its_bound_kept(self):
        schema = {"type": "number", "minimum": 0, "exclusiveMinimum": True}

        assert llm_api._to_inclusive_bounds(schema) == {"type": "number", "minimum": 0}

    def test_a_stray_boolean_flag_is_dropped_so_the_tool_still_converts(self):
        """Pins the trade: repair the malformed node, do not preserve it.

        Probatio refuses a boolean ``exclusiveMinimum`` wherever it sits in a
        subschema slot — with or without the ``minimum`` the Draft-4 form
        requires beside it, and regardless of what else the node carries. A
        preserved flag therefore costs the whole tool, which is a worse
        outcome than dropping a keyword that means nothing on its own.
        """
        assert llm_api._to_inclusive_bounds(
            {"type": "number", "exclusiveMinimum": True}
        ) == {"type": "number"}
        assert llm_api._to_inclusive_bounds(
            {"allOf": [{"type": "number"}, {"exclusiveMinimum": True}]}
        ) == {"allOf": [{"type": "number"}, {}]}

    def test_dependencies_keys_are_property_names(self):
        """Draft-7's ``dependencies`` is a name map like its 2020-12 heirs."""
        schema = {"dependencies": {"exclusiveMinimum": ["a"]}}

        assert llm_api._to_inclusive_bounds(schema) == schema

    def test_the_tighter_bound_wins_when_both_are_present(self):
        # Both directions: the fold must never loosen a bound the schema
        # already carried, whichever of the two happens to be tighter.
        assert llm_api._to_inclusive_bounds({"minimum": 1, "exclusiveMinimum": 5}) == {
            "minimum": 5
        }
        assert llm_api._to_inclusive_bounds({"minimum": 9, "exclusiveMinimum": 5}) == {
            "minimum": 9
        }
        assert llm_api._to_inclusive_bounds({"maximum": 9, "exclusiveMaximum": 5}) == {
            "maximum": 5
        }
        assert llm_api._to_inclusive_bounds({"maximum": 1, "exclusiveMaximum": 5}) == {
            "maximum": 1
        }

    def test_a_malformed_twin_is_replaced_rather_than_kept(self):
        """A non-numeric ``minimum`` is not a bound probatio can read.

        It refuses the schema outright ("'minimum' must be a number, got
        str"), so the folded numeric bound replacing it repairs a tool that
        would otherwise be dropped -- the same trade as the Draft-4 flag.
        """
        assert llm_api._to_inclusive_bounds(
            {"minimum": "x", "exclusiveMinimum": 5}
        ) == {"minimum": 5}

    def test_the_incoming_schema_is_not_mutated(self):
        """Including the instance values, which are copied rather than aliased.

        The result is handed to Core and kept in the search catalog; a shared
        sub-object would let either reach back into the MCP result object.
        """
        schema = {
            "type": "number",
            "exclusiveMinimum": 0,
            "default": {"nested": {"a": 1}},
        }

        result = llm_api._to_inclusive_bounds(schema)
        result["default"]["nested"]["a"] = 99

        assert schema == {
            "type": "number",
            "exclusiveMinimum": 0,
            "default": {"nested": {"a": 1}},
        }

    def test_the_search_catalog_shows_the_same_schema_that_is_mirrored(self):
        """A tool must not advertise one bound and list another."""
        api = _make_api(_make_hass(), mode=EXPOSURE_TOOL_SEARCH)
        tool = SimpleNamespace(
            name="ha_search",
            description="d",
            inputSchema={
                "type": "object",
                "properties": {"budget": {"type": "number", "exclusiveMinimum": 0}},
            },
        )

        tools = api._build_tool_search_tools([tool], {"ha_search"})

        catalog = next(t for t in tools if t.name == "ha_search_tools")._catalog
        entry = next(c for c in catalog if c["name"] == "ha_search")
        assert entry["input_schema"]["properties"]["budget"] == {
            "type": "number",
            "minimum": 0,
        }

    def test_probatio_reemits_a_raw_exclusive_bound_in_the_draft4_form(
        self, real_probatio
    ):
        """Positive control: the premise every docstring here rests on.

        Without this, the tests below only show that the walk rewrites a
        keyword -- not that leaving the keyword alone would break anything.
        Probatio is what Core 2026.9+ converts with; the legacy
        voluptuous-openapi cannot show the defect at all, since it drops an
        exclusive bound on ingest and never emits the Draft-4 flag.
        """
        raw = {
            "type": "object",
            "properties": {"b": {"type": "number", "exclusiveMinimum": 0}},
        }

        out = real_probatio.to_openapi(real_probatio.from_openapi(raw))

        assert out["properties"]["b"].get("exclusiveMinimum") is True

    def test_the_normalised_schema_survives_the_probatio_round_trip(
        self, real_probatio
    ):
        """The same schema, normalised first, carries no flag out the far end."""
        import json as _json

        raw = {
            "type": "object",
            "properties": {"b": {"type": "number", "exclusiveMinimum": 0}},
        }

        out = real_probatio.to_openapi(
            real_probatio.from_openapi(llm_api._to_inclusive_bounds(raw))
        )

        assert "exclusiveMinimum" not in _json.dumps(out)

    def test_the_mirrored_path_converts_a_normalised_schema(self, monkeypatch):
        """Core's converter must never see the bound, in either exposure mode."""
        seen: list[Any] = []

        def _capture(schema: Any) -> Any:
            seen.append(schema)
            return schema

        monkeypatch.setattr(llm_api, "convert_to_voluptuous", _capture)
        tool = SimpleNamespace(
            name="ha_search",
            description="d",
            inputSchema={
                "type": "object",
                "properties": {"budget": {"type": "number", "exclusiveMinimum": 0}},
            },
        )

        _make_api(_make_hass(), mode=EXPOSURE_FULL)._build_full_tools([tool])
        _make_api(_make_hass(), mode=EXPOSURE_TOOL_SEARCH)._build_tool_search_tools(
            [tool], {"ha_search"}
        )

        assert len(seen) == 2
        for schema in seen:
            assert schema["properties"]["budget"] == {"type": "number", "minimum": 0}

    def test_a_tool_on_both_surfaces_is_normalised_once(self, monkeypatch):
        """The catalog entry and the mirrored parameters share one rewrite.

        Two rewrites would also mean two log lines per turn for every pinned
        tool, which is what makes the report below readable.
        """
        # Counted at the entry point, not on the walk: ``_to_inclusive_bounds``
        # recurses, so counting it would measure the schema's depth instead.
        calls: list[Any] = []
        real = llm_api._normalise_schema
        monkeypatch.setattr(
            llm_api,
            "_normalise_schema",
            lambda schema, tool_name: (
                calls.append(tool_name) or real(schema, tool_name)
            ),
        )
        tool = SimpleNamespace(
            name="ha_search",
            description="d",
            inputSchema={
                "type": "object",
                "properties": {"budget": {"type": "number", "exclusiveMinimum": 0}},
            },
        )

        api = _make_api(_make_hass(), mode=EXPOSURE_TOOL_SEARCH)
        api._build_tool_search_tools([tool], {"ha_search"})

        assert len(calls) == 1

    def test_the_rewrite_is_reported_once_per_tool_with_its_name(self, caplog):
        """Both branches log, and both name the tool that carried the schema.

        The catalog path publishes without converting anything, so a log line
        emitted from the converter would say nothing about the tools that
        reach the model through tool search -- the default mode.
        """
        api = _make_api(_make_hass(), mode=EXPOSURE_TOOL_SEARCH)
        folded = SimpleNamespace(
            name="ha_folded",
            description="d",
            inputSchema={"type": "number", "exclusiveMinimum": 0},
        )
        dropped = SimpleNamespace(
            name="ha_dropped",
            description="d",
            inputSchema={"type": "number", "exclusiveMinimum": True},
        )

        with caplog.at_level(logging.DEBUG, logger=llm_api._LOGGER.name):
            api._build_tool_search_tools([folded, dropped], set())

        debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
        warning = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len([r for r in debug if "ha_folded" in r.getMessage()]) == 1
        assert len([r for r in warning if "ha_dropped" in r.getMessage()]) == 1

    def test_one_tool_that_both_drops_and_folds_reports_both(self, caplog):
        """The two branches are independent, and a schema can hit both.

        Reported separately because they call for different things: the drop
        is the server's bug to fix, the fold is a widened edge to expect.
        """
        api = _make_api(_make_hass(), mode=EXPOSURE_TOOL_SEARCH)
        tool = SimpleNamespace(
            name="ha_both",
            description="d",
            inputSchema={
                "type": "object",
                "properties": {
                    "bad": {"type": "number", "exclusiveMinimum": True},
                    "good": {"type": "number", "exclusiveMinimum": 0},
                },
            },
        )

        with caplog.at_level(logging.DEBUG, logger=llm_api._LOGGER.name):
            api._build_tool_search_tools([tool], set())

        levels = {r.levelno for r in caplog.records if "ha_both" in r.getMessage()}
        assert levels == {logging.WARNING, logging.DEBUG}

    def test_two_bad_nodes_do_not_read_as_one(self, caplog):
        """A count, because the keyword name alone cannot carry the number.

        Someone reading this line is looking for the schema to fix. Collapsing
        two malformed nodes into the single-node wording sends them off after
        one bound and leaves the other in place.
        """
        api = _make_api(_make_hass(), mode=EXPOSURE_TOOL_SEARCH)
        tool = SimpleNamespace(
            name="ha_two_bad",
            description="d",
            inputSchema={
                "type": "object",
                "properties": {
                    "a": {"type": "number", "exclusiveMinimum": "x"},
                    "b": {"type": "number", "exclusiveMinimum": "y"},
                },
            },
        )

        with caplog.at_level(logging.DEBUG, logger=llm_api._LOGGER.name):
            api._build_tool_search_tools([tool], set())

        warning = next(
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        )
        assert "exclusiveMinimum x2" in warning

    def test_the_guard_mirrors_the_components_keyword_sets(self):
        """The registry guard repeats these sets rather than importing them.

        That keeps the guard runnable without the Home Assistant component on
        the path, at the cost of two copies that can drift apart silently.
        This module imports both sides anyway, so the equality is pinned here
        rather than left to review.
        """
        from . import test_tool_schema_exclusive_bounds as guard

        assert guard._NAME_MAPS == llm_api._SCHEMA_MAPS
        assert (
            guard._NOT_SUBSCHEMAS == llm_api._INSTANCE_VALUES | llm_api._OPAQUE_KEYWORDS
        )
        # The third copy: a keyword added to the fold table but not to the
        # walk would be normalised by the component and left unguarded here,
        # with the registry-wide test still passing.
        assert set(guard._EXCLUSIVE_KEYWORDS) == {
            exclusive for exclusive, *_ in llm_api._EXCLUSIVE_BOUNDS
        }
        # The fourth copy is a prefix rule rather than a set: OpenAPI ``x-``
        # extensions are opaque to the component and skipped by the guard.
        assert llm_api._is_opaque_key("x-anything")
        assert guard._exclusive_bounds({"x-a": {"exclusiveMinimum": 1}}, "r") == []
        # ...but only as a KEYWORD: a property literally named ``x-limit`` is a
        # subschema on both sides, so its bound is normalised and reported.
        named = {
            "type": "object",
            "properties": {"x-limit": {"type": "number", "exclusiveMinimum": 0}},
        }
        assert llm_api._to_inclusive_bounds(named)["properties"]["x-limit"] == {
            "type": "number",
            "minimum": 0,
        }
        assert guard._exclusive_bounds(named, "r") == [
            "r.properties.x-limit: exclusiveMinimum"
        ]

    def test_an_untouched_schema_is_not_reported(self, caplog):
        """Nothing changed, so nothing is worth an operator's attention."""
        api = _make_api(_make_hass(), mode=EXPOSURE_TOOL_SEARCH)
        tool = SimpleNamespace(
            name="ha_clean",
            description="d",
            inputSchema={"type": "number", "minimum": 0},
        )

        with caplog.at_level(logging.DEBUG, logger=llm_api._LOGGER.name):
            api._build_tool_search_tools([tool], set())

        assert [r for r in caplog.records if "ha_clean" in r.getMessage()] == []
