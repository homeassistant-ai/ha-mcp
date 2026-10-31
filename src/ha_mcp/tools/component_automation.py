"""Automation validation and registry reads through the component's
``ha_mcp_tools/automation_config`` command.

``validate`` runs Core's own automation validator in process, ``describe``
returns Core's trigger or condition descriptions, and ``lookup`` returns the
automation's registry row, the validation status Core reported in its repair
issue and the references Core's automation helpers find. Each helper returns
``None`` when the component did not answer, and the caller uses Core's
REST/WebSocket API instead: the capability is absent, the command was not sent
or got no answer, Core answered ``unknown_command``, the component failed, or
it reported ``unavailable`` (a Core without the validator it calls). None of
the three writes anything, so a lost answer falls back rather than raising.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from ..client.rest_client import HomeAssistantConnectionError
from .component_api import component_supports, get_component_caps, invalidate_caps

logger = logging.getLogger(__name__)

WS_AUTOMATION_CONFIG = "ha_mcp_tools/automation_config"
CAPABILITY = "automation_config"


async def _automation_config(
    client: Any, action: str, **fields: Any
) -> dict[str, Any] | None:
    """The component's reply to one action; None when it gave none."""
    if not component_supports(await get_component_caps(client), CAPABILITY):
        return None
    try:
        result = await client.send_websocket_message(
            {"type": WS_AUTOMATION_CONFIG, "action": action, **fields}
        )
    except HomeAssistantConnectionError as exc:
        logger.warning("%s %s got no answer: %s", WS_AUTOMATION_CONFIG, action, exc)
        return None
    if not result.get("success"):
        if result.get("error_code") == "unknown_command":
            invalidate_caps(client)
        else:
            logger.warning(
                "%s %s failed: %s", WS_AUTOMATION_CONFIG, action, result.get("error")
            )
        return None
    reply = result.get("result")
    return reply if isinstance(reply, dict) else None


async def automation_validate(
    client: Any, config: dict[str, Any]
) -> dict[str, Any] | None:
    """Core's verdict on ``config`` as ``{status, error}``."""
    reply = await _automation_config(client, "validate", config=config)
    if reply is None or reply.get("status") in (None, "unavailable"):
        return None
    return reply


async def automation_describe(
    client: Any, kind: Literal["trigger", "condition"]
) -> dict[str, Any] | None:
    """Core's description of every registered trigger or condition, by key."""
    reply = await _automation_config(client, "describe", kind=kind)
    descriptions = reply.get("descriptions") if reply is not None else None
    return descriptions if isinstance(descriptions, dict) else None


async def automation_lookup(
    client: Any, *, entity_id: str | None = None, unique_id: str | None = None
) -> dict[str, Any] | None:
    """The automation's ``{found, entity, validation, references}``."""
    fields = {"entity_id": entity_id} if entity_id is not None else {}
    if unique_id is not None:
        fields["unique_id"] = unique_id
    reply = await _automation_config(client, "lookup", **fields)
    if reply is None or not isinstance(reply.get("found"), bool):
        return None
    return reply
