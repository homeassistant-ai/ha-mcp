"""Thin async HTTP client to the dashboard screenshot engine.

The engine (balloob's Puppet add-on, or a docker-compose sidecar)
authenticates to Home Assistant with its OWN configured long-lived token, so
this client passes only the dashboard path + render parameters — no HA token
ever flows through ha-mcp or the LLM for screenshots.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from ..errors import ErrorCode, create_error_response
from ..tools.helpers import raise_tool_error
from .provision import TOKEN_HINT, resolve_engine_url

logger = logging.getLogger(__name__)

# Shared Field-description clause for the ``full_page`` screenshot param, reused
# across ha_get_dashboard_screenshot and the get/set screenshot options so the
# wording stays in one place instead of being copy-pasted per tool.
FULL_PAGE_PARAM_DESC = (
    "capture the whole scrollable dashboard instead of just the viewport "
    "(use when content runs below the fold)"
)

DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 800
# Cold-start render-settle. The engine waits this long after the dashboard
# reports loaded so canvas/chart cards (history-graph, ApexCharts,
# mini-graph) have time to paint. Heavy custom chart cards may need more —
# exposed as a per-request parameter on the tools so the caller can raise it.
DEFAULT_WAIT_MS = 2500
_REQUEST_TIMEOUT_S = 60.0

# full_page render height. The Puppet engine clips to the requested viewport
# height (it has no native full-page mode), so capturing a whole scrollable
# dashboard means asking for a tall viewport. This is the engine's max useful
# height; dashboards taller than this still clip, and shorter ones get trailing
# whitespace. Once the engine gains a native fullPage param (upstream), prefer
# that instead — it auto-sizes to content with no cap and no whitespace.
FULL_PAGE_HEIGHT = 4096

# Characters/sequences that would let an LLM-supplied path escape the
# dashboard route and reshape the engine request (scheme, authority,
# query/fragment, traversal, backslash). The engine renders whatever path it
# is handed with a full-HA credential, so the path must stay a plain
# frontend route segment.
_FORBIDDEN_PATH_BITS = ("://", "//", "..", "@", "\\", "?", "#")


def _validate_dashboard_path(dashboard_path: str) -> str:
    """Return a safe, stripped dashboard path or raise ToolError.

    Rejects anything that isn't a plain Lovelace frontend route — no scheme,
    authority, query, fragment, traversal, or backslash — so the caller can't
    point the credentialed engine at arbitrary URLs or admin routes.
    """
    raw = (dashboard_path or "").strip()
    if not raw:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                "dashboard_path is required (e.g. 'lovelace/0' or 'my-dashboard').",
                suggestions=["Pass a Lovelace path such as 'lovelace/0'"],
            )
        )
    if any(bit in raw for bit in _FORBIDDEN_PATH_BITS) or any(
        ord(c) < 0x20 for c in raw
    ):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid dashboard_path {dashboard_path!r}: pass a plain "
                "Lovelace path like 'lovelace/0' or 'my-dashboard/kitchen'.",
                details="Path may not contain URLs, query strings, fragments, "
                "'..' segments, backslashes, or control characters.",
                context={"dashboard_path": dashboard_path},
            )
        )
    # Percent-encode each surviving segment so only the explicit query params
    # (set in capture_dashboard_png) ever reach the engine — defense in depth
    # on top of the rejection above.
    segments = [seg for seg in raw.strip("/").split("/") if seg not in ("", ".")]
    if not segments:
        # e.g. "/" or "/." — the engine would serve its config/UI HTML for the
        # root path, not a dashboard PNG, which would be a confusing silent
        # failure. Require a concrete view path.
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid dashboard_path {dashboard_path!r}: cannot be empty or "
                "the root path.",
                details="Pass a specific Lovelace view, e.g. 'lovelace/0'.",
                context={"dashboard_path": dashboard_path},
            )
        )
    return "/".join(quote(seg, safe="") for seg in segments)


async def capture_dashboard_png(
    dashboard_path: str,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    zoom: float = 1.0,
    wait_ms: int = DEFAULT_WAIT_MS,
    full_page: bool = False,
) -> bytes:
    """Render ``dashboard_path`` to PNG bytes via the screenshot engine.

    With ``full_page=True`` the whole scrollable dashboard is captured rather
    than just the viewport: the engine clips to the requested height, so we ask
    for a tall viewport (``FULL_PAGE_HEIGHT``). ``height`` is ignored in that
    case. See ``FULL_PAGE_HEIGHT`` for the interim caveats (cap + whitespace).

    Raises :class:`ToolError` if the engine is unreachable or returns an error.
    """
    path = _validate_dashboard_path(dashboard_path)
    engine = await resolve_engine_url()
    effective_height = FULL_PAGE_HEIGHT if full_page else int(height)
    params: dict[str, str] = {
        "viewport": f"{int(width)}x{effective_height}",
        "zoom": str(zoom),
        "wait": str(int(wait_ms)),
        "format": "png",
    }
    url = f"{engine}/{path}"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(_REQUEST_TIMEOUT_S)) as h:
            resp = await h.get(url, params=params)
    except httpx.HTTPError as e:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONNECTION_FAILED,
                f"Could not reach the dashboard screenshot engine at {engine}.",
                details=str(e),
                context={"engine_url": engine},
                suggestions=[
                    "Ensure the Puppet screenshot add-on (or sidecar) is "
                    "installed and running",
                    # Puppet restarts itself when navigation fails, so a
                    # missing/invalid token shows up as a dropped connection
                    # rather than an HTTP error.
                    f"If it is running, its access token is likely missing or "
                    f"invalid — {TOKEN_HINT}",
                    "Check HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL on "
                    "Docker/Container deployments",
                ],
            )
        )

    if resp.status_code >= 400:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Screenshot engine returned HTTP {resp.status_code} for "
                f"dashboard '{path}'.",
                details=resp.text[:300],
                context={"status_code": resp.status_code, "path": path},
                suggestions=[
                    "Verify the dashboard path exists",
                    f"If the engine landed on the login page, its access token "
                    f"is missing/invalid — {TOKEN_HINT}",
                    "Increase wait_ms for heavy chart cards",
                ],
            )
        )
    if not resp.content:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Screenshot engine returned an empty image for '{path}'.",
                details="The dashboard path may be invalid, or the engine's "
                "access token may be missing/expired.",
                context={"path": path},
            )
        )
    return resp.content
