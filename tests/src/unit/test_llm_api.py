"""The component's Home Assistant LLM API (#1745).

A mirrored tool call over loopback MCP and how its failures map, which codec
converts the server's tool schemas, and how the toolset registers as LLM
API(s). The agent sees a ``HomeAssistantError`` for any failure to reach the
server; a genuine bug must surface as itself instead of being relabeled as one.
"""

from __future__ import annotations

import importlib
import logging
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import probatio
import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from mcp import McpError, types
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools import llm_api
from custom_components.ha_mcp_tools.const import (
    DATA_LLM_API_UNSUB,
    DOMAIN,
    EXPOSURE_BOTH,
    EXPOSURE_FULL,
    EXPOSURE_TOOL_SEARCH,
    OPT_LLM_API_EXPOSURE,
)

from ._llm_api_helpers import registered_apis
from ._llm_api_server import (
    ServedTool,
    call,
    mirrored_tool,
    payload,
    refused_port,
    serve,
)

_LOOPBACK = pytest.mark.usefixtures("socket_enabled")
_FRESH_CONVERTER = pytest.mark.usefixtures("_resolve_the_converter_afresh")

_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
}
_FULL_ID = f"{DOMAIN}-entry-1745"
_SEARCH_ID = f"{_FULL_ID}-toolsearch"
_URL = "http://127.0.0.1:9584/private_x"


# -- mirrored tool call ---------------------------------------------------------


@_LOOPBACK
async def test_a_call_passes_its_arguments_and_returns_the_servers_result(
    hass: HomeAssistant,
) -> None:
    tools = [
        ServedTool(
            result=types.CallToolResult(
                content=[types.TextContent(type="text", text="found it")]
            )
        )
    ]
    async with serve(tools) as srv:
        result = await call(hass, mirrored_tool(srv.port), {"query": "kitchen light"})

    assert srv.calls == [("ha_search", {"query": "kitchen light"})]
    assert result.error is False
    assert payload(result)["content"] == [{"type": "text", "text": "found it"}]


@_LOOPBACK
async def test_a_refused_connection_raises_homeassistanterror(
    hass: HomeAssistant,
) -> None:
    with (
        refused_port() as port,
        pytest.raises(HomeAssistantError, match="ha_search"),
    ):
        await call(hass, mirrored_tool(port), {})


@_LOOPBACK
async def test_a_protocol_error_from_the_server_raises_homeassistanterror(
    hass: HomeAssistant,
) -> None:
    """JSON-RPC errors surface as McpError, which Core's own mcp integration
    maps the same way."""
    error = McpError(types.ErrorData(code=-32000, message="boom"))
    async with serve(list_error=error) as srv:
        with pytest.raises(HomeAssistantError, match="ha_search"):
            await call(hass, mirrored_tool(srv.port), {})

    assert srv.calls == []


@_LOOPBACK
async def test_a_malformed_answer_propagates_as_the_bug_it_is(
    hass: HomeAssistant,
) -> None:
    """The SDK's task groups wrap a genuine bug; mapping it to a friendly
    transport message would hide its traceback (#1782)."""
    async with serve(empty_list_answer=True) as srv:
        with pytest.raises(ExceptionGroup):
            await call(hass, mirrored_tool(srv.port), {})


@_LOOPBACK
@pytest.mark.parametrize(
    ("error", "transport"),
    [
        pytest.param(
            ExceptionGroup("session", [OSError("refused")]), True, id="transport-only"
        ),
        pytest.param(
            ExceptionGroup("session", [OSError("refused"), TypeError("bug")]),
            False,
            id="mixed-transport-and-bug",
        ),
        pytest.param(
            ExceptionGroup(
                "session",
                [ExceptionGroup("inner", [OSError("refused"), TypeError("bug")])],
            ),
            False,
            id="bug-in-a-nested-group",
        ),
    ],
)
def test_a_group_holding_any_bug_is_not_a_transport_failure(
    error: ExceptionGroup, transport: bool
) -> None:
    """A transport error beside a bug must not relabel the bug as an
    unreachable server (#1782)."""
    assert llm_api.is_transport_failure(error) is transport


@_LOOPBACK
async def test_a_slow_tool_call_times_out_as_homeassistanterror(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_api, "CALL_TOOL_TIMEOUT_SECONDS", 0.05)
    async with serve() as srv:
        srv.call_delay = 1.0
        with pytest.raises(HomeAssistantError, match="ha_search"):
            await call(hass, mirrored_tool(srv.port), {})


