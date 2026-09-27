"""REST routes Home Assistant serves to administrators only.

A non-admin user's request to one of these is answered 401, and Home
Assistant's ``http.ban`` counts every 401 as a failed login, so a burst of
them IP-bans the ha-mcp host (#2546). Verified against home-assistant/core
``api/__init__.py``, ``config/{view,core,config_entries}.py``,
``diagnostics/__init__.py`` and ``hassio/http.py``. Routes that check entity
permissions instead (e.g. ``GET /api/states/<id>``) are not listed.
"""

import re

_ANY_METHOD = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

_ADMIN_ONLY_ROUTES: tuple[tuple[frozenset[str], re.Pattern[str]], ...] = tuple(
    (frozenset(methods), re.compile(pattern))
    for methods, pattern in (
        ({"POST", "DELETE"}, r"states/[^/]+"),
        ({"POST"}, r"events/[^/]+"),
        ({"POST"}, r"template"),
        ({"GET"}, r"error_log"),
        (_ANY_METHOD, r"hassio/.+"),
        (_ANY_METHOD, r"config/(automation|script|scene)/config/[^/]+"),
        ({"POST"}, r"config/core/check_config"),
        ({"GET"}, r"diagnostics/[^/]+/[^/]+(/[^/]+/[^/]+)?"),
        ({"DELETE"}, r"config/config_entries/entry/[^/]+"),
        ({"POST"}, r"config/config_entries/entry/[^/]+/reload"),
        (
            {"GET", "POST"},
            r"config/config_entries/(options/|subentries/)?flow(/[^/]+)?",
        ),
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
