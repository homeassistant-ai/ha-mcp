"""What Home Assistant knows about an automation beyond its stored body.

``ha_config_get_automation`` reports the automation's entity registry row, the
load failure Core recorded for it and the references Core finds in it. The
component's ``automation_config`` lookup answers all three in process. Without
it the registry row comes from ``config/entity_registry/get`` and the load
failure from Core's ``{entity_id}_validation_*`` repair issue; Core serves no
references over its API, so the read carries Core's ``search/related`` answer
in their place, under its own key because it is the broader Related set the
frontend shows.
"""

from __future__ import annotations

from typing import Any

from ..client.rest_client import HomeAssistantError
from .config_write_helpers import fetch_registry_entry

REFERENCES_NEED_COMPONENT = (
    "references need the ha_mcp_tools component's automation_config bridge, "
    "which is not available; related holds Core's broader Related set instead "
    "(also the devices, areas and integrations of what the automation uses)"
)
REFERENCES_OMITTED_WITHOUT_COMPONENT = (
    "references need the ha_mcp_tools component's automation_config bridge, "
    "which is not available; references are omitted when the component is absent"
)
REFERENCES_NOT_LOADED = (
    "references unavailable: Home Assistant has not loaded this automation "
    "(disabled, or it failed to load)"
)


async def registry_details(
    client: Any,
    lookup: dict[str, Any] | None,
    entity_id: str | None,
    warnings: list[str],
    *,
    component_lookup: bool = True,
    related_fallback: bool = True,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """The item's registry row, Core's validation verdict and references.

    ``lookup`` is the component's reply, None when the component did not answer.
    Set ``component_lookup`` false for a domain the component does not serve
    (script): the registry read is still valid, and no missing-component
    warning should be reported. Set ``related_fallback`` false when the caller
    does not also report Core's Related set, so the warning does not promise it.
    """
    if lookup is not None:
        references = lookup.get("references")
        if (
            references is None
            and lookup.get("loaded") is False
            and lookup.get("found") is not False
        ):
            warnings.append(REFERENCES_NOT_LOADED)
        return lookup.get("entity"), lookup.get("validation"), references
    if not component_lookup:
        if entity_id is None:
            return None, None, None
        return await fetch_registry_entry(client, entity_id, warnings), None, None
    warnings.append(
        REFERENCES_NEED_COMPONENT
        if related_fallback
        else REFERENCES_OMITTED_WITHOUT_COMPONENT
    )
    if entity_id is None:
        return None, None, None
    entity = await fetch_registry_entry(client, entity_id, warnings)
    return entity, await repair_validation(client, entity_id, warnings), None


async def core_related(
    client: Any, entity_id: str | None, warnings: list[str]
) -> dict[str, Any] | None:
    """Core's Related set for the automation, what its UI's Related panel shows."""
    if entity_id is None:
        warnings.append(
            "related unavailable: no automation entity was found for this config id"
        )
        return None
    try:
        result = await client.send_websocket_message(
            {"type": "search/related", "item_type": "automation", "item_id": entity_id}
        )
    except HomeAssistantError as e:
        warnings.append(f"related unavailable: {e}")
        return None
    if not result.get("success"):
        reason = result.get("error") or "request failed"
        warnings.append(f"related unavailable: {reason}")
        return None
    related = result.get("result")
    return related if isinstance(related, dict) else None


async def repair_validation(
    client: Any, entity_id: str, warnings: list[str]
) -> dict[str, Any] | None:
    """Core's validation verdict from the repair issue it raises for a failed load."""
    try:
        result = await client.send_websocket_message({"type": "repairs/list_issues"})
    except HomeAssistantError as e:
        warnings.append(f"validation status unavailable: {e}")
        return None
    if not result.get("success"):
        reason = result.get("error") or "request failed"
        warnings.append(f"validation status unavailable: {reason}")
        return None
    prefix = f"{entity_id}_validation_"
    for issue in (result.get("result") or {}).get("issues") or []:
        issue_id = issue.get("issue_id")
        if (
            issue.get("domain") != "automation"
            or not isinstance(issue_id, str)
            or not issue_id.startswith(prefix)
        ):
            continue
        status = issue_id.removeprefix(prefix)
        # Another remainder belongs to an automation whose entity_id
        # extends this one (automation.x_validation_failed_y).
        if status.startswith("failed_") and "_validation_" not in status:
            placeholders = issue.get("translation_placeholders") or {}
            return {"status": status, "error": placeholders.get("error")}
    return None
