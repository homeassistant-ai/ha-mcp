"""Tool metadata + visibility model for the settings UI.

Builds the per-tool metadata rows the settings page renders (real tools
via FastMCP's ``local_provider`` plus injected stubs for
transform-generated / feature-gated tools) and applies the persisted
enabled/disabled/pinned state onto the live FastMCP instance
(``apply_tool_visibility``). The capability badge tier comes from the
same ``categorize_capability`` classifier that routes the
read/write/delete proxies, so the badge and the routing never disagree.

Leaf module (no imports from the settings_ui package) so the handler
families and ``__init__`` can depend on it without cycles.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, NamedTuple, NotRequired, TypedDict

from ..transforms import categorize_capability

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from ..config import Settings
    from ..server import HomeAssistantSmartMCPServer

logger = logging.getLogger(__name__)


class ToolStub(TypedDict):
    """Metadata advertised in the settings UI for a tool that isn't visible
    in ``local_provider._list_tools()``.

    Two reasons a tool needs a stub: it's added by a FastMCP transform at
    runtime (``TRANSFORM_GENERATED_TOOLS``), or it's feature-gated and
    only registers when a setting is on (``FEATURE_GATED_TOOLS``). The
    consumer (`_get_tool_metadata`) renders the same shape for both;
    ``disabled_by`` is the only field that differs and signals UI
    placement of the "Beta — set X" hint.
    """

    title: str
    primary_tag: str
    description: str
    readOnlyHint: NotRequired[bool]
    destructiveHint: NotRequired[bool]
    disabled_by: NotRequired[str]


_VALID_STATES = frozenset({"enabled", "disabled", "pinned"})

# Tools that are always enabled regardless of saved config — the server
# strips them out of any disable list before applying. Five of these
# overlap with DEFAULT_PINNED_TOOLS in transforms/categorized_search.py
# (ha_search, ha_get_overview, ha_report_issue, ha_get_skill_guide,
# ha_manage_backup); ha_get_state is mandatory but not pinned-by-default
# because it is reachable via the ha_call_read_tool proxy when tool search
# is on. Keep these lists in sync where it matters and divergent where it
# matters — don't merge them.
MANDATORY_TOOLS: set[str] = {
    "ha_search",
    "ha_get_overview",
    "ha_get_state",
    "ha_report_issue",
    # Skill guide carries the bundled best-practices trigger conditions
    # in its description — tool-only clients (claude.ai, etc.) rely on
    # seeing it in the catalog. Disabling it would silently break the
    # "consult skill before writing config" workflow.
    "ha_get_skill_guide",
    # Backups are operational essentials — needed as the pre-change safety
    # net before config edits and as the recovery path after them. Kept
    # always-on so users who aggressively disable everything keep a
    # working backup tool.
    "ha_manage_backup",
}

# Tools created by FastMCP transforms (not registered through
# local_provider). No transform-generated tools are currently in use —
# ``ha_get_skill_guide`` is registered the normal way and is visible
# through ``local_provider._list_tools()``. Kept as an empty dict so
# UI rendering, type contracts, and tests don't need to special-case
# the "no transform tools" path; populate when a future transform
# appends tools that need settings-UI visibility.
TRANSFORM_GENERATED_TOOLS: dict[str, ToolStub] = {}

# Tools that exist in the codebase but are only registered when a
# corresponding feature flag/env var is set. When the flag is off, these
# won't appear in local_provider._list_tools(), so we inject stub entries
# into the settings UI so users discover the tool exists and how to enable
# it. Keep this dict in sync with the ``"beta"`` tag added to each tool's
# source file (tools_yaml_config.py, tools_filesystem.py, tools_code.py) — a
# future rename or removal needs to land in both places.
FEATURE_GATED_TOOLS: dict[str, ToolStub] = {
    "ha_config_set_yaml": {
        "title": "Set YAML Config",
        "primary_tag": "System",
        "description": "Add, replace, or remove top-level keys in configuration.yaml or package files.",
        "disabled_by": "enable_yaml_config_editing",
        "destructiveHint": True,
    },
    "ha_manage_custom_tool": {
        "title": "Custom Tool",
        "primary_tag": "System",
        "description": "Create and run a custom tool in a sandbox, or manage saved custom tools (code mode).",
        "disabled_by": "enable_code_mode",
        "destructiveHint": True,
    },
    "ha_list_files": {
        "title": "List Files",
        "primary_tag": "Files",
        "description": "List files in a directory within the Home Assistant config.",
        "disabled_by": "enable_filesystem_tools",
        "readOnlyHint": True,
    },
    "ha_read_file": {
        "title": "Read File",
        "primary_tag": "Files",
        "description": "Read a file from the Home Assistant config directory.",
        "disabled_by": "enable_filesystem_tools",
        "readOnlyHint": True,
    },
    "ha_write_file": {
        "title": "Write File",
        "primary_tag": "Files",
        "description": "Write a file to allowed directories in the Home Assistant config.",
        "disabled_by": "enable_filesystem_tools",
        "destructiveHint": True,
    },
    "ha_delete_file": {
        "title": "Delete File",
        "primary_tag": "Files",
        "description": "Delete a file from allowed directories.",
        "disabled_by": "enable_filesystem_tools",
        "destructiveHint": True,
    },
}


def _capability_fields(
    name: str, *, read_only: bool, destructive: bool
) -> dict[str, Any]:
    """Build the capability fields (``annotations`` + ``category``) of a tool dict.

    Shared by ``_render_stub`` (stubs) and ``_get_tool_metadata`` (real tools)
    so the JSON ``annotations`` payload and the badge ``category`` are derived
    from a single place and can't drift. Hints are dropped from ``annotations``
    when False so the payload stays small; ``category`` comes from the same
    classifier that routes the read/write/delete proxies.
    """
    annotations: dict[str, bool] = {}
    if read_only:
        annotations["readOnlyHint"] = True
    if destructive:
        annotations["destructiveHint"] = True
    return {
        "annotations": annotations,
        "category": categorize_capability(
            name, read_only=read_only, destructive=destructive
        ),
    }


def _render_stub(name: str, meta: ToolStub) -> dict[str, Any]:
    """Render a ToolStub as the dict shape ``_get_tool_metadata`` returns.

    Both transform-generated and feature-gated stubs share the same UI
    representation; the only meaningful difference is whether
    ``disabled_by`` carries the safety-toggle name (which the JS
    template renders as a "Beta — set X" hint).
    """
    rendered: dict[str, Any] = {
        "name": name,
        "title": meta["title"],
        "description": meta["description"],
        "tags": [meta["primary_tag"]],
        "primary_tag": meta["primary_tag"],
        **_capability_fields(
            name,
            read_only=bool(meta.get("readOnlyHint")),
            destructive=bool(meta.get("destructiveHint")),
        ),
    }
    if "disabled_by" in meta:
        rendered["disabled_by"] = meta["disabled_by"]
    return rendered


async def _get_tool_metadata(
    server: HomeAssistantSmartMCPServer,
) -> list[dict[str, Any]]:
    """Extract metadata for all registered tools from the server.

    Uses FastMCP's internal ``local_provider._list_tools()`` because the
    public ``mcp.list_tools()`` filters out tools marked as disabled via
    ``mcp.disable()``. The settings UI specifically needs the UNFILTERED
    list so that users can see and re-enable tools they previously
    disabled. There is no public FastMCP API that returns the unfiltered
    list as of v3.2.0.
    """
    tools: list[dict[str, Any]] = []
    # Groups not considered "primary" when choosing a tool's canonical group —
    # these are cross-cutting tags (e.g. Z-Wave, Zigbee) that should not
    # override the tool's real domain group.
    secondary_tags = {"Z-Wave", "Zigbee"}

    registered = await server.mcp.local_provider._list_tools()
    for tool in registered:
        tags = sorted(tool.tags) if tool.tags else []
        primary_tags = [t for t in tags if t not in secondary_tags]
        primary = primary_tags[0] if primary_tags else (tags[0] if tags else "Other")
        read_only = bool(
            tool.annotations and getattr(tool.annotations, "readOnlyHint", None)
        )
        destructive = bool(
            tool.annotations and getattr(tool.annotations, "destructiveHint", None)
        )
        title = getattr(tool, "title", None) or tool.name
        if tool.annotations and getattr(tool.annotations, "title", None):
            title = tool.annotations.title
        tools.append(
            {
                "name": tool.name,
                "title": title,
                "description": (tool.description or "")[:200],
                "tags": tags,
                "primary_tag": primary,
                **_capability_fields(
                    tool.name, read_only=read_only, destructive=destructive
                ),
            }
        )

    registered_names = {t["name"] for t in tools}

    # Inject stub entries for tools generated by FastMCP transforms — these
    # never reach local_provider so they have to be advertised explicitly.
    for name, transform_meta in TRANSFORM_GENERATED_TOOLS.items():
        if name in registered_names:
            continue
        tools.append(_render_stub(name, transform_meta))
        registered_names.add(name)

    # Inject stub entries for feature-gated tools that aren't registered
    for name, meta in FEATURE_GATED_TOOLS.items():
        if name in registered_names:
            continue
        tools.append(_render_stub(name, meta))

    tools.sort(key=lambda t: (t["primary_tag"], t["name"]))
    return tools


class UserToolStateOverrides(NamedTuple):
    """User-explicit per-tool state overrides loaded from tool_config.json.

    Both sets are immutable frozensets so callers can't pollute the
    return value. They are disjoint by construction (a tool_config entry
    has one state per tool).

    - ``pinned_names``: tools the user explicitly set to "pinned"
    - ``enabled_names``: tools the user explicitly set to "enabled"
      (used by _apply_tool_search to unpin defaults the user re-enabled)
    """

    pinned_names: frozenset[str]
    enabled_names: frozenset[str]


def apply_tool_visibility(
    mcp: FastMCP,
    config: dict[str, Any],
    settings: Settings,
) -> UserToolStateOverrides:
    """Apply tool visibility from config, respecting safety toggles.

    Args:
        mcp: The FastMCP instance to enable/disable tools on.
        config: The tool_config.json contents (per-tool states).
        settings: The server Settings (for enable_yaml_config_editing etc.).

    Returns:
        A :class:`UserToolStateOverrides` carrying the user-pinned tools
        and the user-explicitly-enabled tools. The caller (server.py)
        uses ``enabled_names`` to filter ``DEFAULT_PINNED_TOOLS`` so a
        user can unpin a default by flipping it to "enabled" in the UI.
    """
    disabled_names: set[str] = set()
    pinned_names: set[str] = set()
    enabled_names: set[str] = set()

    tool_states = config.get("tools", {})
    for name, state in tool_states.items():
        if state == "disabled":
            disabled_names.add(name)
        elif state == "pinned":
            pinned_names.add(name)
        elif state == "enabled":
            enabled_names.add(name)

    # AND semantics for the YAML safety toggle: the tool is disabled if
    # *either* the safety toggle is off *or* the user disabled it in the UI.
    # Kept as defense-in-depth even though tools_yaml_config.py already
    # early-returns when the toggle is off (the tool isn't registered, so
    # mcp.disable() is a no-op in that case) — if the registration site
    # ever moves, this still keeps the tool out of the visible catalog.
    if not settings.enable_yaml_config_editing:
        disabled_names.add("ha_config_set_yaml")

    disabled_names -= MANDATORY_TOOLS

    if disabled_names:
        mcp.disable(names=disabled_names)
        logger.info("Disabled tools: %s", ", ".join(sorted(disabled_names)))

    mcp.enable(names=MANDATORY_TOOLS)

    assert pinned_names.isdisjoint(enabled_names), (
        "pinned and enabled overrides must be disjoint by construction"
    )

    return UserToolStateOverrides(
        pinned_names=frozenset(pinned_names),
        enabled_names=frozenset(enabled_names),
    )
