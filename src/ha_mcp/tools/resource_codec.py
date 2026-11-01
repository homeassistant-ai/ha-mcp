"""Inline dashboard-resource codec: ``data:`` URI encoding/decoding and list rendering.

Pure functions shared by the resource tools. Kept apart from
``tools_resources`` so the tool module holds only the MCP surface.
"""

import base64
import hashlib
import re
from typing import Any
from urllib.parse import urlsplit

from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error

# Legacy Cloudflare Worker URL. Only used to recognize and locally decode
# inline resources created before the data: URI switch (#2060) — this server
# never fetches it. The worker deployment stays up so pre-existing dashboards
# keep loading until their resources are converted.
LEGACY_WORKER_BASE_URL = "https://ha-mcp-resources.rapid-math-bbad.workers.dev"

# data: URI media types per inline resource_type ('js'/'html' are url-mode
# only). CSS carries an explicit charset because stylesheets, unlike module
# scripts, have no spec-mandated UTF-8 fallback.
_DATA_URI_MIME: dict[str, str] = {
    "module": "text/javascript",
    "css": "text/css;charset=utf-8",
}

# URL prefixes recognized as ha-mcp inline resources — DERIVED from the media
# types above rather than restated, so the read side cannot drift from the
# write side (a desync would make the server fail to recognize its own
# output, leaking raw base64 URLs into list responses). Each media type is
# accepted with and without the charset parameter so resources written
# before and after a charset change both still decode. Foreign data: URLs
# match nothing here and pass through untouched instead of being claimed.
_ALLOWED_DATA_URI_PREFIXES = tuple(
    f"data:{base}{charset};base64,"
    # dict.fromkeys dedupes while keeping a deterministic order.
    for base in dict.fromkeys(mime.split(";", 1)[0] for mime in _DATA_URI_MIME.values())
    for charset in ("", ";charset=utf-8")
)
# Scheme and media type are case-insensitive (RFC 3986/2397); prefixes are
# lowercase, so matching lowercases the URL head. Longest prefix is 42 chars.
_DATA_URI_HEAD = max(len(p) for p in _ALLOWED_DATA_URI_PREFIXES)

_B64_URLSAFE_RE = re.compile(r"[A-Za-z0-9_-]*={0,2}")

# Longest data: URL echoed verbatim for a resource we cannot decode.
# Beyond this the value is truncated: a corrupt or foreign payload can be
# ~171KB of base64, and echoing it whole would flood the response.
_MAX_ECHOED_URL = 300

# Characters of decoded content shown in list previews.
_PREVIEW_LENGTH = 150

# Total decoded content emitted per include_content=True response. A page
# of max-size resources would otherwise return limit x MAX_CONTENT_SIZE
# (up to ~64MB). Rows past the budget are FLAGGED, never shortened —
# handing back a silently-truncated payload is how a resource gets
# destroyed on write-back.
_MAX_CONTENT_BUDGET = 512_000

# Maximum inline content size, in UTF-8 bytes. The old 24KB bound was the
# content budget derived from the worker's ~32KB encoded URL-path limit;
# data: URIs impose no comparable limit, so the cap is now sized to fit
# established single-file card bundles without truncation while bounding
# what every dashboard load ships (the registered URL rides the Lovelace
# bootstrap to every browser) and what auto-backup snapshots per edit.
MAX_CONTENT_SIZE = 128_000


