"""Shared classifications for tool dispatch middleware."""

from __future__ import annotations

import json
from typing import Any

# Call proxies dispatch the envelope's ``name`` back through middleware. Policy
# rules and read-only checks must apply to the real tool name and arguments, not
# the proxy envelope, so these outer calls bypass those checks.
CALL_PROXY_META_TOOLS = frozenset(
    {
        "ha_call_read_tool",
        "ha_call_write_tool",
        "ha_call_delete_tool",
    }
)

LOCAL_ONLY_TOOLS = frozenset({"ha_search_tools"})

# Code mode is local orchestration that can issue many nested HA calls. Its
# outer envelope must not hold one slot while nested calls reuse that admission;
# each nested HA dispatch instead acquires an outer-call slot normally.
OUTER_QUEUE_ORCHESTRATORS = frozenset({"ha_manage_custom_tool"})

_APPROVAL_MANAGEMENT_TOOL = "ha_dev_manage_server"
_APPROVAL_MANAGEMENT_ACTIONS = frozenset({"list_pending", "approve", "deny"})


def is_approval_management_call(name: str, args: dict[str, Any]) -> bool:
    """Return whether a dispatch manages the policy approval queue.

    Gating or queueing these actions can deadlock approval by making the action
    that decides a pending request wait behind that same blocked workload.
    Update and restart actions remain subject to normal middleware behavior.
    """
    return (
        name == _APPROVAL_MANAGEMENT_TOOL
        and args.get("action") in _APPROVAL_MANAGEMENT_ACTIONS
    )


def coerce_proxy_arguments(arguments: Any) -> dict[str, Any] | None:
    """Normalise a call-proxy envelope's ``arguments`` to a dict or None.

    The categorized proxies tolerate ``arguments`` arriving as a JSON string
    (small models sometimes serialize it) and json.loads it after middleware
    runs. A dict is used as-is; an absent value (``None``) becomes ``{}`` (a
    legitimate no-argument call); a string is parsed and only a JSON *object*
    yields a dict. Anything else returns None, meaning "malformed envelope".
    """
    if isinstance(arguments, dict):
        return arguments
    if arguments is None:
        return {}
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError, ValueError, RecursionError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None
