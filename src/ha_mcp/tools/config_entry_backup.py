"""Flow-helper backups that depend on Core's helper flow list.

A flow helper is a config entry, so edits and deletions reaching it through
the integration tools back it up as ``helper_<type>``, through its options,
like ha_config_set_helper does (#2632). The same list decides whether a type
the component's listing did not cover is a failed read or no helper at all.
"""

from typing import Any

from ..backup_manager import (
    _FlowHelperBackupSkip,
    _FlowHelperReadError,
    _is_flow_helper_domain,
)
from .helper_flows import helper_flow_types, listed_helper_flows


def helper_backup_id(kwargs: dict[str, Any]) -> str:
    """The id ha_config_set_helper snapshots; empty for a create.

    A config subentry is keyed by its parent entry and its own id. Creating
    one (no ``subentry_id``) leaves nothing to snapshot.
    """
    if kwargs.get("helper_type") == "config_subentry":
        entry_id, subentry_id = kwargs.get("entry_id"), kwargs.get("subentry_id")
        return f"{entry_id}/{subentry_id}" if entry_id and subentry_id else ""
    return str(kwargs.get("helper_id") or kwargs.get("entry_id") or "")


def removal_backup_id(kwargs: dict[str, Any]) -> str:
    """The id ha_remove_helpers_integrations snapshots before deleting."""
    if kwargs.get("helper_type") == "config_subentry":
        return helper_backup_id({**kwargs, "entry_id": kwargs.get("target")})
    return str(kwargs.get("target") or "")


def flow_helper_backup_domain(kwargs: dict[str, Any]) -> str:
    return f"helper_{kwargs.get('helper_type')}"


def skip_unless_flow_helper(kwargs: dict[str, Any]) -> bool:
    return not _is_flow_helper_domain(flow_helper_backup_domain(kwargs))


async def uncovered_flow_helper_error(
    client: Any, helper_type: str
) -> _FlowHelperReadError:
    """Why the component's helper listing did not cover ``helper_type``.

    A helper flow Core lists is missing there when the component predates
    reading Core's list (it carries a fixed copy) or its own read of Core's
    loader failed (custom helper integrations drop out): that is a failed read.
    A type Core does not list is no helper at all, so there is nothing to back
    up. A failed read of Core's list raises instead.
    """
    if helper_type in await listed_helper_flows(client, helper_type):
        return _FlowHelperReadError(
            f"The component cannot authoritatively read {helper_type} helpers",
            f"{helper_type}_read_unsupported",
        )
    return _FlowHelperBackupSkip(
        f"{helper_type} is not a helper flow this Home Assistant lists",
        "unknown_helper_type",
    )


async def resolve_config_entry_backup_domain(
    client: Any, kwargs: dict[str, Any], domain: str, entry_id: str
) -> str:
    """Snapshot a flow helper as ``helper_<type>`` (its options) when a generic
    options edit or entry deletion targets its config entry."""
    if domain != "integration" or "." in entry_id:
        return domain
    edits_options = kwargs.get("config") is not None and kwargs.get("enabled") is None
    deletes_entry = (
        kwargs.get("target") is not None and kwargs.get("helper_type") is None
    )
    if not (edits_options or deletes_entry):
        # Enable/disable restores must retain the integration's disabled flag.
        return domain
    # The entry's domain can be any integration, so it is checked against Core's
    # helper flows rather than inferred from the snapshot-domain shape.
    entry = await client.get_config_entry(entry_id)
    if entry.get("domain") in await helper_flow_types(client):
        return f"helper_{entry.get('domain')}"
    return domain
