"""The backup domain a config-entry write snapshots under.

A flow helper is a config entry, so edits and deletions reaching it through
the integration tools back it up as ``helper_<type>``, through its options,
like ha_config_set_helper does (#2632).
"""

from typing import Any

from ..backup_manager import _is_flow_helper_domain


def flow_helper_backup_domain(kwargs: dict[str, Any]) -> str:
    return f"helper_{kwargs.get('helper_type')}"


def skip_unless_flow_helper(kwargs: dict[str, Any]) -> bool:
    return not _is_flow_helper_domain(flow_helper_backup_domain(kwargs))


async def resolve_config_entry_backup_domain(
    client: Any, kwargs: dict[str, Any], domain: str, entry_id: str
) -> str:
    """Capture a flow helper's options for generic options edits and entry deletion."""
    if domain != "integration" or "." in entry_id:
        return domain
    edits_options = kwargs.get("config") is not None and kwargs.get("enabled") is None
    deletes_entry = (
        kwargs.get("target") is not None and kwargs.get("helper_type") is None
    )
    if not (edits_options or deletes_entry):
        # Enable/disable restores must retain the integration's disabled flag.
        return domain
    entry = await client.get_config_entry(entry_id)
    helper_domain = f"helper_{entry.get('domain')}"
    return helper_domain if _is_flow_helper_domain(helper_domain) else domain
