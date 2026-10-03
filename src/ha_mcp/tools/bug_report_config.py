"""Tool configuration diagnostics shared by the issue-report renderers."""

from typing import Any

from ..config import Settings
from ..settings_ui import _persistence
from ..settings_ui._tools_meta import ignored_disabled_tools, tool_config_warnings

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

    config = _persistence.effective_tool_config(settings)
    ignored = ignored_disabled_tools(config, settings)
    requested_count = sum(
        state == "disabled" for state in config.get("tools", {}).values()
    )
    toggles.update(
        requested_disabled_tools_count=requested_count,
        effective_disabled_tools_count=requested_count - len(ignored),
        ignored_disabled_tools=ignored,
        tool_config_warnings=tool_config_warnings(config, settings),
    )
    return toggles


def _format_config_toggles_for_template(toggles: dict[str, Any]) -> str:
    """Render configuration and ignored-disable warnings for issue bodies."""
    if not toggles:
        return "_(config toggles unavailable)_"
    return "\n".join(f"- **{key}:** `{value}`" for key, value in toggles.items())
