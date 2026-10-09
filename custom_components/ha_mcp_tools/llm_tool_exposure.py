"""Read the server's per-tool LLM API exposure stamp (#1745).

The server stamps every ``tools/list`` entry with
``_meta.ha_mcp = {llm_api_exposed, pinned, params}`` (see
``src/ha_mcp/llm_exposure.py``); :mod:`llm_api` filters on it. This module
holds the stamp keys, the legacy fallback for servers that predate the stamp,
and the split of a raw tool list into exposed tools and pinned names.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# The server-side stamp this module filters on (mirrors
# src/ha_mcp/llm_exposure.py — keep the names in sync).
META_NAMESPACE = "ha_mcp"
META_EXPOSED_KEY = "llm_api_exposed"
META_PINNED_KEY = "pinned"
# The server's one-line parameter summary for compact search hits (#2633);
# absent on servers that predate it.
META_PARAMS_KEY = "params"

# Fallback exposure policy for servers that predate the stamp: hide the
# operational-hazard names and the known beta/developer tools. Imperfect by
# construction (a newer beta tool on an old server can't be known here) but
# strictly safer than exposing everything, and logged once per instance
# build. The real policy lives server-side.
FALLBACK_DENY_PREFIXES = ("ha_dev_",)
FALLBACK_DENY_TOOLS = frozenset(
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


def _tool_meta_namespace(tool: Any) -> dict[str, Any] | None:
    """Return the tool's ``_meta.ha_mcp`` namespace, or None when absent."""
    meta = getattr(tool, "meta", None)
    if not isinstance(meta, dict):
        return None
    namespace = meta.get(META_NAMESPACE)
    return namespace if isinstance(namespace, dict) else None


def _fallback_exposed(name: str) -> bool:
    """Legacy exposure policy for servers that predate the meta stamp."""
    if name.startswith(FALLBACK_DENY_PREFIXES):
        return False
    return name not in FALLBACK_DENY_TOOLS


def partition_tools(tools: Iterable[Any]) -> tuple[list[Any], set[str], bool]:
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
        if namespace is not None and META_EXPOSED_KEY in namespace:
            stamped = True
            if namespace.get(META_PINNED_KEY):
                pinned.add(tool.name)
            if namespace.get(META_EXPOSED_KEY):
                exposed.append(tool)
        elif _fallback_exposed(tool.name):
            exposed.append(tool)
    if not stamped:
        # The fallback path already filtered; recompute pinned as empty (an
        # unstamped server gives no pinned signal — the tool-search mode then
        # simply mirrors nothing directly).
        pinned = set()
    return exposed, pinned, stamped
