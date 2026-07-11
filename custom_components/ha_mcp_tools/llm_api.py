"""Expose the in-process server's toolset as a Home Assistant LLM API (#1745).

While the in-process server entry is up, the ha-mcp toolset is registered as
one or two LLM APIs (``homeassistant.helpers.llm``). Any Home Assistant
conversation agent — OpenAI, Google, Ollama, Anthropic, or any other — can
then select it in its "Control Home Assistant" option, and the user chats
with the toolset through the surfaces Home Assistant already has: the Assist
chat UI, the companion apps, and voice satellites. No separate chat frontend
is needed.

Two exposure modes (the ``llm_api_exposure`` entry option picks which are
registered; default is tool-search only):

* **tool search** — the agent gets a tiny catalog: the server's pinned tools
  mirrored directly, plus two meta-tools synthesized here: ``ha_search_tools``
  (find tools by task) and ``ha_call_tool`` (execute a discovered tool). This
  keeps per-turn context small — the shape context-limited models need.
* **full** — every exposed tool is mirrored directly into the agent's tool
  list, one schema each.

**Per-tool exposure is decided by the server, not here.** The server stamps
every ``tools/list`` entry with ``_meta.ha_mcp = {llm_api_exposed, pinned}``
(see ``src/ha_mcp/llm_exposure.py``): user toggles from the settings UI, with
deny-by-default for beta/developer/restart-reload-backup tools. Both modes
filter on the stamp, and the tool-search ``ha_call_tool`` forwarder re-checks
it at call time — a hidden tool is invisible (absent from lists and search
results) and a hallucinated call to one gets a plain unknown-tool error, the
same answer a nonexistent tool gets, so nothing leaks. Globally-disabled
tools never appear in ``tools/list`` at all and the server rejects calling
them by name, and every forwarded call traverses the server's policy /
read-only middleware exactly like any MCP client's call.

The server runs on its own worker thread behind a loopback HTTP listener, and
``ha_mcp`` must never be imported in the HA main process (see
:mod:`embedded_server`), so this module talks real MCP to the server over
loopback streamable HTTP. The ``mcp`` client SDK arrives with the
runtime-installed ha-mcp package (a fastmcp dependency), so every SDK import
here is lazy and the first one runs on the executor.

The tool list is fetched fresh on every ``async_get_api_instance`` call (once
per conversation turn): exposure toggles and runtime-registered custom tools
apply on the agent's next message, and two loopback round-trips per turn are
noise next to the LLM call itself. Tool calls likewise open a short-lived
stateless session each — the in-process server serves ``stateless_http=True``.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.helpers.httpx_client import get_async_client
from voluptuous_openapi import convert_to_voluptuous

from .const import (
    DATA_LLM_API_UNSUB,
    DEFAULT_LLM_API_EXPOSURE,
    DOMAIN,
    EXPOSURE_BOTH,
    EXPOSURE_FULL,
    EXPOSURE_TOOL_SEARCH,
    OPT_LLM_API_EXPOSURE,
)

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

# The server-side stamp this module filters on (mirrors
# src/ha_mcp/llm_exposure.py — keep the names in sync).
_META_NAMESPACE = "ha_mcp"
_META_EXPOSED_KEY = "llm_api_exposed"
_META_PINNED_KEY = "pinned"

# Fallback exposure policy for servers that predate the stamp: hide the
# operational-hazard names and the known beta/developer tools. Imperfect by
# construction (a newer beta tool on an old server can't be known here) but
# strictly safer than exposing everything, and logged once per instance
# build. The real policy lives server-side.
_FALLBACK_DENY_PREFIXES = ("ha_dev_",)
_FALLBACK_DENY_TOOLS = frozenset(
    {
        "ha_restart",
        "ha_reload_core",
        "ha_manage_backup",
        # Beta-tagged tools as of the stamp's introduction (server-side the
        # gate is tag-based and future-proof; this list is only the legacy
        # fallback).
        "ha_config_set_yaml",
        "ha_manage_custom_tool",
        "ha_get_dashboard_screenshot",
        "ha_install_mcp_tools",
        "ha_list_files",
        "ha_read_file",
        "ha_write_file",
        "ha_delete_file",
    }
)

# Names of the meta-tools synthesized for the tool-search mode. ha_search_tools
# deliberately matches the server's own tool-search terminology; if the server
# itself runs ENABLE_TOOL_SEARCH its identically-named tool is excluded from
# mirroring/search results to avoid duplicates.
_SEARCH_TOOL_NAME = "ha_search_tools"
_CALL_TOOL_NAME = "ha_call_tool"
_SEARCH_RESULT_LIMIT = 8

# Used when the server's initialize result carries no instructions (it always
# should — ha-mcp ships server-level instructions — but never render an empty
# prompt if a build does not).
_FALLBACK_API_PROMPT = (
    "The following tools are provided by the HA-MCP server running inside "
    "Home Assistant. They give full control over this Home Assistant "
    "instance: entities, automations, scripts, dashboards, helpers, and "
    "configuration."
)

_TOOL_SEARCH_PROMPT = (
    "\n\n## Tool Discovery\n"
    "This assistant uses search-based tool discovery: most tools are NOT "
    "listed directly.\n"
    f"1. Call {_SEARCH_TOOL_NAME}(query=...) to find tools for the task; "
    "results include each tool's name, description, and input schema.\n"
    f"2. Execute a discovered tool with {_CALL_TOOL_NAME}(name=..., "
    "arguments={...}) — discovered tools are NOT directly callable here.\n"
    "3. The few tools listed directly can be called as usual.\n"
    "Search once per task, not per call — tool names stay valid all "
    "conversation."
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
    http_client: Any = None,
) -> AsyncIterator[tuple[ClientSession, mcp_types.InitializeResult]]:
    """Open an initialized MCP session against the loopback server.

    Imports resolve from ``sys.modules`` — :func:`async_probe_mcp_sdk` did the
    real (blocking) import on the executor before the API was registered.

    ``http_client`` is Home Assistant's shared httpx client
    (``helpers.httpx_client.get_async_client``). Passing it is what keeps
    this loop-safe: without it the SDK constructs its own httpx client per
    session, whose SSL setup loads the CA bundle SYNCHRONOUSLY inside HA's
    event loop (live-found — HA's blocking-call monitor flagged this exact
    line). HA's shared client is built against the process-cached SSL
    context, and the SDK does not close caller-owned clients (HA core's mcp
    integration relies on the same contract).
    """
    from mcp.client.session import ClientSession

    try:
        from mcp.client.streamable_http import streamable_http_client

        transport = (
            streamable_http_client(url=url, http_client=http_client)
            if http_client is not None
            else streamable_http_client(url=url)
        )
    except ImportError:
        # Pre-rename SDK (an older ha-mcp resolved by a pip-spec override
        # pins an older fastmcp/mcp): same call shape, deprecated name, but
        # no http_client kwarg — it builds its own client, so on those old
        # SDKs the blocking-SSL-setup warning is the accepted cost.
        from mcp.client.streamable_http import (
            streamablehttp_client,
        )

        transport = streamablehttp_client(url=url)

    async with (
        transport as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        init_result = await session.initialize()
        yield session, init_result


def _tool_meta_namespace(tool: Any) -> dict[str, Any] | None:
    """Return the tool's ``_meta.ha_mcp`` namespace, or None when absent."""
    meta = getattr(tool, "meta", None)
    if not isinstance(meta, dict):
        return None
    namespace = meta.get(_META_NAMESPACE)
    return namespace if isinstance(namespace, dict) else None


