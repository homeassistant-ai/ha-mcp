"""
Dashboard resource hosting tools for Home Assistant MCP server.

Provides tools for managing dashboard resources (custom cards, CSS, JS):
- Inline resources: Code embedded directly in the resource URL as a
  ``data:`` URI — stored in HA's own resource registry, served by nothing
- External resources: URLs to /local/, /hacsfiles/, or external CDNs

Inline resources were previously served by a Cloudflare Worker that decoded
base64 content embedded in its URL, based on issue #71's claim that browsers
reject ``data:`` URIs for ES6 modules. That claim was wrong — HA's WS API
applies no URL-scheme validation, HA ships no CSP, and every layer loads
``data:`` modules and stylesheets fine (verified live; see #2060). Inline
content now goes straight into a ``data:`` URI: same storage location
(the resource registry), no third-party hop on dashboard loads.
``LEGACY_WORKER_BASE_URL`` survives only to recognize and locally decode
resources created before the switch — the worker is never contacted.
"""

import base64
import logging
from typing import Annotated, Any, Literal, NoReturn

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .auto_backup import with_auto_backup
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .util_helpers import build_pagination_metadata

logger = logging.getLogger(__name__)

# Legacy Cloudflare Worker URL. Only used to recognize and locally decode
# inline resources created before the data: URI switch (#2060) — this server
# never fetches it. The worker deployment stays up so pre-existing dashboards
# keep loading until their resources are converted.
LEGACY_WORKER_BASE_URL = "https://ha-mcp-resources.rapid-math-bbad.workers.dev"

# data: URI media types per resource_type ('js' is rejected for inline mode).
# CSS carries an explicit charset because stylesheets, unlike module scripts,
# have no spec-mandated UTF-8 fallback.
_DATA_URI_MIME = {"module": "text/javascript", "css": "text/css;charset=utf-8"}

# URL prefixes recognized as ha-mcp inline resources. Scoped to the media
# types this tool writes (with and without the charset param) so foreign
# data: resources a user registered by other means are passed through
# untouched instead of being claimed and masked as "[inline]".
_ALLOWED_DATA_URI_PREFIXES = tuple(
    f"data:{mime}{charset};base64,"
    for mime in ("text/javascript", "text/css")
    for charset in ("", ";charset=utf-8")
)
# Scheme and media type are case-insensitive (RFC 3986/2397); prefixes are
# lowercase, so matching lowercases the URL head. Longest prefix is 42 chars.
_DATA_URI_HEAD = max(len(p) for p in _ALLOWED_DATA_URI_PREFIXES)

# Characters of decoded content shown in list previews.
_PREVIEW_LENGTH = 150

# Maximum inline content size. The old 24KB bound was the worker's URL-path
# limit; data: URIs have no such limit (browsers verified to 2MB), so the cap
# is sized to fit established single-file card bundles without truncation
# while bounding what every dashboard load ships (the registered URL rides
# the Lovelace bootstrap to every browser) and what auto-backup snapshots
# per edit.
MAX_CONTENT_SIZE = 128_000

# Top-level HA-config YAML keys that LLMs sometimes emit when they pick this
# tool (`ha_config_set_dashboard_resource`) to "create a scene/automation/...".
# This tool only stores Lovelace JS/CSS resources — the YAML payload would land
# as a Lovelace module, creating orphaned, unreachable HA entities (see #1072).
# Map: top-level key → suggested replacement tool (None where no direct tool
# exists yet — e.g. scenes are tracked in #995).
_HA_CONFIG_YAML_MARKERS: dict[str, str | None] = {
    "automation": "ha_config_set_automation",
    "script": "ha_config_set_script",
    "scene": None,  # Tracked in #995 — scene CRUD tools not yet shipped
    "group": "ha_config_set_group",
    "input_boolean": "ha_config_set_helper(helper_type='input_boolean', ...)",
    "input_number": "ha_config_set_helper(helper_type='input_number', ...)",
    "input_select": "ha_config_set_helper(helper_type='input_select', ...)",
    "input_text": "ha_config_set_helper(helper_type='input_text', ...)",
    "input_datetime": "ha_config_set_helper(helper_type='input_datetime', ...)",
    "input_button": "ha_config_set_helper(helper_type='input_button', ...)",
    "template": None,
    "homeassistant": None,
    "sensor": None,
    "binary_sensor": None,
    "light": None,
    "switch": None,
    "cover": None,
    "climate": None,
    "media_player": None,
    "notify": None,
}


