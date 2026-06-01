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
)
from .util_helpers import (
    fetch_integration_diagnostics,
    filter_active_repairs,
    parse_diagnostics_fields,
)

logger = logging.getLogger(__name__)

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
        annotations={"destructiveHint": True, "title": "Restart Home Assistant"},
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
                pattern in error_msg.lower() for pattern in ("connect", "closed", "504")
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

    @tool(
        name="ha_reload_core",
        tags={"System"},
        annotations={"destructiveHint": True, "title": "Reload Core Components"},
    )
    @log_tool_usage
    async def ha_reload_core(
        self,
        target: str = "all",
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
                # Reload all reloadable components
                results = []
                errors = []

                for reload_target, service_info in RELOAD_TARGETS.items():
                    if service_info is None:  # Skip "all" itself
                        continue

                    domain, service = service_info
                    try:
                        await self._client.call_service(domain, service, {})
                        results.append(reload_target)
                    except Exception as e:
                        # Some services might not be available in all installations
                        error_msg = str(e)
                        if "not found" not in error_msg.lower():
                            errors.append(f"{reload_target}: {error_msg}")

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

    @tool(
        name="ha_get_system_health",
        tags={"System", "Zigbee", "Z-Wave", "Integrations"},
        annotations={
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
        diagnostics_fields: list[str] | str | None = None,
        diagnostics_truncate_at_bytes: Annotated[int, Field(ge=1)] | None = None,
        diagnostics_data_path: str | None = None,
        diagnostics_data_offset: Annotated[int, Field(ge=0)] | None = 0,
        diagnostics_data_limit: Annotated[int, Field(ge=1)] | None = None,
    ) -> dict[str, Any]:
        """
        Get Home Assistant system health, including Zigbee (ZHA), Z-Wave JS, and per-integration diagnostics dumps.

        Returns health check results from integrations, system resources, and connectivity.
        Available information varies by installation type and loaded integrations.

        **Parameters:**
        - include: Optional comma-separated list of additional data to include.
          - "repairs": Repair items from Settings > System > Repairs (active only by default; pass `include_dismissed_repairs=True` for all)
          - "zha_network": ZHA Zigbee devices with radio signal summary (name, LQI, RSSI)
          - "zha_network_full": ZHA Zigbee devices with all device details (can be large on 100+ device networks; prefer "zha_network" for summary)
          - "zwave_network": Z-Wave JS network status and node summary (status, security, routing)
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
        ws_backed = {"repairs", "zha_network", "zha_network_full", "zwave_network"}

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
                # section (config_check / diagnostics) that can still be served
                # without the WebSocket. config_check is the pure-REST
                # replacement for the removed ha_check_config tool, so it must
                # not depend on the health WebSocket (the system_health/info
                # command carries its own 10s timeout and can hang/be absent on
                # some installs). If the caller asked for nothing (the health
                # baseline itself) or only WS-backed sections, the baseline WAS
                # the deliverable: re-raise so the failure surfaces as
                # isError=true, exactly as before this change.
                if not (includes & {"config_check", "diagnostics"}):
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
                "diagnostics",
                "config_check",
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
                unavailable = sorted(includes & ws_backed)
                if unavailable:
                    result.setdefault("warnings", []).append(
                        "These sections require the system_health WebSocket, "
                        f"which is unavailable: {', '.join(unavailable)}"
                    )
                want_repairs = want_zha = want_zwave = False

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
                for section_result in gathered:
                    if isinstance(section_result, asyncio.CancelledError):
                        raise section_result
                    if isinstance(section_result, ToolError):
                        raise section_result
                    if isinstance(section_result, BaseException) and not isinstance(
                        section_result, Exception
                    ):
                        # ``KeyboardInterrupt`` / ``SystemExit`` — never demote
                        # these to a section-level error string.
                        raise section_result
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
            # Get all zwave_js config entries to find entry_id
            entries_result = await ws_client.send_command("config/entries/get")
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
            logger.warning("Failed to fetch Z-Wave network data: %s", e)
            zwave_network["error"] = (
                f"Z-Wave JS integration not available or error: {e}"
            )
        return zwave_network

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
            logger.warning("Failed to check config: %s", e)
            config_check["error"] = f"Config check not available: {e}"
        return config_check


def register_system_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant system management tools."""
    register_tool_methods(mcp, SystemTools(client))
