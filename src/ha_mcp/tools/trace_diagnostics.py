"""Diagnostics for empty trace results."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def _gather_diagnostics(
    client: Any,
    entity_id: str,
    domain: str,
) -> dict[str, Any]:
    """
    Gather diagnostic information when traces are empty.

    This helps users understand why there are no traces available for
    an automation or script.

    Args:
        client: Pooled REST client (REST reads + ``send_websocket_message``)
        entity_id: Full entity_id (e.g., 'automation.motion_light')
        domain: Either 'automation' or 'script'

    Returns:
        Dictionary containing diagnostic information:
        - entity_exists: Whether the entity exists
        - entity_enabled: Whether the automation/script can run
        - entity_state: The entity's state string, such as 'on' or 'unavailable'
        - trace_key_available: Whether Core can key a trace for this item
        - trace_storage_enabled: Whether trace storage is enabled for this item,
          or None when the config read could not answer it
        - last_triggered: Last trigger timestamp if available
        - suggestion: Helpful hint based on the diagnostics
    """
    diagnostics: dict[str, Any] = {
        "entity_exists": False,
        "entity_enabled": False,
        "entity_state": None,
        "trace_key_available": True,
        "trace_storage_enabled": None,
        "last_triggered": None,
        "suggestion": "",
    }
    not_found = (
        f"Could not find {entity_id}. "
        "Verify the entity_id is correct using ha_search()."
    )

    try:
        # Get entity state to check existence and enabled status
        entity_state = await client.get_entity_state(entity_id)

        if entity_state:
            diagnostics["entity_exists"] = True

            # Automations are disabled when their state is off. A script's
            # off state means idle, not disabled; only an unavailable script
            # is treated as unable to run.
            state = entity_state.get("state", "unknown")
            diagnostics["entity_state"] = state
            diagnostics["entity_enabled"] = (
                state != "unavailable" if domain == "script" else state == "on"
            )

            # Get last_triggered from attributes
            attributes = entity_state.get("attributes", {})
            last_triggered = attributes.get("last_triggered")
            if last_triggered:
                diagnostics["last_triggered"] = last_triggered

            # Core stores a trace under the automation's unique id. A YAML
            # automation without an id has no trace key at all; scripts always
            # have one.
            if domain == "automation":
                diagnostics["trace_key_available"] = bool(attributes.get("id"))

            # Both automations and scripts expose the same trace config shape.
            diagnostics["trace_storage_enabled"] = await _is_trace_storage_enabled(
                client, entity_id, domain
            )

            diagnostics["suggestion"] = _diagnostic_suggestion(diagnostics, domain)
        else:
            diagnostics["suggestion"] = not_found

    except Exception as e:  # noqa: BLE001
        # Entity doesn't exist or error occurred
        logger.debug(f"Error getting entity state for diagnostics: {e}")
        diagnostics["suggestion"] = not_found

    return diagnostics


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
        if not isinstance(config_result, dict) or not config_result.get("success"):
            return None
        # Core returns {"result": {"config": raw_config}} for both
        # automation/config and script/config; the stored_traces setting
        # lives under raw_config["trace"].
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
            stored_count = int(stored_traces)
        except (TypeError, ValueError):
            return None
        return stored_count > 0
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Could not get {domain} config: {e}")
        return None


def _diagnostic_suggestion(diagnostics: dict[str, Any], domain: str) -> str:
    """Build a helpful hint explaining why no traces are available."""
    state = diagnostics.get("entity_state")
    if not diagnostics["entity_enabled"]:
        if state == "unavailable":
            return (
                f"The {domain} is unavailable, so Home Assistant cannot run it. "
                "Check the integration that provides it and the Home Assistant "
                "logs."
            )
        return (
            f"The {domain} is currently disabled (state: {state or 'unknown'}). "
            "Enable it to start recording traces."
        )
    if not diagnostics.get("trace_key_available", True):
        return (
            "Home Assistant does not store traces for this automation because "
            "it has no unique id. Add an 'id' to the automation config."
        )
    storage = diagnostics["trace_storage_enabled"]
    if storage is False:
        return (
            f"Trace storage is disabled for this {domain}. "
            f"Set 'stored_traces' to a positive number in the {domain} config."
        )
    if storage is None:
        return (
            f"Could not read whether trace storage is enabled for this {domain}. "
            f"Check the 'trace' setting in its {domain} config."
        )
    if diagnostics["last_triggered"] is None:
        if domain == "script":
            return (
                "Run the script to generate traces. Home Assistant keeps only "
                "a limited number of recent runs."
            )
        return (
            f"The {domain} has never been triggered. "
            "Wait for it to trigger or manually trigger it to generate traces."
        )
    return (
        "Traces may have been cleared or expired. "
        "Home Assistant only keeps a limited number of recent traces."
    )