def _detect_ha_config_yaml(content: str) -> str | None:
    """Detect HA-config YAML at the start of inline-resource content.

    Returns the matching top-level key (without the colon) when content's
    first significant line opens an HA-config block, else None. Plain JS/CSS
    never starts with ``<word>:`` followed by whitespace/EOL/YAML-marker —
    JS opens with ``import``/``export``/``const``/``//``/``/*``/``function``
    or similar, CSS with selectors/at-rules/``/*``. False-positive surface
    is therefore narrow.

    Skips a leading BOM (``\\ufeff``), blank lines, full-line ``#`` comments,
    and YAML doc-start markers (``---``) before reading the first content
    line — these decorations are common in real-world HA YAML files and
    would otherwise let misrouted content slip past the first-line check.

    See #1072 for the misroute pattern this guards against.
    """
    # `str.strip()` does not remove U+FEFF (BOM) — it is not Unicode-whitespace.
    stripped = content.lstrip("\ufeff")
    first_line = ""
    for raw_line in stripped.splitlines():
        bare = raw_line.strip()
        if not bare or bare == "---" or bare.startswith("#"):
            continue
        first_line = bare
        break
    if not first_line:
        return None
    for domain in _HA_CONFIG_YAML_MARKERS:
        prefix = f"{domain}:"
        if first_line == prefix:
            # Block-form `automation:` exactly — unambiguous.
            return domain
        if first_line.startswith(prefix):
            # `automation: <something>` — only count it as YAML if the char
            # after the colon is whitespace, EOL, or a YAML marker. CSS
            # selectors like `automation:hover` (hypothetical) would have
            # an alpha char after the colon and not match.
            sep = first_line[len(prefix)]
            if sep in (" ", "\t", "|", ">", "-", "[", "{", "#"):
                return domain
    return None


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
    except Exception:
        return None


def _decode_legacy_worker_url(url: str) -> str | None:
    """Decode a legacy worker inline URL back to content (pure local base64).

    The worker embedded URL-safe base64 in its path; decoding needs no
    network. Anchored to the worker origin — lookalike hosts that merely
    contain the worker host string are not decoded. Returns None if the URL
    is not a legacy worker URL.
    """
    if not url.startswith(f"{LEGACY_WORKER_BASE_URL}/"):
        return None
    try:
        # Extract base64 part: https://worker.dev/{base64}?type=module
        encoded = url.removeprefix(f"{LEGACY_WORKER_BASE_URL}/").split("?")[0]
        return base64.urlsafe_b64decode(encoded).decode("utf-8")
    except Exception:
        return None


def _is_inline_url(url: str) -> bool:
    """Check if a URL is an inline resource URL (current data: or legacy worker)."""
    return _match_data_uri_prefix(url) is not None or url.startswith(
        f"{LEGACY_WORKER_BASE_URL}/"
    )


