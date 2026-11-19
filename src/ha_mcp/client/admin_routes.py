"""REST routes Home Assistant refuses to non-admin users with a counted 401.

Each answers a non-admin with a raised 401, which ``http.ban`` counts as a
failed login, so a burst of them IP-bans the ha-mcp host (#2546). Verified
against home-assistant/core ``api/__init__.py``,
``config/{view,core,config_entries}.py`` and ``diagnostics/__init__.py``.
Not listed: routes that check entity permissions instead (e.g.
``GET /api/states/<id>``), the hassio proxy (its 401 is returned, not raised,
so it is not counted), and admin-only services, which ``call_service`` sends
over WebSocket for a non-admin token.

It also holds the request hook that keeps this server's administrator token
off the ha_mcp_tools component's own HTTP views.
"""

import posixpath
import re
import urllib.parse

import httpx

from .exceptions import HomeAssistantAPIError

_ANY_METHOD = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

_ADMIN_ONLY_ROUTES: tuple[tuple[frozenset[str], re.Pattern[str]], ...] = tuple(
    (frozenset(methods), re.compile(pattern))
    for methods, pattern in (
        ({"POST", "DELETE"}, r"states/[^/]+"),
        ({"POST"}, r"events/[^/]+"),
        ({"GET"}, r"stream"),
        ({"POST"}, r"template"),
        ({"GET"}, r"error_log"),
        (_ANY_METHOD, r"config/(automation|script|scene)/config/[^/]+"),
        ({"POST"}, r"config/core/check_config"),
        ({"GET"}, r"diagnostics/[^/]+/[^/]+(/[^/]+/[^/]+)?"),
        ({"DELETE"}, r"config/config_entries/entry/[^/]+"),
        ({"POST"}, r"config/config_entries/entry/[^/]+/reload"),
        ({"POST"}, r"config/config_entries/(options/|subentries/)?flow"),
        ({"GET", "POST"}, r"config/config_entries/(options/|subentries/)?flow/[^/]+"),
    )
)


def is_admin_only_route(method: str, endpoint: str) -> bool:
    """True when ``endpoint`` (relative to ``/api``) is admin-only for ``method``."""
    path = endpoint.split("?", 1)[0].strip("/")
    method = method.upper()
    return any(
        method in methods and pattern.fullmatch(path)
        for methods, pattern in _ADMIN_ONLY_ROUTES
    )


def normalized_path(path: str) -> str:
    """Fold percent-escapes, ``//``, ``.`` and ``..`` so an equivalent spelling
    cannot slip past (Home Assistant decodes ``%62ackups`` to ``backups`` before
    routing, and httpx sends neither the query nor a ``#`` fragment as part of
    the path).

    Leading slashes are stripped first: POSIX normpath keeps exactly two.
    """
    decoded = urllib.parse.unquote(re.split("[?#]", path, maxsplit=1)[0])
    stripped = decoded.strip().lower().lstrip("/")
    return posixpath.normpath("/" + stripped)


async def refuse_component_views(request: httpx.Request) -> None:
    """httpx request hook: never send to the component's ``/api/ha_mcp_tools/``.

    Its settings panel mints a session for this administrator token, and that
    session approves held tool calls. The hook sees the final URL, after httpx
    resolved ``..``, so a tool ID that climbs out of its route is caught too.
    """
    path = normalized_path(request.url.raw_path.decode("ascii"))
    if "/api/ha_mcp_tools/" in f"{path}/":
        raise HomeAssistantAPIError(
            "The ha_mcp_tools component's HTTP views are off limits to this "
            f"server: {request.method} {path} not sent"
        )