# -- schema converter -----------------------------------------------------------
#
# The component's manifest installs ``voluptuous_openapi`` and converts with it;
# Core itself ships only ``probatio``. Without the manifest's package the
# component falls back to ``probatio``, once per process and never on the event
# loop.


@pytest.fixture
def _resolve_the_converter_afresh() -> Iterator[None]:
    """The choice is memoised for the process; each test starts unresolved."""
    llm_api.reset_schema_converter()
    yield
    llm_api.reset_schema_converter()


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == llm_api.__name__ and record.levelno == logging.WARNING
    ]


@_FRESH_CONVERTER
def test_the_voluptuous_openapi_converter_is_used_without_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger=llm_api.__name__):
        converted = llm_api.convert_to_voluptuous(_SCHEMA)

    assert isinstance(converted, vol.Schema)
    assert converted({"query": "kitchen"}) == {"query": "kitchen"}
    with pytest.raises(vol.Invalid):
        converted({})
    assert _warnings(caplog) == []


@_FRESH_CONVERTER
def test_a_core_without_voluptuous_openapi_converts_with_probatio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "voluptuous_openapi", None)

    converted = llm_api.convert_to_voluptuous(_SCHEMA)

    assert isinstance(converted, probatio.Schema)
    assert converted({"query": "kitchen"}) == {"query": "kitchen"}
    with pytest.raises(probatio.Invalid):
        converted({})


