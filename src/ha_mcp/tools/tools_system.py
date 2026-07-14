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
from typing import Annotated, Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    get_connected_ws_client,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .util_helpers import (
    JSON_STRING_COERCION,
    fetch_integration_diagnostics,
    filter_active_repairs,
    parse_diagnostics_fields,
    summarize_theme_listing,
)

logger = logging.getLogger(__name__)


def _reraise_if_fatal(exc: BaseException) -> None:
    """Re-raise exceptions that must unwind rather than be demoted to an
    embedded section error:

    - ``CancelledError`` / ``KeyboardInterrupt`` / ``SystemExit`` (all
      ``BaseException`` but not ``Exception``) — task cancellation and
      interpreter exit.
    - ``ToolError`` — carries the MCP ``isError`` contract.
    - ``HomeAssistantConnectionError`` — once the HA transport is dead the
      remaining section fetches will fail anyway, so propagate the root
      cause as ``isError=true`` rather than embed N per-section connection
      errors. The codebase's ``rest_client._request`` already wraps
      ``OSError`` / timeout / transport failures into this class, so it is
      the single fatal class to gate on.

    This implements the cross-section policy proposed in #1624: a dead
    connection fails loud rather than degrade per-section. Every section
    helper in ``ha_get_system_health`` routes its ``except Exception`` block
    through this gate as its first line, so the policy is consistent across
    the ws ``sections`` gather pre-pass and every inline section. Recoverable
    ``Exception``-level failures still fall through to the caller's
    embed-as-error handling.
    """
    # Local import: ``rest_client`` imports from tool helpers transitively,
    # so a module-level import here would risk a circular import in the
    # tools package.
    from ..client.rest_client import HomeAssistantConnectionError

    if (
        isinstance(exc, ToolError)
        or not isinstance(exc, Exception)
        or isinstance(exc, HomeAssistantConnectionError)
    ):
        raise exc


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
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "title": "Restart Home Assistant",
        },
    )
    @log_tool_usage
    async def ha_restart(
        self,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """
        Restart Home Assistant.

        **WARNING: This will restart the entire Home Assistant instance!**
        All automations will be temporarily unavailable during restart.
        The restart typically takes 1-5 minutes depending on your setup.

        **Parameters:**
        - confirm: Must be set to True to confirm the restart. This is a safety
                   measure to prevent accidental restarts.

        **Best Practices:**
        1. Config is validated automatically before the restart proceeds; to
           pre-check, call ha_get_system_health(include="config_check")
        2. Notify users before restarting (if applicable)
        3. Schedule restarts during low-activity periods

        **Example Usage:**
        ```python
        # Optional pre-check (ha_restart also validates config automatically)
        health = ha_get_system_health(include="config_check")
        if health["config_check"]["is_valid"]:
            # Restart with confirmation
            result = ha_restart(confirm=True)
        ```

        **Alternative:** For configuration changes, consider using ha_reload_core()
        instead, which reloads specific components without a full restart.
        """
        if not confirm:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Restart not confirmed",
                    details=(
                        "You must set confirm=True to restart Home Assistant. "
                        "This is a safety measure to prevent accidental restarts."
                    ),
                    suggestions=[
                        'Pre-check config via ha_get_system_health(include="config_check")',
                        "Call ha_restart(confirm=True) to proceed with restart",
                        "Consider using ha_reload_core() for config-only changes",
                    ],
                )
            )

        restart_initiated = False
        try:
            # Check configuration first as a safety measure
            config_result = await self._client.check_config()
            if config_result.get("result") != "valid":
                errors = config_result.get("errors") or []
                raise_tool_error(
                    create_error_response(
                        ErrorCode.CONFIG_INVALID,
                        "Configuration is invalid - restart aborted",
                        details=(
                            "Home Assistant configuration has errors. "
                            "Fix the errors before restarting."
                        ),
                        context={"config_errors": errors},
                    )
                )

            # Call the restart service - mark as initiated before the call
            # as the connection may be closed before we get a response
            restart_initiated = True
            await self._client.call_service("homeassistant", "restart", {})

            return {
                "success": True,
                "message": (
                    "Home Assistant restart initiated. "
                    "The system will be unavailable for 1-5 minutes."
                ),
                "warnings": [
                    "Connection will be lost during restart. "
                    "Wait for Home Assistant to become available again."
                ],
            }

        except ToolError:
            raise
        except Exception as e:
            error_msg = str(e)
            # Connection errors after restart initiated are expected
            # (HA closes connections during restart)
            if restart_initiated and any(
                pattern in error_msg.lower()
                for pattern in (
                    "connect",
                    "closed",
                    "504",
                    "502",
                    "503",
                    "gateway",
                    "unavailable",
                )
            ):
                return {
                    "success": True,
                    "message": (
                        "Home Assistant restart initiated. "
                        "Connection was closed as expected during restart."
                    ),
                    "warnings": ["Wait 1-5 minutes for Home Assistant to restart."],
                }

            exception_to_structured_error(e)
            return None  # unreachable: exception_to_structured_error always raises

    @tool(
        name="ha_reload_core",
        tags={"System"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "title": "Reload Core Components",
        },
    )
    @log_tool_usage
    async def ha_reload_core(
        self,
        target: str = "all",
        entry_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Reload Home Assistant configuration without full restart.

        This tool reloads specific configuration components, allowing changes
        to take effect without restarting the entire Home Assistant instance.
        This is much faster than a full restart.

        **Parameters:**
        - target: What to reload. Options:
          - "all": Reload all reloadable components
          - "automations": Reload automation configurations
          - "scripts": Reload script configurations
          - "scenes": Reload scene configurations
          - "groups": Reload group configurations
          - "input_booleans": Reload input_boolean helpers
          - "input_numbers": Reload input_number helpers
          - "input_texts": Reload input_text helpers
          - "input_selects": Reload input_select helpers
          - "input_datetimes": Reload input_datetime helpers
          - "input_buttons": Reload input_button helpers
          - "timers": Reload timer helpers
          - "templates": Reload template sensors/entities
          - "persons": Reload person configurations
          - "zones": Reload zone configurations
          - "core": Reload core configuration (customize, packages)
          - "themes": Reload frontend themes
        - entry_id: Reload a SINGLE config entry (one integration instance)
          instead of sweeping subsystems — the fast path after editing a custom
          component on disk. Pass it alone (leave `target` at its "all" default);
          combining it with an explicit `target` is a validation error. Find the
          id via ha_get_integration.

        **Example Usage:**
        ```python
        # Reload just automations after editing
        ha_reload_core(target="automations")

        # Reload all configurations
        ha_reload_core(target="all")

        # Reload input helpers after adding new ones
        ha_reload_core(target="input_booleans")
        ```

        **When to Use:**
        - After editing automation/script YAML files
        - After adding new input helpers via YAML
        - After modifying customize.yaml
        - After theme changes
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
                # Reload all reloadable components. Fire the calls concurrently
                # but cap the in-flight count with a semaphore so a large
                # install doesn't hit HA with ~16 simultaneous service calls.
                # A single failing target must not cancel its siblings, so the
                # calls are gathered with ``return_exceptions=True`` and
                # attributed per target below (preserving RELOAD_TARGETS order).
                reloadable = [
                    (name, info)
                    for name, info in RELOAD_TARGETS.items()
                    if info is not None
                ]
                semaphore = asyncio.Semaphore(4)

                async def _reload_one(domain: str, service: str) -> None:
                    async with semaphore:
                        await self._client.call_service(domain, service, {})

                outcomes = await asyncio.gather(
                    *(
                        _reload_one(domain, service)
                        for _, (domain, service) in reloadable
                    ),
                    return_exceptions=True,
                )

                results = []
                errors = []
                for (reload_target, _), outcome in zip(
                    reloadable, outcomes, strict=True
                ):
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
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"target": target},
                suggestions=[
                    f"Ensure the {target} integration is loaded",
                    "Check Home Assistant logs for details",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

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
        except Exception as e:
            # Local import mirrors ``_reraise_if_fatal``: rest_client imports
            # from the tool helpers transitively, so a module-level import
            # would risk a circular import in the tools package.
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

    @tool(
        name="ha_get_system_health",
        tags={"System", "Zigbee", "Z-Wave", "Thread", "Matter", "Integrations"},
        annotations={
            "openWorldHint": False,
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get System Health (incl. ZHA/Z-Wave/integration diagnostics)",
        },
    )
    @log_tool_usage
    async def ha_get_system_health(
        self,
        include: str | None = None,
        include_dismissed_repairs: bool | None = False,
        config_entry_id: str | None = None,
        device_id: str | None = None,
        diagnostics_fields: Annotated[
            list[str] | str | None, JSON_STRING_COERCION
        ] = None,
        diagnostics_truncate_at_bytes: Annotated[int, Field(ge=1)] | None = None,
        diagnostics_data_path: str | None = None,
        diagnostics_data_offset: Annotated[int, Field(ge=0)] | None = 0,
        diagnostics_data_limit: Annotated[int, Field(ge=1)] | None = None,
    ) -> dict[str, Any]:
        """
        Get Home Assistant system health, including Zigbee (ZHA), Z-Wave JS, and per-integration diagnostics dumps.

        Returns health check results from integrations, system resources, and connectivity.
        Available information varies by installation type and loaded integrations.

        The result also carries an ``ha_mcp_update`` object —
        ``{current, latest, update_available}`` — reporting whether a newer
        ha-mcp release is available (from PyPI for pip/Docker, or the Supervisor
        add-on store for the add-on), so you can proactively tell the user to
        upgrade. Present on every install type including the HA add-on (so a user
        who missed the Supervisor's update prompt still hears about it); omitted
        only for the ``unknown`` version and when ``HA_MCP_DISABLE_UPDATE_CHECK``
        is set.

        **Parameters:**
        - include: Optional comma-separated list of additional data to include.
          - "repairs": Repair items from Settings > System > Repairs (active only by default; pass `include_dismissed_repairs=True` for all)
          - "zha_network": ZHA Zigbee devices with radio signal summary (name, LQI, RSSI)
          - "zha_network_full": ZHA Zigbee devices with all device details (can be large on 100+ device networks; prefer "zha_network" for summary)
          - "zwave_network": Z-Wave JS network status and node summary (status, security, routing)
          - "thread_network": Thread/OpenThread Border Router (OTBR) summary — per border-router channel, extended_pan_id, and border_agent_id (integration-presence + radio-network view, not per-node Thread health)
          - "matter_network": Matter integration presence summary — config_entry_id, state, and title (per-node health is exposed separately via Matter node diagnostics, not here)
          - "themes": Installed theme names and defaults (sorted list of theme names, count, default_theme, default_dark_theme)
          - "diagnostics": Per-integration diagnostics dump — integration-defined JSON
            (commonly includes redacted config, device list, state snapshots; exact
            top-level keys vary by integration). REQUIRES ``config_entry_id``. The
            canonical artifact users grab via Settings → Devices & Services →
            [integration] → ⋯ → Download diagnostics. Use this when triaging integration
            bugs or filing ``ha_report_issue`` for a specific integration. Payloads can
            be large (Hue ~290 KB, ZHA/MQTT/ESPHome several MB) — pair with
            ``diagnostics_fields`` or ``diagnostics_truncate_at_bytes`` to fit the LLM
            context budget.
          - "config_check": Validate HA configuration via POST /config/core/check_config
            (the pre-restart safety check; ha_restart runs it automatically). Returns
            {result: valid|invalid, is_valid, errors}; read-only/idempotent, takes no args.
          - "dead_entities": Surface orphaned/stale entity-registry entries by diffing
            the registry against the state machine and the live config-entries set.
            Returns confidence-tiered buckets — ``config_entry_orphans`` (owning
            integration instance gone; definitively dead) and ``stale_restored`` (HA
            restored the entity from the registry on startup but the loaded integration
            no longer provides it). Each item carries entity_id + platform so a client
            can propose cleanup with ha_remove_entity. Deliberately excludes
            ``unknown``-state entities and merely-offline devices to keep false positives
            low. Read-only; takes no args.
          - Example: include="repairs,zha_network,zwave_network,config_check"
          - Example: include="diagnostics", config_entry_id="abc123..."
        - include_dismissed_repairs: Include user-dismissed/ignored repairs (default: False). Only meaningful when "repairs" is in `include`.
        - config_entry_id: Required when ``include`` contains ``diagnostics``. The config
          entry ID of the integration (find via ``ha_get_integration``).
        - device_id: Optional. When set with ``include=diagnostics``, returns the
          device-scoped diagnostics dump for that specific device under the integration
          (rather than the full integration dump). Some integrations only expose
          config-entry-level dumps; others expose both.
        - diagnostics_fields: Optional list of top-level keys to keep from the
          diagnostics ``data`` payload (e.g. ``["home_assistant", "issues"]``). Accepts
          a JSON list or comma-separated string. Only applies with ``include=diagnostics``.
        - diagnostics_truncate_at_bytes: Optional byte cap on the serialized
          diagnostics payload (post-projection / post-data_path). On hit,
          drops ``data`` and emits ``truncated=true``, ``bytes_total``,
          ``byte_cap``, plus ``available_fields`` (when the capped value
          is a dict). Only applies when ``include`` contains ``diagnostics``.
          Recommended starting point: 20000 bytes.
        - diagnostics_data_path: Optional dotted path into the diagnostics
          ``data`` sub-tree (e.g. ``"data.devices"`` for ZHA per-device records).
          Walks into the post-fields payload. Resolution failures replace
          ``data`` with ``null`` and surface ``data_path_error``. Only applies
          when ``include`` contains ``diagnostics``.
        - diagnostics_data_offset / diagnostics_data_limit: Pagination on
          list-valued ``diagnostics_data_path`` results. When ``data_limit``
          is set and the resolved path is a list, ``data`` becomes
          ``{"path", "items", "offset", "limit", "total", "has_more"}``. Only
          applies when ``include`` contains ``diagnostics``.

          Example workflow (walk a list-valued sub-tree one page at a time;
          the exact ``data_path`` varies by integration version):
          ``ha_get_system_health(include="diagnostics", config_entry_id="abc",
          diagnostics_data_path="<list-valued path>", diagnostics_data_limit=10)``
          → inspect the page envelope's ``total`` / ``has_more`` → repeat
          with ``diagnostics_data_offset=10`` for the next slice.
        """
        includes = self._parse_includes(include)
        include_dismissed_repairs_bool = bool(include_dismissed_repairs)

        # Sections that require the system_health WebSocket connection; the
        # REST-based sections (config_check, diagnostics) do not.
        ws_backed = {
            "repairs",
            "zha_network",
            "zha_network_full",
            "zwave_network",
            "thread_network",
            "matter_network",
            "themes",
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
                # Degrade gracefully ONLY when the caller asked for a REST-based
                # section (config_check / diagnostics / dead_entities) that can
                # still be served without the WebSocket. config_check is the
                # pure-REST replacement for the removed ha_check_config tool, so
                # it must not depend on the health WebSocket (the
                # system_health/info command carries its own 10s timeout and can
                # hang/be absent on some installs). dead_entities uses the REST
                # client's own per-client WebSocket bridge for the registry +
                # config-entries (not this health ws_client), so it is likewise
                # independent of the baseline. If the caller asked for nothing
                # (the health baseline itself) or only WS-backed sections, the
                # baseline WAS the deliverable: re-raise so the failure surfaces
                # as isError=true, exactly as before this change.
                if not (includes & {"config_check", "diagnostics", "dead_entities"}):
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
                        "returning REST-based sections only."
                    ],
                }

            # Warn about unrecognized include values
            VALID_INCLUDES = {
                "repairs",
                "zha_network",
                "zha_network_full",
                "zwave_network",
                "thread_network",
                "matter_network",
                "diagnostics",
                "config_check",
                "themes",
                "dead_entities",
            }
            unknown = includes - VALID_INCLUDES
            if unknown:
                result.setdefault("warnings", []).append(
                    f"Unknown include sections ignored: {', '.join(sorted(unknown))}"
                )

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
            want_themes = "themes" in includes

            if ws_client is None:
                # Health WebSocket unavailable: WS-backed sections can't run.
                # Give each requested WS-backed section a machine-readable error
                # sub-dict under its own key (same shape the section carries when
                # the baseline is up but the fetch fails), plus a summary
                # warning, then skip them so the REST sections below still run.
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
                if want_themes:
                    result["themes"] = {"error": ws_error}
                unavailable = sorted(includes & ws_backed)
                if unavailable:
                    result.setdefault("warnings", []).append(
                        "These sections require the system_health WebSocket, "
                        f"which is unavailable: {', '.join(unavailable)}"
                    )
                want_repairs = want_zha = want_zwave = want_thread = want_matter = (
                    want_themes
                ) = False

            sections: list[tuple[str, Coroutine[Any, Any, dict[str, Any]]]] = []
            if want_repairs:
                sections.append(
                    (
                        "repairs",
                        self._fetch_repairs(
                            ws_client,
                            include_dismissed=include_dismissed_repairs_bool,
                        ),
                    )
                )
            if want_zha:
                sections.append(
                    ("zha_network", self._fetch_zha_network(ws_client, full=zha_full))
                )
            if want_zwave:
                sections.append(("zwave_network", self._fetch_zwave_network(ws_client)))
            if want_thread:
                sections.append(
                    ("thread_network", self._fetch_thread_network(ws_client))
                )
            if want_matter:
                sections.append(
                    ("matter_network", self._fetch_matter_network(ws_client))
                )
            if want_themes:
                sections.append(("themes", self._fetch_themes(ws_client)))

            if sections:
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
                # ``_reraise_if_fatal`` encapsulates the policy (cancellation,
                # interpreter-exit, ``ToolError``, and the codebase's transport
                # ``HomeAssistantConnectionError``) — the single source of
                # truth shared with each section helper's ``except`` chain.
                for section_result in gathered:
                    if isinstance(section_result, BaseException):
                        _reraise_if_fatal(section_result)
                for (section_name, _), section_result in zip(
                    sections, gathered, strict=True
                ):
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
                            "error": (
                                f"{type(section_result).__name__}: {section_result}"
                            )
                        }
                    else:
                        result[section_name] = section_result

            # Diagnostics-related coercions live outside the includes branch
            # so the orphan-args warning at the ``elif`` after the
            # ``if "diagnostics" in includes`` block (see below) can see
            # canonicalised values — passing ``diagnostics_data_offset=20``
            # without ``include=diagnostics`` would otherwise slip past the gate.
            fields_list = parse_diagnostics_fields(diagnostics_fields)
            truncate_bytes = diagnostics_truncate_at_bytes
            data_offset_int = (
                diagnostics_data_offset if diagnostics_data_offset is not None else 0
            )
            data_limit_int = diagnostics_data_limit
            # Type-guard ``diagnostics_data_path`` here so a bad caller (dict /
            # list) surfaces as ``VALIDATION_INVALID_PARAMETER`` instead of
            # leaking as ``INTERNAL_ERROR`` from the resolver's ``.strip()``
            # downstream.
            if diagnostics_data_path is not None and not isinstance(
                diagnostics_data_path, str
            ):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "diagnostics_data_path must be a string, got "
                        f"{type(diagnostics_data_path).__name__}",
                        context={"parameter": "diagnostics_data_path"},
                    )
                )

            if "diagnostics" in includes:
                # ``fetch_integration_diagnostics`` carries the empty-id guard
                # (returns {"config_entry_id": ..., "error": ...}); calling it
                # unconditionally keeps the missing-id error shape consistent
                # with the populated path instead of returning a bare
                # ``{"error": ...}`` sub-dict on the inline branch. Forward
                # ``config_entry_id`` as-is (None / "") so the helper's echo
                # field reflects what the caller actually passed.
                result["diagnostics"] = await fetch_integration_diagnostics(
                    self._client,
                    config_entry_id,
                    device_id,
                    fields=fields_list,
                    truncate_at_bytes=truncate_bytes,
                    data_path=diagnostics_data_path,
                    data_offset=data_offset_int,
                    data_limit=data_limit_int,
                )
            elif (
                config_entry_id
                or device_id
                or diagnostics_fields is not None
                or diagnostics_truncate_at_bytes is not None
                or diagnostics_data_path is not None
                or diagnostics_data_limit is not None
                or data_offset_int > 0
            ):
                result.setdefault("warnings", []).append(
                    "config_entry_id, device_id, diagnostics_fields, "
                    "diagnostics_truncate_at_bytes, diagnostics_data_path, "
                    "diagnostics_data_offset, and/or diagnostics_data_limit "
                    "were provided but ignored because 'diagnostics' is not "
                    "in include"
                )

            if "config_check" in includes:
                # REST call on self._client (POST /config/core/check_config),
                # not a ws_client command — so it runs inline like diagnostics
                # rather than in the ws ``sections`` gather above. Standalone
                # ``if`` (not chained to diagnostics) so both can be requested
                # in one call.
                result["config_check"] = await self._fetch_config_check()

            if "dead_entities" in includes:
                # REST + the REST client's own per-client WebSocket bridge
                # (states via /api/states, registry + config-entries via the
                # bridge), not the health ws_client — so it runs inline like
                # config_check and survives a baseline-WS-down install.
                dead_section = await self._fetch_dead_entities()
                # Pop the ``_warnings`` sentinel and bubble it to the top-level
                # ``result["warnings"]`` (the documented contract location).
                # The section helper uses this sentinel so its return signature
                # stays uniform with every other section (a plain dict) while
                # avoiding a collision with the reserved ``warnings`` term.
                section_warnings = dead_section.pop("_warnings", None)
                if section_warnings:
                    result.setdefault("warnings", []).extend(section_warnings)
                result["dead_entities"] = dead_section

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
        except Exception as e:
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
        except Exception:
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
            _, event_response = await ws_client.send_command_with_event(
                "system_health/info", wait_timeout=10.0
            )
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
        except Exception as e:
            await self._safe_disconnect(ws_client)
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    str(e),
                )
            )

        health_info = event_response.get("event", {})
        component_count = len(health_info) if isinstance(health_info, dict) else 0

        result: dict[str, Any] = {
            "success": True,
            "health_info": health_info,
            "component_count": component_count,
            "message": f"Retrieved health info for {component_count} components",
        }

        return ws_client, result

    @staticmethod
    async def _fetch_repairs(
        ws_client: Any, *, include_dismissed: bool = False
    ) -> dict[str, Any]:
        """Fetch repair issues from Home Assistant.

        Filters out user-dismissed ("ignored") repairs by default to match the
        HA Repairs UI. Pass ``include_dismissed=True`` to return all issues
        and report the dismissed count alongside the active count.
        """
        repairs: dict[str, Any] = {"issues": [], "count": 0}
        try:
            repairs_result = await ws_client.send_command("repairs/list_issues")
            if repairs_result.get("success"):
                all_issues = repairs_result.get("result", {}).get("issues", [])
                visible_issues = filter_active_repairs(
                    all_issues, include_dismissed=include_dismissed
                )
                repairs = {
                    "issues": visible_issues,
                    "count": len(visible_issues),
                }
                if not include_dismissed:
                    dismissed_count = len(all_issues) - len(visible_issues)
                    if dismissed_count:
                        repairs["dismissed_count"] = dismissed_count
            else:
                err = repairs_result.get("error") or {}
                err_msg = (
                    err.get("message") if isinstance(err, dict) else str(err)
                ) or "unknown error"
                logger.warning(
                    "repairs/list_issues returned success=false: %s", err_msg
                )
                repairs["error"] = f"Repairs data not available: {err_msg}"
        except Exception as e:
            _reraise_if_fatal(e)
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
        except Exception as e:
            _reraise_if_fatal(e)
            logger.warning("Failed to fetch ZHA network data: %s", e)
            zha_network["error"] = f"ZHA integration not available or error: {e}"
        return zha_network

    @staticmethod
    async def _fetch_zwave_network(ws_client: Any) -> dict[str, Any]:
        """Fetch Z-Wave JS network status and node summary."""
        ZWAVE_NODE_LIMIT = 50
        zwave_network: dict[str, Any] = {
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
            entries_result = await ws_client.send_command("config_entries/get")
            zwave_entry_id = None
            if entries_result.get("success"):
                for entry in entries_result.get("result", []):
                    if entry.get("domain") == "zwave_js":
                        zwave_entry_id = entry.get("entry_id")
                        break

            if not zwave_entry_id:
                zwave_network["error"] = "Z-Wave JS integration not found"
                return zwave_network

            # Get network status (controller info)
            network_result = await ws_client.send_command(
                "zwave_js/network_status",
                entry_id=zwave_entry_id,
            )
            if network_result.get("success"):
                net_data = network_result.get("result", {})
                zwave_network["controller"] = net_data.get("controller", {})
                nodes = net_data.get("controller", {}).get("nodes", [])
                total_nodes = len(nodes)
                capped_nodes = nodes[:ZWAVE_NODE_LIMIT]
                node_summaries = [
                    {
                        "node_id": n.get("node_id"),
                        "status": n.get("status"),
                        "is_routing": n.get("is_routing"),
                        "is_secure": n.get("is_secure"),
                        "zwave_plus_version": n.get("zwave_plus_version"),
                        "is_controller_node": n.get("is_controller_node"),
                    }
                    for n in capped_nodes
                ]
                zwave_network["nodes"] = node_summaries
                zwave_network["count"] = len(node_summaries)
                zwave_network["total_count"] = total_nodes
                if total_nodes > ZWAVE_NODE_LIMIT:
                    zwave_network["truncated"] = True
                    zwave_network["hint"] = (
                        f"Showing {ZWAVE_NODE_LIMIT} of {total_nodes} nodes. "
                        "Use ha_get_device(integration='zwave_js') for full device list."
                    )
        except Exception as e:
            _reraise_if_fatal(e)
            logger.warning("Failed to fetch Z-Wave network data: %s", e)
            zwave_network["error"] = (
                f"Z-Wave JS integration not available or error: {e}"
            )
        return zwave_network

    @staticmethod
    async def _fetch_thread_network(ws_client: Any) -> dict[str, Any]:
        """Fetch a Thread/OpenThread Border Router (OTBR) summary.

        Calls the ``otbr/info`` WebSocket command (HA's Thread/OTBR
        integration) and returns a lightweight list of per-border-router
        summaries — each with its ``extended_address``, ``channel``,
        ``extended_pan_id`` and ``border_agent_id``. Per-node Thread health is
        not exposed by
        this command, so this section is an integration-presence + radio-network
        view rather than a per-device dump. ``otbr/info`` responds
        ``success=false`` (code ``not_loaded``) when no OTBR is configured;
        that surfaces as an ``error`` sub-dict like the other sections.
        """
        thread_network: dict[str, Any] = {"border_routers": [], "count": 0}
        try:
            info_result = await ws_client.send_command("otbr/info")
            if info_result.get("success"):
                # ``otbr/info`` returns a dict keyed by each border router's
                # extended address; the value carries the per-OTBR fields.
                routers_raw = info_result.get("result") or {}
                border_routers = [
                    {
                        "extended_address": ext_addr,
                        "channel": (info or {}).get("channel"),
                        "extended_pan_id": (info or {}).get("extended_pan_id"),
                        "border_agent_id": (info or {}).get("border_agent_id"),
                    }
                    for ext_addr, info in routers_raw.items()
                ]
                thread_network = {
                    "border_routers": border_routers,
                    "count": len(border_routers),
                }
            else:
                err = info_result.get("error") or {}
                err_msg = (
                    err.get("message") if isinstance(err, dict) else str(err)
                ) or "unknown error"
                thread_network["error"] = (
                    f"Thread/OTBR integration not available: {err_msg}"
                )
        except Exception as e:
            _reraise_if_fatal(e)
            logger.warning("Failed to fetch Thread network data: %s", e)
            thread_network["error"] = (
                f"Thread/OTBR integration not available or error: {e}"
            )
        return thread_network

    @staticmethod
    async def _fetch_matter_network(ws_client: Any) -> dict[str, Any]:
        """Fetch a lightweight Matter integration-presence summary.

        Resolves the Matter config entry via ``config_entries/get`` (filtering
        ``domain == "matter"``) and returns its ``config_entry_id``, ``state``
        and ``title``. Matter exposes health *per node* via the
        ``matter/node_diagnostics`` command, so this section is deliberately an
        integration presence/state summary, not a per-device dump. Returns
        ``{"error": "Matter integration not found"}`` when no Matter entry
        exists.
        """
        matter_network: dict[str, Any] = {}
        try:
            # ``config_entries/get`` (underscore) is the canonical command —
            # the slash form is rejected as "Unknown command" (same caveat the
            # zwave helper documents above).
            entries_result = await ws_client.send_command("config_entries/get")
            matter_entry = None
            if entries_result.get("success"):
                for entry in entries_result.get("result", []):
                    if entry.get("domain") == "matter":
                        matter_entry = entry
                        break

            if matter_entry is None:
                matter_network["error"] = "Matter integration not found"
                return matter_network

            matter_network = {
                "config_entry_id": matter_entry.get("entry_id"),
                "state": matter_entry.get("state"),
                "title": matter_entry.get("title"),
            }
        except Exception as e:
            _reraise_if_fatal(e)
            logger.warning("Failed to fetch Matter network data: %s", e)
            matter_network["error"] = f"Matter integration not available or error: {e}"
        return matter_network

    @staticmethod
    async def _fetch_themes(ws_client: Any) -> dict[str, Any]:
        """Fetch installed theme names and defaults from Home Assistant.

        Returns theme NAMES plus defaults, not the full per-theme CSS variable
        dicts (installed community themes can carry hundreds of variables; this
        section is a listing/verify surface, not a content dump).
        """
        themes_data: dict[str, Any] = {
            "themes": [],
            "count": 0,
            "default_theme": None,
            "default_dark_theme": None,
        }
        try:
            themes_result = await ws_client.send_command("frontend/get_themes")
            if themes_result.get("success"):
                themes_data = summarize_theme_listing(themes_result.get("result") or {})
            else:
                err = themes_result.get("error") or {}
                err_msg = (
                    err.get("message") if isinstance(err, dict) else str(err)
                ) or "unknown error"
                logger.warning(
                    "frontend/get_themes returned success=false: %s", err_msg
                )
                themes_data["error"] = f"Themes data not available: {err_msg}"
        except Exception as e:
            _reraise_if_fatal(e)
            logger.warning("Failed to fetch themes: %s", e)
            themes_data["error"] = f"Themes data not available: {e}"
        return themes_data

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
        malformed envelope). Fatal exceptions (per ``_reraise_if_fatal``)
        unwind instead of being returned as an error string.
        """
        if isinstance(resp, BaseException):
            # gather(return_exceptions=True) hands back the raw exception; let
            # truly-fatal ones unwind instead of masking them as "unavailable".
            _reraise_if_fatal(resp)
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

    async def _fetch_dead_entities(self) -> dict[str, Any]:
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
            # Index the gather result (rather than tuple-unpack) so mypy can
            # type each element through the return_exceptions=True overload;
            # mirrors smart_search/_entities.py::_fetch_search_entities.
            results = await asyncio.gather(
                self._client.get_states(),
                self._client.send_websocket_message(
                    {"type": "config/entity_registry/list"}
                ),
                self._client.send_websocket_message({"type": "config_entries/get"}),
                return_exceptions=True,
            )
            states = results[0]

            if isinstance(states, BaseException):
                # Truly-fatal errors must propagate, not demote to a section
                # error string (mirrors the ws sections gather pre-pass).
                _reraise_if_fatal(states)
                dead["error"] = f"Could not fetch entity states: {states}"
                return dead
            if not isinstance(states, list):
                dead["error"] = (
                    "Could not fetch entity states: expected list, got "
                    f"{type(states).__name__}"
                )
                return dead
            registry, registry_err = self._ws_result_list(results[1])
            if registry is None:
                # Preserve the underlying cause (envelope error message,
                # exception type, or wrong-shape description) so the client
                # can distinguish auth vs command vs malformed envelope
                # rather than see a fixed "unavailable" substitute.
                dead["error"] = (
                    f"Could not fetch entity registry "
                    f"(config/entity_registry/list: {registry_err})"
                )
                return dead

            # config-entries is the only optional source: without it the
            # definitive orphan tier can't be computed, but stale_restored still
            # can — so degrade rather than fail the whole section.
            entries, entries_err = self._ws_result_list(results[2])
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
            # A ToolError (incl. one re-raised by _reraise_if_fatal) carries the
            # MCP isError contract — let it reach ha_get_system_health's own
            # ``except ToolError: raise`` instead of being demoted to a section
            # error string here (AGENTS.md error-handling guard pattern).
            raise
        except Exception as e:
            _reraise_if_fatal(e)
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

        Returns an embeddable sub-dict (matching the ``_fetch_repairs`` /
        ``_fetch_zha_network`` convention): baseline keys always present, with
        an ``error`` field on backend failure. Never raises, so a config-check
        failure surfaces as ``result["config_check"]["error"]`` without sinking
        the rest of ha_get_system_health. Instance method (not @staticmethod)
        because it calls the REST client (``self._client``), like the
        diagnostics path.
        """
        config_check: dict[str, Any] = {
            "result": "unknown",
            "is_valid": False,
            "errors": [],
        }
        try:
            config_result = await self._client.check_config()
            # The API returns {"result": "valid"} or
            # {"result": "invalid", "errors": [...]}.
            is_valid = config_result.get("result") == "valid"
            errors = config_result.get("errors") or []  # Handle None case
            config_check = {
                "result": "valid" if is_valid else "invalid",
                "is_valid": is_valid,
                "errors": errors,
            }
        except Exception as e:
            _reraise_if_fatal(e)
            logger.warning("Failed to check config: %s", e)
            config_check["error"] = f"Config check not available: {e}"
        return config_check


def register_system_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant system management tools."""
    register_tool_methods(mcp, SystemTools(client))
