"""Diagnostics for empty trace results."""

import logging
from typing import Any

from ..client.rest_client import HomeAssistantError
from .automation_registry_details import registry_details
from .component_automation import automation_lookup

logger = logging.getLogger(__name__)


async def _gather_diagnostics(
    client: Any,
    entity_id: str,
    domain: str,
) -> tuple[dict[str, Any], list[str]]:
    """Explain why Core has no stored traces for an automation or script.

    Returns the diagnostics dict and the warnings that should be surfaced at
    the response level, separately so the list response can merge them with
    its other warnings instead of nesting them under ``diagnostics``.


    The registry row, Core's loaded flag, its validation/repair issue and the
    references come from the component's ``automation_config`` lookup when it
    is available; the REST/WS path is the explicit fallback. Trace storage is
    read from Core's own ``automation/config`` / ``script/config`` command.
    """
    diagnostics: dict[str, Any] = {
        "entity_exists": False,
        "entity_enabled": False,
        "entity_state": None,
        "trace_key_available": False,
        "trace_storage_enabled": None,
        "loaded": None,
        "validation": None,
        "references": None,
        "last_triggered": None,
        "suggestion": "",
    }
    warnings: list[str] = []

    try:
        state = await client.get_entity_state(entity_id)
    except HomeAssistantError as err:
        warnings.append(f"entity state unavailable: {err}")
        state = None

    if isinstance(state, dict):
        diagnostics["entity_exists"] = True
        state_value = state.get("state", "unknown")
        diagnostics["entity_state"] = state_value
        diagnostics["entity_enabled"] = (
            state_value != "unavailable" if domain == "script" else state_value == "on"
        )
        last_triggered = (state.get("attributes") or {}).get("last_triggered")
        if last_triggered:
            diagnostics["last_triggered"] = last_triggered

    lookup = (
        await automation_lookup(client, entity_id=entity_id)
        if domain == "automation"
        else None
    )
    entity, validation, references = await registry_details(
        client,
        lookup,
        entity_id,
        warnings,
        component_lookup=domain == "automation",
        related_fallback=False,
    )
    diagnostics["validation"] = validation
    diagnostics["references"] = references
    if isinstance(validation, dict) and validation.get("status") not in {
        None,
        "ok",
    }:
        warnings.append(
            f"automation validation failed ({validation['status']}): "
            f"{validation.get('error') or 'see the repair issue'}"
        )
    if lookup is not None:
        diagnostics["loaded"] = lookup.get("loaded")
    if isinstance(entity, dict):
        diagnostics["entity_exists"] = True
        diagnostics["trace_key_available"] = bool(entity.get("unique_id"))

    diagnostics["trace_storage_enabled"] = await _is_trace_storage_enabled(
        client, entity_id, domain
    )
    diagnostics["suggestion"] = _diagnostic_suggestion(diagnostics, domain)
    return diagnostics, warnings


async def _is_trace_storage_enabled(
    client: Any,
    entity_id: str,
    domain: str,
) -> bool | None:
    """Return whether stored_traces is enabled, or None when it cannot be read."""
    try:
        config_result = await client.send_websocket_message(
            {"type": f"{domain}/config", "entity_id": entity_id}
        )
    except HomeAssistantError as err:
        logger.debug("Could not get %s config: %s", domain, err)
        return None
    if not isinstance(config_result, dict) or not config_result.get("success"):
        return None
    result = config_result.get("result")
    raw_config = result.get("config") if isinstance(result, dict) else None
    if not isinstance(raw_config, dict):
        return None
    trace_config = raw_config.get("trace")
    if not isinstance(trace_config, dict):
        return True
    stored_traces = trace_config.get("stored_traces")
    if stored_traces is None:
        return True
    try:
        return int(stored_traces) > 0
    except TypeError, ValueError:
        return None


def _diagnostic_suggestion(diagnostics: dict[str, Any], domain: str) -> str:
    """Describe the first Core-provided reason no trace is available."""
    if not diagnostics.get("entity_exists"):
        return f"Could not find {domain} entity. Verify the entity_id with ha_search()."
    validation = diagnostics.get("validation")
    if isinstance(validation, dict) and validation.get("status") not in {
        None,
        "ok",
    }:
        error = validation.get("error")
        return (
            f"Core reports this {domain} did not load ({validation.get('status')}): "
            f"{error or 'see the repair issue'}"
        )
    if diagnostics.get("trace_storage_enabled") is False:
        return (
            f"Trace storage is disabled for this {domain} (stored_traces: 0); "
            "enable it in the automation/script trace settings."
        )
    if not diagnostics.get("trace_key_available"):
        return (
            "This item has no registry unique_id, so Core has no key under "
            "which to store a trace (common for YAML automations without an id)."
        )
    if diagnostics.get("loaded") is False:
        return (
            "Core reports this automation is not loaded. Check its validation "
            "or repair issue; automation.trigger can still execute a disabled "
            "automation."
        )
    if diagnostics.get("entity_state") == "unavailable":
        return (
            f"The {domain} is unavailable, so Home Assistant cannot run it. "
            "Check the integration that provides it and the Home Assistant logs."
        )
    if domain == "automation" and diagnostics.get("entity_state") == "off":
        return (
            "This automation is disabled (state off). automation.trigger can "
            "still execute it and record a trace."
        )
    return (
        "No stored trace is available yet. Run the automation/script once, "
        "then list traces again."
    )