def content_sha256(content: str) -> str:
    """SHA-256 hex digest over the UTF-8 bytes of decoded resource content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _data_uri_for(content: str, resource_type: str) -> str:
    """Encode content as a base64 ``data:`` URI for the given resource type.

    Standard base64 (not URL-safe): browsers decode data: URIs with the
    standard alphabet. Deterministic — same content, same URI.
    """
    mime = _DATA_URI_MIME.get(resource_type)
    if mime is None:
        raise_tool_error(
            create_error_response(
                code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                message=f"Inline content does not support resource_type={resource_type!r}",
                suggestions=["Use resource_type='module' or 'css' for inline content"],
            )
        )
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _match_data_uri_prefix(url: str) -> str | None:
    """Return the matched ha-mcp inline data: prefix of ``url``, else None.

    Prefix matching is case-insensitive (scheme and media type are
    case-insensitive per RFC 3986/2397); the payload's case is preserved.
    """
    head = url[:_DATA_URI_HEAD].lower()
    for prefix in _ALLOWED_DATA_URI_PREFIXES:
        if head.startswith(prefix):
            return prefix
    return None


def _decode_data_uri(url: str) -> str | None:
    """Decode one of our base64 ``data:`` URIs back to content.

    None for foreign data: URLs (other media types, percent-encoded form) —
    those are passed through untouched, not claimed as inline resources —
    and for corrupt payloads (``validate=True``: a payload the browser's
    forgiving-base64 would reject must not be reported as valid content).
    """
    prefix = _match_data_uri_prefix(url)
    if prefix is None:
        return None
    try:
        return base64.b64decode(url[len(prefix) :], validate=True).decode("utf-8")
    except Exception:  # noqa: BLE001
        return None


def _is_data_url(url: str) -> bool:
    """True when ``url`` parses as the ``data:`` scheme in a browser.

    ``urlsplit`` applies WHATWG's leading C0-control-or-space strip and
    ASCII tab/newline/CR removal before reading the scheme (the
    CVE-2023-24329 hardening; this package requires Python >=3.13), and
    lowercases it. A raw ``url[:5]`` check would miss ``" data:..."`` and
    ``"da\\tta:..."``, which browsers still load.
    """
    return urlsplit(url).scheme == "data"


def _pad_b64(encoded: str) -> str:
    """Reject non-alphabet characters, then restore any stripped ``=`` padding.

    ``urlsafe_b64decode`` has no ``validate=`` parameter, so strictness has
    to be enforced here; the padding fix-up keeps genuine worker URLs (which
    always carried padding) working either way.
    """
    if not _B64_URLSAFE_RE.fullmatch(encoded):
        raise ValueError("non-base64 characters in payload")
    return encoded + "=" * (-len(encoded) % 4)


def _has_inline_url_shape(url: str) -> bool:
    """True when ``url`` LOOKS like one of ours, regardless of decodability.

    Only used to flag a recognized-but-corrupt payload; the decode result
    itself (never this) decides what counts as an inline resource.
    """
    if _match_data_uri_prefix(url) is not None:
        return True
    prefix = f"{LEGACY_WORKER_BASE_URL}/"
    return url[: len(prefix)].lower() == prefix.lower()


def _looks_like_preview(content: str) -> bool:
    """Cheap pre-filter: could ``content`` be a list-tool ``_preview``?

    Previews are ``_PREVIEW_LENGTH`` characters plus a trailing ``"..."``.
    Two shapes qualify: that signature itself (tolerating surrounding
    whitespace), and exactly ``_PREVIEW_LENGTH`` characters, which is what
    an agent produces by stripping the ellipsis.

    Deliberately narrow. Everything matching pays a WS round-trip and, if
    that round-trip fails, is refused — so matching ordinary short content
    (a legitimate 20-character CSS tweak) would make routine updates
    fragile for no safety gain, since such content cannot be a preview of
    anything.
    """
    stripped = content.strip()
    if stripped.endswith("...") and len(stripped) <= _PREVIEW_LENGTH + 3:
        return True
    return len(stripped) == _PREVIEW_LENGTH


def _is_preview_of(content: str, stored: str) -> bool:
    """True when ``content`` is the truncated preview of ``stored``.

    Tolerates the ellipsis being stripped and surrounding whitespace, so a
    lightly-mangled preview writeback is still caught.
    """
    if len(stored) <= _PREVIEW_LENGTH:
        # Short resources have no truncated preview to confuse with.
        return False
    candidate = content.strip().removesuffix("...")
    return bool(candidate) and stored.startswith(candidate)


def _decode_inline_content(url: str) -> tuple[str, bool] | None:
    """Decode an inline resource URL to ``(content, is_legacy_worker)``.

    ``None`` when the URL is not one of ours, or is one of ours but holds
    an empty/corrupt payload. This is the single source of truth for "is
    this an inline resource we can show", so the list summary counts and
    the per-resource markers can never disagree. Returning one ``None``
    rather than a ``(None, bool)`` pair keeps the meaningless
    "not-ours-but-legacy" combination unrepresentable and forces callers
    through one check instead of indexing the tuple.
    """
    content = _decode_data_uri(url)
    if content:
        return content, False
    legacy = _decode_legacy_worker_url(url)
    if legacy:
        return legacy, True
    return None


def _decode_legacy_worker_url(url: str) -> str | None:
    """Decode a legacy worker inline URL back to content (pure local base64).

    The worker embedded URL-safe base64 in its path; decoding needs no
    network. Anchored to the worker origin — lookalike hosts that merely
    contain the worker host string are not decoded. Returns None if the URL
    is not a legacy worker URL.
    """
    prefix = f"{LEGACY_WORKER_BASE_URL}/"
    # Scheme and host are case-insensitive; the base64 payload is not.
    if url[: len(prefix)].lower() != prefix.lower():
        return None
    try:
        # Extract base64 part: https://worker.dev/{base64}?type=module
        encoded = url[len(prefix) :].split("?", maxsplit=1)[0]
        # validate=True mirrors _decode_data_uri: urlsafe_b64decode otherwise
        # silently DISCARDS non-alphabet characters, so a junk path could
        # decode to plausible text and be masked as inline content.
        return base64.urlsafe_b64decode(_pad_b64(encoded)).decode("utf-8")
    except Exception:  # noqa: BLE001
        return None


def _summarize_resources(
    resources: list[Any],
    keep_start: int,
    keep_stop: int,
) -> tuple[dict[str, int], int, dict[int, tuple[str, bool]]]:
    """Count resources by type, and decode every inline one exactly once.

    Returns ``(by_type_counts, inline_count, {index: (content, is_legacy)})``.

    Decoding is deliberately WHOLE-SET: ``inline_count`` reports the full
    registry and must agree with the per-resource ``_inline`` markers, and
    only a real decode can tell a genuine inline resource from a corrupt
    payload that merely looks like one.

    RETENTION, by contrast, is page-scoped: only indices in
    ``[keep_start, keep_stop)`` are kept in the returned map. Holding every
    decoded payload would mean a ``limit=1`` call sat on the whole
    registry's content — up to ``MAX_CONTENT_SIZE`` per resource — for a
    one-row response. Because the page's entries are retained, nothing is
    decoded twice.
    """
    by_type_counts: dict[str, int] = {}
    inline_count = 0
    decoded_by_index: dict[int, tuple[str, bool]] = {}
    for index, resource in enumerate(resources):
        res = resource if isinstance(resource, dict) else {}
        res_type = res.get("type")
        if isinstance(res_type, str):
            by_type_counts[res_type] = by_type_counts.get(res_type, 0) + 1
        url = res.get("url", "")
        if isinstance(url, str):
            decoded = _decode_inline_content(url)
            if decoded:
                inline_count += 1
                if keep_start <= index < keep_stop:
                    decoded_by_index[index] = decoded
    return by_type_counts, inline_count, decoded_by_index


def _process_resource_list(
    resources: list[Any],
    decoded_page: list[tuple[str, bool] | None],
    include_content: bool,
    *,
    include_hash: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Render one page of resources. Returns ``(rows, truncated_count)``.

    ``decoded_page`` carries the already-decoded ``(content, is_legacy)`` for
    each row (``None`` when the resource is not a decodable inline one), so
    this never re-decodes and can never classify a resource differently than
    the caller's summary did.

    With ``include_content``, content is emitted until the per-response byte
    budget is spent; rows past it are marked ``_content_truncated`` rather
    than silently shortened, because a truncated value written back would
    destroy the resource.
    """
    processed: list[dict[str, Any]] = []
    truncated_count = 0
    content_budget = _MAX_CONTENT_BUDGET

    for resource, decoded in zip(resources, decoded_page, strict=True):
        res = dict(resource) if isinstance(resource, dict) else {"url": resource}
        url = res.get("url", "")
        content, is_legacy = decoded if decoded else (None, False)

        if content:
            content_bytes = len(content.encode("utf-8"))
            res["_inline"] = True
            res["_size"] = content_bytes
            if include_hash:
                res["_sha256"] = content_sha256(content)
            if is_legacy:
                # The full remediation text lives once at response level;
                # repeating it per resource wasted ~330 chars each.
                res["_legacy_worker"] = True

            if include_content:
                cost = content_bytes
                if cost <= content_budget:
                    res["_content"] = content
                    content_budget -= cost
                else:
                    res["_content_truncated"] = True
                    truncated_count += 1
            else:
                preview = content[:_PREVIEW_LENGTH]
                if len(content) > _PREVIEW_LENGTH:
                    preview += "..."
                res["_preview"] = preview

            res["url"] = "[inline]"
        elif isinstance(url, str) and _has_inline_url_shape(url):
            # Recognized as one of ours but the payload does not decode
            # (empty, corrupt, or not UTF-8). Say so explicitly: without a
            # marker this reads as "an external resource, not my problem",
            # which is the wrong conclusion for someone debugging a card
            # that stopped rendering. The real URL stays visible (truncated
            # if huge) because that is what diagnosis needs.
            res["_decode_error"] = True
            if len(url) > _MAX_ECHOED_URL:
                res["_url_length"] = len(url)
                res["url"] = url[:_MAX_ECHOED_URL] + "…[truncated]"

        processed.append(res)
    return processed, truncated_count


def _check_ws_error(result: Any) -> str | None:
    """Check a WebSocket result for errors. Returns error message or None."""
    if isinstance(result, dict) and not result.get("success", True):
        error = result.get("error", {})
        if isinstance(error, dict):
            msg: str = error.get("message", str(error)) or "Unknown error"
            return msg
        return str(error) or "Unknown error"
    return None
