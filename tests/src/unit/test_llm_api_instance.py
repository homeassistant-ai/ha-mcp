"""The toolset a conversation agent gets from the LLM API (issue #1745).

Core asks the registered API for an instance; the component lists the
in-process server's tools over loopback MCP and builds either the full catalog
or the compact tool-search one from the server's per-tool exposure stamp.
The server here is a real MCP server on loopback (``_llm_api_server``).
"""

from __future__ import annotations

import logging

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from mcp import types

from custom_components.ha_mcp_tools import llm_api, llm_tool_exposure
from custom_components.ha_mcp_tools.const import (
    EXPOSURE_FULL,
    EXPOSURE_TOOL_SEARCH,
)

from ._llm_api_helpers import llm_context
from ._llm_api_server import (
    SEARCH_SCHEMA,
    ServedTool,
    api_instance,
    call,
    payload,
    refused_port,
    serve,
    tool_named,
)

pytestmark = pytest.mark.usefixtures("socket_enabled")


def _names(instance: llm.APIInstance) -> list[str]:
    return [tool.name for tool in instance.tools]


class TestFullMode:
    async def test_lists_exposed_tools_with_converted_schemas_and_the_servers_prompt(
        self, hass: HomeAssistant
    ) -> None:
        tools = [ServedTool("ha_search"), ServedTool("ha_get_state")]
        async with serve(tools, instructions="Use the skills-first workflow.") as srv:
            instance = await api_instance(hass, srv.port, EXPOSURE_FULL)

        assert _names(instance) == ["ha_search", "ha_get_state"]
        search = instance.tools[0]
        assert search.description == "ha_search description"
        assert search.parameters == llm_api.convert_to_voluptuous(SEARCH_SCHEMA)
        # The server's own instructions, without the tool-search section.
        assert instance.api_prompt == "Use the skills-first workflow."

    async def test_a_tool_the_server_hides_is_invisible_to_the_agent(
        self, hass: HomeAssistant
    ) -> None:
        """The stamp is the exposure decision, whatever the raw MCP surface has."""
        tools = [ServedTool("ha_get_state"), ServedTool("ha_restart", exposed=False)]
        async with serve(tools) as srv:
            instance = await api_instance(hass, srv.port, EXPOSURE_FULL)

        assert _names(instance) == ["ha_get_state"]

    async def test_an_unstamped_server_falls_back_to_the_deny_list(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Older server packages don't stamp exposure: the component applies
        its conservative built-in deny-list instead, loudly."""
        tools = [
            ServedTool(name, stamped=False)
            for name in (
                "ha_get_state",
                "ha_restart",
                "ha_write_file",
                "ha_dev_manage_server",
            )
        ]
        async with serve(tools) as srv:
            with caplog.at_level(logging.WARNING):
                instance = await api_instance(hass, srv.port, EXPOSURE_FULL)

        assert _names(instance) == ["ha_get_state"]
        assert "does not stamp LLM-API exposure metadata" in caplog.text

    async def test_a_server_without_instructions_still_gives_the_agent_a_prompt(
        self, hass: HomeAssistant
    ) -> None:
        async with serve(instructions=None) as srv:
            instance = await api_instance(hass, srv.port, EXPOSURE_FULL)

        assert instance.api_prompt.strip()

    async def test_an_unconvertible_schema_skips_that_tool_only(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        tools = [
            ServedTool("ha_bad", input_schema={"type": "not-a-json-schema-type"}),
            ServedTool("ha_good"),
        ]
        async with serve(tools) as srv:
            with caplog.at_level(logging.WARNING):
                instance = await api_instance(hass, srv.port, EXPOSURE_FULL)

        assert _names(instance) == ["ha_good"]
        assert "Skipping tool ha_bad" in caplog.text

    async def test_an_unreachable_server_raises_homeassistanterror(
        self, hass: HomeAssistant
    ) -> None:
        with (
            refused_port() as port,
            pytest.raises(HomeAssistantError, match="Could not reach"),
        ):
            await api_instance(hass, port, EXPOSURE_FULL)

    async def test_a_wedged_server_times_out_as_homeassistanterror(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(llm_api, "LIST_TOOLS_TIMEOUT_SECONDS", 0.05)
        async with serve() as srv:
            srv.list_delay = 1.0
            with pytest.raises(HomeAssistantError, match="Could not reach"):
                await api_instance(hass, srv.port, EXPOSURE_FULL)

    async def test_a_malformed_tool_list_is_a_bug_not_an_unreachable_server(
        self, hass: HomeAssistant
    ) -> None:
        """The SDK's task groups wrap in-session failures indiscriminately, so
        a group carrying a genuine bug must not be relabeled (#1782)."""
        async with serve(empty_list_answer=True) as srv:
            with pytest.raises(ExceptionGroup):
                await api_instance(hass, srv.port, EXPOSURE_FULL)


class TestToolSearchMode:
    @staticmethod
    def _catalog() -> list[ServedTool]:
        return [
            ServedTool("ha_search", pinned=True),
            ServedTool("ha_get_state", description="Get entity state"),
            ServedTool("ha_config_set_automation", description="Create automation"),
            ServedTool("ha_restart", exposed=False),
        ]

    async def test_compact_catalog_shape(self, hass: HomeAssistant) -> None:
        async with serve(self._catalog()) as srv:
            instance = await api_instance(hass, srv.port, EXPOSURE_TOOL_SEARCH)

        # A pinned exposed tool is mirrored directly; hidden and unpinned ones
        # are reachable only through the meta-tools.
        assert _names(instance) == ["ha_search", "ha_search_tools", "ha_call_tool"]
        assert "Tool Discovery" in instance.api_prompt

    async def test_search_finds_exposed_tools_only(self, hass: HomeAssistant) -> None:
        async with serve(self._catalog()) as srv:
            instance = await api_instance(hass, srv.port, EXPOSURE_TOOL_SEARCH)
            result = await call(
                hass,
                tool_named(instance, "ha_search_tools"),
                {"query": "create automation"},
            )

        hits = payload(result)["results"]
        names = [hit["name"] for hit in hits]
        assert "ha_config_set_automation" in names
        assert "ha_restart" not in names
        # Hits are compact; the full schema is a second, by-name hop.
        assert all({*hit} == {"name", "description", "params"} for hit in hits)

    async def test_a_search_with_no_match_guides_the_retry(
        self, hass: HomeAssistant
    ) -> None:
        async with serve(self._catalog()) as srv:
            instance = await api_instance(hass, srv.port, EXPOSURE_TOOL_SEARCH)
            result = await call(
                hass, tool_named(instance, "ha_search_tools"), {"query": "zzzznothing"}
            )

        assert payload(result)["results"] == []
        assert "message" in payload(result)

    async def test_call_tool_runs_an_exposed_tool_on_the_server(
        self, hass: HomeAssistant
    ) -> None:
        tools = [
            ServedTool(
                "ha_get_state",
                result=types.CallToolResult(
                    content=[types.TextContent(type="text", text="sun is up")]
                ),
            ),
            ServedTool("ha_restart", exposed=False),
        ]
        async with serve(tools) as srv:
            instance = await api_instance(hass, srv.port, EXPOSURE_TOOL_SEARCH)
            result = await call(
                hass,
                tool_named(instance, "ha_call_tool"),
                {"name": "ha_get_state", "arguments": {"entity_id": "sun.sun"}},
            )

        assert srv.calls == [("ha_get_state", {"entity_id": "sun.sun"})]
        assert result.error is False
        assert payload(result)["content"] == [{"type": "text", "text": "sun is up"}]

    @pytest.mark.parametrize(
        "target", ["ha_restart", "ha_totally_made_up"], ids=["hidden", "nonexistent"]
    )
    async def test_call_tool_answers_a_hidden_tool_like_a_nonexistent_one(
        self, hass: HomeAssistant, target: str
    ) -> None:
        """Hiding without enforcement would let a model that guesses names skirt
        the exposure settings, and a distinct error would leak existence."""
        async with serve(self._catalog()) as srv:
            instance = await api_instance(hass, srv.port, EXPOSURE_TOOL_SEARCH)
            result = await call(
                hass,
                tool_named(instance, "ha_call_tool"),
                {"name": target, "arguments": {}},
            )

        assert srv.calls == []
        assert result.error is True
        assert payload(result)["error"] == f"Unknown tool '{target}'."

    async def test_a_server_side_search_tool_is_never_mirrored(
        self, hass: HomeAssistant
    ) -> None:
        """A server running its own tool search registers a real
        ``ha_search_tools``: one name, one behavior."""
        tools = [*self._catalog(), ServedTool("ha_search_tools", pinned=True)]
        async with serve(tools) as srv:
            instance = await api_instance(hass, srv.port, EXPOSURE_TOOL_SEARCH)
            search = tool_named(instance, "ha_search_tools")
            result = await call(hass, search, {"query": "search tools"})

        assert _names(instance).count("ha_search_tools") == 1
        assert isinstance(search, llm_api.HaMcpSearchTool)
        assert "ha_search_tools" not in [
            hit["name"] for hit in payload(result)["results"]
        ]


class TestDefaultMode:
    async def test_an_omitted_mode_builds_the_tool_search_instance(
        self, hass: HomeAssistant
    ) -> None:
        """Defaulting to the full catalog would make an omitted mode maximally
        exposed."""
        async with serve([ServedTool("ha_get_state")]) as srv:
            api = llm_api.HaMcpLlmApi(
                hass=hass,
                id="default-mode",
                name="HA-MCP Server (tool search)",
                server_url=f"http://127.0.0.1:{srv.port}/private_x",
            )
            instance = await api.async_get_api_instance(llm_context())

        assert _names(instance) == ["ha_search_tools", "ha_call_tool"]
        assert "Tool Discovery" in instance.api_prompt

    async def test_an_unrecognised_stored_mode_gives_the_tool_search_toolset(
        self, hass: HomeAssistant
    ) -> None:
        async with serve([ServedTool("ha_get_state")]) as srv:
            instance = await api_instance(hass, srv.port, "bogus")

        assert _names(instance) == ["ha_search_tools", "ha_call_tool"]


def test_component_meta_keys_match_server() -> None:
    """The stamp keys are duplicated because the component must never import
    ha_mcp at runtime; this test tier can import both, so the keep-in-sync
    comment is enforced mechanically here."""
    from ha_mcp.llm_exposure import (
        META_EXPOSED_KEY,
        META_NAMESPACE,
        META_PINNED_KEY,
    )

    assert llm_tool_exposure.META_NAMESPACE == META_NAMESPACE
    assert llm_tool_exposure.META_EXPOSED_KEY == META_EXPOSED_KEY
    assert llm_tool_exposure.META_PINNED_KEY == META_PINNED_KEY