class ResourceTools:
    """Dashboard resource hosting tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_config_list_dashboard_resources",
        tags={"Dashboards"},
        annotations={
            "openWorldHint": True,
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "List Dashboard Resources",
        },
    )
    @log_tool_usage
    async def ha_config_list_dashboard_resources(
        self,
        include_content: Annotated[
            bool,
            Field(
                description="Include full decoded content for inline resources. "
                "Default False to save tokens (shows 150-char preview instead)."
            ),
        ] = False,
        limit: Annotated[
            int,
            Field(
                default=100,
                ge=1,
                le=500,
                description="Max resources to return per page (default: 100)",
            ),
        ] = 100,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of resources to skip for pagination (default: 0)",
            ),
        ] = 0,
    ) -> dict[str, Any]:
        """
        List Lovelace dashboard resources (custom cards, themes, CSS/JS).

        Returns one page of registered resources; `total_count` and `has_more`
        report the full set. For inline resources (created with
        ha_config_set_dashboard_resource(content=...)), shows a preview of the content
        instead of the full encoded URL to save tokens.

        `inline_count` and `by_type` summarise every resource, not just this page.

        Args:
            include_content: If True, includes full decoded content for inline
                resources in "_content" field. Default False (150-char preview only).

        Resource types:
        - module: ES6 JavaScript modules (modern custom cards)
        - js: Legacy JavaScript files
        - css: CSS stylesheets

        Each resource has a unique ID for update/delete operations.

        EXAMPLES:
        - First page of resources: ha_config_list_dashboard_resources()
        - Next page: ha_config_list_dashboard_resources(offset=100)
        - List with full content: ha_config_list_dashboard_resources(include_content=True)

        Note: Home Assistant 2026.6+ exposes resource management in the UI by
        default, and API access works regardless of UI availability.
        """
        try:
            result = await self._client.send_websocket_message(
                {"type": "lovelace/resources"}
            )

            # Handle WebSocket response format
            if isinstance(result, dict) and "result" in result:
                resources = result["result"]
            elif isinstance(result, list):
                resources = result
            else:
                resources = []

            # Summaries cover ALL resources via cheap URL-shape checks; the
            # decode/preview work runs only on the returned page, so
            # include_content=True responses stay bounded by limit/offset
            # instead of materializing every inline resource's content.
            by_type_counts = {"module": 0, "js": 0, "css": 0}
            inline_count = 0
            for resource in resources:
                res = resource if isinstance(resource, dict) else {}
                res_type = res.get("type", "unknown")
                if res_type in by_type_counts:
                    by_type_counts[res_type] += 1
                url = res.get("url", "")
                if isinstance(url, str) and _is_inline_url(url):
                    inline_count += 1

            total_count = len(resources)
            page = resources[offset : offset + limit]
            paginated = _process_resource_list(page, include_content)

            return {
                "success": True,
                "action": "list",
                "resources": paginated,
                **build_pagination_metadata(total_count, offset, limit, len(paginated)),
                "inline_count": inline_count,
                "by_type": by_type_counts,
            }
        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error listing dashboard resources: {e}")
            exception_to_structured_error(
                e,
                context={"tool": "ha_config_list_dashboard_resources"},
                suggestions=[
                    "Ensure Home Assistant is running and accessible",
                    "Check that you have admin permissions",
                ],
            )
            # ``exception_to_structured_error`` always raises (NoReturn); this
            # explicit raise makes the function's exit unambiguous (no implicit
            # ``return None`` fall-through) and is never reached at runtime.
            raise

    @tool(
        name="ha_config_set_dashboard_resource",
        tags={"Dashboards"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "title": "Set Dashboard Resource",
        },
    )
    @with_auto_backup(domain="dashboard_resource", id_param="resource_id")
    @log_tool_usage
    async def ha_config_set_dashboard_resource(
        self,
        content: Annotated[
            str | None,
            Field(
                description="JavaScript or CSS code to host inline (max ~128KB). "
                "The code is embedded directly in the resource URL as a data: URI - "
                "no file storage or external hosting involved. "
                "Mutually exclusive with url. Supports 'module' and 'css' types only."
            ),
        ] = None,
        url: Annotated[
            str | None,
            Field(
                description="URL of the resource. Can be: "
                "/local/file.js (www/ directory), "
                "/hacsfiles/component/file.js (HACS), "
                "https://cdn.example.com/card.js (external). "
                "Mutually exclusive with content."
            ),
        ] = None,
        resource_type: Annotated[
            Literal["module", "js", "css"],
            Field(
                description="Resource type: 'module' for ES6 modules (modern cards, default), "
                "'js' for legacy JavaScript (url mode only), 'css' for stylesheets"
            ),
        ] = "module",
        resource_id: Annotated[
            str | None,
            Field(
                description="Resource ID to update. If omitted, creates a new resource. "
                "Get IDs from ha_config_list_dashboard_resources()"
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Create or update a dashboard resource (inline code or external URL).

        Provide exactly one of:
        - content: Inline JavaScript or CSS code (embedded in the resource URL
          as a data: URI — no file storage or external hosting involved)
        - url: External resource URL (/local/, /hacsfiles/, or https://...)

        INLINE MODE (content=):
        - Custom card code written inline
        - CSS styling for dashboards
        - Self-contained files up to ~128KB
        - URLs are deterministic (same content = same URL)
        - Content must be self-contained: a data: URI has no base URL, so
          relative imports inside a module and relative url() references
          inside CSS cannot resolve (use fully-qualified URLs instead)
        - Supports 'module' and 'css' types only (not 'js')

        URL MODE (url=):
        - Files in /config/www/ directory (/local/...)
        - HACS-installed cards (/hacsfiles/...)
        - External CDN resources (https://...)
        - Supports all types: 'module', 'js', 'css'

        RESOURCE TYPES:
        - module: ES6 JavaScript modules (recommended for custom cards)
        - js: Legacy JavaScript files (older custom cards, url mode only)
        - css: CSS stylesheets (themes, global styles)

        EXAMPLES:

        Inline custom card:
        ha_config_set_dashboard_resource(
            content=\"\"\"
            class MyCard extends HTMLElement {
              setConfig(config) { this.config = config; }
              set hass(hass) {
                this.innerHTML = `<ha-card>Hello ${hass.states[this.config.entity]?.state}</ha-card>`;
              }
            }
            customElements.define('my-card', MyCard);
            \"\"\",
            resource_type="module"
        )

        Add custom card from www/ directory:
        ha_config_set_dashboard_resource(
            url="/local/my-custom-card.js",
            resource_type="module"
        )

        Add HACS card (after installing via ha_manage_hacs(action='download')):
        ha_config_set_dashboard_resource(
            url="/hacsfiles/lovelace-mushroom/mushroom.js",
            resource_type="module"
        )

        Update existing resource:
        ha_config_set_dashboard_resource(
            url="/local/my-card-v2.js",
            resource_type="module",
            resource_id="abc123"
        )

        Note: After adding a resource, clear browser cache or hard refresh
        (Ctrl+Shift+R) to load changes.
        """
        # Validate: exactly one of content or url must be provided
        if content is not None and url is not None:
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message="Provide either 'content' (inline code) or 'url' (external), not both",
                    suggestions=[
                        "Use content= for inline JavaScript/CSS code",
                        "Use url= for /local/, /hacsfiles/, or https:// resources",
                    ],
                )
            )

        if content is None and url is None:
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message="Either 'content' (inline code) or 'url' (external) is required",
                    suggestions=[
                        "Use content= for inline JavaScript/CSS code",
                        "Use url= for /local/, /hacsfiles/, or https:// resources",
                    ],
                )
            )

        if content is not None:
            return await self._set_inline_resource(content, resource_type, resource_id)

        # Hand-built data: URLs would bypass every inline-mode guard (empty /
        # size / type checks and the #1072 YAML-misroute rejection) now that
        # the inline format is documented in this docstring. Route callers to
        # content=, where those guards run.
        if url is not None and url[:5].lower() == "data:":
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message="data: URLs cannot be registered via url= — "
                    "pass the code itself via content= instead",
                    suggestions=[
                        "Use content=<your JS/CSS source> (the tool builds the data: URI)",
                        "url= is for /local/, /hacsfiles/, and https:// resources",
                    ],
                )
            )
        return await self._set_url_resource(url, resource_type, resource_id)

    def _raise_ha_config_yaml_misroute(
        self, detected_yaml: str, resource_type: str
    ) -> NoReturn:
        """Raise the structured error for HA-config YAML misrouted as inline resource content."""
        right_tool = _HA_CONFIG_YAML_MARKERS[detected_yaml]
        suggestions = ["This tool stores Lovelace JavaScript/CSS resources only"]
        if right_tool:
            suggestions.insert(
                0,
                f"For `{detected_yaml}:` configuration, use {right_tool} instead",
            )
        elif detected_yaml == "scene":
            suggestions.insert(
                0,
                "Scene configuration tools are tracked in #995; "
                "until they ship, scenes can only be created via the HA UI",
            )
        else:
            suggestions.insert(
                0,
                f"No direct tool exists for `{detected_yaml}:` config; "
                "configure it via the HA UI or YAML packages",
            )
        raise_tool_error(
            create_error_response(
                code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                message=(
                    f"Content starts with HA-configuration YAML "
                    f"(`{detected_yaml}:`) — this tool only accepts Lovelace "
                    f"JavaScript or CSS resources, not Home Assistant config "
                    f"(see issue #1072)."
                ),
                context={
                    "detected_marker": f"{detected_yaml}:",
                    "resource_type": resource_type,
                },
                suggestions=suggestions,
            )
        )

    async def _set_inline_resource(
        self,
        content: str,
        resource_type: str,
        resource_id: str | None,
    ) -> dict[str, Any]:
        """Create or update an inline dashboard resource."""
        if not content.strip():
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message="Content cannot be empty",
                )
            )

        if resource_type == "js":
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message="Inline content does not support resource_type='js'",
                    suggestions=[
                        "Use resource_type='module' for ES6 JavaScript (recommended)",
                        "Use url= mode with resource_type='js' for legacy files",
                    ],
                )
            )

        # Catch the misroute where LLMs pick this tool to create a scene /
        # automation / helper / ... by passing HA-config YAML as `content`.
        # The tool only stores Lovelace JS/CSS — YAML lands as a Lovelace
        # module, creating orphaned, unreachable entities. See #1072.
        detected_yaml = _detect_ha_config_yaml(content)
        if detected_yaml is not None:
            self._raise_ha_config_yaml_misroute(detected_yaml, resource_type)

        content_bytes = content.encode("utf-8")
        content_size = len(content_bytes)

        if content_size > MAX_CONTENT_SIZE:
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message=f"Content too large: {content_size:,} bytes (max {MAX_CONTENT_SIZE:,})",
                    context={"size": content_size},
                    suggestions=[
                        "Minify the code to reduce size",
                        "Split into multiple smaller modules",
                        "Use url= with a /local/ path for larger files",
                    ],
                )
            )

        if resource_id is not None:
            await self._reject_truncated_preview_resave(content, resource_id)

        resource_url = _data_uri_for(content, resource_type)

        try:
            result, action = await self._upsert_resource(
                resource_id, resource_url, resource_type
            )

            error_msg = _check_ws_error(result)
            if error_msg:
                raise_tool_error(
                    create_error_response(
                        code=ErrorCode.SERVICE_CALL_FAILED,
                        message=str(error_msg),
                        context={"action": action},
                    )
                )

            new_resource_id = _extract_resource_id(result, resource_id)

            logger.info(
                f"Inline dashboard resource {action}: id={new_resource_id}, "
                f"type={resource_type}, size={content_size}"
            )

            return {
                "success": True,
                "action": action,
                "resource_id": new_resource_id,
                "resource_type": resource_type,
                "size": content_size,
                "note": "Clear browser cache or hard refresh to load changes",
            }
        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error setting inline dashboard resource: {e}")
            exception_to_structured_error(
                e,
                context={
                    "tool": "ha_config_set_dashboard_resource",
                    "action": "update" if resource_id else "create",
                },
                suggestions=[
                    "Ensure Home Assistant is running and accessible",
                    "Check that you have admin permissions",
                ],
            )
            # ``exception_to_structured_error`` always raises (NoReturn); this
            # explicit raise makes the function's exit unambiguous (no implicit
            # ``return None`` fall-through) and is never reached at runtime.
            raise

    async def _reject_truncated_preview_resave(
        self, content: str, resource_id: str
    ) -> None:
        """Refuse to overwrite an inline resource with its own truncated preview.

        The list tool's default output carries ``_preview`` — the first
        ``_PREVIEW_LENGTH`` characters plus ``"..."``. An agent that re-saves
        a resource using that string as ``content`` would silently replace
        the user's card with a syntactically broken fragment. Only content
        shaped exactly like a preview triggers the (one WS round-trip) check
        against the stored resource, so normal updates pay nothing.
        """
        if not (len(content) == _PREVIEW_LENGTH + 3 and content.endswith("...")):
            return
        try:
            result = await self._client.send_websocket_message(
                {"type": "lovelace/resources"}
            )
        except Exception:
            # Best-effort guard: a listing hiccup must not block the update;
            # a real connectivity problem fails the upsert itself right after.
            return
        resources = result.get("result") if isinstance(result, dict) else result
        if not isinstance(resources, list):
            return
        for res in resources:
            if str(res.get("id")) != str(resource_id):
                continue
            url = res.get("url", "")
            stored = None
            if isinstance(url, str):
                stored = _decode_data_uri(url) or _decode_legacy_worker_url(url)
            if (
                stored
                and len(stored) > _PREVIEW_LENGTH
                and content == stored[:_PREVIEW_LENGTH] + "..."
            ):
                raise_tool_error(
                    create_error_response(
                        code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                        message="Content is the truncated 150-char preview from "
                        "ha_config_list_dashboard_resources, not the full "
                        "resource code — saving it would destroy the resource",
                        context={"resource_id": resource_id},
                        suggestions=[
                            "Fetch the full code with ha_config_list_dashboard_resources(include_content=True)",
                            "Then re-save with the complete content",
                        ],
                    )
                )
            return

    async def _set_url_resource(
        self,
        url: str | None,
        resource_type: str,
        resource_id: str | None,
    ) -> dict[str, Any]:
        """Create or update an external URL dashboard resource."""
        try:
            result, action = await self._upsert_resource(
                resource_id, url, resource_type
            )

            error_msg = _check_ws_error(result)
            if error_msg:
                error_str = str(error_msg).lower()
                if "already exists" in error_str or "duplicate" in error_str:
                    raise_tool_error(
                        create_error_response(
                            code=ErrorCode.SERVICE_CALL_FAILED,
                            message="Resource with this URL already exists",
                            context={"action": action, "url": url},
                            suggestions=[
                                "Use ha_config_list_dashboard_resources() to find existing resource",
                                "Provide resource_id to update the existing resource",
                            ],
                        )
                    )
                raise_tool_error(
                    create_error_response(
                        code=ErrorCode.SERVICE_CALL_FAILED,
                        message=str(error_msg),
                        context={"action": action, "url": url},
                    )
                )

            new_resource_id = _extract_resource_id(result, resource_id)

            logger.info(
                f"Dashboard resource {action}: id={new_resource_id}, "
                f"type={resource_type}, url={url}"
            )

            return {
                "success": True,
                "action": action,
                "resource_id": new_resource_id,
                "resource_type": resource_type,
                "url": url,
                "note": "Clear browser cache or hard refresh to load changes",
            }
        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error setting dashboard resource: {e}")
            exception_to_structured_error(
                e,
                context={
                    "tool": "ha_config_set_dashboard_resource",
                    "action": "update" if resource_id else "create",
                    "url": url,
                },
                suggestions=[
                    "Ensure Home Assistant is running and accessible",
                    "Check that you have admin permissions",
                    "Verify the URL is correctly formatted",
                ],
            )
            # ``exception_to_structured_error`` always raises (NoReturn); this
            # explicit raise makes the function's exit unambiguous (no implicit
            # ``return None`` fall-through) and is never reached at runtime.
            raise

    async def _upsert_resource(
        self,
        resource_id: str | None,
        url: str | None,
        resource_type: str,
    ) -> tuple[dict[str, Any], str]:
        """Create or update a lovelace resource. Returns (result, action)."""
        # ``None`` stays the documented "create-new" sentinel; explicit
        # empty/whitespace ``resource_id`` would silently route to the
        # ``else`` create branch below and lose update intent (destructive
        # intent-loss class, same as the registry-metadata twins).
        if resource_id is not None:
            validate_identifier_not_empty(
                resource_id,
                "resource_id",
                suggestions=[
                    "Omit resource_id entirely to create a new resource",
                    "Pass a valid resource_id to update an existing resource",
                ],
                context={"action": "set"},
            )
        if resource_id:
            result = await self._client.send_websocket_message(
                {
                    "type": "lovelace/resources/update",
                    "resource_id": resource_id,
                    "url": url,
                    "res_type": resource_type,
                }
            )
            return result, "updated"
        else:
            result = await self._client.send_websocket_message(
                {
                    "type": "lovelace/resources/create",
                    "url": url,
                    "res_type": resource_type,
                }
            )
            return result, "created"

    @tool(
        name="ha_config_delete_dashboard_resource",
        tags={"Dashboards"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "title": "Delete Dashboard Resource",
        },
    )
    @with_auto_backup(domain="dashboard_resource", id_param="resource_id")
    @log_tool_usage
    async def ha_config_delete_dashboard_resource(
        self,
        resource_id: Annotated[
            str,
            Field(
                description="Resource ID to delete. Get from ha_config_list_dashboard_resources()"
            ),
        ],
    ) -> dict[str, Any]:
        """
        Delete a dashboard resource.

        Removes a resource from Home Assistant. The resource will no longer
        be loaded on dashboards.

        WARNING: Deleting a resource used by custom cards in your dashboards
        will cause those cards to fail to load.

        EXAMPLES:
        ha_config_delete_dashboard_resource(resource_id="abc123")

        Note: Use ha_config_list_dashboard_resources() to find resource IDs
        before deleting. Ensure no dashboards depend on the resource.
        """
        try:
            # Empty/whitespace would surface as a misleading HA delete-failure.
            validate_identifier_not_empty(
                resource_id,
                "resource_id",
                suggestions=[
                    "Pass a valid resource_id (use ha_config_list_dashboard_resources() to list)",
                ],
                context={"action": "delete"},
            )
            result = await self._client.send_websocket_message(
                {
                    "type": "lovelace/resources/delete",
                    "resource_id": resource_id,
                }
            )

            # Check for errors
            error_msg = _check_ws_error(result)
            if error_msg:
                error_str = str(error_msg).lower()
                if "not found" in error_str or "unable to find" in error_str:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.RESOURCE_NOT_FOUND,
                            f"Dashboard resource '{resource_id}' not found",
                            details=(
                                f"Resource '{resource_id}' not found. "
                                "Use ha_config_list_dashboard_resources() to see available resources."
                            ),
                            context={"action": "delete", "resource_id": resource_id},
                        )
                    )

                raise_tool_error(
                    create_error_response(
                        code=ErrorCode.SERVICE_CALL_FAILED,
                        message=f"Failed to delete dashboard resource: {error_msg}",
                        context={"action": "delete", "resource_id": resource_id},
                        suggestions=[
                            "Verify resource ID using ha_config_list_dashboard_resources()",
                            "Check that you have admin permissions",
                        ],
                    )
                )

            logger.info(f"Dashboard resource deleted: id={resource_id}")

            return {
                "success": True,
                "action": "delete",
                "resource_id": resource_id,
                "message": "Resource deleted successfully",
            }
        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error deleting dashboard resource: {e}")
            exception_to_structured_error(
                e,
                context={"action": "delete", "resource_id": resource_id},
                suggestions=[
                    "Verify resource ID using ha_config_list_dashboard_resources()",
                    "Check that you have admin permissions",
                ],
            )
            # ``exception_to_structured_error`` always raises (NoReturn); this
            # explicit raise makes the function's exit unambiguous (no implicit
            # ``return None`` fall-through) and is never reached at runtime.
            raise