def _fallback_exposed(name: str) -> bool:
    """Legacy exposure policy for servers that predate the meta stamp."""
    if name.startswith(_FALLBACK_DENY_PREFIXES):
        return False
    return name not in _FALLBACK_DENY_TOOLS


def _partition_tools(tools: Iterable[Any]) -> tuple[list[Any], set[str], bool]:
    """Split a raw tools/list into (exposed tools, pinned names, stamped).

    ``stamped`` is False when NO tool carried the server's exposure stamp —
    an older server package — in which case the conservative component-side
    fallback policy was applied instead.
    """
    stamped = False
    exposed: list[Any] = []
    pinned: set[str] = set()
    for tool in tools:
        namespace = _tool_meta_namespace(tool)
        if namespace is not None and _META_EXPOSED_KEY in namespace:
            stamped = True
            if namespace.get(_META_PINNED_KEY):
                pinned.add(tool.name)
            if namespace.get(_META_EXPOSED_KEY):
                exposed.append(tool)
        elif _fallback_exposed(tool.name):
            exposed.append(tool)
    if not stamped:
        # The fallback path already filtered; recompute pinned as empty (an
        # unstamped server gives no pinned signal — the tool-search mode then
        # simply mirrors nothing directly).
        pinned = set()
    return exposed, pinned, stamped


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
        return await _forward_tool_call(
            hass, self._server_url, self.name, tool_input.tool_args
        )


