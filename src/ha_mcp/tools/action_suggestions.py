"""Suggestions for a device action a domain rejects.

A rejected action is only actionable feedback if the suggestion names actions
the target domain actually accepts. The tables live in
``utils/domain_handlers.py``; this module turns one into the caller-facing
suggestion list, so the single-device and bulk validators cannot drift apart
(#3024).
"""

from __future__ import annotations


def invalid_action_suggestions(
    domain: str, entity_id: str, valid_actions: list[str]
) -> list[str]:
    """Name only actions the target domain actually accepts.

    A read-only domain has no device actions at all, so listing them produced
    ``Valid actions for sensor: `` -- an empty list -- plus a toggle hint the
    domain rejects, sending the caller straight back to the same failing call.
    The toggle hint is likewise worthless where ``on``/``off`` are not the
    vocabulary (cover, lock, alarm_control_panel, scene, vacuum).
    """
    if not valid_actions:
        return [
            f"Domain '{domain}' is read-only and accepts no device actions",
            f"Read the value with ha_get_state('{entity_id}')",
            "Use ha_search to find a controllable entity for the same device",
        ]
    suggestions = [f"Valid actions for {domain}: {', '.join(valid_actions)}"]
    if "toggle" in valid_actions and ({"on", "off"} & set(valid_actions)):
        suggestions.append("Use 'toggle' for simple on/off control")
    return suggestions
