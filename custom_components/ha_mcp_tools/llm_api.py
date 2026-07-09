"""Expose the in-process server's toolset as a Home Assistant LLM API (#1745).

While the in-process server entry is up, the full ha-mcp toolset is registered
as an LLM API (``homeassistant.helpers.llm``). Any Home Assistant conversation
agent — OpenAI, Google, Ollama, Anthropic, or any other — can then select it in
its "Control Home Assistant" option, and the user chats with the toolset
through the surfaces Home Assistant already has: the Assist chat UI, the
companion apps, and voice satellites. No separate chat frontend is needed.

The server runs on its own worker thread behind a loopback HTTP listener, and
``ha_mcp`` must never be imported in the HA main process (see
:mod:`embedded_server`), so this module talks real MCP to the server over
loopback streamable HTTP — the same pattern Home Assistant core's ``mcp``
integration uses for remote servers. The ``mcp`` client SDK arrives with the
runtime-installed ha-mcp package (a fastmcp dependency), so every SDK import
here is lazy and the first one runs on the executor: the SDK only needs to be
importable once the server package install has already succeeded.

The tool list is fetched fresh on every ``async_get_api_instance`` call (once
per conversation turn): the server registers user-defined custom tools at
runtime, and two loopback round-trips per turn are noise next to the LLM call
itself. Tool calls likewise open a short-lived stateless session each — the
in-process server serves ``stateless_http=True``, so there is no session state
to reuse.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from voluptuous_openapi import convert_to_voluptuous

from .const import DATA_LLM_API_UNSUB, DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.util.json import JsonObjectType
    from mcp import types as mcp_types
    from mcp.client.session import ClientSession

_LOGGER = logging.getLogger(__name__)

# Listing tools is two loopback round-trips (initialize + tools/list); a slow
# answer means the server thread is wedged, not that the network is slow.
_LIST_TOOLS_TIMEOUT_SECONDS = 10.0

# Tool calls run real work — WebSocket-verified device control, dashboard
# screenshots, config writes that poll for completion — well beyond the 10s a
# remote-server integration would allow. The conversation agent shows a spinner
# for the duration, so err generous rather than kill a legitimate slow tool.
_CALL_TOOL_TIMEOUT_SECONDS = 300.0

# Used when the server's initialize result carries no instructions (it always
# should — ha-mcp ships server-level instructions — but never render an empty
# prompt if a build does not).
_FALLBACK_API_PROMPT = (
    "The following tools are provided by the HA-MCP server running inside "
    "Home Assistant. They give full control over this Home Assistant "
    "instance: entities, automations, scripts, dashboards, helpers, and "
    "configuration."
)


def _transport_error_leaves() -> tuple[type[BaseException], ...]:
    """Return the non-group exception classes a loopback exchange can raise.

    OSError covers a refused/dropped loopback connect; TimeoutError comes
    from our asyncio.timeout budget. httpx errors and protocol-level McpError
    can also escape a session call UNWRAPPED (HA core's mcp integration
    catches both the same way), but neither class is importable at module
    level — both arrive with the runtime-installed server package — hence a
    function instead of a module constant.
    """
    errors: tuple[type[BaseException], ...] = (TimeoutError, OSError)
    try:
        import httpx
        from mcp import McpError
    except ImportError:  # pragma: no cover - SDK-less builds never open a session
        return errors
    return (*errors, httpx.HTTPError, McpError)


def _transport_errors() -> tuple[type[BaseException], ...]:
    """Return the ``except`` target for one loopback MCP exchange.

    Evaluated at exception time (an ``except`` expression is), so the lazy
    imports in :func:`_transport_error_leaves` have already succeeded by
    then. Includes ExceptionGroup because the SDK's anyio task groups wrap
    in-session failures — but a caught group must still pass
    :func:`_is_transport_failure` before being mapped to a friendly error,
    or a genuine bug that happened inside the task group would be relabeled
    as a transport failure (review finding).
    """
    return (*_transport_error_leaves(), ExceptionGroup)


def _is_transport_failure(err: BaseException) -> bool:
    """Return True when ``err`` is purely a transport failure.

    A group counts only when EVERY leaf (nested groups included) is a
    transport error: a group carrying any non-transport member is a genuine
    bug that must propagate with its loud traceback instead of being
    remapped to a "could not reach the server" message.
    """
    if isinstance(err, ExceptionGroup):
        return all(_is_transport_failure(exc) for exc in err.exceptions)
    return isinstance(err, _transport_error_leaves())


def _import_mcp_sdk() -> None:
    """Import the mcp client SDK modules (blocking; run on the executor).

    Raises ImportError when the SDK is not importable — the caller decides
    whether that skips registration (SDK missing entirely) or surfaces as a
    conversation error.
    """
    importlib.import_module("mcp.client.session")
    importlib.import_module("mcp.client.streamable_http")


async def async_probe_mcp_sdk(hass: HomeAssistant) -> bool:
    """Return True when the mcp client SDK imports (first import off-loop)."""
    try:
        await hass.async_add_executor_job(_import_mcp_sdk)
    except ImportError as err:
        _LOGGER.warning(
            "The installed server package provides no importable 'mcp' client "
            "SDK (%s); the conversation-agent LLM API will not be available",
            err,
        )
        return False
    return True


@asynccontextmanager
async def _mcp_session(
    url: str,
) -> AsyncIterator[tuple[ClientSession, mcp_types.InitializeResult]]:
    """Open an initialized MCP session against the loopback server.

    Imports resolve from ``sys.modules`` — :func:`async_probe_mcp_sdk` did the
    real (blocking) import on the executor before the API was registered.
    """
    from mcp.client.session import ClientSession

    try:
        from mcp.client.streamable_http import streamable_http_client
    except ImportError:
        # Pre-rename SDK (an older ha-mcp resolved by a pip-spec override
        # pins an older fastmcp/mcp): same call shape, deprecated name. The
        # ignore covers the signature drift between the two declarations —
        # only the url kwarg is used here, which both accept.
        from mcp.client.streamable_http import (  # type: ignore[assignment]
            streamablehttp_client as streamable_http_client,
        )

    async with (
        streamable_http_client(url=url) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        init_result = await session.initialize()
        yield session, init_result


class HaMcpTool(llm.Tool):
    """One ha-mcp tool, called over loopback MCP."""

    def __init__(
        self,
        name: str,
        description: str | None,
        parameters: vol.Schema,
        server_url: str,
    ) -> None:
        """Store the converted schema and the loopback endpoint."""
        self.name = name
        self.description = description
        self.parameters = parameters
        self._server_url = server_url

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Call the tool on the in-process server and return its result."""
        try:
            async with (
                asyncio.timeout(_CALL_TOOL_TIMEOUT_SECONDS),
                _mcp_session(self._server_url) as (session, _init),
            ):
                result = await session.call_tool(
                    tool_input.tool_name, tool_input.tool_args
                )
        except _transport_errors() as err:
            if not _is_transport_failure(err):
                raise
            raise HomeAssistantError(
                f"Error calling the HA-MCP tool {tool_input.tool_name}: {err}"
            ) from err
        # Full CallToolResult (content blocks, structuredContent, isError) —
        # the same shape HA core's mcp integration hands to agents; ha-mcp
        # signals tool failure via isError + structured error JSON, which the
        # agent reads and reacts to like any tool output.
        return result.model_dump(exclude_unset=True, exclude_none=True)


