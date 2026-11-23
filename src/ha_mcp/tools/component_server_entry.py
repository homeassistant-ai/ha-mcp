"""The component's ``ha_mcp_tools/server_entry`` read.

The component locates its own in-process server entry and says whether the
spec configured there is installed yet, so ``ha_dev_manage_server`` need not
probe every ``ha_mcp_tools`` entry's options flow from the outside.
"""

from __future__ import annotations

import logging
from typing import Any

from ..client.rest_client import HomeAssistantCommandError, HomeAssistantCommandTimeout
from ..client.websocket_client import get_websocket_client
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)

logger = logging.getLogger(__name__)

WS_SERVER_ENTRY = "ha_mcp_tools/server_entry"


async def fetch_server_entry_via_component(client: Any) -> dict[str, Any] | None:
    """One ``ha_mcp_tools/server_entry`` read; ``None`` ⇒ use the legacy probe.

    Returns the component's ``{entry_id, pip_spec, install_pending}`` payload —
    ``entry_id`` is ``None`` when no server entry exists, an AUTHORITATIVE
    verdict the component reaches in-process (its own ``DOMAIN`` entries),
    not a "try the legacy path" signal. Returns ``None`` (⇒ legacy probe) on
    capability miss, downgrade (``unknown_command`` → invalidate the cached
    caps), command error/timeout (logged), or an empty-string ``entry_id``
    (a malformed reply shape — not the component's real "no entry" signal,
    which is ``None`` — so it is not trusted as authoritative either) — same
    taxonomy as ``component_devices.fetch_device_via_component``. A
    ``HomeAssistantConnectionError`` — a pooled-WS drop, or a failed
    (re)connect — is caught here and mapped to ``None``: the legacy probe
    (``find_server_config_entry``) rides the ``send_websocket_message`` bridge,
    which answers a component-side fault rather than dying with it — so a
    transport failure must fall back rather than escape.
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "server_entry"):
        return None
    try:
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
        raw = await ws.send_command(WS_SERVER_ENTRY)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; fell back to legacy: %r", WS_SERVER_ENTRY, exc)
        return None
    except Exception:
        # HomeAssistantConnectionError or any other error → legacy probe
        # (which rides the send_websocket_message bridge).
        logger.warning("%s failed; fell back to legacy", WS_SERVER_ENTRY, exc_info=True)
        return None
    result = raw.get("result")
    if not isinstance(result, dict) or "entry_id" not in result:
        return None
    if result.get("entry_id") == "":
        return None
    return result


async def install_pending(
    client: Any, server_entry: dict[str, Any] | None, warnings: list[str]
) -> bool | None:
    """Whether the component has yet to install the server entry's configured spec.

    ``server_entry`` is the ``fetch_server_entry_via_component`` result ``info``
    already read. ``None`` when that is unknown: a component without the
    ``server_entry_install_pending`` flag, or one whose read failed, adds a
    warning to update it; one that cannot tell (Home Assistant runs with
    ``skip_pip``) adds none. True adds a warning that the in-process server
    does not run the configured spec, whatever ``pip_spec`` says.
    """
    caps = await get_component_caps(client)
    if server_entry is None or not component_supports(
        caps, "server_entry_install_pending"
    ):
        warnings.append(
            "Update the ha_mcp_tools component to see whether the server "
            "entry's pip_spec is installed yet."
        )
        return None
    pending = server_entry.get("install_pending")
    if not isinstance(pending, bool):
        return None
    if pending:
        warnings.append(
            "The ha_mcp_tools component has not installed the server entry's "
            "pip_spec yet: its install is still running, was deferred or "
            "failed, or the entry has not started (for instance with its "
            "access switched off), so the in-process server does not run that "
            "spec. If it is still pending after a few minutes, check that "
            "access is on, reload the server entry or restart Home Assistant, "
            "and check the Home Assistant log."
        )
    return pending