async def _forward_tool_call(
    hass: HomeAssistant, server_url: str, name: str, arguments: dict[str, Any]
) -> JsonObjectType:
    """Forward one tool call over loopback and dump the result for the agent."""
    try:
        async with (
            asyncio.timeout(_CALL_TOOL_TIMEOUT_SECONDS),
            _mcp_session(server_url, get_async_client(hass)) as (session, _init),
        ):
            result = await session.call_tool(name, arguments)
    except _transport_errors() as err:
        if not _is_transport_failure(err):
            raise
        raise HomeAssistantError(
            f"Error calling the HA-MCP tool {name}: {err}"
        ) from err
    # Full CallToolResult (content blocks, structuredContent, isError) —
    # the same shape HA core's mcp integration hands to agents; ha-mcp
    # signals tool failure via isError + structured error JSON, which the
    # agent reads and reacts to like any tool output.
    return result.model_dump(exclude_unset=True, exclude_none=True)


def _search_score(query_words: list[str], name: str, description: str) -> int:
    """Score a tool against the query (simple word overlap + substring)."""
    haystack = f"{name} {description}".lower()
    name_lower = name.lower()
    score = 0
    for word in query_words:
        if word in name_lower:
            score += 3
        elif word in haystack:
            score += 1
    return score


class HaMcpSearchTool(llm.Tool):
    """Meta-tool: find ha-mcp tools relevant to a task (tool-search mode).

    Searches only the EXPOSED catalog snapshot taken at instance build, so a
    hidden tool can never appear in results.
    """

    name = _SEARCH_TOOL_NAME
    description = (
        "Search the Home Assistant MCP toolset for tools relevant to a task. "
        "Returns each match's name, description, and input schema. Execute "
        f"matches with {_CALL_TOOL_NAME}."
    )
    parameters = vol.Schema({vol.Required("query"): str})

    def __init__(self, catalog: list[dict[str, Any]]) -> None:
        """Hold the exposed-catalog snapshot (name/description/schema dicts)."""
        self._catalog = catalog

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Return the top-scoring exposed tools for the query."""
        query_words = [
            w for w in str(tool_input.tool_args.get("query", "")).lower().split() if w
        ]
        scored = sorted(
            (
                (_search_score(query_words, t["name"], t["description"]), t)
                for t in self._catalog
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        results = [t for score, t in scored[:_SEARCH_RESULT_LIMIT] if score > 0]
        if not results:
            return {
                "results": [],
                "message": (
                    "No matching tools. Try different task words (e.g. "
                    "'automation', 'light', 'history', 'dashboard')."
                ),
            }
        return {"results": results}


class HaMcpCallTool(llm.Tool):
    """Meta-tool: execute a tool discovered via search (tool-search mode).

    The exposure re-check at call time is the enforcement half of the
    tool-search mode: hiding a tool from search results alone would not stop
    a model that guesses a name. A non-exposed name gets the same
    unknown-tool answer a nonexistent name gets — existence never leaks.
    """

    name = _CALL_TOOL_NAME
    description = (
        "Execute a Home Assistant MCP tool by name with a dictionary of "
        f"arguments. Discover tools and their schemas with {_SEARCH_TOOL_NAME} "
        "first."
    )
    parameters = vol.Schema(
        {
            vol.Required("name"): str,
            vol.Optional("arguments", default=dict): dict,
        }
    )

    def __init__(self, server_url: str, exposed_names: set[str]) -> None:
        """Hold the loopback endpoint and the exposed-name allowlist."""
        self._server_url = server_url
        self._exposed_names = exposed_names

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Forward the call when the target is exposed; unknown-tool otherwise."""
        name = str(tool_input.tool_args.get("name", ""))
        arguments = tool_input.tool_args.get("arguments") or {}
        if name not in self._exposed_names:
            return {
                "error": f"Unknown tool '{name}'.",
                "suggestion": (f"Use {_SEARCH_TOOL_NAME} to discover available tools."),
            }
        return await _forward_tool_call(hass, self._server_url, name, arguments)


