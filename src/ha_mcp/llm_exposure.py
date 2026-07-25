"""Per-tool exposure control for the HA conversation-agent LLM API (#1745).

The ``ha_mcp_tools`` custom component registers this server's toolset as a
Home Assistant LLM API so conversation agents (and through them Assist chat
and voice) can drive the tools. Which tools that surface offers is a separate
axis from the global enable/disable in the settings UI:

* a tool **disabled** in the settings UI is gone for every client — FastMCP
  removes it from ``tools/list`` and rejects ``tools/call`` by name;
* a tool **hidden from the LLM API** (this module) stays fully available to
  regular MCP clients (claude.ai, Claude Code, ...) and is only invisible to
  conversation agents — in both the full-catalog and tool-search exposure
  modes of the component.

The single source of truth travels **in-band**: :class:`LlmExposureMiddleware`
stamps every ``tools/list`` entry with
``_meta.ha_mcp = {"llm_api_exposed": bool, "pinned": bool, "policy": {...}}``
so the component (one more loopback MCP client) filters on data that can never
drift from the server's settings, with zero extra round-trips. The ``policy``
block reports the serving server's gating state (#1990 — see META_POLICY_KEY). Stamping re-reads the
persisted settings behind a short coalescing cache (2s TTL), so settings-UI
changes apply on the agent's next conversation turn without a restart.

Defaults are deny-by-default for the risky sets (owner decision, #1745):
beta-tagged tools, developer-mode tools, and the restart/reload/backup
family start hidden from conversation agents until a user explicitly enables
them in the settings UI. Everything else starts exposed — selecting the LLM
API on an agent is itself an explicit opt-in.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from fastmcp.server.middleware import Middleware

if TYPE_CHECKING:
    from fastmcp.server.middleware import CallNext, MiddlewareContext
    from fastmcp.tools import Tool
    from mcp import types as mt

logger = logging.getLogger(__name__)

# Key under tools/list ``_meta`` (and the key inside it) the custom component
# filters on. The component treats an ABSENT stamp as "server too old for
# per-tool exposure" and falls back to its own conservative deny-list.
META_NAMESPACE = "ha_mcp"
META_EXPOSED_KEY = "llm_api_exposed"
META_PINNED_KEY = "pinned"

# Serving-server policy/identity block stamped alongside the per-tool keys
# (#1990). A client (or a debugging agent) reading tools/list can see the
# ACTUAL gating state of the server answering this connection — configured
# flag, live middleware, and rule count — plus the deployment mode. Without
# this, a client pointed at a different server than the one the user
# configured rules on fails silently: calls execute ungated and nothing
# anywhere says "this server has zero rules".
META_POLICY_KEY = "policy"

# Key in tool_config.json holding the user's per-tool overrides
# ({tool_name: bool}). Sparse on purpose: only tools the user explicitly
# flipped are stored, so tools added by future releases keep getting their
# DEFAULT (e.g. a new beta tool defaults hidden) instead of freezing the
# catalog shape at save time.
LLM_API_CONFIG_KEY = "llm_api"

# Tools hidden from conversation agents by default regardless of tags:
# operational hazards where an agent mistake is expensive (restart loops,
# config reloads mid-edit, backup churn). Users can still enable each in the
# settings UI.
LLM_API_DEFAULT_OFF_TOOLS: frozenset[str] = frozenset(
    {
        "ha_restart",
        "ha_reload_core",
        "ha_manage_backup",
    }
)

# Developer-mode tools are hidden by default via this name prefix (covers
# ha_dev_manage_settings / ha_dev_manage_server and any future ha_dev_*).
LLM_API_DEV_TOOL_PREFIX = "ha_dev_"

# The beta gate is tag-based so future beta tools default to hidden without
# touching this module.
_BETA_TAG = "beta"

# How long a live settings read is reused before re-reading the config file.
# tools/list fires once per conversation turn per agent; the TTL only matters
# when several agents list within the same moment.
_OVERRIDES_TTL_SECONDS = 2.0


def default_llm_api_exposed(name: str, tags: Sequence[str] | set[str]) -> bool:
    """Return the default LLM-API exposure for a tool (no user override)."""
    if _BETA_TAG in set(tags):
        return False
    if name.startswith(LLM_API_DEV_TOOL_PREFIX):
        return False
    return name not in LLM_API_DEFAULT_OFF_TOOLS


def effective_llm_api_exposed(
    name: str,
    tags: Sequence[str] | set[str],
    overrides: Mapping[str, Any],
) -> bool:
    """Return the effective exposure: user override if set, else the default."""
    override = overrides.get(name)
    if isinstance(override, bool):
        return override
    return default_llm_api_exposed(name, tags)


def load_llm_api_overrides() -> dict[str, bool]:
    """Read the persisted per-tool overrides from tool_config.json (live)."""
    # Imported here, not at module top: settings_ui imports this module for
    # the defaults, so a top-level import would be circular.
    from .settings_ui import load_tool_config

    config = load_tool_config()
    # load_tool_config returns whatever the JSON file parses to — guard the
    # valid-JSON-but-not-an-object case (e.g. `[]`) instead of raising.
    if not isinstance(config, dict):
        return {}
    raw = config.get(LLM_API_CONFIG_KEY, {})
    if not isinstance(raw, dict):
        return {}
    return {
        name: value
        for name, value in raw.items()
        if isinstance(name, str) and isinstance(value, bool)
    }


def _pinned_tool_names() -> set[str]:
    """Return the effective pinned set, matching the server's tool-search view.

    Mirrors ``server.py::_apply_tool_search``'s always-visible computation:
    the built-in DEFAULT_PINNED_TOOLS minus tools the user explicitly set to
    plain "enabled" (an unpin), plus every tool the user set to "pinned"
    (file or env). The component mirrors pinned tools directly into the
    agent's tool list when the exposure mode is tool-search.
    """
    from .settings_ui import effective_tool_config
    from .transforms import DEFAULT_PINNED_TOOLS

    states = effective_tool_config().get("tools", {})
    pinned = {name for name, state in states.items() if state == "pinned"}
    unpinned_defaults = {
        name for name in DEFAULT_PINNED_TOOLS if states.get(name) == "enabled"
    }
    return (set(DEFAULT_PINNED_TOOLS) - unpinned_defaults) | pinned


class LlmExposureMiddleware(Middleware):
    """Stamp every listed tool with its LLM-API exposure + pinned state.

    Runs after the visibility layer, so globally-disabled tools are already
    absent and never carry a stamp. The stamp is additive metadata — no tool
    is hidden or altered for regular MCP clients.
    """

    def __init__(self, policy_live: Callable[[], bool] | None = None) -> None:
        """Initialize the short-lived settings cache.

        ``policy_live`` reports whether the gating middleware/queue are
        actually wired on this server (they only wire at startup); the
        stamp carries it so "configured but not enforcing until restart"
        is visible on the wire.
        """
        self._cache: tuple[float, dict[str, bool], set[str]] | None = None
        self._policy_live = policy_live
        self._policy_cache: tuple[float, dict[str, Any]] | None = None

    def _current_settings(self) -> tuple[dict[str, bool], set[str]]:
        """Return (overrides, pinned) with a short TTL over the file reads."""
        now = time.monotonic()
        if self._cache is not None:
            stamp, overrides, pinned = self._cache
            if now - stamp < _OVERRIDES_TTL_SECONDS:
                return overrides, pinned
        try:
            overrides = load_llm_api_overrides()
            pinned = _pinned_tool_names()
        except Exception:
            # Stamping must never break tools/list for regular clients — but
            # the fallback direction matters for an exposure CONTROL: pure
            # defaults would re-EXPOSE tools the user explicitly hid (their
            # override lives in the unreadable settings), so serve the
            # last-known-good values when we have them (review finding).
            # Only a failure with no prior successful read stamps pure
            # defaults. Visibly, either way.
            if self._cache is not None:
                _stamp, overrides, pinned = self._cache
                logger.warning(
                    "Could not read LLM-API exposure settings; keeping the "
                    "last known values",
                    exc_info=True,
                )
            else:
                overrides, pinned = {}, set()
                logger.warning(
                    "Could not read LLM-API exposure settings and no prior "
                    "read succeeded; stamping defaults",
                    exc_info=True,
                )
        self._cache = (now, overrides, pinned)
        return overrides, pinned

    def _policy_block(self) -> dict[str, Any]:
        """Serving-server policy/identity block (TTL-cached like the overrides).

        Best-effort: a failure to read settings or the policy file stamps
        conservative values (enabled=False / rules=0) rather than breaking
        tools/list — the block is diagnostic metadata, not enforcement.
        """
        now = time.monotonic()
        if (
            self._policy_cache is not None
            and now - self._policy_cache[0] < _OVERRIDES_TTL_SECONDS
        ):
            return self._policy_cache[1]
        from ._version import is_embedded, is_running_in_addon

        block: dict[str, Any] = {
            "enabled": False,
            "live": bool(self._policy_live()) if self._policy_live else False,
            "rules": 0,
            "deployment": (
                "embedded"
                if is_embedded()
                else ("addon" if is_running_in_addon() else "standalone")
            ),
        }
        try:
            from .config import get_global_settings

            block["enabled"] = bool(get_global_settings().enable_tool_security_policies)
        except Exception:
            logger.debug("policy stamp: settings read failed", exc_info=True)
        try:
            from .policy.persistence import load_policy
            from .utils.data_paths import get_data_dir

            block["rules"] = len(load_policy(get_data_dir()).rules)
        except Exception:
            logger.debug("policy stamp: policy read failed", exc_info=True)
        self._policy_cache = (now, block)
        return block

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Stamp ``_meta.ha_mcp`` on every tool in the list result."""
        tools = await call_next(context)
        overrides, pinned = self._current_settings()
        policy_block = self._policy_block()

        stamped: list[Tool] = []
        for tool in tools:
            meta = dict(tool.meta or {})
            namespace = dict(meta.get(META_NAMESPACE) or {})
            namespace[META_EXPOSED_KEY] = effective_llm_api_exposed(
                tool.name, tool.tags or set(), overrides
            )
            namespace[META_PINNED_KEY] = tool.name in pinned
            namespace[META_POLICY_KEY] = policy_block
            meta[META_NAMESPACE] = namespace
            stamped.append(tool.model_copy(update={"meta": meta}))
        return stamped
