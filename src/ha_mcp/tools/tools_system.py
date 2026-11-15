"""
System management tools for Home Assistant MCP Server.

This module provides tools for Home Assistant system administration including:
- Configuration validation
- Service restarts and reloads
- System health monitoring
"""

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import (
    exception_to_structured_error,
    get_connected_ws_client,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .system_health_sections import (
    add_health_labels,
    add_repair_data,
    add_repair_texts,
    read_integration_startup,
    read_matter_network,
    read_supervisor,
    read_system_health,
    reraise_if_fatal,
)
from .system_restart import restart_home_assistant
from .tool_hints import read_only_hints, write_hints
from .util_helpers import filter_active_repairs

logger = logging.getLogger(__name__)

# The ha_mcp_tools/system_snapshot WS command: one consistent in-process read of
# config_entries/issues/entities/states, collapsing what used to be up to 3x
# ``config_entries/get`` fetches (a TOCTOU across sections) into one snapshot.
WS_SYSTEM_SNAPSHOT = "ha_mcp_tools/system_snapshot"


@dataclass(frozen=True)
class _SystemSnapshotSlices:
    """The component's ``system_snapshot`` slices, re-wrapped for the section
    helpers that already unwrap the legacy ``{success, result}`` WS envelope
    (mirrors ``ha_get_overview``'s ``_OverviewSlices`` in ``search/overview.py``).

    ``config_entries`` / ``registry`` are wrapped in the ``{success, result}``
    envelope ``_fetch_zwave_network`` / ``_fetch_dead_entities`` (via
    ``_ws_result_list``) already unwrap;
    ``states`` stays the bare list matching ``get_states()``, which
    ``_fetch_dead_entities`` reads directly.
    """

    config_entries: dict[str, Any]
    states: list[dict[str, Any]]
    registry: dict[str, Any]


def _build_system_snapshot_slices(
    result: dict[str, Any],
) -> _SystemSnapshotSlices | None:
    """Adapt the component's bare ``system_snapshot`` slices into the envelope
    shape the section helpers unwrap. Returns ``None`` (⇒ every consuming
    section falls back to its legacy fetch) when the payload isn't
    trustworthy: not a dict, or any of the three slices read here
    (``config_entries`` / ``entities`` / ``states``) missing or not a list.
    They are always present per the component's contract (a disabled section
    is an empty list, never a missing key), so a missing key here means the
    response is malformed, not merely disabled.
    """
    if not isinstance(result, dict):
        return None
    for key in ("config_entries", "entities", "states"):
        if not isinstance(result.get(key), list):
            return None
    return _SystemSnapshotSlices(
        config_entries={"success": True, "result": result["config_entries"]},
        states=result["states"],
        registry={"success": True, "result": result["entities"]},
    )


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


class SystemTools:
    """System management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_restart",
        tags={"System"},
        annotations=write_hints(
            "Restart Home Assistant",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )
    @log_tool_usage
    async def ha_restart(
        self,
        confirm: Annotated[
            bool, Field(description="Must be True to confirm the restart.")
        ] = False,
        safe_mode: Annotated[
            bool,
            Field(
                description=(
                    "Restart in safe mode: no custom integrations or custom "
                    "frontend resources load until the next normal restart."
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Execute a Home Assistant restart.

        WARNING: restarts the entire Home Assistant instance; all automations
        are unavailable during the restart, which typically takes 1-5 minutes.
        Config is validated automatically before the restart proceeds (to
        pre-check, call ha_get_system_health(include="config_check")). For
        configuration changes, consider ha_reload_core() instead, which reloads
        specific components without a full restart. When this server runs
        inside Home Assistant (embedded), it replies before the restart starts
        and is unreachable until Home Assistant is back; with safe_mode it
        stays down until Home Assistant is next restarted normally, because it
        is itself a custom integration.

        EXAMPLE: ha_restart(confirm=True)
        """
        return await restart_home_assistant(
            self._client, confirm=confirm, safe_mode=safe_mode
        )

    @tool(
        name="ha_reload_core",
        tags={"System"},
        annotations=write_hints(
            "Reload Core Components",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )
    @log_tool_usage
    async def ha_reload_core(
        self,
        target: Annotated[
            str,
            Field(
                description=(
                    "What to reload: 'all', 'automations', 'scripts', 'scenes', "
                    "'groups', 'input_booleans', 'input_numbers', 'input_texts', "
                    "'input_selects', 'input_datetimes', 'input_buttons', "
                    "'timers', 'templates', 'persons', 'zones', 'core' "
                    "(customize, packages) or 'themes'."
                )
            ),
        ] = "all",
        entry_id: Annotated[
            str | None,
            Field(
                description=(
                    "Reload a SINGLE config entry (one integration instance) "
                    "instead of sweeping subsystems — the fast path after editing "
                    "a custom component on disk. Pass it alone (leave target at "
                    "'all'); combining it with an explicit target is a validation "
                    "error. Find the id via ha_get_integration."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Execute a Home Assistant configuration reload without a full restart.

        Reloads specific configuration components so changes take effect without
        restarting Home Assistant — much faster than a full restart. Use it after
        editing automation/script YAML, adding YAML helpers, or changing
        customize.yaml or themes.

        EXAMPLES:
        - ha_reload_core(target="automations")
        - ha_reload_core(entry_id="abc123")
        """
        target = target.lower().strip()

        # Single config-entry reload (issue #1813 fold-in): reload just the
        # integration instance identified by ``entry_id`` rather than sweeping
        # every reloadable subsystem — the fast path after editing a custom
        # component on disk. ``target`` defaults to "all" (meaning "no subsystem
        # chosen"), so entry_id + the default is an entry-only reload; entry_id
        # paired with an explicit subsystem target is contradictory.
        if entry_id is not None:
            entry_id = validate_identifier_not_empty(
                entry_id,
                "entry_id",
                suggestions=[
                    "Find the entry_id via ha_get_integration",
                    "Omit entry_id to reload a whole subsystem via target",
                ],
            )
            if target != "all":
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "entry_id cannot be combined with a specific reload "
                        f"target ('{target}')",
                        context={"entry_id": entry_id, "target": target},
                        suggestions=[
                            "Pass entry_id alone to reload one config entry",
                            f"Omit entry_id to reload the '{target}' subsystem",
                        ],
                    )
                )
            return await self._reload_config_entry(entry_id)

        if target not in RELOAD_TARGETS:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid reload target: {target}",
                    context={
                        "target": target,
                        "valid_targets": list(RELOAD_TARGETS.keys()),
                    },
                    suggestions=[f"Use one of: {', '.join(RELOAD_TARGETS.keys())}"],
                )
            )

        try:
            if target == "all":
                return await self._reload_all_targets()

            else:
                # Reload specific component
                service_info = RELOAD_TARGETS[target]
                if service_info is None:
                    # This shouldn't happen as we check for "all" above
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.INTERNAL_ERROR,
                            f"Invalid target configuration for: {target}",
                            context={"target": target},
                        )
                    )
                domain, service = service_info
                await self._client.call_service(domain, service, {})

                return {
                    "success": True,
                    "message": f"Successfully reloaded {target}",
                    "target": target,
                    "service": f"{domain}.{service}",
                }

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={"target": target},
                suggestions=[
                    f"Ensure the {target} integration is loaded",
                    "Check Home Assistant logs for details",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    async def _reload_all_targets(self) -> dict[str, Any]:
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
                await self._client.call_service(domain, service, {})

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

    async def _reload_config_entry(self, entry_id: str) -> dict[str, Any]:
        """Reload a single config entry via the REST config-entries endpoint.

        POSTs to ``/config/config_entries/entry/{entry_id}/reload`` through the
        REST client's generic request method — the ``/entry/`` path segment the
        hand-rolled workaround in issue #1813 was easy to get wrong. A 404
        surfaces as a not-found error naming the ``entry_id``; other failures
        route through the shared exception classifier. Returns the tool's
        standard envelope plus ``reloaded``/``entry_id``.
        """
        try:
            resp = await self._client._request(
                "POST", f"/config/config_entries/entry/{entry_id}/reload"
            )
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
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

    @tool(
        name="ha_get_system_health",
        tags={"System", "Zigbee", "Z-Wave", "Thread", "Matter", "Integrations"},
        annotations=read_only_hints(
            "Get System Health (incl. ZHA/Z-Wave/Thread/Matter networks)",
            open_world=True,
        ),
    )
    @log_tool_usage
    async def ha_get_system_health(
        self,
        include: Annotated[
            str | None,
            Field(
                description=(
                    "Comma-separated extra sections: 'repairs' (Repair items with "
                    "the title, description and data the Repairs page shows, "
                    "active only unless include_dismissed_repairs=True); "
                    "'zha_network' (ZHA devices with radio signal summary: name, "
                    "LQI, RSSI); 'zha_network_full' (all ZHA device details; large "
                    "on 100+ device networks); 'zwave_network' (Z-Wave JS driver "
                    "connection, controller and node status); "
                    "'thread_network' (the Thread datasets Home Assistant knows, "
                    "and its OTBR border routers; no network keys); "
                    "'matter_network' (what the Matter panel reads: Core's Matter "
                    "config entry and network topology, nodes and connections "
                    "capped at 50 each); "
                    "'supervisor' (Supervisor installs: resolution info with the "
                    "unhealthy and unsupported reasons, and the Core and "
                    "Supervisor container stats); 'integration_startup' (each "
                    "integration's setup time in seconds, slowest first); "
                    "'config_check' (validate the HA configuration, the "
                    "pre-restart check ha_restart runs automatically; returns "
                    "Core's {result: valid|invalid, errors, warnings}, the "
                    "messages as one string or null; warnings alone stay "
                    "valid); 'dead_entities' "
                    "(orphaned/stale entity-registry entries: config_entry_orphans "
                    "whose owning integration instance is gone, and stale_restored "
                    "entries HA restored on startup that the loaded integration no "
                    "longer provides, each with entity_id + platform for cleanup "
                    "via ha_remove_entity; unknown-state entities and "
                    "merely-offline devices are excluded)."
                )
            ),
        ] = None,
        include_dismissed_repairs: Annotated[
            bool | None,
            Field(
                description=(
                    "Include user-dismissed/ignored repairs. Only meaningful when "
                    "'repairs' is in include. Dismiss or restore one with "
                    "ha_manage_updates(action='ignore_repair' / 'unignore_repair')."
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Get Home Assistant system health, including Zigbee (ZHA), Z-Wave JS, Thread and Matter networks.

        Returns health check results from integrations, system resources, and
        connectivity; available information varies by installation type and
        loaded integrations. Each domain carries the ``name`` and key ``labels``
        the System information dialog shows. Checks that Home Assistant runs in
        the background are awaited up to a deadline; one still running shows as
        pending, with a warning. Optional sections are selected with `include`.

        Not for one integration's diagnostics: use
        ha_get_integration(include_diagnostics=True). Not for themes: use
        ha_manage_theme(action='list'). Not for a single device's radio state:
        use ha_get_device. Use this tool for the state of the whole
        installation: health checks, repairs, radio networks, Supervisor,
        startup times and the configuration check.

        The result may include ``ha_mcp_update`` ``{current, latest,
        update_available}``. When update_available is true, tell the user a newer
        ha-mcp release is available (with ``python_required``, give ``upgrade_command``).

        EXAMPLES:
        - ha_get_system_health(include="repairs,zha_network,zwave_network,config_check")
        - ha_get_system_health(include="supervisor,integration_startup")
        """
        includes = self._parse_includes(include)
        include_dismissed_repairs_bool = bool(include_dismissed_repairs)

        # Sections that require the system_health WebSocket connection;
        # config_check, supervisor and dead_entities do not.
        ws_backed = {
            "repairs",
            "zha_network",
            "zha_network_full",
            "zwave_network",
            "thread_network",
            "matter_network",
            "integration_startup",
        }

        ws_client = None

        try:
            try:
                ws_client, result = await self._fetch_health_info()
            except ToolError as health_err:
                # The system_health/info baseline (WebSocket) is unavailable.
                # Only ``await self._fetch_health_info()`` runs in this inner
                # try, and it raises ToolError solely for baseline-unavailable
                # conditions (connect failure / timeout / WS error), so this
                # catch cannot swallow an unrelated ToolError.
                #
                # Degrade gracefully ONLY when the caller asked for a section
                # (config_check / supervisor / dead_entities) that can still be
                # served without the health WebSocket. config_check is the
                # pure-REST replacement for the removed ha_check_config tool, so
                # it must not depend on the health WebSocket (the
                # system_health/info read can take up to HEALTH_DEADLINE_SECONDS
                # and can be absent on some installs). dead_entities uses the REST
                # client's own per-client WebSocket bridge for the registry +
                # config-entries (not this health ws_client), so it is likewise
                # independent of the baseline. If the caller asked for nothing
                # (the health baseline itself) or only WS-backed sections, the
                # baseline WAS the deliverable: re-raise so the failure surfaces
                # as isError=true, exactly as before this change.
                if not (includes & {"config_check", "supervisor", "dead_entities"}):
                    raise
                logger.warning("system_health baseline unavailable: %s", health_err)
                ws_client = None
                result = {
                    "success": True,
                    "baseline_available": False,
                    "health_info": {},
                    "component_count": 0,
                    "message": "System health baseline unavailable.",
                    "warnings": [
                        "system_health baseline unavailable; "
                        "returning only the sections that do not need it."
                    ],
                }

            await add_health_labels(self._client, result)

            # Warn about unrecognized include values
            self._warn_unknown_includes(result, includes)

            # Fetch optional sections concurrently. The ws_client serialises
            # outgoing writes via its internal `_send_lock`, but per-message
            # futures keyed by message_id let response waits overlap — so this
            # gives request pipelining instead of head-of-line blocking.
            #
            # Each ``_fetch_*`` helper already returns an embeddable sub-dict
            # with an ``error`` field on backend failure (it never raises);
            # ``return_exceptions=True`` is belt-and-suspenders against a future
            # helper edit that lets an exception escape.
            zha_full = "zha_network_full" in includes
            zha_summary = "zha_network" in includes
            want_repairs = "repairs" in includes
            want_zha = zha_full or zha_summary
            want_zwave = "zwave_network" in includes
            want_thread = "thread_network" in includes
            want_matter = "matter_network" in includes
            want_startup = "integration_startup" in includes

            if ws_client is None:
                # Health WebSocket unavailable: WS-backed sections can't run.
                # Give each requested WS-backed section a machine-readable error
                # sub-dict under its own key (same shape the section carries when
                # the baseline is up but the fetch fails), plus a summary
                # warning, then skip them so the other sections below still run.
                self._stub_ws_unavailable(
                    result,
                    includes,
                    ws_backed,
                    want_repairs=want_repairs,
                    want_zha=want_zha,
                    want_zwave=want_zwave,
                    want_thread=want_thread,
                    want_matter=want_matter,
                    want_startup=want_startup,
                )
                want_repairs = want_zha = want_zwave = want_thread = want_matter = (
                    want_startup
                ) = False

            want_dead_entities = "dead_entities" in includes

            # One ``ha_mcp_tools/system_snapshot`` read up front, threaded into
            # every consuming section below, when the component supports it and
            # at least one consuming section was actually requested — never
            # fetched (and never counted against the component) for a call that
            # only wants repairs/zha_network/thread_network/matter_network/
            # integration_startup/supervisor/config_check. ``None`` (capability miss, command error/timeout, or
            # a malformed slice) means every consuming section below falls back
            # to its own legacy fetch — all-or-nothing per call, never a partial
            # hybrid. Only the slices a requested section actually needs are
            # asked for, so e.g. a zwave-only call doesn't pay for a full
            # states/entities dump.
            snapshot: _SystemSnapshotSlices | None = None
            if want_zwave or want_dead_entities:
                snapshot = await self._fetch_system_snapshot(
                    include_entities=want_dead_entities,
                    include_states=want_dead_entities,
                )

            sections = self._build_ws_sections(
                ws_client,
                snapshot,
                include_dismissed_repairs_bool,
                zha_full,
                want_repairs=want_repairs,
                want_zha=want_zha,
                want_zwave=want_zwave,
                want_thread=want_thread,
                want_matter=want_matter,
                want_startup=want_startup,
            )
            await self._gather_and_attribute(sections, result)

            if "supervisor" in includes:
                result["supervisor"] = await read_supervisor(self._client)

            if "config_check" in includes:
                # REST call on self._client (POST /config/core/check_config),
                # not a ws_client command — so it runs inline rather than in
                # the ws ``sections`` gather above.
                result["config_check"] = await self._fetch_config_check()

            await self._run_dead_entities_section(result, snapshot, want_dead_entities)

            # Surface the MCP server's own update status so the model can relay
            # it in chat. ``get_update_field`` is best-effort, thread-offloaded,
            # and never raises (omits the field on any hiccup); see
            # ha_mcp.update_check for gating/throttle details.
            from ..update_check import get_update_field

            mcp_update = await get_update_field()
            if mcp_update is not None:
                result["ha_mcp_update"] = mcp_update

            return result

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                suggestions=[
                    "System health may not be available in all HA installations",
                    "Try ha_get_overview() for basic system information",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises
        finally:
            await self._safe_disconnect(ws_client)

    @staticmethod
    def _warn_unknown_includes(result: dict[str, Any], includes: set[str]) -> None:
        """Append a warning to ``result`` for any unrecognized include sections.

        Extracted verbatim from ``ha_get_system_health``.
        """
        VALID_INCLUDES = {
            "repairs",
            "zha_network",
            "zha_network_full",
            "zwave_network",
            "thread_network",
            "matter_network",
            "integration_startup",
            "supervisor",
            "config_check",
            "dead_entities",
        }
        unknown = includes - VALID_INCLUDES
        if unknown:
            result.setdefault("warnings", []).append(
                f"Unknown include sections ignored: {', '.join(sorted(unknown))}"
            )
        for section, replacement in (
            ("diagnostics", "ha_get_integration(include_diagnostics=True)"),
            ("themes", "ha_manage_theme(action='list')"),
        ):
            if section in unknown:
                result["warnings"].append(f"'{section}' moved to {replacement}")

    @staticmethod
    def _stub_ws_unavailable(
        result: dict[str, Any],
        includes: set[str],
        ws_backed: set[str],
        *,
        want_repairs: bool,
        want_zha: bool,
        want_zwave: bool,
        want_thread: bool,
        want_matter: bool,
        want_startup: bool,
    ) -> None:
        """Stub each requested WS-backed section as an error and add a summary
        warning when the health WebSocket is unavailable.

        Extracted verbatim from ``ha_get_system_health``.
        """
        ws_error = "requires the system_health WebSocket, which is unavailable"
        if want_repairs:
            result["repairs"] = {"error": ws_error}
        if want_zha:
            result["zha_network"] = {"error": ws_error}
        if want_zwave:
            result["zwave_network"] = {"error": ws_error}
        if want_thread:
            result["thread_network"] = {"error": ws_error}
        if want_matter:
            result["matter_network"] = {"error": ws_error}
        if want_startup:
            result["integration_startup"] = {"error": ws_error}
        unavailable = sorted(includes & ws_backed)
        if unavailable:
            result.setdefault("warnings", []).append(
                "These sections require the system_health WebSocket, "
                f"which is unavailable: {', '.join(unavailable)}"
            )

    def _build_ws_sections(
        self,
        ws_client: Any,
        snapshot: _SystemSnapshotSlices | None,
        include_dismissed_repairs_bool: bool,
        zha_full: bool,
        *,
        want_repairs: bool,
        want_zha: bool,
        want_zwave: bool,
        want_thread: bool,
        want_matter: bool,
        want_startup: bool,
    ) -> list[tuple[str, Coroutine[Any, Any, dict[str, Any]]]]:
        """Build the list of ``(name, coroutine)`` WS-backed sections to gather."""
        sections: list[tuple[str, Coroutine[Any, Any, dict[str, Any]]]] = []
        if want_repairs:
            sections.append(
                (
                    "repairs",
                    self._fetch_repairs(
                        ws_client, include_dismissed=include_dismissed_repairs_bool
                    ),
                )
            )
        if want_zha:
            sections.append(
                ("zha_network", self._fetch_zha_network(ws_client, full=zha_full))
            )
        if want_zwave:
            sections.append(
                (
                    "zwave_network",
                    self._fetch_zwave_network(
                        ws_client,
                        prefetched_entries=(
                            snapshot.config_entries if snapshot else None
                        ),
                    ),
                )
            )
        if want_thread:
            sections.append(("thread_network", self._fetch_thread_network(ws_client)))
        if want_matter:
            sections.append(
                (
                    "matter_network",
                    read_matter_network(ws_client),
                )
            )
        if want_startup:
            sections.append(
                (
                    "integration_startup",
                    read_integration_startup(ws_client, self._client),
                )
            )
        return sections

    @staticmethod
    async def _gather_and_attribute(
        sections: list[tuple[str, Coroutine[Any, Any, dict[str, Any]]]],
        result: dict[str, Any],
    ) -> None:
        """Gather the WS-backed section coroutines and attribute each outcome to
        ``result[<section>]``, re-raising fatal exceptions.
        """
        if not sections:
            return
        gathered = await asyncio.gather(
            *[coro for _, coro in sections], return_exceptions=True
        )
        # Pre-pass: re-raise anything that must unwind the request
        # rather than land as an embedded section error. ``gather``
        # with ``return_exceptions=True`` returns ``CancelledError``
        # (and any other ``BaseException``) as a result element
        # instead of propagating, and a ``ToolError`` raised from
        # inside a helper would otherwise be silently demoted to
        # ``{"error": "ToolError: …"}`` and break the MCP
        # ``isError=true`` contract for the whole tool.
        # ``reraise_if_fatal`` encapsulates the policy (cancellation,
        # interpreter-exit, ``ToolError``, and the codebase's transport
        # ``HomeAssistantConnectionError``) — the single source of
        # truth shared with each section helper's ``except`` chain.
        for section_result in gathered:
            if isinstance(section_result, BaseException):
                reraise_if_fatal(section_result)
        for (section_name, _), section_result in zip(sections, gathered, strict=True):
            if isinstance(section_result, Exception):
                # Last-resort fallback: emit a minimal ``{error: ...}``
                # dict so an unexpected exception attributes to its
                # section instead of bubbling out and dropping siblings.
                # The helpers themselves return richer
                # ``{<key>: <baseline>, ..., error: ...}`` shapes on
                # their own (caught) failures; this branch is the
                # belt-and-suspenders path that fires only on a
                # helper-edit regression that lets an exception escape.
                logger.warning(
                    "Concurrent fetch for section %r raised: %s: %s",
                    section_name,
                    type(section_result).__name__,
                    section_result,
                )
                result[section_name] = {
                    "error": (f"{type(section_result).__name__}: {section_result}")
                }
            else:
                if isinstance(section_result, dict) and (
                    bubbled := section_result.pop("_warnings", None)
                ):
                    result.setdefault("warnings", []).extend(bubbled)
                result[section_name] = section_result

    async def _run_dead_entities_section(
        self,
        result: dict[str, Any],
        snapshot: _SystemSnapshotSlices | None,
        want_dead_entities: bool,
    ) -> None:
        """Run the dead-entities section and bubble its warnings to
        ``result["warnings"]`` when requested.

        Extracted verbatim from ``ha_get_system_health``.
        """
        if not want_dead_entities:
            return
        # REST + the REST client's own per-client WebSocket bridge
        # (states via /api/states, registry + config-entries via the
        # bridge), not the health ws_client — so it runs inline like
        # config_check and survives a baseline-WS-down install. When
        # the snapshot fetched above succeeded, its entities/states/
        # config_entries slices replace all three fetches.
        dead_section = await self._fetch_dead_entities(
            prefetched_states=snapshot.states if snapshot else None,
            prefetched_registry=snapshot.registry if snapshot else None,
            prefetched_entries=(snapshot.config_entries if snapshot else None),
        )
        # Pop the ``_warnings`` sentinel and bubble it to the top-level
        # ``result["warnings"]`` (the documented contract location).
        # The section helper uses this sentinel so its return signature
        # stays uniform with every other section (a plain dict) while
        # avoiding a collision with the reserved ``warnings`` term.
        section_warnings = dead_section.pop("_warnings", None)
        if section_warnings:
            result.setdefault("warnings", []).extend(section_warnings)
        result["dead_entities"] = dead_section

    @staticmethod
    def _parse_includes(include: str | None) -> set[str]:
        """Parse the comma-separated include parameter into a set of section names."""
        if not include:
            return set()
        return {s.strip().lower() for s in include.split(",") if s.strip()}

    @staticmethod
    async def _safe_disconnect(ws_client: Any) -> None:
        """Best-effort WebSocket disconnect; never raises."""
        if ws_client is None:
            return
        try:
            await ws_client.disconnect()
        except Exception:  # noqa: BLE001
            # Best-effort cleanup: a disconnect failure on an already-closing
            # socket is not actionable and must not mask the real result.
            pass

    async def _fetch_health_info(self) -> tuple[Any, dict[str, Any]]:
        """Connect to WebSocket and retrieve system health info.

        Returns:
            A tuple of (ws_client, result_dict) where ws_client is needed
            for subsequent optional fetches.
        """
        ws_client, error = await get_connected_ws_client(
            self._client.base_url,
            self._client.token,
            verify_ssl=self._client.verify_ssl,
        )
        if error or ws_client is None:
            raise_tool_error(
                error
                or create_error_response(
                    ErrorCode.CONNECTION_FAILED,
                    "Failed to connect to Home Assistant WebSocket",
                )
            )

        try:
            result = await read_system_health(ws_client)
        except TimeoutError:
            # The connection opened but the command stalled — disconnect it
            # before raising so we don't leak the socket.
            await self._safe_disconnect(ws_client)
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Timeout waiting for system health data",
                )
            )
        except Exception as e:  # noqa: BLE001
            await self._safe_disconnect(ws_client)
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    str(e),
                )
            )
        except asyncio.CancelledError:
            await self._safe_disconnect(ws_client)
            raise

        return ws_client, result

    async def _fetch_system_snapshot(
        self,
        *,
        include_entities: bool,
        include_states: bool,
    ) -> _SystemSnapshotSlices | None:
        """One ``ha_mcp_tools/system_snapshot`` read; ``None`` ⇒ every consuming
        section runs its own legacy fetch.

        Threads into ``_fetch_zwave_network`` (config_entries — the zwave_js
        status call itself is unchanged), and ``_fetch_dead_entities``
        (entities + states + config_entries), collapsing what was up to 2x
        ``config_entries/get`` round-trips (a TOCTOU across sections) into one
        consistent in-process read. Repairs are not read from it: Core's ``repairs/list_issues`` also
        drops the issues of a disabled config entry, and the slice does not.

        All-or-nothing per call, mirroring ``_fetch_exposure_via_component`` /
        ``_overview_via_component``: a capability miss, command error/timeout,
        or malformed slice falls back to the legacy per-section fetches for
        EVERY consuming section in this call — never a partial hybrid where
        one section reads the snapshot and a sibling reads legacy data from a
        different instant. ``unknown_command`` invalidates the cached caps
        (component downgraded mid-session); any other command error/timeout
        just logs and falls back, leaving caps cached. A
        ``HomeAssistantConnectionError`` - a pooled-WS drop, or a failed
        (re)connect - is caught here and mapped to ``None``: the consuming
        sections' legacy fetches run on a DEDICATED health WS client
        (zwave) plus REST ``get_states()`` and the
        ``send_websocket_message`` bridge (dead_entities), so a component-side
        fault degrades per-section rather than failing the WHOLE
        ``ha_get_system_health``. A genuinely dead transport is a different
        case and deliberately does NOT degrade: the bridge raises (#1947) and
        the dead-entities detector fails loud rather than answer while blind.

        Only the ``include_*`` flags the caller actually needs are requested
        (``want_dead_entities`` alone needs entities and states), so a call
        asking only for ``zwave_network`` doesn't pay for a full
        states/entities dump.
        """
        caps = await get_component_caps(self._client)
        if not component_supports(caps, "system_snapshot"):
            return None
        try:
            ws = await get_websocket_client(
                url=self._client.base_url,
                token=self._client.token,
                verify_ssl=getattr(self._client, "verify_ssl", None),
            )
            raw = await ws.send_command(
                WS_SYSTEM_SNAPSHOT,
                include_issues=False,
                include_config_entries=True,
                include_entities=include_entities,
                include_states=include_states,
            )
        except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
            if is_unknown_command(exc):
                invalidate_caps(self._client)
            else:
                logger.warning(
                    "%s failed; fell back to legacy: %r", WS_SYSTEM_SNAPSHOT, exc
                )
            return None
        except Exception as exc:  # noqa: BLE001
            # HomeAssistantConnectionError: a pooled-WS drop or a failed
            # (re)connect. The legacy sections degrade individually (dedicated
            # health WS + REST + the bridge), so fall back rather than fail the
            # whole tool on a component-side fault. A genuinely dead transport
            # still fails loud from the bridge itself (#1947).
            logger.warning(
                "%s connection error; falling back to legacy: %r",
                WS_SYSTEM_SNAPSHOT,
                exc,
            )
            return None
        return _build_system_snapshot_slices(raw.get("result") or {})

    async def _fetch_repairs(
        self, ws_client: Any, *, include_dismissed: bool = False
    ) -> dict[str, Any]:
        """Fetch repair issues from Home Assistant's ``repairs/list_issues``.

        Core's own serialization and filter: active issues, without those of a
        disabled config entry. User-dismissed ("ignored") repairs are left out by
        default, as on the HA Repairs page, and ``dismissed_count`` counts them
        when there are any; ``include_dismissed=True`` also returns them. Each
        issue gets the title and description the Repairs page shows.
        """
        repairs: dict[str, Any] = {"issues": [], "count": 0}
        try:
            repairs_result = await ws_client.send_command("repairs/list_issues")
            all_issues = (repairs_result.get("result") or {}).get("issues", [])
            visible_issues = filter_active_repairs(
                all_issues, include_dismissed=include_dismissed
            )
            repairs = {
                "issues": visible_issues,
                "count": len(visible_issues),
            }
            if dismissed_count := len(all_issues) - len(visible_issues):
                repairs["dismissed_count"] = dismissed_count
            if issue_warnings := [
                *await add_repair_texts(self._client, visible_issues),
                *await add_repair_data(ws_client, visible_issues),
            ]:
                repairs["_warnings"] = issue_warnings
        except Exception as e:  # noqa: BLE001
            reraise_if_fatal(e)
            logger.warning("Failed to fetch repairs: %s", e)
            repairs["error"] = f"Repairs data not available: {e}"
        return repairs

    @staticmethod
    async def _fetch_zha_network(ws_client: Any, *, full: bool) -> dict[str, Any]:
        """Fetch ZHA Zigbee network device data."""
        ZHA_SUMMARY_LIMIT = 50
        ZHA_FULL_LIMIT = 25
        zha_network: dict[str, Any] = {"devices": [], "count": 0, "total_count": 0}
        try:
            zha_result = await ws_client.send_command("zha/devices")
            if zha_result.get("success"):
                raw_devices = zha_result.get("result", [])
                total = len(raw_devices)
                device_limit = ZHA_FULL_LIMIT if full else ZHA_SUMMARY_LIMIT
                truncated = total > device_limit
                capped_devices = raw_devices[:device_limit]
                if full:
                    zha_devices = capped_devices
                else:
                    zha_devices = [
                        {
                            "ieee": d.get("ieee"),
                            "name": d.get("user_given_name") or d.get("name"),
                            "manufacturer": d.get("manufacturer"),
                            "model": d.get("model"),
                            "lqi": d.get("lqi"),
                            "rssi": d.get("rssi"),
                            "available": d.get("available"),
                        }
                        for d in capped_devices
                    ]
                zha_network = {
                    "devices": zha_devices,
                    "count": len(zha_devices),
                    "total_count": total,
                }
                if truncated:
                    zha_network["truncated"] = True
                    zha_network["hint"] = (
                        f"Showing {device_limit} of {total} devices. "
                        "Use ha_get_device(integration='zha') for full device list."
                    )
        except Exception as e:  # noqa: BLE001
            reraise_if_fatal(e)
            logger.warning("Failed to fetch ZHA network data: %s", e)
            zha_network["error"] = f"ZHA integration not available or error: {e}"
        return zha_network

    @staticmethod
    async def _fetch_zwave_network(
        ws_client: Any,
        *,
        prefetched_entries: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch Z-Wave JS network status: driver connection, controller, nodes.

        Core's ``zwave_js/network_status`` carries the server connection in
        ``client`` and every node's status inside ``controller``; the nodes are
        moved out of ``controller`` into the capped ``nodes`` list.
        ``prefetched_entries`` (the component's ``system_snapshot``
        ``config_entries`` slice, re-wrapped in the ``{success, result:
        [...]}`` envelope) skips the ``config_entries/get`` WS call when
        given; the ``zwave_js/network_status`` call itself always runs
        (system_snapshot doesn't reach into the radio integration).
        """
        ZWAVE_NODE_LIMIT = 50
        zwave_network: dict[str, Any] = {
            "client": {},
            "controller": {},
            "nodes": [],
            "count": 0,
            "total_count": 0,
        }
        try:
            # Get all zwave_js config entries to find entry_id. The HA command
            # is ``config_entries/get`` (underscore); the slash form is rejected
            # as "Unknown command", which the outer except would mask as
            # "Z-Wave JS integration not available".
            entries_result = (
                prefetched_entries
                if prefetched_entries is not None
                else await ws_client.send_command("config_entries/get")
            )
            zwave_entry_id = None
            if entries_result.get("success"):
                for entry in entries_result.get("result", []):
                    if (
                        entry.get("domain") == "zwave_js"
                        and entry.get("disabled_by") is None
                        and entry.get("source") != "ignore"
                    ):
                        zwave_entry_id = entry.get("entry_id")
                        break

            if not zwave_entry_id:
                zwave_network["error"] = "No enabled Z-Wave JS config entry found"
                return zwave_network

            # Get network status (controller info)
            network_result = await ws_client.send_command(
                "zwave_js/network_status",
                entry_id=zwave_entry_id,
            )
            if network_result.get("success"):
                net_data = network_result.get("result", {})
                controller = dict(net_data.get("controller", {}))
                nodes = controller.pop("nodes", [])
                total_nodes = len(nodes)
                zwave_network["client"] = net_data.get("client", {})
                zwave_network["controller"] = controller
                zwave_network["nodes"] = nodes[:ZWAVE_NODE_LIMIT]
                zwave_network["count"] = len(zwave_network["nodes"])
                zwave_network["total_count"] = total_nodes
                if total_nodes > ZWAVE_NODE_LIMIT:
                    zwave_network["truncated"] = True
                    zwave_network["hint"] = (
                        f"Showing {ZWAVE_NODE_LIMIT} of {total_nodes} nodes. "
                        "Use ha_get_device(integration='zwave_js') for full device list."
                    )
        except Exception as e:  # noqa: BLE001
            reraise_if_fatal(e)
            logger.warning("Failed to fetch Z-Wave network data: %s", e)
            zwave_network["error"] = (
                f"Z-Wave JS integration not available or error: {e}"
            )
        return zwave_network

    @staticmethod
    async def _fetch_thread_network(ws_client: Any) -> dict[str, Any]:
        """Fetch Home Assistant's Thread datasets and its OTBR border routers.

        ``thread/list_datasets`` gives every Thread network Home Assistant
        knows, with ``preferred`` and ``source``; a refused read lands in
        ``datasets_error``. ``otbr/info`` gives one row per OpenThread Border
        Router, forwarded without its ``active_dataset_tlvs``, which holds the
        network key. Core refuses ``otbr/info`` when no OTBR is loaded, and the
        refusal lands in ``border_routers_error``. ``error`` means the whole
        section failed.
        """
        thread_network: dict[str, Any] = {"datasets": [], "border_routers": []}
        try:
            try:
                datasets_result = await ws_client.send_command("thread/list_datasets")
                thread_network["datasets"] = (datasets_result.get("result") or {}).get(
                    "datasets", []
                )
            except HomeAssistantCommandError as e:
                thread_network["datasets_error"] = f"Thread datasets not available: {e}"
            try:
                info_result = await ws_client.send_command("otbr/info")
                thread_network["border_routers"] = [
                    {k: v for k, v in info.items() if k != "active_dataset_tlvs"}
                    for info in (info_result.get("result") or {}).values()
                ]
            except HomeAssistantCommandError as e:
                thread_network["border_routers_error"] = (
                    f"OTBR border routers not available: {e}"
                )
        except Exception as e:  # noqa: BLE001
            reraise_if_fatal(e)
            logger.warning("Failed to fetch Thread network data: %s", e)
            thread_network["error"] = (
                f"Thread/OTBR integration not available or error: {e}"
            )
        thread_network["dataset_count"] = len(thread_network["datasets"])
        thread_network["border_router_count"] = len(thread_network["border_routers"])
        return thread_network

    @staticmethod
    def _ws_result_list(
        resp: Any,
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        """Unwrap a ``send_websocket_message`` response into ``(list, None)``
        on success or ``(None, error_str)`` on failure.

        ``send_websocket_message`` returns the HA WebSocket envelope
        (``{"success": bool, "result": [...]}``) on success and
        ``{"success": False, "error": ...}`` on failure; ``return_exceptions``
        in the caller's ``gather`` can also hand back a raw exception.
        Preserves the underlying cause string (envelope error message,
        exception type, or wrong-shape description) so the caller can
        attribute the failure rather than substitute a fixed "unavailable"
        message that hides the root cause (auth vs command error vs
        malformed envelope). Fatal exceptions (per ``reraise_if_fatal``)
        unwind instead of being returned as an error string.

        A dead transport never reaches this helper as an envelope:
        ``send_websocket_message`` raises ``HomeAssistantConnectionError``
        when no answer came back (#1947), so it arrives either as the raw
        exception from ``gather(return_exceptions=True)`` below or by
        propagating out of the await — and dead entities, being a detector
        whose short list would read as "nothing is dead", fails loud on it
        via the ordinary ``reraise_if_fatal`` path.
        """
        if isinstance(resp, BaseException):
            # gather(return_exceptions=True) hands back the raw exception; let
            # truly-fatal ones unwind instead of masking them as "unavailable".
            reraise_if_fatal(resp)
            return None, f"{type(resp).__name__}: {resp}"
        if not isinstance(resp, dict):
            return None, f"unexpected response type: {type(resp).__name__}"
        # Require success truthy before trusting ``result`` — matches the
        # ``if result.get("success")`` convention used by the other WS handlers
        # in this file (and treats a malformed envelope missing the key as a
        # failure rather than reading a half-built result).
        if not resp.get("success"):
            err = resp.get("error")
            if isinstance(err, dict):
                err_msg = err.get("message") or err.get("code") or str(err)
            elif err:
                err_msg = str(err)
            else:
                err_msg = "unknown error"
            return None, str(err_msg)
        result = resp.get("result")
        if isinstance(result, list):
            return result, None
        return None, f"unexpected result shape: {type(result).__name__}"

    async def _acquire_dead_entity_sources(
        self,
        prefetched_states: list[dict[str, Any]] | None,
        prefetched_registry: dict[str, Any] | None,
        prefetched_entries: dict[str, Any] | None,
        dead: dict[str, Any],
        bubble_warnings: list[str],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], set[str] | None] | None:
        """Fetch + validate the states / registry / config-entries sources.

        Returns ``(state_by_id, registry, live_entry_ids)`` on success, or
        ``None`` after recording a hard-error message on ``dead`` (the caller
        then returns ``dead``). Degradable config-entries issues append to
        ``bubble_warnings`` and still return the tuple. Extracted verbatim from
        ``_fetch_dead_entities``.
        """
        # All three slices are supplied together by the caller as one
        # unit (the ``system_snapshot`` contract guarantees they're
        # never partial — see ``_build_system_snapshot_slices``), so the
        # gate requires all three rather than just ``prefetched_registry``
        # to keep that invariant explicit here rather than relying on the
        # call site.
        if (
            prefetched_states is not None
            and prefetched_registry is not None
            and prefetched_entries is not None
        ):
            states: Any = prefetched_states
            registry_raw: Any = prefetched_registry
            entries_raw: Any = prefetched_entries
        else:
            # Index the gather result (rather than tuple-unpack) so mypy
            # can type each element through the return_exceptions=True
            # overload; mirrors smart_search/_entities.py::_fetch_search_entities.
            results = await asyncio.gather(
                self._client.get_states(),
                self._client.send_websocket_message(
                    {"type": "config/entity_registry/list"}
                ),
                self._client.send_websocket_message({"type": "config_entries/get"}),
                return_exceptions=True,
            )
            states = results[0]
            registry_raw = results[1]
            entries_raw = results[2]

        if isinstance(states, BaseException):
            # Truly-fatal errors must propagate, not demote to a section
            # error string (mirrors the ws sections gather pre-pass).
            reraise_if_fatal(states)
            dead["error"] = f"Could not fetch entity states: {states}"
            return None
        if not isinstance(states, list):
            dead["error"] = (
                "Could not fetch entity states: expected list, got "
                f"{type(states).__name__}"
            )
            return None
        registry, registry_err = self._ws_result_list(registry_raw)
        if registry is None:
            # Preserve the underlying cause (envelope error message,
            # exception type, or wrong-shape description) so the client
            # can distinguish auth vs command vs malformed envelope
            # rather than see a fixed "unavailable" substitute.
            dead["error"] = (
                f"Could not fetch entity registry "
                f"(config/entity_registry/list: {registry_err})"
            )
            return None

        # config-entries is the only optional source: without it the
        # definitive orphan tier can't be computed, but stale_restored still
        # can — so degrade rather than fail the whole section.
        entries, entries_err = self._ws_result_list(entries_raw)
        live_entry_ids: set[str] | None = None
        if entries is None:
            # Genuine fetch failure — preserve the cause so a backend
            # failure isn't reported as "no entries".
            dead["config_entries_checked"] = False
            bubble_warnings.append(
                f"config_entries/get failed ({entries_err}); "
                "config_entry_orphans tier skipped (cannot distinguish a "
                "removed integration from a failed fetch). stale_restored "
                "still computed."
            )
        elif not entries:
            # Real empty list — HA reports no config entries configured.
            # Distinct from a fetch failure: the message names the actual
            # state. The orphan tier still skips since there is no live
            # set to diff against; stale_restored still computed.
            dead["config_entries_checked"] = False
            bubble_warnings.append(
                "config_entries/get returned an empty list (no "
                "integrations configured); config_entry_orphans tier "
                "skipped. stale_restored still computed."
            )
        else:
            live_entry_ids = {
                e["entry_id"]
                for e in entries
                if isinstance(e, dict) and e.get("entry_id")
            }
            dead["config_entries_checked"] = True

        state_by_id = {
            s["entity_id"]: s
            for s in states
            if isinstance(s, dict) and s.get("entity_id")
        }
        return state_by_id, registry, live_entry_ids

    @staticmethod
    def _classify_registry_entries(
        registry: list[dict[str, Any]],
        state_by_id: dict[str, Any],
        live_entry_ids: set[str] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Classify registry entries into ``(orphans, stale_restored)`` buckets.

        Extracted verbatim from ``_fetch_dead_entities``' classification loop.
        """
        orphans: list[dict[str, Any]] = []
        stale: list[dict[str, Any]] = []
        for entry in registry:
            if not isinstance(entry, dict):
                continue
            eid = entry.get("entity_id")
            if not eid:
                continue
            cfg = entry.get("config_entry_id")
            disabled_by = entry.get("disabled_by")

            # Tier 1 — config-entry orphan (only when the live set loaded).
            # Covers disabled leftovers too: a disabled entity whose config
            # entry is gone is still dead cruft (disabled_by is surfaced on
            # the item so the client sees why it lingered).
            if live_entry_ids is not None and cfg and cfg not in live_entry_ids:
                orphans.append(
                    {
                        "entity_id": eid,
                        "platform": entry.get("platform"),
                        "config_entry_id": cfg,
                        "disabled_by": disabled_by,
                        "has_state": eid in state_by_id,
                    }
                )
                continue

            # Tier 2 — stale restored. Skip intentionally-disabled entries
            # (they normally have no state object anyway).
            if disabled_by is not None:
                continue
            state_obj = state_by_id.get(eid)
            if state_obj is None:
                continue
            attrs = state_obj.get("attributes")
            if (
                state_obj.get("state") == "unavailable"
                and isinstance(attrs, dict)
                and attrs.get("restored")
            ):
                stale.append(
                    {
                        "entity_id": eid,
                        "platform": entry.get("platform"),
                        "config_entry_id": cfg,
                    }
                )
        return orphans, stale

    async def _fetch_dead_entities(
        self,
        *,
        prefetched_states: list[dict[str, Any]] | None = None,
        prefetched_registry: dict[str, Any] | None = None,
        prefetched_entries: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Surface orphaned/stale entity-registry entries.

        Diffs the entity registry against the state machine and the live
        config-entries set, classifying findings into confidence tiers:

        - ``config_entry_orphans`` (definitive): registry entries whose
          ``config_entry_id`` is no longer present in ``config_entries/get`` —
          the owning integration instance was removed, leaving the registry
          entry behind.
        - ``stale_restored`` (likely): entries HA recreated from the registry on
          startup — state ``unavailable`` with ``restored: true`` — whose owning
          config entry still exists. The integration is loaded but no longer
          provides the entity (renamed/removed device, re-paired Zigbee).

        Deliberately NEVER flagged, to keep false positives low: ``unknown``
        state (alive, just no current value — e.g. weather/disaster-alert
        sensors), bare ``unavailable`` without ``restored`` (a loaded
        integration reporting a device merely offline right now), and entries
        disabled via ``disabled_by`` (intentional, unless their config entry is
        also gone — those still surface as orphans). The ``restored`` flag is
        what HA sets when it rebuilds a state object from the registry's cached
        last state because no live platform currently provides the entity; it is
        the discriminator between "dead" and "temporarily offline". (This tracks
        HA Core's state-restoration behaviour; re-verify if classification drifts
        after an HA upgrade.)

        Entities can appear under ``stale_restored`` transiently right after a
        restart, before integrations finish loading; ``note`` flags this.

        Instance method (not @staticmethod): uses the REST client
        (``self._client``) for states plus its per-client WebSocket bridge for
        the registry + config entries, so it needs no system_health ws_client
        and runs even when the health baseline is unavailable.

        ``prefetched_states`` / ``prefetched_registry`` / ``prefetched_entries``
        (the component's ``system_snapshot`` ``states`` / ``entities`` /
        ``config_entries`` slices — ``registry`` / ``entries`` re-wrapped in
        the ``{success, result: [...]}`` envelope ``_ws_result_list`` already
        unwraps, ``states`` as the bare list ``get_states()`` returns) are
        supplied together by the caller as one unit; when given, all three
        fetches below are skipped.
        """
        DEAD_ENTITIES_LIMIT = 50
        dead: dict[str, Any] = {
            "config_entry_orphans": {"items": [], "count": 0, "total_count": 0},
            "stale_restored": {"items": [], "count": 0, "total_count": 0},
            "summary": {"candidate_total": 0, "registry_total": 0},
        }
        # Warnings collected here are bubbled to the top-level
        # ``result["warnings"]`` by the aggregator. The section returns a
        # plain dict (like every other section helper, keeping the return
        # signature uniform) with a ``_warnings`` sentinel that
        # ``ha_get_system_health`` pops and extends onto ``result["warnings"]``
        # — the documented contract location, which a section-local
        # ``warnings`` key would collide with.
        bubble_warnings: list[str] = []
        try:
            sources = await self._acquire_dead_entity_sources(
                prefetched_states,
                prefetched_registry,
                prefetched_entries,
                dead,
                bubble_warnings,
            )
            if sources is None:
                return dead
            state_by_id, registry, live_entry_ids = sources

            orphans, stale = self._classify_registry_entries(
                registry, state_by_id, live_entry_ids
            )

            def _bucket(items: list[dict[str, Any]]) -> dict[str, Any]:
                total = len(items)
                capped = items[:DEAD_ENTITIES_LIMIT]
                bucket: dict[str, Any] = {
                    "items": capped,
                    "count": len(capped),
                    "total_count": total,
                }
                if total > DEAD_ENTITIES_LIMIT:
                    bucket["truncated"] = True
                    bucket["hint"] = (
                        f"Showing {DEAD_ENTITIES_LIMIT} of {total}; "
                        "remove cleanup candidates in batches and re-run."
                    )
                return bucket

            candidate_total = len(orphans) + len(stale)
            dead["config_entry_orphans"] = _bucket(orphans)
            dead["stale_restored"] = _bucket(stale)
            dead["summary"] = {
                "candidate_total": candidate_total,
                "registry_total": len(registry),
            }
            # Only attach the guidance note when there is something to act on —
            # no point spending tokens on cleanup advice for an empty result.
            if candidate_total:
                dead["note"] = (
                    "Excludes 'unknown'-state entities and merely-offline "
                    "devices (bare 'unavailable' without 'restored'). Entries "
                    "can appear under stale_restored transiently right after a "
                    "restart; re-run if HA restarted recently. Remove a "
                    "confirmed-dead entity with ha_remove_entity(entity_id)."
                )
        except ToolError:
            # A ToolError (incl. one re-raised by reraise_if_fatal) carries the
            # MCP isError contract — let it reach ha_get_system_health's own
            # ``except ToolError: raise`` instead of being demoted to a section
            # error string here (.gemini/styleguide.md structured-error guard pattern).
            raise
        except Exception as e:
            reraise_if_fatal(e)
            # ``logger.exception`` so an unexpected diff bug gets a full
            # traceback rather than a one-line warning that hides the
            # site of the regression.
            logger.exception("Failed to compute dead entities")
            dead["error"] = f"Dead-entities diff not available: {e}"
        # ``_warnings`` is a sentinel that ``ha_get_system_health`` pops to
        # ``result["warnings"]``; it never reaches the client. Attaching it
        # outside the try/except keeps it correct on both happy and
        # embed-as-error paths.
        if bubble_warnings:
            dead["_warnings"] = bubble_warnings
        return dead

    async def _fetch_config_check(self) -> dict[str, Any]:
        """Validate HA configuration via POST /config/core/check_config.

        Returns Core's reply as Core sends it: ``result`` (``valid`` or
        ``invalid``) plus ``errors`` and ``warnings``, each one string or null;
        only errors make the result ``invalid``. A backend failure surfaces as
        ``{"error": ...}`` without sinking the rest of ha_get_system_health.
        """
        try:
            config_result: dict[str, Any] = await self._client.check_config()
        except Exception as e:  # noqa: BLE001
            reraise_if_fatal(e)
            logger.warning("Failed to check config: %s", e)
            return {"error": f"Config check not available: {e}"}
        return config_result


def register_system_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant system management tools."""
    register_tool_methods(mcp, SystemTools(client))
