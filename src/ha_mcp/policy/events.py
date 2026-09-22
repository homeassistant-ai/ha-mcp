"""Home Assistant event surface for the tool approval queue.

The queue lives in the server process and is only visible in the settings
UI. Mirroring each pending request onto Home Assistant's event bus lets
users build their own notifications and automations around it, and is the
only signal available when nobody has the settings tab open.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import anyio

from .approval_queue import PendingApproval
from .model import Rule

if TYPE_CHECKING:
    from ..client.rest_client import HomeAssistantClient

logger = logging.getLogger(__name__)

APPROVAL_REQUESTED_EVENT = "ha_mcp_approval_requested"

# Per-argument size cap for the event payload. The settings UI shows one
# admin the full arguments; the event bus fans them out to every listener
# and to the frontend's event dev-tools, so a whole file or YAML document
# from ha_write_file / ha_config_set_yaml would be broadcast verbatim.
# Enough to identify what is being approved, not enough to be a copy of
# the payload -- the UI stays the full-fidelity view.
ARG_VALUE_LIMIT = 512

# Cap on the fire-and-forget notification. The REST client's own timeout
# (30s by default) would otherwise eat most of a default 60s approval
# window while the caller is already blocked and waiting.
EMIT_TIMEOUT_SECONDS = 5.0


def _cap_value(value: Any) -> Any:
    """Shorten one argument value for the broadcast.

    A size limit and nothing more: it does not redact. A short value goes
    out in full whatever it holds, so an argument carrying a secret is
    broadcast as it stands. The cap bounds how much of a large payload
    reaches every listener; it does not make the content safe.
    """
    if isinstance(value, str):
        if len(value) <= ARG_VALUE_LIMIT:
            return value
        omitted = len(value) - ARG_VALUE_LIMIT
        return f"{value[:ARG_VALUE_LIMIT]} <{omitted} more characters omitted>"
    if isinstance(value, (dict, list)):
        encoded = json.dumps(value, default=str)
        if len(encoded) <= ARG_VALUE_LIMIT:
            return value
        return f"<{len(encoded)} bytes of {type(value).__name__} omitted>"
    return value


def build_requested_payload(
    entry: PendingApproval,
    rule: Rule | None = None,
    *,
    single_use: bool = False,
) -> dict[str, Any]:
    """Build the ``ha_mcp_approval_requested`` event data.

    Carries the arguments as well as the token so an automation can show
    what is being approved, not just that something is -- each value capped
    at ``ARG_VALUE_LIMIT``. The settings UI's pending list remains the place
    to read an argument in full.

    ``matched_rule`` is present exactly when a rule matched this call. The
    policy's two fail-safes (``evaluator.evaluate``) gate a call without a
    matching rule, and then there is none to name.
    """
    payload: dict[str, Any] = {
        "token": entry.token,
        "tool_name": entry.tool_name,
        "args": {key: _cap_value(value) for key, value in entry.args.items()},
        "created_at": entry.created_at.isoformat(),
    }
    if single_use:
        # A dynamic-selector request is bound to the one call that created
        # it and dies when that call stops waiting, which is sooner than
        # its TTL: announcing ``expires_at`` here would promise minutes
        # where there are seconds. ``_raise_pending_error`` omits the
        # countdown on this path for the same reason.
        payload["single_use"] = True
    else:
        payload["expires_at"] = entry.expires_at.isoformat()
    if rule is not None:
        payload["matched_rule"] = {
            "tool_name": rule.tool_name,
            "when": [p.model_dump(mode="json") for p in rule.when],
        }
    return payload


async def emit_approval_requested(
    client: HomeAssistantClient,
    entry: PendingApproval,
    rule: Rule | None = None,
    *,
    single_use: bool = False,
) -> None:
    """Fire ``ha_mcp_approval_requested`` for one pending entry.

    Best effort: the approval gate itself must not fail because the
    notification could not be delivered. A failure is logged at WARNING
    with its traceback, because a user relying on this event as their only
    approval signal needs the silence explained.

    ``POST /api/events/<type>`` works identically for every installation
    method — the embedded component provisions the server a loopback URL
    and an admin token, so there is no component-only path here.
    """
    try:
        with anyio.move_on_after(EMIT_TIMEOUT_SECONDS) as scope:
            await client.fire_event(
                APPROVAL_REQUESTED_EVENT,
                build_requested_payload(entry, rule, single_use=single_use),
            )
        if scope.cancelled_caught:
            logger.warning(
                "policy events: firing %s for tool=%s timed out after %.0fs; "
                "the pending request is still queued and visible in the "
                "settings UI",
                APPROVAL_REQUESTED_EVENT,
                entry.tool_name,
                EMIT_TIMEOUT_SECONDS,
            )
    except Exception:
        # Broad by intent: every failure mode here has the same handling —
        # log it and leave the gate working. Narrowing would turn an
        # unanticipated transport error into a failed tool call that the
        # policy engine had already decided to merely hold.
        logger.warning(
            "policy events: failed to fire %s for tool=%s; the pending "
            "request is still queued and visible in the settings UI",
            APPROVAL_REQUESTED_EVENT,
            entry.tool_name,
            exc_info=True,
        )