@dataclass(kw_only=True)
class HaMcpLlmApi(llm.API):
    """The in-process ha-mcp server's toolset as a Home Assistant LLM API."""

    server_url: str
    # Valid instance modes are only tool_search and full — EXPOSURE_BOTH is
    # an option value that _apis_for_mode expands into two instances and must
    # never reach here. The default is the compact/safe shape, matching the
    # option default (review finding: defaulting to full made an omitted
    # mode maximally exposed).
    mode: str = EXPOSURE_TOOL_SEARCH

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
                _mcp_session(self.server_url, get_async_client(self.hass)) as (
                    session,
                    init_result,
                ),
            ):
                list_result = await session.list_tools()
        except _transport_errors() as err:
            if not _is_transport_failure(err):
                raise
            raise HomeAssistantError(
                f"Could not reach the in-process HA-MCP server: {err}"
            ) from err

        exposed, pinned, stamped = _partition_tools(list_result.tools)
        # Never mirror or search a server-side tool that shares a synthesized
        # meta-tool's name (the server's own tool-search mode registers an
        # ha_search_tools) — one name, one behavior.
        exposed = [
            t for t in exposed if t.name not in (_SEARCH_TOOL_NAME, _CALL_TOOL_NAME)
        ]
        if not stamped:
            _LOGGER.warning(
                "The running server does not stamp LLM-API exposure metadata "
                "(older ha-mcp package); applying the component's built-in "
                "conservative deny-list instead. Update the server package "
                "for per-tool control from the settings UI."
            )

        prompt = init_result.instructions or _FALLBACK_API_PROMPT
        # full is the explicit opt-in; anything else — including an unknown
        # value — falls through to the compact/safe tool-search shape.
        if self.mode == EXPOSURE_FULL:
            tools = self._build_full_tools(exposed)
        else:
            tools = self._build_tool_search_tools(exposed, pinned)
            prompt += _TOOL_SEARCH_PROMPT

        return llm.APIInstance(self, prompt, llm_context, tools)

    def _convert_parameters(self, tool: Any) -> vol.Schema | None:
        """Convert one tool's JSON schema, or None (logged) when it fails."""
        try:
            # cast: voluptuous_openapi is an untyped (ignored) import, so the
            # call returns Any; its documented return type is vol.Schema.
            return cast(vol.Schema, convert_to_voluptuous(tool.inputSchema))
        except Exception:
            # One unconvertible schema must not take down the whole
            # toolset for the conversation — skip that tool, loudly.
            _LOGGER.warning(
                "Skipping tool %s: could not convert its input schema",
                tool.name,
                exc_info=True,
            )
            return None

    def _build_full_tools(self, exposed: list[Any]) -> list[llm.Tool]:
        """Mirror every exposed tool directly (full-catalog mode)."""
        tools: list[llm.Tool] = []
        for tool in exposed:
            parameters = self._convert_parameters(tool)
            if parameters is None:
                continue
            tools.append(
                HaMcpTool(tool.name, tool.description, parameters, self.server_url)
            )
        return tools

    def _build_tool_search_tools(
        self, exposed: list[Any], pinned: set[str]
    ) -> list[llm.Tool]:
        """Build the compact catalog: mirrored pinned tools + meta-tools."""
        tools: list[llm.Tool] = []
        exposed_names: set[str] = set()
        catalog: list[dict[str, Any]] = []
        for tool in exposed:
            exposed_names.add(tool.name)
            catalog.append(
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema,
                }
            )
            if tool.name in pinned:
                parameters = self._convert_parameters(tool)
                if parameters is not None:
                    tools.append(
                        HaMcpTool(
                            tool.name, tool.description, parameters, self.server_url
                        )
                    )
        tools.append(HaMcpSearchTool(catalog))
        tools.append(HaMcpCallTool(self.server_url, exposed_names))
        return tools


def _apis_for_mode(
    hass: HomeAssistant, entry: ConfigEntry, server_url: str, exposure: str
) -> list[HaMcpLlmApi]:
    """Build the API registration set for the configured exposure mode."""
    full = HaMcpLlmApi(
        hass=hass,
        id=f"{DOMAIN}-{entry.entry_id}",
        name=entry.title,
        server_url=server_url,
        mode=EXPOSURE_FULL,
    )
    search = HaMcpLlmApi(
        hass=hass,
        id=f"{DOMAIN}-{entry.entry_id}-toolsearch",
        name=f"{entry.title} (tool search)",
        server_url=server_url,
        mode=EXPOSURE_TOOL_SEARCH,
    )
    if exposure == EXPOSURE_FULL:
        return [full]
    if exposure == EXPOSURE_BOTH:
        return [full, search]
    # Default and explicit tool_search both land here; an unknown stored
    # value degrades to the default rather than failing bring-up.
    return [search]


async def async_register_llm_api(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    port: int,
    secret_path: str,
) -> None:
    """Register the toolset as LLM API(s) per the exposure option (advisory).

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

        exposure = str(
            entry.options.get(OPT_LLM_API_EXPOSURE, DEFAULT_LLM_API_EXPOSURE)
        )
        server_url = f"http://127.0.0.1:{port}{secret_path}"
        unsubs = [
            llm.async_register_api(hass, api)
            for api in _apis_for_mode(hass, entry, server_url, exposure)
        ]
        hass.data.setdefault(DOMAIN, {})[DATA_LLM_API_UNSUB] = unsubs
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
        "Registered the HA-MCP toolset as LLM API (%s mode) — select it in a "
        "conversation agent's settings to chat with it (text or voice)",
        exposure,
    )


def async_unregister_llm_api(hass: HomeAssistant) -> None:
    """Unregister the LLM API(s) if registered (idempotent, teardown-safe)."""
    unsubs = hass.data.get(DOMAIN, {}).pop(DATA_LLM_API_UNSUB, None)
    if not unsubs:
        return
    for unsub in unsubs:
        unsub()
