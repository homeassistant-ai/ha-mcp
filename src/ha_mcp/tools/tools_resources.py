"""
Dashboard resource hosting tools for Home Assistant MCP server.

Provides tools for managing dashboard resources (custom cards, CSS, JS):
- Inline resources: Code embedded directly in the resource URL as a
  ``data:`` URI — stored in HA's own resource registry, served by nothing
- External resources: URLs to /local/ (incl. /local/community/), /hacsfiles/, or CDNs

Inline resources were previously served by a Cloudflare Worker that decoded
base64 content embedded in its URL, based on issue #71's claim that browsers
reject ``data:`` URIs for ES6 modules. That claim was wrong — HA's WS API
applies no URL-scheme validation, HA ships no CSP, and every layer loads
``data:`` modules and stylesheets fine (verified live; see #2060). Inline
content now goes straight into a ``data:`` URI: same storage location
(the resource registry), no third-party hop on dashboard loads.
"""

import logging
from typing import Annotated, Any, NoReturn

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..errors import ErrorCode, create_error_response
from .auto_backup import with_auto_backup
from .coercion import JSON_STRING_COERCION
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
    ws_failure_code,
)
from .resource_codec import (
    _DATA_URI_MIME,
    _MAX_CONTENT_BUDGET,
    MAX_CONTENT_SIZE,
    _check_ws_error,
    _data_uri_for,
    _decode_inline_content,
    _is_data_url,
    _is_preview_of,
    _looks_like_preview,
    _process_resource_list,
    _summarize_resources,
    content_sha256,
)
from .resource_patch import (
    MAX_EDITS,
    ResourceEdit,
    ResourceLookupError,
    apply_resource_edits,
    check_config_hash,
    find_resource,
    load_inline_resource,
)
from .response_helpers import build_pagination_metadata
from .tool_hints import read_only_hints, write_hints

logger = logging.getLogger(__name__)

# Core validates res_type against its RESOURCE_TYPES; a copy here would reject
# a type Core adds before Core sees it, so the parameter is a plain string.
_RESOURCE_TYPE = str

# Core registers lovelace/resources/{create,update,delete} only in storage
# resource mode; in YAML mode they answer unknown_command.
_YAML_MODE_MESSAGE = (
    "Dashboard resources are YAML-managed on this instance (lovelace: "
    "resources: in configuration.yaml), so they cannot be changed through the API"
)

# Emitted once per response (not per resource) when any listed resource
# still lives on the legacy worker.
_LEGACY_MIGRATION_HINT = (
    "Resources flagged '_legacy_worker' are still hosted on the legacy "
    "Cloudflare worker. To convert one: fetch its FULL code with "
    "ha_config_list_dashboard_resources(include_content=True), then re-save "
    "it with ha_config_set_dashboard_resource(content=..., resource_id=...). "
    "Never pass '_preview' as content — it is truncated and would destroy "
    "the resource."
)

