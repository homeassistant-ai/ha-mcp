"""Tool configuration diagnostics shared by the issue-report renderers."""

import logging
from typing import Any

from ..config import Settings
from ..settings_ui import _persistence
from ..settings_ui._tools_meta import ignored_disabled_tools, tool_config_warnings

logger = logging.getLogger(__name__)

_CONFIG_TOGGLE_FIELDS: tuple[str, ...] = (
    "enable_beta_features",
    "read_only_mode",
    "enable_tool_security_policies",
    "enable_websocket",
    "enable_dashboard_partial_tools",
    "enable_tool_search",
    "tool_search_max_results",
    "enable_yaml_config_editing",
    "enable_filesystem_tools",
    "enable_code_mode",
    "enabled_tool_modules",
    "enable_snapshot_actions",
    "backup_read_only",
)


def collect_config_toggles(settings: Settings) -> dict[str, Any]:
    """Capture settings and the same requested tool states used at startup."""
    toggles = {
        field: value
        for field in _CONFIG_TOGGLE_FIELDS
        if (value := getattr(settings, field, None)) is not None
    }
    # Preserve the existing environment-seed counters for report consumers.
    for field in ("disabled_tools", "pinned_tools"):
        raw = getattr(settings, field, "") or ""
        toggles[f"{field}_count"] = len(
            [item for item in raw.split(",") if item.strip()]
        )

    toggles.update(_tool_config_diagnostics(settings))
    return toggles


def _tool_config_diagnostics(settings: Settings) -> dict[str, Any]:
    """Keep unreadable tool states distinct from a confirmed empty disable list."""
    try:
        config = _persistence.effective_tool_config(settings)
        states = config.get("tools", {})
        if not isinstance(states, dict):
            raise TypeError("tools must be a state mapping")
        ignored = ignored_disabled_tools(config, settings)
        requested_count = sum(state == "disabled" for state in states.values())
        return {
            "tool_config_status": "available",
            "requested_disabled_tools_count": requested_count,
            "effective_disabled_tools_count": requested_count - len(ignored),
            "ignored_disabled_tools": ignored,
            "tool_config_warnings": tool_config_warnings(config, settings),
        }
    except Exception as exc:
        logger.exception("Failed to collect tool configuration diagnostics")
        return {
            "tool_config_status": f"unavailable ({type(exc).__name__})",
            "requested_disabled_tools_count": None,
            "effective_disabled_tools_count": None,
            "ignored_disabled_tools": None,
            "tool_config_warnings": None,
        }