@_FRESH_CONVERTER
def test_the_fallback_announces_which_codec_is_converting(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing else reports that the component runs on the other codec.

    A requirement that failed to install never gets here: Home Assistant
    logs that itself and abandons the integration before importing it. What
    does get here -- skip_pip, or a deps tree that lost the package --
    produces no message anywhere else, and the conversion then succeeds
    quietly with a codec that retypes integers.
    """
    monkeypatch.setitem(sys.modules, "voluptuous_openapi", None)

    with caplog.at_level(logging.DEBUG, logger=llm_api.__name__):
        llm_api.convert_to_voluptuous(_SCHEMA)

    [warning] = _warnings(caplog)
    assert "voluptuous-openapi" in warning


@_FRESH_CONVERTER
def test_a_failure_inside_the_converter_is_not_mistaken_for_its_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falling back would hide a broken install behind a working codec."""
    package = tmp_path / "voluptuous_openapi"
    package.mkdir()
    (package / "__init__.py").write_text("import legacy_dependency_not_installed\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [n for n in sys.modules if n.partition(".")[0] == "voluptuous_openapi"]:
        monkeypatch.delitem(sys.modules, name)

    with pytest.raises(ModuleNotFoundError, match="legacy_dependency_not_installed"):
        llm_api.convert_to_voluptuous(_SCHEMA)


@_FRESH_CONVERTER
async def test_the_converter_is_imported_once_and_off_the_event_loop(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Importing it on the loop would trip Home Assistant's blocking-call check."""
    loop_thread = threading.get_ident()
    watched = {"mcp.client.session", "mcp.client.streamable_http", "voluptuous_openapi"}
    imports: list[tuple[str, int]] = []
    real_import_module = importlib.import_module

    def recording_import_module(name: str, package: str | None = None) -> Any:
        if name in watched:
            imports.append((name, threading.get_ident()))
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", recording_import_module)

    assert await llm_api.async_probe_mcp_sdk(hass)
    llm_api.convert_to_voluptuous(_SCHEMA)
    llm_api.convert_to_voluptuous(_SCHEMA)

    assert [name for name, _ in imports] == [
        "mcp.client.session",
        "mcp.client.streamable_http",
        "voluptuous_openapi",
    ]
    assert all(thread != loop_thread for _, thread in imports)


# -- registration ---------------------------------------------------------------
#
# ``async_register_llm_api`` runs on every in-process server bring-up. The
# exposure option picks which APIs a conversation agent can select, a repeated
# bring-up must not stack registrations, and nothing that goes wrong here may
# reach the bring-up, which would tear the running server down.


def _add_entry(
    hass: HomeAssistant, options: dict[str, Any] | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="HA-MCP Server",
        entry_id="entry-1745",
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


def _ours(hass: HomeAssistant) -> dict[str, Any]:
    """The APIs this component registered; Core registers its own as well."""
    return {
        api_id: api
        for api_id, api in registered_apis(hass).items()
        if api_id.startswith(DOMAIN)
    }


async def _register(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    port: int = 9584,
    secret_path: str = "/private_x",
) -> None:
    await llm_api.async_register_llm_api(
        hass, entry, port=port, secret_path=secret_path
    )


async def test_default_exposure_registers_only_the_tool_search_api(
    hass: HomeAssistant,
) -> None:
    await _register(hass, _add_entry(hass))

    apis = _ours(hass)
    assert set(apis) == {_SEARCH_ID}
    api = apis[_SEARCH_ID]
    assert api.name == "HA-MCP Server (tool search)"
    assert api.mode == EXPOSURE_TOOL_SEARCH
    assert api.server_url == _URL
    unsubs = hass.data[DOMAIN][DATA_LLM_API_UNSUB]
    assert len(unsubs) == 1 and all(callable(unsub) for unsub in unsubs)


async def test_full_exposure_registers_only_the_full_api(
    hass: HomeAssistant,
) -> None:
    await _register(hass, _add_entry(hass, {OPT_LLM_API_EXPOSURE: EXPOSURE_FULL}))

    apis = _ours(hass)
    assert set(apis) == {_FULL_ID}
    assert apis[_FULL_ID].mode == EXPOSURE_FULL
    assert apis[_FULL_ID].name == "HA-MCP Server"


async def test_both_exposure_registers_two_apis_for_one_server(
    hass: HomeAssistant,
) -> None:
    await _register(hass, _add_entry(hass, {OPT_LLM_API_EXPOSURE: EXPOSURE_BOTH}))

    apis = _ours(hass)
    assert set(apis) == {_FULL_ID, _SEARCH_ID}
    assert {api.server_url for api in apis.values()} == {_URL}
    assert len(hass.data[DOMAIN][DATA_LLM_API_UNSUB]) == 2


async def test_an_unknown_stored_exposure_degrades_to_the_default(
    hass: HomeAssistant,
) -> None:
    await _register(hass, _add_entry(hass, {OPT_LLM_API_EXPOSURE: "bogus"}))

    assert set(_ours(hass)) == {_SEARCH_ID}


async def test_unregister_removes_the_apis_and_is_idempotent(
    hass: HomeAssistant,
) -> None:
    await _register(hass, _add_entry(hass, {OPT_LLM_API_EXPOSURE: EXPOSURE_BOTH}))

    llm_api.async_unregister_llm_api(hass)

    assert _ours(hass) == {}
    assert DATA_LLM_API_UNSUB not in hass.data[DOMAIN]
    # Reload paths run the teardown again; the second run must not raise.
    llm_api.async_unregister_llm_api(hass)


async def test_a_second_bring_up_replaces_the_stale_registration(
    hass: HomeAssistant,
) -> None:
    """A crashed reload never ran its teardown; Core refuses duplicate ids."""
    entry = _add_entry(hass)

    await _register(hass, entry)
    await _register(hass, entry, port=9999, secret_path="/private_y")

    apis = _ours(hass)
    assert len(apis) == 1
    assert apis[_SEARCH_ID].server_url == "http://127.0.0.1:9999/private_y"


async def test_an_id_core_refuses_is_logged_not_raised(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Anything escaping would tear the already-running server down (#1782)."""
    llm.async_register_api(
        hass,
        llm_api.HaMcpLlmApi(hass=hass, id=_SEARCH_ID, name="taken", server_url=_URL),
    )

    with caplog.at_level(logging.WARNING):
        await _register(hass, _add_entry(hass))

    assert DATA_LLM_API_UNSUB not in hass.data.get(DOMAIN, {})
    assert "Could not register the HA-MCP LLM API" in caplog.text


async def test_an_unexpected_registration_failure_is_logged_not_raised(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        llm, "async_register_api", MagicMock(side_effect=RuntimeError("unexpected"))
    )

    with caplog.at_level(logging.WARNING):
        await _register(hass, _add_entry(hass))

    assert DATA_LLM_API_UNSUB not in hass.data.get(DOMAIN, {})
    assert "Could not register the HA-MCP LLM API" in caplog.text


async def test_a_build_without_the_mcp_sdk_skips_the_feature_with_a_warning(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The SDK arrives with the runtime-installed server package."""
    monkeypatch.setitem(sys.modules, "mcp.client.session", None)

    with caplog.at_level(logging.WARNING):
        await _register(hass, _add_entry(hass))

    assert _ours(hass) == {}
    assert DATA_LLM_API_UNSUB not in hass.data.get(DOMAIN, {})
    assert "required LLM dependency is not importable" in caplog.text
