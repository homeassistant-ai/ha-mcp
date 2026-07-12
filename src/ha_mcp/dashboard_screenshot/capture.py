"""Thin async HTTP client to the dashboard screenshot engine.

The engine (balloob's Puppet add-on, or a docker-compose sidecar)
authenticates to Home Assistant with its OWN configured long-lived token, so
this client passes only the dashboard path + render parameters — no HA token
ever flows through ha-mcp or the LLM for screenshots.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Literal, NoReturn, cast
from urllib.parse import quote

import httpx
from fastmcp.exceptions import ToolError

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

_IMAGE_SIGNATURES: dict[ScreenshotFormat, tuple[bytes, ...]] = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpeg": (b"\xff\xd8\xff",),
    "webp": (b"RIFF", b"WEBP"),
    "bmp": (b"BM",),
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

# Bound raw response bytes before FastMCP base64-encodes them. These are server
# safety limits, not claims about a particular MCP client's smaller vision or
# context-window limits.
MAX_IMAGE_PAYLOAD_BYTES = 20 * 1024 * 1024
MAX_BATCH_PAYLOAD_BYTES = 40 * 1024 * 1024
MAX_ENGINE_ERROR_BODY_BYTES = 300

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
    raise AssertionError("unreachable: raise_tool_error always raises")


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
    assert type(value) is int
    if not minimum <= value <= maximum:
        _raise_invalid_parameter(
            parameter,
            value,
            f"an integer from {minimum} through {maximum}",
        )
    return value


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


def _validate_image_format(value: Any) -> ScreenshotFormat:
    """Return one supported screenshot format with its literal type preserved."""
    for candidate in _MIME_TYPES:
        if value == candidate:
            return candidate
    _raise_invalid_parameter("image_format", value, "png, jpeg, webp, or bmp")


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
        if auto_height:
            return [_ViewportRequest(None, options.width, "auto", None)]
        fixed_height = options.height
        assert isinstance(fixed_height, int)
        capture_width, capture_height, resolved_orientation = _oriented_dimensions(
            options.width, fixed_height, options.orientation
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


def validate_capture_parameters(
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
    if (
        orientation is not None
        and valid_presets is None
        and (full_page or valid_height == "auto")
    ):
        _raise_invalid_parameter(
            "orientation",
            orientation,
            "null when using a custom auto-height viewport, or a named viewport preset",
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

    valid_format = _validate_image_format(image_format)
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


def _declared_content_length(response: httpx.Response) -> int | None:
    """Return a trustworthy non-negative Content-Length when one is present."""
    raw_value = response.headers.get("content-length")
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except ValueError:
        return None
    return value if value >= 0 else None


async def _read_limited_response(
    response: httpx.Response, limit_bytes: int
) -> tuple[bytes, bool]:
    """Stream at most ``limit_bytes`` and report whether more data existed."""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        remaining = limit_bytes - len(body)
        if len(chunk) > remaining:
            body.extend(chunk[:remaining])
            return bytes(body), True
        body.extend(chunk)
    return bytes(body), False


def _raise_payload_too_large(
    *,
    request_context: dict[str, Any],
    capture_count: int,
    completed_count: int,
    declared_bytes: int | None,
    received_bytes: int | None,
    aggregate_bytes: int,
    limit_kind: Literal["image", "batch"],
) -> NoReturn:
    """Raise the issue #1786 image-size failure class with audit context."""
    limit_bytes = (
        MAX_IMAGE_PAYLOAD_BYTES if limit_kind == "image" else MAX_BATCH_PAYLOAD_BYTES
    )
    raise_tool_error(
        create_error_response(
            ErrorCode.IMAGE_PAYLOAD_TOO_LARGE,
            "Screenshot image payload exceeds the server's safe inline-image "
            f"{limit_kind} limit.",
            context={
                **request_context,
                "capture_count": capture_count,
                "completed_count": completed_count,
                "declared_bytes": declared_bytes,
                "received_bytes": received_bytes,
                "aggregate_bytes_before_capture": aggregate_bytes,
                "limit_kind": limit_kind,
                "limit_bytes": limit_bytes,
                "per_image_limit_bytes": MAX_IMAGE_PAYLOAD_BYTES,
                "batch_limit_bytes": MAX_BATCH_PAYLOAD_BYTES,
            },
            suggestions=[
                "Request fewer viewport presets in one call",
                "Use png, jpeg, or webp instead of bmp for large captures",
                "Reduce viewport dimensions or capture a single view",
            ],
        )
    )
    raise AssertionError("unreachable: raise_tool_error always raises")