# Top-level HA-config YAML keys that LLMs sometimes emit when they pick this
# tool (`ha_config_set_dashboard_resource`) to "create a scene/automation/...".
# This tool only stores Lovelace JS/CSS resources — the YAML payload would land
# as a Lovelace module, creating orphaned, unreachable HA entities (see #1072).
# Map: top-level key → the ha-mcp tool that creates that kind of config without
# YAML (None where only YAML configuration can).
_HA_CONFIG_YAML_MARKERS: dict[str, str | None] = {
    "automation": "ha_config_set_automation",
    "script": "ha_config_set_script",
    "scene": "ha_config_set_scene",
    "group": "ha_config_set_group",
    "input_boolean": "ha_config_set_helper(helper_type='input_boolean', ...)",
    "input_number": "ha_config_set_helper(helper_type='input_number', ...)",
    "input_select": "ha_config_set_helper(helper_type='input_select', ...)",
    "input_text": "ha_config_set_helper(helper_type='input_text', ...)",
    "input_datetime": "ha_config_set_helper(helper_type='input_datetime', ...)",
    "input_button": "ha_config_set_helper(helper_type='input_button', ...)",
    "template": "ha_config_set_helper(helper_type='template', ...) "
    "(trigger-based templates are YAML-only: ha_config_set_yaml when YAML "
    "config editing is enabled, otherwise the HA UI or YAML packages)",
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


class ResourceTools:
    """Dashboard resource hosting tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_config_list_dashboard_resources",
        tags={"Dashboards"},
        annotations=read_only_hints("List Dashboard Resources", open_world=True),
    )
    @log_tool_usage
    async def ha_config_list_dashboard_resources(
        self,
        include_content: Annotated[
            bool,
            Field(
                description='Include full decoded content for inline resources in the "_content" '
                "field. When False, a 150-char preview is shown instead. Rows past the "
                "per-response content budget carry '_content_truncated': True instead "
                "of '_content'; request a smaller page before copying content into an "
                "update."
            ),
        ] = False,
        include_hash: Annotated[
            bool,
            Field(
                description='Add "_config_hash" (SHA-256 hex digest of the decoded '
                "UTF-8 content) to each inline resource, e.g. to verify an edit or "
                "to pass as config_hash without fetching the content."
            ),
        ] = False,
        limit: Annotated[
            int,
            Field(
                default=100,
                ge=1,
                le=500,
                description="Max resources to return per page",
            ),
        ] = 100,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of resources to skip for pagination",
            ),
        ] = 0,
    ) -> dict[str, Any]:
        """List Lovelace dashboard resources (custom cards, themes, CSS/JS).

        Returns one page of registered resources; `total_count` and `has_more`
        report the full set, and `inline_count` and `by_type` summarise every
        resource, not just this page. Each resource has a unique ID for
        update/delete operations. For inline resources (created with
        ha_config_set_dashboard_resource(content=...)), shows a preview of the
        content instead of the full encoded URL to save tokens.
        `resource_mode` is Core's lovelace resource mode; in 'yaml' mode
        resources cannot be created, updated or deleted through the API.

        EXAMPLES:
        - Next page: ha_config_list_dashboard_resources(offset=100)
        - List with full content: ha_config_list_dashboard_resources(include_content=True)
        - Verify an edit by hash: ha_config_list_dashboard_resources(include_hash=True)

        Home Assistant 2026.6+ exposes resource management in the UI by default;
        API access works regardless of UI availability.
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

            by_type_counts, inline_count, decoded_by_index = _summarize_resources(
                resources, offset, offset + limit
            )

            total_count = len(resources)
            page = resources[offset : offset + limit]
            page_decoded = [decoded_by_index.get(offset + i) for i in range(len(page))]
            paginated, truncated_count = _process_resource_list(
                page, page_decoded, include_content, include_hash=include_hash
            )

            response: dict[str, Any] = {
                "success": True,
                "action": "list",
                "resources": paginated,
                **build_pagination_metadata(total_count, offset, limit, len(paginated)),
                "inline_count": inline_count,
                "by_type": by_type_counts,
            }
            if truncated_count:
                response["content_truncated"] = truncated_count
                response["content_truncation_note"] = (
                    f"{truncated_count} resource(s) exceeded the "
                    f"{_MAX_CONTENT_BUDGET:,}-byte per-response content budget and "
                    "carry '_content_truncated': True. Re-request them with a "
                    "smaller limit (e.g. limit=1 plus offset) to get their full "
                    "content — never save a truncated value back."
                )
            if any(r.get("_legacy_worker") for r in paginated):
                response["migration_hint"] = _LEGACY_MIGRATION_HINT
            resource_mode = await self._resource_mode()
            if resource_mode:
                response["resource_mode"] = resource_mode
                if resource_mode == "yaml":
                    response["note"] = _YAML_MODE_MESSAGE
            return response
        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error listing dashboard resources: {e}")
            exception_to_structured_error(
                e, context={"tool": "ha_config_list_dashboard_resources"}
            )
            # Unreachable (exception_to_structured_error is NoReturn).
            raise

    @tool(
        name="ha_config_set_dashboard_resource",
        tags={"Dashboards"},
        annotations=write_hints(
            "Set Dashboard Resource",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )
    @with_auto_backup(domain="dashboard_resource", id_param="resource_id")
    @log_tool_usage
    async def ha_config_set_dashboard_resource(
        self,
        content: Annotated[
            str | None,
            Field(description="JavaScript or CSS code to host inline."),
        ] = None,
        url: Annotated[
            str | None,
            Field(description="URL of the resource."),
        ] = None,
        resource_type: Annotated[
            _RESOURCE_TYPE,
            Field(
                description="Resource type, validated by Home Assistant: 'module' for ES6 "
                "modules (modern cards), 'js' for legacy JavaScript (older custom cards), "
                "'css' for stylesheets (themes, global styles), 'html' for HTML imports"
            ),
        ] = "module",
        resource_id: Annotated[
            str | None,
            Field(
                description="Resource ID to update. If omitted, creates a new resource. "
                "Get IDs from ha_config_list_dashboard_resources()"
            ),
        ] = None,
        edits: Annotated[
            list[ResourceEdit] | None,
            JSON_STRING_COERCION,
            Field(
                min_length=1,
                max_length=MAX_EDITS,
                description="Search/replace edits for an existing inline resource, "
                "applied in order. Requires resource_id; resource_type is ignored "
                "and the stored type is kept.",
            ),
        ] = None,
        config_hash: Annotated[
            str | None,
            Field(
                description="With edits: refuse the patch unless the stored content "
                "has this hash when read, just before the write (the list's "
                "_config_hash with include_hash=True, or a previous write "
                "response's config_hash). A preflight "
                "check only: a change made by another client after that read is "
                "not detected and can be overwritten."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Create or update a dashboard resource (inline code or external URL).

        Provide exactly one of:
        - edits: Search/replace edits on an existing inline resource. Any
          failing edit (or a config_hash mismatch) aborts the whole call
          and saves nothing. Prefer this over content= for small changes to a
          large card: only the changed text is sent.
        - content: Inline JavaScript or CSS code (embedded in the resource URL
          as a data: URI — no file storage or external hosting involved).
          Supports 'module' and 'css' types only; URLs are
          deterministic (same content = same URL); up to ~128KB. Content must be
          self-contained: a data: URI has no base URL, so relative imports inside
          a module and relative url() references inside CSS cannot resolve (use
          fully-qualified URLs instead). If Home Assistant is behind a reverse
          proxy that injects a Content-Security-Policy without 'data:' in
          script-src/style-src, the browser blocks these resources: this call
          still succeeds and the card simply never renders. Register the code as
          a file and use url='/local/...' on such a deployment. (HA itself ships
          no CSP.)
        - url: External resource URL — /local/... (/config/www/; Marketplace
          cards: /local/community/...), /hacsfiles/... (HACS cards) or
          https://... (CDN). Supports all types: 'module', 'js', 'css', 'html'.

        EXAMPLES:
        - Inline: ha_config_set_dashboard_resource(content="class MyCard extends HTMLElement { ... } customElements.define('my-card', MyCard);", resource_type="module")
        - Store cards need no call: in storage mode HACS and the Marketplace register the resource on install (adding it again duplicates it); with YAML-mode resources, list it under lovelace: resources: in configuration.yaml instead.
        - Update: ha_config_set_dashboard_resource(url="/local/my-card-v2.js", resource_type="module", resource_id="abc123")
        - Patch: ha_config_set_dashboard_resource(resource_id="abc123", edits=[{"old": "color: red", "new": "color: blue"}], config_hash="<_config_hash from the list>")

        Inline writes return the stored content's config_hash.

        After adding a resource, a browser cache clear or hard refresh
        (Ctrl+Shift+R) is needed to load it.
        """
        if edits is not None:
            return await self._patch_inline_resource(
                resource_id, edits, config_hash, other_modes=(content, url)
            )
        if config_hash is not None:
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message="config_hash only applies together with edits",
                    suggestions=[
                        "Pass edits=[...] with resource_id, or drop config_hash"
                    ],
                )
            )

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
        if url is not None and _is_data_url(url):
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
        if right_tool:
            first = f"For `{detected_yaml}:` configuration, use {right_tool} instead"
        else:
            first = (
                f"`{detected_yaml}:` is YAML configuration: ha_config_set_yaml "
                "writes the keys it allows when YAML config editing is enabled; "
                "otherwise use the HA UI or YAML packages"
            )
        suggestions = [first, "This tool stores Lovelace JavaScript/CSS resources only"]
        raise_tool_error(
            create_error_response(
                code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                message=(
                    "Content starts with HA-configuration YAML "
                    f"(`{detected_yaml}:`) — this tool only accepts Lovelace "
                    "JavaScript or CSS resources, not Home Assistant config."
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
        resource_type: _RESOURCE_TYPE,
        resource_id: str | None,
        *,
        check_preview: bool = True,
    ) -> dict[str, Any]:
        """Create or update an inline dashboard resource.

        ``check_preview=False`` skips the truncated-preview guard. Only edit
        results pass it: they are derived from the stored content, not
        copied from a list response, so a preview-shaped result (e.g. a
        suffix deletion leaving ``_PREVIEW_LENGTH`` characters) is intended.
        """
        if not content.strip():
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message="Content cannot be empty",
                )
            )

        if resource_type not in _DATA_URI_MIME:
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message=f"Inline content does not support resource_type={resource_type!r}",
                    suggestions=[
                        "Use resource_type='module' for ES6 JavaScript (recommended)",
                        "Use url= mode for a file of another resource type",
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
                    context={"size": content_size, "limit": MAX_CONTENT_SIZE},
                    suggestions=[
                        "Minify the code to reduce size",
                        "Split into multiple smaller modules",
                        "Use url= with a /local/ path for larger files",
                    ],
                )
            )

        if resource_id is not None and check_preview:
            await self._reject_truncated_preview_resave(content, resource_id)

        resource_url = _data_uri_for(content, resource_type)

        try:
            result, action = await self._upsert_resource(
                resource_id, resource_url, resource_type
            )

            if _check_ws_error(result):
                await self._raise_write_error(
                    result, {"action": action, "resource_id": resource_id}
                )

            new_resource_id = _extract_resource_id(result, resource_id)
            _require_created_resource_id(new_resource_id, action)

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
                "config_hash": _stored_content_hash(result),
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
            )
            # Unreachable (exception_to_structured_error is NoReturn).
            raise

    async def _patch_inline_resource(
        self,
        resource_id: str | None,
        edits: list[ResourceEdit],
        config_hash: str | None,
        other_modes: tuple[str | None, str | None],
    ) -> dict[str, Any]:
        """Apply search/replace edits to a stored inline resource, all or nothing."""
        if any(mode is not None for mode in other_modes):
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message="Provide only one of 'edits', 'content' or 'url'",
                    suggestions=[
                        "Use edits= alone to change part of an existing inline resource",
                    ],
                )
            )
        if resource_id is None:
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.VALIDATION_MISSING_PARAMETER,
                    message="edits requires resource_id",
                    suggestions=[
                        "Get the ID from ha_config_list_dashboard_resources()",
                    ],
                )
            )
        validate_identifier_not_empty(
            resource_id,
            "resource_id",
            suggestions=["Pass the ID of the inline resource to patch"],
            context={"action": "patch"},
        )
        try:
            stored, resource_type = await load_inline_resource(
                self._client, resource_id
            )
        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error reading dashboard resource to patch: {e}")
            exception_to_structured_error(
                e,
                context={
                    "tool": "ha_config_set_dashboard_resource",
                    "action": "patch",
                    "resource_id": resource_id,
                },
            )
            # Unreachable (exception_to_structured_error is NoReturn).
            raise
        check_config_hash(stored, config_hash)
        patched = apply_resource_edits(stored, edits)
        response = await self._set_inline_resource(
            patched, resource_type, resource_id, check_preview=False
        )
        return {**response, "action": "patched", "applied": len(edits)}

    async def _reject_truncated_preview_resave(
        self, content: str, resource_id: str
    ) -> None:
        """Refuse to overwrite an inline resource with its own truncated preview.

        The list tool's default output carries ``_preview`` — the first
        ``_PREVIEW_LENGTH`` characters plus ``"..."``. An agent that re-saves
        a resource using that string as ``content`` would silently replace
        the user's card with a syntactically broken fragment.

        Only preview-shaped content triggers the (one WS round-trip) check,
        so normal updates pay nothing. When the check itself cannot run, it
        FAILS CLOSED: the write is refused rather than allowed unverified.
        A preview-shaped payload is already the suspicious case, and the
        costs are lopsided — a wrong refusal costs one retry with the real
        content, a wrong allow destroys the user's card and still reports
        ``success: True``.
        """
        if not _looks_like_preview(content):
            return

        def _unverifiable(detail: str) -> NoReturn:
            logger.warning(
                "Preview-writeback guard could not verify resource %s (%s); "
                "refusing the update rather than risking content loss",
                resource_id,
                detail,
            )
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.SERVICE_CALL_FAILED,
                    message=(
                        "Content looks like the truncated preview from "
                        "ha_config_list_dashboard_resources, and the stored "
                        f"resource could not be read back to confirm ({detail}). "
                        "Refusing the write: saving a preview would destroy the "
                        "resource."
                    ),
                    context={"resource_id": resource_id},
                    suggestions=[
                        "Fetch the full code with ha_config_list_dashboard_resources(include_content=True)",
                        "Re-save with the complete content (this check only triggers on preview-shaped content)",
                    ],
                )
            )

        try:
            found = await find_resource(self._client, resource_id)
        except ResourceLookupError as e:
            _unverifiable(e.detail)
        except Exception as e:  # noqa: BLE001
            _unverifiable(f"listing the resources failed: {e}")
        if found is None:
            return
        stored = found[1]
        if stored and _is_preview_of(content, stored):
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message="Content is the truncated preview from "
                    "ha_config_list_dashboard_resources, not the full "
                    "resource code — saving it would destroy the resource",
                    context={"resource_id": resource_id},
                    suggestions=[
                        "Fetch the full code with ha_config_list_dashboard_resources(include_content=True)",
                        "Then re-save with the complete content",
                    ],
                )
            )

    async def _set_url_resource(
        self,
        url: str | None,
        resource_type: _RESOURCE_TYPE,
        resource_id: str | None,
    ) -> dict[str, Any]:
        """Create or update an external URL dashboard resource."""
        try:
            result, action = await self._upsert_resource(
                resource_id, url, resource_type
            )

            if _check_ws_error(result):
                await self._raise_write_error(
                    result, {"action": action, "resource_id": resource_id, "url": url}
                )

            new_resource_id = _extract_resource_id(result, resource_id)
            _require_created_resource_id(new_resource_id, action)

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
            )
            # Unreachable (exception_to_structured_error is NoReturn).
            raise

    async def _resource_mode(self) -> str | None:
        """Core's lovelace ``resource_mode`` ('storage'/'yaml'), None if unreadable."""
        try:
            result = await self._client.send_websocket_message(
                {"type": "lovelace/info"}
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not read lovelace/info: {e}")
            return None
        info = result.get("result") if isinstance(result, dict) else None
        mode = info.get("resource_mode") if isinstance(info, dict) else None
        return mode if isinstance(mode, str) else None

    async def _raise_write_error(
        self, result: Any, context: dict[str, Any]
    ) -> NoReturn:
        """Raise Core's rejection of a resource write, routed on Core's error code."""
        error_msg = str(_check_ws_error(result))
        code = result.get("error_code") if isinstance(result, dict) else None
        resource_id = context.get("resource_id")
        if code == "not_found" and resource_id:
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Dashboard resource '{resource_id}' not found",
                    details=error_msg,
                    context=context,
                    suggestions=[
                        "Use ha_config_list_dashboard_resources() to see available resources"
                    ],
                )
            )
        if code == "unknown_command" and await self._resource_mode() == "yaml":
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    _YAML_MODE_MESSAGE,
                    details=error_msg,
                    context={**context, "resource_mode": "yaml"},
                    suggestions=[
                        "Edit lovelace: resources: in configuration.yaml, then run "
                        "the lovelace.reload_resources action"
                    ],
                )
            )
        raise_tool_error(
            create_error_response(
                ws_failure_code(result)
                if isinstance(result, dict)
                else ErrorCode.SERVICE_CALL_FAILED,
                error_msg,
                context={**context, "ha_error_code": code} if code else context,
            )
        )

    async def _upsert_resource(
        self,
        resource_id: str | None,
        url: str | None,
        resource_type: _RESOURCE_TYPE,
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
        annotations=write_hints(
            "Delete Dashboard Resource",
            destructive=True,
            idempotent=True,
            open_world=False,
        ),
    )
    @with_auto_backup(domain="dashboard_resource", id_param="resource_id")
    @log_tool_usage
    async def ha_config_delete_dashboard_resource(
        self,
        resource_id: Annotated[
            str,
            Field(description="Resource ID to delete."),
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

            if _check_ws_error(result):
                await self._raise_write_error(
                    result, {"action": "delete", "resource_id": resource_id}
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
                e, context={"action": "delete", "resource_id": resource_id}
            )
            # Unreachable (exception_to_structured_error is NoReturn).
            raise


def register_resources_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register dashboard resource tools."""
    register_tool_methods(mcp, ResourceTools(client))


def _extract_resource_id(result: Any, fallback_id: str | None) -> str | None:
    """Extract resource ID from a WebSocket result.

    On the create path ``fallback_id`` is None, so a response without an id
    would yield ``resource_id: None`` alongside ``success: True`` — leaving
    the caller no handle to update or delete what it just created. Callers
    guard that with ``_require_created_resource_id``.
    """
    resource_info = result.get("result") if isinstance(result, dict) else result
    if isinstance(resource_info, dict):
        return resource_info.get("id", fallback_id)
    return fallback_id


def _stored_content_hash(result: Any) -> str | None:
    """Hash the content Home Assistant stored, decoded from the write's result.

    ``lovelace/resources/create`` and ``/update`` return the stored item;
    hashing its URL rather than the string sent keeps the hash tied to what
    HA holds. None when the result carries no decodable inline URL.
    """
    item = result.get("result") if isinstance(result, dict) else None
    url = item.get("url") if isinstance(item, dict) else None
    decoded = _decode_inline_content(url) if isinstance(url, str) else None
    return content_sha256(decoded[0]) if decoded else None


def _require_created_resource_id(resource_id: str | None, action: str) -> None:
    """Fail a create that came back without an id rather than reporting success."""
    if action == "created" and not resource_id:
        raise_tool_error(
            create_error_response(
                code=ErrorCode.SERVICE_CALL_FAILED,
                message=(
                    "Home Assistant reported the resource was created but "
                    "returned no resource_id, so it cannot be updated or "
                    "deleted by this tool"
                ),
                context={"action": action},
                suggestions=[
                    "Run ha_config_list_dashboard_resources() to locate the resource and confirm whether it was created",
                ],
            )
        )