def register_resources_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register dashboard resource tools."""
    register_tool_methods(mcp, ResourceTools(client))


def _process_resource_list(
    resources: list[Any], include_content: bool
) -> list[dict[str, Any]]:
    """Process raw resources, decoding inline URLs for preview.

    Current inline resources are ``data:`` URIs; resources created before
    the worker retirement (#2060) still point at the worker and get a
    migration hint. Both decode locally — no network involved.
    """
    processed = []
    for resource in resources:
        res = dict(resource)
        url = res.get("url", "")

        if isinstance(url, str) and _is_inline_url(url):
            content = _decode_data_uri(url)
            if not content:
                content = _decode_legacy_worker_url(url)
                if content:
                    res["_legacy_worker"] = True
                    res["_migration"] = (
                        "Hosted on the legacy Cloudflare worker. First fetch "
                        "the FULL code with ha_config_list_dashboard_resources("
                        "include_content=True), then re-save it with "
                        "ha_config_set_dashboard_resource(content=..., "
                        "resource_id=...) to convert this resource to a "
                        "self-contained data: URI. Never use _preview as "
                        "content — it is truncated."
                    )
            # Empty or undecodable payloads fall through with their real URL
            # visible — masking them as "[inline]" would hide exactly what a
            # user debugging a broken resource needs to see.
            if content:
                res["_inline"] = True
                res["_size"] = len(content.encode("utf-8"))

                if include_content:
                    res["_content"] = content
                else:
                    preview = content[:_PREVIEW_LENGTH]
                    if len(content) > _PREVIEW_LENGTH:
                        preview += "..."
                    res["_preview"] = preview

                res["url"] = "[inline]"

        processed.append(res)
    return processed


def _check_ws_error(result: Any) -> str | None:
    """Check a WebSocket result for errors. Returns error message or None."""
    if isinstance(result, dict) and not result.get("success", True):
        error = result.get("error", {})
        if isinstance(error, dict):
            msg: str = error.get("message", str(error)) or "Unknown error"
            return msg
        return str(error) or "Unknown error"
    return None


def _extract_resource_id(result: Any, fallback_id: str | None) -> str | None:
    """Extract resource ID from a WebSocket result."""
    resource_info = result.get("result") if isinstance(result, dict) else result
    if isinstance(resource_info, dict):
        return resource_info.get("id", fallback_id)
    return fallback_id
