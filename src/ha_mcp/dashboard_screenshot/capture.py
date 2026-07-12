"""Thin async HTTP client to the dashboard screenshot engine.

The engine (balloob's Puppet add-on, or a docker-compose sidecar)
authenticates to Home Assistant with its OWN configured long-lived token, so
this client passes only the dashboard path + render parameters — no HA token
ever flows through ha-mcp or the LLM for screenshots.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Literal, NoReturn, cast
from urllib.parse import quote

import httpx

from ..errors import ErrorCode, create_error_response
from ..tools.helpers import raise_tool_error
from .provision import TOKEN_HINT, resolve_engine_url

logger = logging.getLogger(__name__)

ScreenshotFormat = Literal["png", "jpeg", "webp", "bmp"]
ViewportPreset = Literal["mobile", "tablet", "desktop"]
Orientation = Literal["portrait", "landscape"]

VIEWPORT_PRESETS: dict[ViewportPreset, tuple[int, int]] = {
    "mobile": (390, 844),
    "tablet": (768, 1024),
    "desktop": (1280, 800),
}

_MIME_TYPES: dict[ScreenshotFormat, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "bmp": "image/bmp",
}

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
DEFAULT_RENDER_TIMEOUT_SECONDS = 60.0

MIN_VIEWPORT_DIMENSION = 64
MAX_VIEWPORT_DIMENSION = 4096
MIN_ZOOM = 0.1
MAX_ZOOM = 5.0
MAX_WAIT_MS = 30_000
MIN_RENDER_TIMEOUT_SECONDS = 1.0
MAX_RENDER_TIMEOUT_SECONDS = 300.0

# Characters/sequences that would let an LLM-supplied path escape the
# dashboard route and reshape the engine request (scheme, authority,
# query/fragment, traversal, backslash). The engine renders whatever path it
# is handed with a full-HA credential, so the path must stay a plain
# frontend route segment.
_FORBIDDEN_PATH_BITS = ("://", "//", "..", "@", "\\", "?", "#")


@dataclass(frozen=True, slots=True)
class DashboardImageCapture:
    """Image bytes plus metadata describing the render request.

    Puppet returns only an image and its content type. Viewport and frontend
    context values here therefore describe what ha-mcp requested, not values
    independently confirmed by the rendered Home Assistant frontend.
    """

    data: bytes
    width: int
    height: int | Literal["auto"]
    preset: ViewportPreset | None
    orientation: Orientation | None
    image_format: ScreenshotFormat
    mime_type: str
    size_bytes: int
    requested: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _CaptureOptions:
    """Validated batch options."""

    width: int
    height: int | Literal["auto"]
    viewport_presets: list[ViewportPreset] | None
    orientation: Orientation | None
    zoom: float
    wait_ms: int
    full_page: bool
    theme: str | None
    dark_mode: bool
    language: str | None
    image_format: ScreenshotFormat
    render_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class _ViewportRequest:
    """One normalized viewport in an ordered capture batch."""

    preset: ViewportPreset | None
    width: int
    height: int | Literal["auto"]
    orientation: Orientation | None


def _normalize_dashboard_path(dashboard_path: str) -> str:
    """Return a safe, stripped, unencoded dashboard path or raise ToolError.

    Rejects URL syntax, traversal, and control characters. External raw-path
    callers must additionally verify the first segment against Home
    Assistant's registered Lovelace dashboards; structured and config-tool
    callers derive it from dashboard configuration instead.
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
    return "/".join(segments)


def _validate_dashboard_path(dashboard_path: str) -> str:
    """Return the safe dashboard path encoded for an engine HTTP request."""
    normalized = _normalize_dashboard_path(dashboard_path)
    # Encode exactly once at the transport boundary so canonical render-path
    # metadata stays readable and paths resolved by ``paths.py`` are not
    # double-encoded when capture validates them again.
    return "/".join(quote(segment, safe="") for segment in normalized.split("/"))


def _raise_invalid_parameter(
    parameter: str,
    value: Any,
    expectation: str,
) -> NoReturn:
    """Raise a structured invalid-parameter error."""
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"Invalid {parameter}: {expectation}.",
            context={"parameter": parameter, "value": value},
            suggestions=[f"Pass {parameter} as {expectation}"],
        )
    )