async def _read_image_response(
    response: httpx.Response,
    *,
    mime_type: str,
    image_format: ScreenshotFormat,
    request_context: dict[str, Any],
    aggregate_bytes: int,
    remaining_batch_bytes: int,
) -> bytes:
    """Validate and stream one successful engine image within payload limits."""
    response_mime_type = (
        response.headers.get("content-type", "").partition(";")[0].strip().lower()
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
                    + f"requested {image_format} format",
                ],
            )
        )

    declared_bytes = _declared_content_length(response)
    if declared_bytes is not None and declared_bytes > MAX_IMAGE_PAYLOAD_BYTES:
        _raise_payload_too_large(
            request_context=request_context,
            capture_count=int(request_context["capture_count"]),
            completed_count=int(request_context["completed_count"]),
            declared_bytes=declared_bytes,
            received_bytes=0,
            aggregate_bytes=aggregate_bytes,
            limit_kind="image",
        )
    if declared_bytes is not None and declared_bytes > remaining_batch_bytes:
        _raise_payload_too_large(
            request_context=request_context,
            capture_count=int(request_context["capture_count"]),
            completed_count=int(request_context["completed_count"]),
            declared_bytes=declared_bytes,
            received_bytes=0,
            aggregate_bytes=aggregate_bytes,
            limit_kind="batch",
        )

    read_limit = min(MAX_IMAGE_PAYLOAD_BYTES, remaining_batch_bytes)
    image_data, limit_exceeded = await _read_limited_response(response, read_limit)
    if limit_exceeded:
        limit_kind: Literal["image", "batch"] = (
            "image" if remaining_batch_bytes >= MAX_IMAGE_PAYLOAD_BYTES else "batch"
        )
        _raise_payload_too_large(
            request_context=request_context,
            capture_count=int(request_context["capture_count"]),
            completed_count=int(request_context["completed_count"]),
            declared_bytes=declared_bytes,
            received_bytes=read_limit + 1,
            aggregate_bytes=aggregate_bytes,
            limit_kind=limit_kind,
        )
    if not image_data:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Screenshot engine returned an empty image for '{request_context['path']}'.",
                details="The dashboard path may be invalid, or the engine's access "
                "token may be missing or expired.",
                context=request_context,
            )
        )
    signatures = _IMAGE_SIGNATURES[image_format]
    valid_signature = image_data.startswith(signatures[0])
    if image_format == "webp":
        valid_signature = valid_signature and image_data[8:12] == signatures[1]
    if not valid_signature:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Screenshot engine returned bytes that do not match the requested "
                f"{image_format} image format.",
                context={
                    **request_context,
                    "expected_content_type": mime_type,
                    "received_content_type": response_mime_type,
                    "received_signature_hex": image_data[:12].hex(),
                },
                suggestions=[
                    "Verify the URL points to the Puppet screenshot engine",
                    "Check whether an authentication or proxy page was returned "
                    + "with an incorrect image Content-Type",
                ],
            )
        )
    return image_data


async def _request_viewport_image(
    http_client: httpx.AsyncClient,
    *,
    url: str,
    path: str,
    engine: str,
    params: dict[str, str],
    options: _CaptureOptions,
    viewport: _ViewportRequest,
    mime_type: str,
    request_context: dict[str, Any],
    aggregate_bytes: int,
    remaining_batch_bytes: int,
) -> tuple[bytes, int | Literal["auto"], bool]:
    """Render one viewport, including the legacy full-page compatibility retry."""
    fallback_used = False
    capture_height = viewport.height
    while True:
        try:
            async with http_client.stream("GET", url, params=params) as response:
                if response.status_code >= 400:
                    error_body, error_truncated = await _read_limited_response(
                        response, MAX_ENGINE_ERROR_BODY_BYTES
                    )
                    error_text = error_body.decode(errors="replace")
                    if (
                        response.status_code == 400
                        and options.full_page
                        and viewport.height == "auto"
                        and not fallback_used
                    ):
                        # Puppet <2.5.0 does not understand WIDTHxauto.
                        fallback_used = True
                        capture_height = MAX_VIEWPORT_DIMENSION
                        params["viewport"] = (
                            f"{viewport.width}x{MAX_VIEWPORT_DIMENSION}"
                        )
                        continue
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.SERVICE_CALL_FAILED,
                            "Screenshot engine returned HTTP "
                            f"{response.status_code} for dashboard '{path}'.",
                            details=error_text + ("…" if error_truncated else ""),
                            context={
                                **request_context,
                                "status_code": response.status_code,
                                "legacy_full_page_fallback_attempted": fallback_used,
                            },
                            suggestions=[
                                "Verify the dashboard path exists",
                                "If the engine landed on the login page, its "
                                + f"access token is missing or invalid — {TOKEN_HINT}",
                                "Increase wait_ms for heavy chart cards",
                            ],
                        )
                    )
                image_data = await _read_image_response(
                    response,
                    mime_type=mime_type,
                    image_format=options.image_format,
                    request_context=request_context,
                    aggregate_bytes=aggregate_bytes,
                    remaining_batch_bytes=remaining_batch_bytes,
                )
        except httpx.TimeoutException as exc:
            raise_tool_error(
                create_error_response(
                    ErrorCode.TIMEOUT_API_REQUEST,
                    "Dashboard screenshot rendering timed out after "
                    f"{options.render_timeout_seconds:g} seconds.",
                    details=str(exc),
                    context={
                        **request_context,
                        "engine_url": engine,
                        "render_timeout_seconds": options.render_timeout_seconds,
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
                        + "is installed and running",
                        "If it is running, its access token is likely "
                        + f"missing or invalid — {TOKEN_HINT}",
                        "Check HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL on "
                        + "Docker/Container deployments",
                    ],
                )
            )
        return image_data, capture_height, fallback_used


