"""Reloads behind ha_reload_core: subsystem sweeps and single config entries.

A single-entry reload refuses ha-mcp's own ``ha_mcp_tools`` entries: the
unshielded REST reload route tears down the server answering the call and,
in embedded mode, strands the entry in ``UNLOAD_IN_PROGRESS`` until Home
Assistant restarts (see ``tools_dev.schedule_deferred_entry_reload``).
"""

import asyncio
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..errors import ErrorCode, create_error_response
from .config_entry_flow import refuse_component_entry
from .helpers import exception_to_structured_error, raise_tool_error

# Mapping of reload targets to their service domains and services
RELOAD_TARGETS = {
    "all": None,  # Special case - reload all
    "automations": ("automation", "reload"),
    "scripts": ("script", "reload"),
    "scenes": ("scene", "reload"),
    "groups": ("group", "reload"),
    "input_booleans": ("input_boolean", "reload"),
    "input_numbers": ("input_number", "reload"),
    "input_texts": ("input_text", "reload"),
    "input_selects": ("input_select", "reload"),
    "input_datetimes": ("input_datetime", "reload"),
    "input_buttons": ("input_button", "reload"),
    "timers": ("timer", "reload"),
    "templates": ("template", "reload"),
    "persons": ("person", "reload"),
    "zones": ("zone", "reload"),
    "core": ("homeassistant", "reload_core_config"),
    "themes": ("frontend", "reload_themes"),
}


async def reload_all_targets(client: Any) -> dict[str, Any]:
    """Reload every reloadable subsystem concurrently, capped by a semaphore.

    Extracted verbatim from ``ha_reload_core``'s ``target == "all"`` branch.
    """
    # Reload all reloadable components. Fire the calls concurrently
    # but cap the in-flight count with a semaphore so a large
    # install doesn't hit HA with ~16 simultaneous service calls.
    # A single failing target must not cancel its siblings, so the
    # calls are gathered with ``return_exceptions=True`` and
    # attributed per target below (preserving RELOAD_TARGETS order).
    reloadable = [
        (name, info) for name, info in RELOAD_TARGETS.items() if info is not None
    ]
    semaphore = asyncio.Semaphore(4)

    async def _reload_one(domain: str, service: str) -> None:
        async with semaphore:
            await client.call_service(domain, service, {})

    outcomes = await asyncio.gather(
        *(_reload_one(domain, service) for _, (domain, service) in reloadable),
        return_exceptions=True,
    )

    results = []
    errors = []
    for (reload_target, _), outcome in zip(reloadable, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            # A non-Exception BaseException (CancelledError,
            # KeyboardInterrupt, SystemExit) must still unwind the
            # request, exactly as the prior sequential
            # ``except Exception`` let it propagate — never demote
            # it to a per-target warning.
            if not isinstance(outcome, Exception):
                raise outcome
            # Some services might not be available in all installations
            error_msg = str(outcome)
            if "not found" not in error_msg.lower():
                errors.append(f"{reload_target}: {error_msg}")
        else:
            results.append(reload_target)

    response: dict[str, Any] = {
        "success": True,
        "message": f"Reloaded {len(results)} components",
        "reloaded": results,
    }
    if errors:
        response["warnings"] = errors
    return response


async def reload_config_entry(client: Any, entry_id: str) -> dict[str, Any]:
    """Reload a single config entry via the REST config-entries endpoint.

    POSTs to ``/config/config_entries/entry/{entry_id}/reload`` through the
    REST client's generic request method — the ``/entry/`` path segment the
    hand-rolled workaround in issue #1813 was easy to get wrong. The entry is
    looked up first so ha-mcp's own entries are refused before the POST. A
    404 from either call surfaces as a not-found error naming the
    ``entry_id``; other failures route through the shared exception
    classifier. Returns the tool's standard envelope plus
    ``reloaded``/``entry_id``.
    """
    try:
        entry = await client.get_config_entry(entry_id)
        refuse_component_entry(entry.get("domain"), action="reload", entry_id=entry_id)
        resp = await client._request(
            "POST", f"/config/config_entries/entry/{entry_id}/reload"
        )
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        # Local import mirrors ``tools_system._reraise_if_fatal``: rest_client
        # imports from the tool helpers transitively, so a module-level
        # import would risk a circular import in the tools package.
        from ..client.rest_client import HomeAssistantAPIError

        if isinstance(e, HomeAssistantAPIError) and e.status_code == 404:
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Config entry not found: {entry_id}",
                    context={"entry_id": entry_id},
                    suggestions=[
                        "Verify the entry_id via ha_get_integration",
                    ],
                )
            )
        if isinstance(e, HomeAssistantAPIError) and e.status_code == 403:
            # Core returns 403 "Entry cannot be reloaded" for
            # OperationNotAllowed — an entry whose integration does not
            # support reload — not an auth problem.
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Config entry {entry_id} cannot be reloaded "
                    "(the integration does not support reload)",
                    context={"entry_id": entry_id},
                    suggestions=[
                        "Restart Home Assistant (ha_restart) to apply "
                        "changes to this integration",
                    ],
                )
            )
        exception_to_structured_error(
            e,
            context={"entry_id": entry_id},
            suggestions=[
                "Verify the entry_id via ha_get_integration",
                "Check Home Assistant logs for details",
            ],
        )

    # Core reports require_restart=True when the entry could not be
    # hot-reloaded (its state is not recoverable) — a success response
    # that still needs a restart to take effect.
    require_restart = isinstance(resp, dict) and bool(resp.get("require_restart"))
    if require_restart:
        return {
            "success": True,
            "message": (
                f"Config entry {entry_id} cannot be hot-reloaded; "
                "a Home Assistant restart is required for changes to "
                "take effect"
            ),
            "reloaded": False,
            "require_restart": True,
            "entry_id": entry_id,
        }
    return {
        "success": True,
        "message": f"Reloaded config entry {entry_id}",
        "reloaded": True,
        "require_restart": False,
        "entry_id": entry_id,
    }
