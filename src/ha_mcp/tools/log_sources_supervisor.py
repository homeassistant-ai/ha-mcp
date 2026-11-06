"""Supervisor-backed log sources served by ``ha_get_logs``.

App (add-on) container logs and the six Supervisor-managed system services.
Separate from ``log_sources`` because these two reach Home Assistant through
Supervisor rather than Core, and carry the whole token/role error vocabulary
that follows from it. Mixed into ``LogTools`` (``tools_logs``).

Split out of ``tools_utility`` under `.gemini/styleguide.md` § Tool
Consolidation and Module Size.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from .._version import is_running_in_addon
from ..client.journal_window import lines_since, read_logs_since
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

    async def _journal_window(
        self, path: str, hours_back: int, now: datetime | None
    ) -> tuple[list[str], dict[str, Any]]:
        """The last ``hours_back`` hours of entries, and fields describing the read.

        A read that stopped before the cutoff says so, so that a short window
        is not mistaken for a quiet one.
        """
        cutoff = (now or datetime.now(UTC)) - timedelta(hours=hours_back)
        read = await read_logs_since(self._client, path, cutoff)
        oldest = read.oldest.isoformat(timespec="seconds") if read.oldest else None
        fields: dict[str, Any] = {
            "hours_back": hours_back,
            "window_start": cutoff.isoformat(timespec="seconds"),
            "window_complete": read.reached_cutoff,
            "line_format": "verbose",
        }
        if read.journal_start:
            fields["warnings"] = [
                f"The log begins at {oldest}, inside the {hours_back}-hour "
                "window; nothing older is kept."
            ]
        elif not read.reached_cutoff:
            fields["warnings"] = [
                f"Read {read.pages} blocks of the journal back to {oldest} without "
                f"reaching the {hours_back}-hour cutoff, so older entries in the "
                "window were not read. Use a smaller hours_back."
            ]
        return lines_since(read.lines, cutoff), fields

    async def _log_lines(
        self,
        path: str,
        hours_back: int | None,
        now: datetime | None,
        read_tail: Callable[[], Awaitable[str]],
    ) -> tuple[list[str], dict[str, Any]]:
        """The newest lines from ``read_tail``, or the ``hours_back`` window."""
        if hours_back is not None:
            return await self._journal_window(path, hours_back, now)
        log_text = await read_tail()
        return (log_text.splitlines() if log_text else []), {}

    async def _get_supervisor_log(
        self,
        slug: str,
        limit: int | None = None,
        search: str | None = None,
        order: Literal["newest", "oldest"] = "newest",
        hours_back: int | None = None,
        now: datetime | None = None,
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
            lines, window = await self._log_lines(
                f"addons/{slug}",
                hours_back,
                now,
                lambda: self._client.get_addon_logs(slug, lines=fetch_lines),
            )

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
            data.update(window)

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

    async def _handle_system_service_api_error(
        self, e: HomeAssistantAPIError, service: str
    ) -> NoReturn:
        """Raise a structured error for a Supervisor per-service-logs failure.

        Branches on HTTP status: 403 (role/permission, addon vs. non-addon
        remediation differs), 404 (service not exposed on this HA OS version;
        the non-addon branch keeps a failed supervision probe distinct from a
        confirmed unsupervised install), else falls through to a generic
        Supervisor-error message.
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
            allowed = f"Allowed services: {', '.join(sorted(SYSTEM_SERVICE_SLUGS))}"
            if is_running_in_addon():
                # The request went straight to Supervisor, so a 404 is
                # Supervisor's own answer: the service is not exposed.
                suggestions = [
                    f"Service '{service}' not found at "
                    f"http://supervisor/{service}/logs — Supervisor may "
                    "not expose it on this HA OS version",
                    allowed,
                ]
            else:
                await self._client._is_supervised_install()
                if self._client._supervised_detected is True:
                    suggestions = [
                        f"Service '{service}' not found at "
                        f"http://supervisor/{service}/logs — Supervisor may "
                        "not expose it on this HA OS version",
                        allowed,
                    ]
                elif self._client._supervised_detected is False:
                    suggestions = [
                        "These log sources need a Supervisor-managed install "
                        "(Home Assistant OS or Supervised); this instance is "
                        "not managed by Supervisor",
                        allowed,
                    ]
                else:
                    # The probe could not confirm either way (transport
                    # failure leaves the flag None); do not assert the
                    # instance is unmanaged.
                    suggestions = [
                        "Supervisor management could not be confirmed (the "
                        "probe failed): the instance may be unsupervised or "
                        "this HA OS version may not expose the service",
                        allowed,
                    ]
            exception_to_structured_error(
                e,
                context={"source": "system_service", "slug": service},
                suggestions=suggestions,
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
        hours_back: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Fetch HA system-service logs from Supervisor's per-service endpoint.

        ``service`` ∈ ``SYSTEM_SERVICE_SLUGS`` (the six Supervisor-managed
        services: supervisor, host, core, dns, audio, multicast).
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
            lines, window = await self._log_lines(
                service,
                hours_back,
                now,
                lambda: self._client._get_system_service_logs(
                    service, lines=fetch_lines
                ),
            )

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
            data.update(window)

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
            await self._handle_system_service_api_error(e, service)
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