async def _request_or_collect_failure(
    http_client: httpx.AsyncClient,
    *,
    url: str,
    path: str,
    engine: str,
    params: dict[str, str],
    options: _CaptureOptions,
    viewport: _ViewportRequest,
    mime_type: str,
    request_context: dict[str, Any],
    aggregate_bytes: int,
    remaining_batch_bytes: int,
    partial_failures: list[dict[str, Any]] | None,
) -> tuple[bytes, int | Literal["auto"], bool] | None:
    """Render one viewport or record its structured failure for a partial batch."""
    try:
        return await _request_viewport_image(
            http_client,
            url=url,
            path=path,
            engine=engine,
            params=params,
            options=options,
            viewport=viewport,
            mime_type=mime_type,
            request_context=request_context,
            aggregate_bytes=aggregate_bytes,
            remaining_batch_bytes=remaining_batch_bytes,
        )
    except ToolError as exc:
        if partial_failures is None:
            raise
        try:
            failure = json.loads(str(exc))
        except (json.JSONDecodeError, TypeError):
            failure = None
        if not isinstance(failure, dict):
            failure = create_error_response(
                ErrorCode.INTERNAL_ERROR,
                "Dashboard capture failed with an unstructured error.",
                details=str(exc),
                context=request_context,
            )
        partial_failures.append(failure)
        return None


def _complete_capture_batch(
    captures: list[DashboardImageCapture],
    partial_failures: list[dict[str, Any]] | None,
) -> list[DashboardImageCapture]:
    """Return partial successes, but raise when every requested capture failed."""
    if not captures and partial_failures:
        aggregate_failure = {
            **partial_failures[0],
            "all_captures_failed": True,
            "failure_count": len(partial_failures),
            "screenshot_failures": partial_failures,
        }
        raise_tool_error(aggregate_failure)
    return captures


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
    partial_failures: list[dict[str, Any]] | None = None,
) -> list[DashboardImageCapture]:
    """Render one or more ordered dashboard images via the screenshot engine.

    Named presets override explicit dimensions. An explicit orientation swaps
    preset or custom dimensions when needed; omitting it preserves each
    preset's native orientation. ``full_page`` is a compatibility alias for
    requesting the engine's native ``WIDTHxauto`` viewport.
    """
    path = _validate_dashboard_path(dashboard_path)
    options = validate_capture_parameters(
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
        for capture_index, viewport in enumerate(viewports):
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
                "capture_index": capture_index,
                "capture_count": len(viewports),
                "completed_count": len(captures),
            }
            aggregate_bytes = sum(capture.size_bytes for capture in captures)
            remaining_batch_bytes = MAX_BATCH_PAYLOAD_BYTES - aggregate_bytes
            if remaining_batch_bytes <= 0:
                if partial_failures is not None:
                    partial_failures.append(
                        create_error_response(
                            ErrorCode.IMAGE_PAYLOAD_TOO_LARGE,
                            "Screenshot image batch reached the server's safe "
                            "inline-image limit.",
                            context={
                                **request_context,
                                "aggregate_bytes_before_capture": aggregate_bytes,
                                "limit_kind": "batch",
                                "limit_bytes": MAX_BATCH_PAYLOAD_BYTES,
                            },
                        )
                    )
                    break
                _raise_payload_too_large(
                    request_context=request_context,
                    capture_count=len(viewports),
                    completed_count=len(captures),
                    declared_bytes=None,
                    received_bytes=0,
                    aggregate_bytes=aggregate_bytes,
                    limit_kind="batch",
                )

            outcome = await _request_or_collect_failure(
                http_client,
                url=url,
                path=path,
                engine=engine,
                params=params,
                options=options,
                viewport=viewport,
                mime_type=mime_type,
                request_context=request_context,
                aggregate_bytes=aggregate_bytes,
                remaining_batch_bytes=remaining_batch_bytes,
                partial_failures=partial_failures,
            )
            if outcome is None:
                if (
                    partial_failures
                    and partial_failures[-1].get("limit_kind") == "batch"
                ):
                    break
                continue
            image_data, capture_height, fallback_used = outcome

            requested = {
                "zoom": options.zoom,
                "wait_ms": options.wait_ms,
                "full_page": options.full_page,
                "orientation": options.orientation,
                "theme": options.theme,
                "dark_mode": options.dark_mode,
                "language": options.language,
                "render_timeout_seconds": options.render_timeout_seconds,
                "legacy_full_page_fallback": fallback_used,
            }
            captures.append(
                DashboardImageCapture(
                    data=image_data,
                    width=viewport.width,
                    height=capture_height,
                    preset=viewport.preset,
                    orientation=viewport.orientation,
                    image_format=options.image_format,
                    mime_type=mime_type,
                    size_bytes=len(image_data),
                    requested=requested.copy(),
                )
            )

    return _complete_capture_batch(captures, partial_failures)


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