def _bounded_int(
    parameter: str,
    value: Any,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """Validate and return a bounded integer parameter."""
    if isinstance(value, bool) or not isinstance(value, int):
        _raise_invalid_parameter(parameter, value, "an integer")
    if not minimum <= value <= maximum:
        _raise_invalid_parameter(
            parameter,
            value,
            f"an integer from {minimum} through {maximum}",
        )
    return cast(int, value)


def _bounded_float(
    parameter: str,
    value: Any,
    *,
    minimum: float,
    maximum: float,
) -> float:
    """Validate and return a finite bounded numeric parameter."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _raise_invalid_parameter(parameter, value, "a number")
    normalized = float(value)
    if not math.isfinite(normalized) or not minimum <= normalized <= maximum:
        _raise_invalid_parameter(
            parameter,
            value,
            f"a finite number from {minimum:g} through {maximum:g}",
        )
    return normalized


def _validate_optional_text(parameter: str, value: Any) -> str | None:
    """Validate an optional non-blank text parameter without normalizing it."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        _raise_invalid_parameter(parameter, value, "a non-empty string or null")
    return value


def _oriented_dimensions(
    width: int,
    height: int,
    orientation: Orientation | None,
) -> tuple[int, int, Orientation]:
    """Normalize dimensions to an explicit orientation and report the result."""
    if (orientation == "portrait" and width > height) or (
        orientation == "landscape" and height > width
    ):
        width, height = height, width

    resolved: Orientation = orientation or (
        "portrait" if height > width else "landscape"
    )
    return width, height, resolved


def _capture_viewports(
    options: _CaptureOptions,
) -> list[_ViewportRequest]:
    """Resolve custom or named viewport requests in caller-provided order."""
    auto_height = options.full_page or options.height == "auto"

    if options.viewport_presets is None:
        if options.height == "auto":
            return [_ViewportRequest(None, options.width, "auto", options.orientation)]
        capture_width, capture_height, resolved_orientation = _oriented_dimensions(
            options.width, options.height, options.orientation
        )
        return [
            _ViewportRequest(
                preset=None,
                width=capture_width,
                height="auto" if auto_height else capture_height,
                orientation=resolved_orientation,
            )
        ]

    captures: list[_ViewportRequest] = []
    for preset in options.viewport_presets:
        preset_width, preset_height = VIEWPORT_PRESETS[preset]
        capture_width, capture_height, resolved_orientation = _oriented_dimensions(
            preset_width, preset_height, options.orientation
        )
        captures.append(
            _ViewportRequest(
                preset=preset,
                width=capture_width,
                height="auto" if auto_height else capture_height,
                orientation=resolved_orientation,
            )
        )
    return captures


def _validate_viewport_presets(value: Any) -> list[ViewportPreset] | None:
    """Validate an optional ordered set of unique named viewports."""
    valid_presets: list[ViewportPreset] | None = None
    if value is not None:
        if not isinstance(value, list) or not value:
            _raise_invalid_parameter(
                "viewport_presets",
                value,
                "a non-empty list containing mobile, tablet, and/or desktop",
            )
        if len(value) > len(VIEWPORT_PRESETS):
            _raise_invalid_parameter(
                "viewport_presets",
                value,
                "at most one each of mobile, tablet, and desktop",
            )
        if any(
            not isinstance(preset, str) or preset not in VIEWPORT_PRESETS
            for preset in value
        ):
            _raise_invalid_parameter(
                "viewport_presets",
                value,
                "a list containing only mobile, tablet, and/or desktop",
            )
        if len(set(value)) != len(value):
            _raise_invalid_parameter(
                "viewport_presets",
                value,
                "a list without duplicate presets",
            )
        valid_presets = cast(list[ViewportPreset], value.copy())
    return valid_presets


def _validate_capture_parameters(
    *,
    width: Any,
    height: Any,
    viewport_presets: Any,
    orientation: Any,
    zoom: Any,
    wait_ms: Any,
    full_page: Any,
    theme: Any,
    dark_mode: Any,
    language: Any,
    image_format: Any,
    render_timeout_seconds: Any,
) -> _CaptureOptions:
    """Validate capture inputs independently of FastMCP/Pydantic callers."""
    valid_width = _bounded_int(
        "width",
        width,
        minimum=MIN_VIEWPORT_DIMENSION,
        maximum=MAX_VIEWPORT_DIMENSION,
    )
    if height == "auto":
        valid_height: int | Literal["auto"] = "auto"
    else:
        valid_height = _bounded_int(
            "height",
            height,
            minimum=MIN_VIEWPORT_DIMENSION,
            maximum=MAX_VIEWPORT_DIMENSION,
        )

    valid_presets = _validate_viewport_presets(viewport_presets)

    if orientation not in (None, "portrait", "landscape"):
        _raise_invalid_parameter(
            "orientation", orientation, "portrait, landscape, or null"
        )
    valid_orientation = cast(Orientation | None, orientation)

    valid_zoom = _bounded_float("zoom", zoom, minimum=MIN_ZOOM, maximum=MAX_ZOOM)
    valid_wait_ms = _bounded_int("wait_ms", wait_ms, minimum=0, maximum=MAX_WAIT_MS)
    if not isinstance(full_page, bool):
        _raise_invalid_parameter("full_page", full_page, "true or false")
    if not isinstance(dark_mode, bool):
        _raise_invalid_parameter("dark_mode", dark_mode, "true or false")

    valid_theme = _validate_optional_text("theme", theme)
    valid_language = _validate_optional_text("language", language)

    if not isinstance(image_format, str) or image_format not in _MIME_TYPES:
        _raise_invalid_parameter(
            "image_format", image_format, "png, jpeg, webp, or bmp"
        )
    valid_format = cast(ScreenshotFormat, image_format)
    valid_timeout = _bounded_float(
        "render_timeout_seconds",
        render_timeout_seconds,
        minimum=MIN_RENDER_TIMEOUT_SECONDS,
        maximum=MAX_RENDER_TIMEOUT_SECONDS,
    )
    return _CaptureOptions(
        width=valid_width,
        height=valid_height,
        viewport_presets=valid_presets,
        orientation=valid_orientation,
        zoom=valid_zoom,
        wait_ms=valid_wait_ms,
        full_page=full_page,
        theme=valid_theme,
        dark_mode=dark_mode,
        language=valid_language,
        image_format=valid_format,
        render_timeout_seconds=valid_timeout,
    )


async def capture_dashboard_images(
    dashboard_path: str,
    *,
    width: int = DEFAULT_WIDTH,
    height: int | Literal["auto"] = DEFAULT_HEIGHT,
    viewport_presets: list[ViewportPreset] | None = None,
    orientation: Orientation | None = None,
    zoom: float = 1.0,
    wait_ms: int = DEFAULT_WAIT_MS,
    full_page: bool = False,
    theme: str | None = None,
    dark_mode: bool = False,
    language: str | None = None,
    image_format: ScreenshotFormat = "png",
    render_timeout_seconds: float = DEFAULT_RENDER_TIMEOUT_SECONDS,
) -> list[DashboardImageCapture]:
    """Render one or more ordered dashboard images via the screenshot engine.

    Named presets override explicit dimensions. An explicit orientation swaps
    preset or custom dimensions when needed; omitting it preserves each
    preset's native orientation. ``full_page`` is a compatibility alias for
    requesting the engine's native ``WIDTHxauto`` viewport.
    """
    path = _validate_dashboard_path(dashboard_path)
    options = _validate_capture_parameters(
        width=width,
        height=height,
        viewport_presets=viewport_presets,
        orientation=orientation,
        zoom=zoom,
        wait_ms=wait_ms,
        full_page=full_page,
        theme=theme,
        dark_mode=dark_mode,
        language=language,
        image_format=image_format,
        render_timeout_seconds=render_timeout_seconds,
    )
    viewports = _capture_viewports(options)

    engine = await resolve_engine_url()
    url = f"{engine}/{path}"
    mime_type = _MIME_TYPES[options.image_format]
    captures: list[DashboardImageCapture] = []

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(options.render_timeout_seconds)
    ) as http_client:
        for viewport in viewports:
            params: dict[str, str] = {
                "viewport": f"{viewport.width}x{viewport.height}",
                "zoom": str(options.zoom),
                "wait": str(options.wait_ms),
                "format": options.image_format,
            }
            if options.theme is not None:
                params["theme"] = options.theme
            if options.dark_mode:
                # Puppet checks only for query-key presence.
                params["dark"] = ""
            if options.language is not None:
                params["lang"] = options.language

            request_context = {
                "path": path,
                "preset": viewport.preset,
                "width": viewport.width,
                "height": viewport.height,
                "requested_format": options.image_format,
            }
            try:
                response = await http_client.get(url, params=params)
            except httpx.TimeoutException as exc:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.TIMEOUT_API_REQUEST,
                        "Dashboard screenshot rendering timed out after "
                        f"{render_timeout_seconds:g} seconds.",
                        details=str(exc),
                        context={
                            **request_context,
                            "engine_url": engine,
                            "render_timeout_seconds": render_timeout_seconds,
                        },
                        suggestions=[
                            "Increase render_timeout_seconds for a slow engine",
                            "Reduce wait_ms or render fewer viewport presets",
                            "Check the screenshot engine logs",
                        ],
                    )
                )
            except httpx.HTTPError as exc:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.CONNECTION_FAILED,
                        f"Could not reach the dashboard screenshot engine at {engine}.",
                        details=str(exc),
                        context={**request_context, "engine_url": engine},
                        suggestions=[
                            "Ensure the Puppet screenshot add-on (or sidecar) "
                            "is installed and running",
                            "If it is running, its access token is likely "
                            f"missing or invalid — {TOKEN_HINT}",
                            "Check HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL on "
                            "Docker/Container deployments",
                        ],
                    )
                )

            if response.status_code >= 400:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Screenshot engine returned HTTP {response.status_code} "
                        f"for dashboard '{path}'.",
                        details=response.text[:300],
                        context={
                            **request_context,
                            "status_code": response.status_code,
                        },
                        suggestions=[
                            "Verify the dashboard path exists",
                            "If the engine landed on the login page, its access "
                            f"token is missing or invalid — {TOKEN_HINT}",
                            "Increase wait_ms for heavy chart cards",
                        ],
                    )
                )
            if not response.content:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Screenshot engine returned an empty image for '{path}'.",
                        details="The dashboard path may be invalid, or the "
                        "engine's access token may be missing or expired.",
                        context=request_context,
                    )
                )

            response_mime_type = (
                response.headers.get("content-type", "")
                .partition(";")[0]
                .strip()
                .lower()
            )
            if response_mime_type != mime_type:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        "Screenshot engine returned an unexpected content type.",
                        details=f"Expected {mime_type}, received "
                        f"{response_mime_type or 'no Content-Type header'}.",
                        context={
                            **request_context,
                            "expected_content_type": mime_type,
                            "received_content_type": response_mime_type or None,
                        },
                        suggestions=[
                            "Verify the URL points to the Puppet screenshot engine",
                            "Check that the screenshot engine supports the "
                            f"requested {image_format} format",
                        ],
                    )
                )

            requested = {
                "zoom": options.zoom,
                "wait_ms": options.wait_ms,
                "full_page": options.full_page,
                "orientation": options.orientation,
                "theme": options.theme,
                "dark_mode": options.dark_mode,
                "language": options.language,
                "render_timeout_seconds": options.render_timeout_seconds,
            }
            captures.append(
                DashboardImageCapture(
                    data=response.content,
                    width=viewport.width,
                    height=viewport.height,
                    preset=viewport.preset,
                    orientation=viewport.orientation,
                    image_format=options.image_format,
                    mime_type=mime_type,
                    size_bytes=len(response.content),
                    requested=requested.copy(),
                )
            )

    return captures


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

    This compatibility wrapper returns the first image from
    :func:`capture_dashboard_images`. With ``full_page=True``, ``height`` is
    ignored and the engine receives its native ``WIDTHxauto`` viewport.

    Raises :class:`ToolError` if the engine is unreachable or returns an error.
    """
    captures = await capture_dashboard_images(
        dashboard_path,
        width=width,
        height=height,
        zoom=zoom,
        wait_ms=wait_ms,
        full_page=full_page,
        image_format="png",
    )
    return captures[0].data
