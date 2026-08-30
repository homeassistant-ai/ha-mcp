"""Supervisor-backed log sources served by ``ha_get_logs``.

App (add-on) container logs and the eight Supervisor-managed system services.
Separate from ``log_sources`` because these two reach Home Assistant through
Supervisor rather than Core, and carry the whole token/role error vocabulary
that follows from it. Mixed into ``LogTools`` (``tools_logs``).

Split out of ``tools_utility`` under .gemini/styleguide.md § Tool Consolidation and Module Size.
"""

from typing import Any, Literal, NoReturn

from fastmcp.exceptions import ToolError

from .._version import is_running_in_addon
from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, raise_tool_error
from .log_common import (
    DEFAULT_LOG_LIMIT,
    SUPERVISOR_SEARCH_WINDOW_LINES,
    SYSTEM_SERVICE_SLUGS,
    _addon_auth_error_suggestions,
    _coerce_limit,
)


class SupervisorLogSourcesMixin:
    """App (add-on) and system-service logs, fetched through Supervisor.

    ``_client`` is supplied by the host class (``LogTools``); this mixin adds
    no state of its own.
    """

    _client: Any

    async def _get_supervisor_log(
        self,
        slug: str,
        limit: int | None = None,
        search: str | None = None,
        order: Literal["newest", "oldest"] = "newest",
    ) -> dict[str, Any]:
        """Fetch app (add-on) container logs.

        Delegates to ``HomeAssistantClient.get_addon_logs`` which branches on
        ``is_running_in_addon()``: inside the app container hits Supervisor
        directly at ``http://supervisor/addons/<slug>/logs`` (the HA-Core
        proxy at ``/api/hassio/addons/<slug>/logs`` rejects the Supervisor
        token there — see #1116); on non-addon installs falls back to the
        HA-Core proxy. Both paths return ``text/plain``.
        """
        effective_limit = _coerce_limit(
            limit, default=DEFAULT_LOG_LIMIT, suggestion_example="100"
        )

        # Request a journald window sized to the caller's limit —
        # Supervisor's /logs endpoints default to their last-100-lines
        # window, which silently capped any larger limit before ?lines=
        # was plumbed through (found via #1721's e2e).
        fetch_lines = (
            max(effective_limit, SUPERVISOR_SEARCH_WINDOW_LINES)
            if search
            else effective_limit
        )

        try:
            log_text = await self._client.get_addon_logs(slug, lines=fetch_lines)

            lines = log_text.splitlines() if log_text else []

            filters_applied: dict[str, str] = {}

            if search:
                search_lower = search.lower()
                lines = [ln for ln in lines if search_lower in ln.lower()]
                filters_applied["search"] = search

            total_lines = len(lines)
            # Always take the most-recent window (the tail); 'order' controls
            # only the display direction of that window.
            lines = lines[-effective_limit:]
            if order == "newest":
                lines = list(reversed(lines))

            data: dict[str, Any] = {
                "success": True,
                "source": "supervisor",
                "slug": slug,
                "log": "\n".join(lines),
                "total_lines": total_lines,
                "returned_lines": len(lines),
                "limit": effective_limit,
                "order": order,
            }
            if filters_applied:
                data["filters_applied"] = filters_applied

            return data

        except ToolError:
            raise
        except HomeAssistantAuthError as e:
            # Listed before HomeAssistantAPIError because AuthError is a sibling,
            # not a subclass — without this explicit clause the 401 from
            # _supervisor_logs_get / _raw_request propagates raw to FastMCP and
            # surfaces without a structured `code` field.
            #
            # Suggestions branch on is_running_in_addon(): addon installs go
            # direct to Supervisor (the failure mode is a missing/rotated
            # SUPERVISOR_TOKEN), non-addon installs hit HA Core's hassio
            # proxy with the user's LLA (the failure mode is a non-admin or
            # expired LLA — SUPERVISOR_TOKEN doesn't even apply).
            exception_to_structured_error(
                e,
                context={"source": "supervisor", "slug": slug},
                suggestions=_addon_auth_error_suggestions(),
            )
        except HomeAssistantAPIError as e:
            status = getattr(e, "status_code", None)
            if status == 400:
                # Supervisor-side rejection — not caller validation. The default
                # `exception_to_structured_error` path would map 400 →
                # VALIDATION_INVALID_PARAMETER, which reads as "caller passed
                # bad input"; a downstream proxy rejection is better modelled
                # as SERVICE_CALL_FAILED.
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        str(e),
                        context={"source": "supervisor", "slug": slug},
                        suggestions=[
                            f"Supervisor rejected the request for '{slug}' — "
                            "verify slug format or that the app (add-on) is installed "
                            "and running",
                            "Use ha_get_app() to list installed app slugs",
                            "Ensure Supervisor is available (HA OS or Supervised install)",
                        ],
                    )
                )
            if status == 404:
                first_suggestion = f"App (add-on) '{slug}' not found or not installed"
            else:
                first_suggestion = f"Verify app (add-on) slug '{slug}' is correct"
            exception_to_structured_error(
                e,
                context={"source": "supervisor", "slug": slug},
                suggestions=[
                    first_suggestion,
                    "Use ha_get_app() to list installed app slugs",
                    "Ensure Supervisor is available (HA OS or Supervised install)",
                ],
            )
        except (
            HomeAssistantConnectionError,
            TimeoutError,
            OSError,
        ) as e:
            exception_to_structured_error(
                e,
                context={"source": "supervisor", "slug": slug},
                suggestions=[
                    "Check Home Assistant connection",
                    f"Verify app slug '{slug}' is correct",
                    "Use ha_get_app() to list installed app slugs",
                    "Ensure Supervisor is available (HA OS or Supervised install)",
                ],
            )
            raise  # unreachable: exception_to_structured_error always raises
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    def _handle_system_service_api_error(
        self, e: HomeAssistantAPIError, service: str
    ) -> NoReturn:
        """Raise a structured error for a Supervisor per-service-logs failure.

        Branches on HTTP status: 403 (role/permission, addon vs. non-addon
        remediation differs), 404 (service not exposed on this HA OS
        version), else falls through to a generic Supervisor-error message.
        """
        status = getattr(e, "status_code", None)
        if status == 403:
            # In-addon: Supervisor returns 403 when the addon's hassio_role
            # is below 'manager'. Non-addon: HA Core's hassio proxy returns
            # 403 when the LLA's user lacks admin — completely different
            # remediation. Branch on the gate accordingly.
            if is_running_in_addon():
                suggestions = [
                    "Addon's hassio_role must be 'manager' or higher to "
                    + "read /<service>/logs",
                    "Verify the addon was reinstalled after the role bump "
                    + "took effect",
                ]
            else:
                suggestions = [
                    "The Long-Lived Access Token must belong to a user "
                    + "with admin privileges",
                    "Generate a new LLAT under an admin account and set "
                    + "HOMEASSISTANT_TOKEN to it",
                ]
            exception_to_structured_error(
                e,
                context={"source": "system_service", "slug": service},
                suggestions=suggestions,
            )
        if status == 404:
            exception_to_structured_error(
                e,
                context={"source": "system_service", "slug": service},
                suggestions=[
                    f"Service '{service}' not found at "
                    f"http://supervisor/{service}/logs — Supervisor may "
                    "not expose it on this HA OS version",
                    f"Allowed services: {', '.join(sorted(SYSTEM_SERVICE_SLUGS))}",
                ],
            )
        exception_to_structured_error(
            e,
            context={"source": "system_service", "slug": service},
            suggestions=[
                f"Supervisor returned an error for /{service}/logs",
                "Ensure Supervisor is available (HA OS or Supervised install)",
            ],
        )

    async def _get_system_service_log(
        self,
        service: str,
        limit: int | None = None,
        search: str | None = None,
        order: Literal["newest", "oldest"] = "newest",
    ) -> dict[str, Any]:
        """Fetch HA system-service logs from Supervisor's per-service endpoint.

        ``service`` ∈ ``SYSTEM_SERVICE_SLUGS`` (the eight Supervisor-managed
        services: supervisor, host, core, dns, audio, cli, multicast, observer).
        Caller (``ha_get_logs(source='system_service')``) validates against
        ``SYSTEM_SERVICE_SLUGS`` before dispatch. Routed through
        ``HomeAssistantClient._get_system_service_logs`` which gates on
        ``is_running_in_addon()``: addon installs hit Supervisor directly at
        ``http://supervisor/<service>/logs`` (requires ``hassio_role: manager``
        in the addon manifest), non-addon installs fall back to the HA Core
        proxy at ``/api/hassio/<service>/logs`` (requires an admin LLA).
        """
        effective_limit = _coerce_limit(
            limit, default=DEFAULT_LOG_LIMIT, suggestion_example="100"
        )

        fetch_lines = (
            max(effective_limit, SUPERVISOR_SEARCH_WINDOW_LINES)
            if search
            else effective_limit
        )

        try:
            log_text = await self._client._get_system_service_logs(
                service, lines=fetch_lines
            )

            lines = log_text.splitlines() if log_text else []

            filters_applied: dict[str, str] = {}
            if search:
                search_lower = search.lower()
                lines = [ln for ln in lines if search_lower in ln.lower()]
                filters_applied["search"] = search

            total_lines = len(lines)
            # Always take the most-recent window (the tail); 'order' controls
            # only the display direction of that window.
            lines = lines[-effective_limit:]
            if order == "newest":
                lines = list(reversed(lines))

            data: dict[str, Any] = {
                "success": True,
                "source": "system_service",
                "slug": service,
                "log": "\n".join(lines),
                "total_lines": total_lines,
                "returned_lines": len(lines),
                "limit": effective_limit,
                "order": order,
            }
            if filters_applied:
                data["filters_applied"] = filters_applied

            return data

        except ToolError:
            raise
        except HomeAssistantAuthError as e:
            # Listed before HomeAssistantAPIError because AuthError is a sibling,
            # not a subclass — without this explicit clause the 401 from
            # _supervisor_logs_get / _raw_request propagates raw to FastMCP and
            # surfaces without a structured `code` field.
            #
            # Suggestions branch on is_running_in_addon() (see _get_supervisor_log
            # for the rationale): SUPERVISOR_TOKEN suggestions only make sense
            # inside the addon container; non-addon installs need admin-LLA hints.
            exception_to_structured_error(
                e,
                context={"source": "system_service", "slug": service},
                suggestions=_addon_auth_error_suggestions(),
            )
        except HomeAssistantAPIError as e:
            self._handle_system_service_api_error(e, service)
        except (
            HomeAssistantConnectionError,
            TimeoutError,
            OSError,
        ) as e:
            exception_to_structured_error(
                e,
                context={"source": "system_service", "slug": service},
                suggestions=[
                    "Check Home Assistant connection",
                    "Ensure Supervisor is available (HA OS or Supervised install)",
                ],
            )
            raise  # unreachable: exception_to_structured_error always raises
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable
