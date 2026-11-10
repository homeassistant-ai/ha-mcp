"""Supervisor-backed log sources served by ``ha_get_logs``.

App (add-on) container logs and the six Supervisor-managed system services.
Separate from ``log_sources`` because these two reach Home Assistant through
Supervisor rather than Core, and carry the whole token/role error vocabulary
that follows from it. Mixed into ``LogTools`` (``tools_logs``).

Split out of ``tools_utility`` under `.gemini/styleguide.md` § Tool
Consolidation and Module Size.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from .._version import is_running_in_addon
from ..client.journal_window import read_logs_page, read_logs_window, window_start
from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, raise_tool_error
from .log_common import (
    DEFAULT_LOG_LIMIT,
    MAX_LIMIT,
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

    @staticmethod
    def _window_call(
        source: str, slug: str, hours_back: int, limit: int, offset: int
    ) -> str:
        """A copyable call reading the window oldest-first from ``offset``."""
        return (
            f"ha_get_logs(source={source!r}, slug={slug!r}, "
            f"hours_back={hours_back}, order='oldest', limit={limit}, "
            f"offset={offset})"
        )

    async def _journal_page(
        self,
        source: str,
        slug: str,
        path: str,
        hours_back: int,
        limit: int,
        offset: int,
        now: datetime | None,
    ) -> dict[str, Any]:
        """``limit`` entries of the window from its ``offset``-th, served by the journal."""
        read_at = now or datetime.now(UTC)
        since = window_start(read_at, hours_back)
        page = await read_logs_page(self._client, path, since, offset, limit)
        data: dict[str, Any] = {
            "success": True,
            "source": source,
            "slug": slug,
            "log": "\n".join(page.lines),
            "total_lines": len(page.lines),
            "returned_lines": len(page.lines),
            "limit": limit,
            "order": "oldest",
            "offset": offset,
            "hours_back": hours_back,
            "window_start": since.isoformat(timespec="seconds"),
            "read_at": read_at.isoformat(timespec="seconds"),
            "window_complete": not page.has_more,
            "has_more": page.has_more,
        }
        if page.has_more:
            data["next_offset"] = offset + limit
            data["pagination_hint"] = (
                f"Showing entries {offset + 1}-{offset + limit} of the window. "
                "To continue, use: "
                + self._window_call(source, slug, hours_back, limit, offset + limit)
            )
        return data

    async def _journal_window(
        self, path: str, hours_back: int, now: datetime | None
    ) -> tuple[list[str], dict[str, Any], int]:
        """The window's entries up to the cap, fields describing the read, and the cap.

        The journal selects the window (``realtime=`` Range) from its start
        to the journal's end; a window too large to read whole says so, so
        that a partial window is not mistaken for a whole one. ``read_at`` is
        when the read started: entries logged while it ran are included.
        """
        read_at = now or datetime.now(UTC)
        since = window_start(read_at, hours_back)
        read = await read_logs_window(self._client, path, since)
        fields: dict[str, Any] = {
            "hours_back": hours_back,
            "window_start": since.isoformat(timespec="seconds"),
            "read_at": read_at.isoformat(timespec="seconds"),
            "window_complete": read.complete,
        }
        if not read.complete:
            fields["warnings"] = [
                f"The {hours_back}-hour window holds more than {read.cap} "
                f"entries; only the newest {read.cap} were read, so older "
                "entries in the window are missing."
            ]
        return read.lines, fields, read.cap

    def _window_paging(
        self,
        source: str,
        slug: str,
        fields: dict[str, Any],
        cap: int,
        unreturned: int,
        limit: int,
        search: str | None,
    ) -> dict[str, Any]:
        """``has_more`` and a hint for a whole-window read, which offset does not page."""
        complete = fields["window_complete"]
        hours_back = fields["hours_back"]
        paging: dict[str, Any] = {"has_more": unreturned > 0 or not complete}
        start = self._window_call(source, slug, hours_back, limit, 0)
        if not complete and search:
            paging["pagination_hint"] = (
                f"Only the newest {cap} entries of the window were searched; "
                "use a smaller hours_back to search the rest."
            )
        elif not complete:
            paging["pagination_hint"] = (
                f"The window holds more than {cap} entries; only the newest "
                f"were read. To read it from its first entry, use: {start}"
            )
        elif unreturned and search:
            paging["pagination_hint"] = (
                f"{unreturned} more matching lines in this window; raise limit "
                f"(max {MAX_LIMIT}) or narrow 'search'."
            )
        elif unreturned:
            paging["pagination_hint"] = (
                f"{unreturned} more lines in this window. To read it from its "
                f"first entry, use: {start}"
            )
        return paging

    async def _read_service_log(
        self,
        source: str,
        slug: str,
        path: str,
        limit: int | None,
        search: str | None,
        order: Literal["newest", "oldest"],
        hours_back: int | None,
        offset: int,
        now: datetime | None,
        read_tail: Callable[[int], Awaitable[str]],
    ) -> dict[str, Any]:
        """The log of an app or system service, by one of three routes.

        Without ``hours_back``: the newest lines, read through ``read_tail``
        (Supervisor's tail, sized to ``limit`` or the search window). With
        ``hours_back``, ``order="oldest"`` and no search: one page of the
        window from its ``offset``-th entry, served by the journal, so any part
        of the window can be reached. Otherwise with ``hours_back``: the whole
        window (up to ``MAX_ENTRIES`` entries), searched, newest lines shown;
        ``offset`` does not apply, and the hint points to the oldest-first
        route for what is not shown.
        """
        effective_limit = _coerce_limit(
            limit, default=DEFAULT_LOG_LIMIT, suggestion_example="100"
        )
        if hours_back is not None and order == "oldest" and not search:
            return await self._journal_page(
                source, slug, path, hours_back, effective_limit, offset, now
            )

        window: dict[str, Any] = {}
        cap = 0
        if hours_back is not None:
            lines, window, cap = await self._journal_window(path, hours_back, now)
        else:
            # Supervisor's /logs endpoints default to their last 100 lines,
            # so the tail is sized to the caller's limit (found via #1721's
            # e2e), or to the search window when filtering.
            log_text = await read_tail(
                max(effective_limit, SUPERVISOR_SEARCH_WINDOW_LINES)
                if search
                else effective_limit
            )
            lines = log_text.splitlines() if log_text else []

        filters_applied: dict[str, str] = {}
        if search:
            search_lower = search.lower()
            lines = [ln for ln in lines if search_lower in ln.lower()]
            filters_applied["search"] = search

        total_lines = len(lines)
        # The newest lines of what was read; 'order' sets their direction.
        lines = lines[-effective_limit:]
        if order == "newest":
            lines = list(reversed(lines))

        data: dict[str, Any] = {
            "success": True,
            "source": source,
            "slug": slug,
            "log": "\n".join(lines),
            "total_lines": total_lines,
            "returned_lines": len(lines),
            "limit": effective_limit,
            "order": order,
        }
        if filters_applied:
            data["filters_applied"] = filters_applied
        if hours_back is not None:
            data.update(window)
            data.update(
                self._window_paging(
                    source,
                    slug,
                    window,
                    cap,
                    total_lines - len(lines),
                    effective_limit,
                    search,
                )
            )
        return data

    async def _get_supervisor_log(
        self,
        slug: str,
        limit: int | None = None,
        search: str | None = None,
        order: Literal["newest", "oldest"] = "newest",
        hours_back: int | None = None,
        offset: int = 0,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Fetch app (add-on) container logs; the routes are ``_read_service_log``'s.

        The tail goes through ``HomeAssistantClient.get_addon_logs``, and the
        ``hours_back`` window through ``journal_window``; both branch on
        ``is_running_in_addon()``: inside the app container they hit
        Supervisor directly at ``http://supervisor/addons/<slug>/logs`` (the
        HA-Core proxy at ``/api/hassio/addons/<slug>/logs`` rejects the
        Supervisor token there — see #1116); on non-addon installs they use
        the HA-Core proxy. Both return ``text/plain``.
        """
        try:
            return await self._read_service_log(
                "supervisor",
                slug,
                f"addons/{slug}",
                limit,
                search,
                order,
                hours_back,
                offset,
                now,
                lambda lines: self._client.get_addon_logs(slug, lines=lines),
            )

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
        offset: int = 0,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Fetch HA system-service logs; the routes are ``_read_service_log``'s.

        ``service`` ∈ ``SYSTEM_SERVICE_SLUGS`` (the six Supervisor-managed
        services: supervisor, host, core, dns, audio, multicast).
        Caller (``ha_get_logs(source='system_service')``) validates against
        ``SYSTEM_SERVICE_SLUGS`` before dispatch. The tail goes through
        ``HomeAssistantClient._get_system_service_logs`` and the
        ``hours_back`` window through ``journal_window``; both gate on
        ``is_running_in_addon()``: addon installs hit Supervisor directly at
        ``http://supervisor/<service>/logs`` (requires ``hassio_role: manager``
        in the addon manifest), non-addon installs use the HA Core proxy at
        ``/api/hassio/<service>/logs`` (requires an admin LLA).
        """
        try:
            return await self._read_service_log(
                "system_service",
                service,
                service,
                limit,
                search,
                order,
                hours_back,
                offset,
                now,
                lambda lines: self._client._get_system_service_logs(
                    service, lines=lines
                ),
            )

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
