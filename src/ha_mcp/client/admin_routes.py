"""REST routes Home Assistant refuses to non-admin users with a counted 401.

Each answers a non-admin with a raised 401, which ``http.ban`` counts as a
failed login, so a burst of them IP-bans the ha-mcp host (#2546). Verified
against home-assistant/core ``api/__init__.py``,
``config/{view,core,config_entries}.py`` and ``diagnostics/__init__.py``.
Not listed: routes that check entity permissions instead (e.g.
``GET /api/states/<id>``), the hassio proxy (its 401 is returned, not raised,
so it is not counted), and admin-only services, which ``call_service`` sends
over WebSocket for a non-admin token.
"""

import re

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