@dataclass(kw_only=True)
class HaMcpLlmApi(llm.API):
    """The in-process ha-mcp server's toolset as a Home Assistant LLM API."""

    server_url: str

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        """Fetch the current tool list and return an API instance.

        Fetched fresh each conversation turn (see the module docstring); the
        server's own initialize ``instructions`` become the API prompt, so the
        agent gets the same guidance every MCP client gets.
        """
        try:
            async with (
                asyncio.timeout(_LIST_TOOLS_TIMEOUT_SECONDS),
                _mcp_session(self.server_url) as (session, init_result),
            ):
                list_result = await session.list_tools()
        except _transport_errors() as err:
            if not _is_transport_failure(err):
                raise
            raise HomeAssistantError(
                f"Could not reach the in-process HA-MCP server: {err}"
            ) from err

        tools: list[llm.Tool] = []
        for tool in list_result.tools:
            try:
                parameters = convert_to_voluptuous(tool.inputSchema)
            except Exception:
                # One unconvertible schema must not take down the whole
                # toolset for the conversation — skip that tool, loudly.
                _LOGGER.warning(
                    "Skipping tool %s: could not convert its input schema",
                    tool.name,
                    exc_info=True,
                )
                continue
            tools.append(
                HaMcpTool(tool.name, tool.description, parameters, self.server_url)
            )

        return llm.APIInstance(
            self,
            init_result.instructions or _FALLBACK_API_PROMPT,
            llm_context,
            tools,
        )


async def async_register_llm_api(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    port: int,
    secret_path: str,
) -> None:
    """Register the running server's toolset as an LLM API (advisory).

    Called from the bring-up success path. Never raises — and that has to be
    literal, not aspirational: any exception escaping here lands in the
    bring-up's outer ``except Exception``, which tears the already-running
    server down and files a "start" repair issue for what is a cosmetic
    failure (review finding). Hence the broad containment: whatever goes
    wrong is logged and the feature is simply absent until the next (re)load.
    Cancellation (a BaseException) still propagates.
    """
    try:
        if not await async_probe_mcp_sdk(hass):
            return

        # Re-registration guard: a bring-up after a teardown that could not
        # run (or a duplicate bring-up) must replace the stale registration,
        # not fail on the duplicate id.
        async_unregister_llm_api(hass)

        api = HaMcpLlmApi(
            hass=hass,
            id=f"{DOMAIN}-{entry.entry_id}",
            name=entry.title,
            server_url=f"http://127.0.0.1:{port}{secret_path}",
        )
        unsub = llm.async_register_api(hass, api)
        hass.data.setdefault(DOMAIN, {})[DATA_LLM_API_UNSUB] = unsub
    except Exception:
        _LOGGER.warning(
            "Could not register the HA-MCP LLM API; conversation agents will "
            "not see the toolset until the entry is reloaded",
            exc_info=True,
        )
        return
    # The embedded e2e (test_llm_api_registered_inside_ha) asserts on this
    # message to prove the registration ran inside a real HA — keep the
    # "Registered the HA-MCP toolset as LLM API" prefix stable.
    _LOGGER.info(
        "Registered the HA-MCP toolset as LLM API %r — select it in a "
        "conversation agent's settings to chat with it (text or voice)",
        api.id,
    )


def async_unregister_llm_api(hass: HomeAssistant) -> None:
    """Unregister the LLM API if registered (idempotent, teardown-safe)."""
    unsub = hass.data.get(DOMAIN, {}).pop(DATA_LLM_API_UNSUB, None)
    if unsub is not None:
        unsub()
