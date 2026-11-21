"""
Service discovery tools for Home Assistant MCP server.

This module provides service listing and discovery capabilities,
allowing AI to explore available Home Assistant services/actions.
"""

import logging
import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response, create_validation_error
from .coercion import JSON_STRING_COERCION, parse_string_list_param
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .flow_translations import configured_language
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .response_helpers import (
    build_pagination_metadata,
    project_fields,
    result_fields_warning,
)
from .tool_hints import read_only_hints

logger = logging.getLogger(__name__)

# WS command name for the component's service catalog read (domain-scoped; see
# ``component_api`` module docstring for the capability-negotiation pattern this
# consumer follows).
WS_SERVICES_LIST = "ha_mcp_tools/services_list"


class ServiceDiscoveryTools:
    """Service discovery tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_list_services",
        tags={"Service & Device Control"},
        annotations=read_only_hints("List Available Services", open_world=False),
    )
    @log_tool_usage
    async def ha_list_services(
        self,
        domain: Annotated[
            str | None,
            Field(description="Filter by domain (e.g., 'light', 'switch', 'climate')."),
        ] = None,
        query: Annotated[
            str | None,
            Field(description="Search in service names and descriptions."),
        ] = None,
        limit: Annotated[
            int,
            Field(
                default=50,
                ge=1,
                le=200,
                description="Max services to return per page",
            ),
        ] = 50,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of services to skip for pagination",
            ),
        ] = 0,
        detail_level: Annotated[
            Literal["summary", "full"],
            Field(
                default="summary",
                description=(
                    "'summary': name, description, domain, service, target and response "
                    "when the service returns data. 'full': additionally include "
                    "parameter field schemas."
                ),
            ),
        ] = "summary",
        service_fields: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    'Project each service record to only the specified keys. E.g. ["name", '
                    '"description"] returns slim service records. None = full records. '
                    "Unknown keys yield empty records. Available keys: name, description, "
                    "domain, service, target (when present), response (when present), "
                    "fields (full mode only)."
                ),
            ),
        ] = None,
        fields: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Return only the specified top-level response keys to reduce response "
                    'size (e.g. ["services"]). None = full response. Available keys: '
                    "success, domains, services, total_count, count, offset, limit, "
                    "has_more, next_offset, detail_level, filters_applied."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """List available Home Assistant services with optional pagination and detail control.

        Discovers services/actions that can be called via ha_call_service.

        EXAMPLES:
        - Light services with full parameter details: ha_list_services(domain="light", detail_level="full")
        - Search: ha_list_services(query="temperature")
        - Next page: ha_list_services(offset=50)
        """
        parsed_fields: list[str] | None = None
        if fields is not None:
            try:
                parsed_fields = parse_string_list_param(
                    fields, "fields", allow_csv=True
                )
            except ValueError as exc:
                raise_tool_error(create_validation_error(str(exc), parameter="fields"))
        parsed_service_fields: list[str] | None = None
        if service_fields is not None:
            try:
                parsed_service_fields = parse_string_list_param(
                    service_fields, "service_fields", allow_csv=True
                )
                if (
                    parsed_service_fields is not None
                    and len(parsed_service_fields) == 0
                ):
                    raise ValueError("service_fields must contain at least one key")
            except ValueError as exc:
                raise_tool_error(
                    create_validation_error(str(exc), parameter="service_fields")
                )
        try:
            limit_int = limit
            offset_int = offset

            catalog_warnings: list[str] = []
            rest_services, translations = await _fetch_catalog_and_translations(
                self._client, domain, catalog_warnings
            )

            # Process and filter services
            result = _process_services(
                rest_services=rest_services,
                translations=translations,
                domain_filter=domain,
                query_filter=query,
                limit=limit_int,
                offset=offset_int,
                detail_level=detail_level,
                warnings=catalog_warnings,
            )

            if parsed_service_fields is not None and "services" in result:
                _orig_svc_vals = list(result["services"].values())
                result["services"] = {
                    k: {fk: fv for fk, fv in v.items() if fk in parsed_service_fields}
                    for k, v in result["services"].items()
                }
                _warn = result_fields_warning(
                    _orig_svc_vals,
                    list(result["services"].values()),
                    parsed_service_fields,
                    param_name="service_fields",
                )
                if _warn:
                    result.setdefault("warnings", []).append(_warn)

            _attach_warnings(result, catalog_warnings)

            return project_fields(result, parsed_fields)

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to list services: {e}")
            exception_to_structured_error(
                e,
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify WebSocket API is available",
                    "Try with a specific domain filter",
                ],
            )
            return (
                None  # exception_to_structured_error always raises; explicit for CodeQL
            )


def register_services_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register service discovery tools with the MCP server."""
    register_tool_methods(mcp, ServiceDiscoveryTools(client))


def _attach_warnings(result: dict[str, Any], warnings: list[str]) -> None:
    """Merge degradation warnings into a response's top-level ``warnings``.

    The catalog is REST-backed and still answers when the WebSocket is down,
    so without this the service descriptions just go missing and the response
    looks complete (#1947). Empty in, key omitted.
    """
    if warnings:
        result.setdefault("warnings", []).extend(warnings)


async def _fetch_catalog_and_translations(
    client: Any, domain: str | None, warnings: list[str] | None = None
) -> tuple[Any, dict[str, Any]]:
    """The ``(rest_services, translations)`` pair ``_process_services`` consumes.

    Tries the component (``domain``-scoped, one frame) first; falls back to the
    legacy REST ``get_services()`` + WS ``frontend/get_translations`` pair on any
    capability miss or fallback-eligible error. Both sources are Core's own
    ``async_get_all_descriptions`` output, so the server's shaping runs unchanged
    over either.
    """
    component_payload = await _fetch_services_list_via_component(client, domain=domain)
    if component_payload is not None:
        return component_payload["services"], component_payload["translations"]
    language, language_warning = await configured_language(
        client, "service descriptions"
    )
    if language_warning and warnings is not None:
        warnings.append(language_warning)
    rest_services = await client.get_services()
    translations = await _get_service_translations(client, language, warnings)
    return rest_services, translations


async def _fetch_services_list_via_component(
    client: Any, *, domain: str | None
) -> dict[str, Any] | None:
    """One ``ha_mcp_tools/services_list`` read; ``None`` ⇒ run the legacy path.

    Returns ``{"services": [...], "translations": {...}}`` — ``services`` is the
    REST ``/api/services`` list shape and ``translations`` the same flat
    ``frontend/get_translations`` resources map ``_get_service_translations``
    returns — so ``_process_services`` consumes either source unchanged.

    The command accepts only ``domain`` and an optional ``language``. This
    consumer threads ``domain`` alone (an exact match, identical semantics on
    both sides). ``query`` is not a command parameter: the exact
    concatenation-based filter stays here in ``_process_services``, applied to
    the full domain-scoped catalog, so ``domains`` still lists every domain that
    passes ``domain_filter``. ``language`` is not sent either — the component
    resolves ``hass.config.language`` itself, and an explicit wire value would
    override the instance's configured language. ``None`` on capability
    miss, downgrade (``unknown_command`` → invalidate the cached caps), or
    command error/timeout (logged) — the caller falls back to the legacy REST +
    WS translations fetch.

    Per the uniform transport-fallback taxonomy, a connection-establishment
    failure IS caught here and mapped to ``None`` (legacy fallback), like every
    component fetch helper. This tool's legacy path is the REST ``get_services()`` +
    a per-request WS bridge for translations, NOT the shared pooled WS — so a WS
    outage must not kill the tool when REST can still serve the catalog. The
    catch stays broad so no unexpected fault escapes and kills the tool; routing
    any
    non-command component failure back to the REST catalog fetch is safe here
    (mirrors ``get_component_caps``' own broad-catch precedent).
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "services_list"):
        return None
    kwargs: dict[str, Any] = {}
    if domain is not None:
        kwargs["domain"] = domain
    try:
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
        raw = await ws.send_command(WS_SERVICES_LIST, **kwargs)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; fell back to legacy: %r", WS_SERVICES_LIST, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        # DEVIATION (see docstring): the legacy path is REST + a per-request WS
        # bridge, NOT the shared pooled WS. A pooled-WS drop or a failed
        # (re)connect, both HomeAssistantConnectionError since #1947, must fall
        # back to REST rather than kill the tool.
        logger.warning(
            "%s connection error; falling back to REST legacy: %r",
            WS_SERVICES_LIST,
            exc,
        )
        return None
    result = raw.get("result")
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("services"), list)
        or not isinstance(result.get("translations"), dict)
    ):
        logger.debug(
            "%s returned a malformed result (services/translations wrong shape); "
            "falling back to legacy",
            WS_SERVICES_LIST,
        )
        return None
    return result


async def _get_service_translations(
    client: Any, language: str, warnings: list[str] | None = None
) -> dict[str, Any]:
    """
    Get service translations from Home Assistant via WebSocket.

    Uses the frontend/get_translations command to retrieve
    human-readable service names and descriptions in Core's configured language.
    """
    try:
        response = await client.send_websocket_message(
            {
                "type": "frontend/get_translations",
                "language": language,
                "category": "services",
            }
        )

        if (
            isinstance(response, dict)
            and response.get("success")
            and response.get("result")
        ):
            result = response["result"]
            if isinstance(result, dict):
                resources: dict[str, Any] = result.get("resources", {})
                return resources
        return {}

    except (
        HomeAssistantConnectionError,
        HomeAssistantAPIError,
        HomeAssistantAuthError,
        TimeoutError,
        OSError,
    ) as e:
        logger.warning(f"Failed to get service translations: {e}")
        # An empty map is also what an instance with no translations returns,
        # so a caller that wants to tell the two apart passes ``warnings``
        # and reports that the catalog came back without descriptions (#1947).
        if warnings is not None:
            warnings.append(f"service descriptions unavailable: {e}")
        return {}


def _process_services(
    rest_services: Any,
    translations: dict[str, Any],
    domain_filter: str | None = None,
    query_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
    detail_level: Literal["summary", "full"] = "summary",
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """
    Process raw service data into structured output.

    Args:
        rest_services: Raw services from REST API
        translations: Service translations from WebSocket
        domain_filter: Optional domain to filter by
        query_filter: Optional search query
        limit: Maximum number of services per page
        offset: Number of services to skip
        detail_level: 'summary' or 'full'
        warnings: Degradation warnings to extend when Core's field payload is
            not the mapping shape this shaping expects (the discarded metadata
            is labelled incomplete rather than silently dropped)

    Returns:
        Processed service dictionary with pagination metadata
    """
    services: dict[str, dict[str, Any]] = {}
    domains_seen: set[str] = set()
    incomplete_fields: list[str] = []

    # Handle both list and dict formats from REST API
    if isinstance(rest_services, list):
        # Format: [{"domain": "light", "services": {...}}, ...]
        service_data = rest_services
    elif isinstance(rest_services, dict):
        # Format: {"light": {"services": {...}}, ...}
        service_data = [
            {"domain": domain, "services": data.get("services", data)}
            for domain, data in rest_services.items()
        ]
    else:
        raise_tool_error(
            create_error_response(
                ErrorCode.INTERNAL_UNEXPECTED,
                "Unexpected service data format",
                suggestions=[
                    "Retry the request — this may be a transient issue",
                    "Check Home Assistant is running and responding correctly",
                ],
            )
        )

    query_lower = query_filter.lower() if query_filter else None

    for domain_entry in service_data:
        domain = domain_entry.get("domain", "")
        if not domain:
            continue

        # Apply domain filter
        if domain_filter and domain != domain_filter:
            continue

        domains_seen.add(domain)
        domain_services = domain_entry.get("services", {})

        for service_name, service_def in domain_services.items():
            service_key = f"{domain}.{service_name}"
            entry = _build_service_entry(
                domain,
                service_name,
                service_def,
                translations,
                query_lower,
                detail_level,
                incomplete=incomplete_fields if detail_level == "full" else None,
            )
            if entry is not None:
                services[service_key] = entry

    if incomplete_fields and warnings is not None:
        shown = ", ".join(incomplete_fields[:5])
        more = (
            f" (+{len(incomplete_fields) - 5} more)"
            if len(incomplete_fields) > 5
            else ""
        )
        warnings.append(
            "service field metadata incomplete: expected a mapping, got another "
            f"type at {shown}{more}"
        )

    # Sort domains alphabetically
    sorted_domains = sorted(domains_seen)

    # Apply pagination to the collected services
    all_keys = list(services.keys())
    total_count = len(all_keys)
    paginated_keys = all_keys[offset : offset + limit]
    paginated_services = {k: services[k] for k in paginated_keys}

    return {
        "success": True,
        "domains": sorted_domains,
        "services": paginated_services,
        **build_pagination_metadata(
            total_count, offset, limit, len(paginated_services)
        ),
        "detail_level": detail_level,
        "filters_applied": {
            "domain": domain_filter,
            "query": query_filter,
        },
    }


def _build_service_entry(
    domain: str,
    service_name: str,
    service_def: dict[str, Any],
    translations: dict[str, Any],
    query_lower: str | None,
    detail_level: Literal["summary", "full"],
    incomplete: list[str] | None = None,
) -> dict[str, Any] | None:
    """Build a single service entry, returning None if it doesn't match the query.

    ``translations`` is Core's flat resources map (``component.<domain>.services.
    <service>.name``), and ``name``/``description`` fall back to the text Core's
    own description carries for dynamic services (``script.*``, ``notify.*``).
    """
    prefix = f"component.{domain}.services.{service_name}"
    placeholders = service_def.get("description_placeholders")
    name = _apply_placeholders(
        _translation(translations, f"{prefix}.name")
        or service_def.get("name")
        or service_name.replace("_", " ").title(),
        placeholders,
    )
    description = _apply_placeholders(
        _translation(translations, f"{prefix}.description")
        or service_def.get("description")
        or "",
        placeholders,
    )

    # Apply query filter
    if query_lower:
        searchable = f"{domain}.{service_name} {name} {description}".lower()
        if query_lower not in searchable:
            return None

    entry: dict[str, Any] = {
        "name": name,
        "description": description,
        "domain": domain,
        "service": service_name,
    }

    # Include full field schemas only in 'full' detail mode
    if detail_level == "full":
        entry["fields"] = _process_service_fields(
            service_def.get("fields", {}),
            translations,
            prefix,
            placeholders,
            service_key=f"{domain}.{service_name}",
            incomplete=incomplete,
        )

    # Add target only if present
    target = service_def.get("target")
    if target is not None:
        entry["target"] = target

    # Core reports whether a service can return data; agents need this before
    # calling ha_call_service with return_response.
    response = service_def.get("response")
    if response is not None:
        entry["response"] = response

    return entry


def _translation(translations: Mapping[str, Any], key: str) -> str | None:
    """Return a flat translation string, or None when Core has no entry."""
    value = translations.get(key)
    return value if isinstance(value, str) and value else None


_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")


def _apply_placeholders(text: str, placeholders: Any) -> str:
    """Substitute Core's ``description_placeholders`` without touching literals.

    Only keys Core supplied are replaced, so a description containing JSON or an
    unknown placeholder survives unchanged instead of raising like ``str.format``.
    """
    if not text or not isinstance(placeholders, Mapping):
        return text

    def _replace(match: re.Match[str]) -> str:
        value = placeholders.get(match.group(1))
        return str(value) if value is not None else match.group(0)

    return _PLACEHOLDER_RE.sub(_replace, text)


def _process_service_fields(
    fields_def: Any,
    translations: Mapping[str, Any],
    prefix: str,
    placeholders: Any,
    *,
    service_key: str = "",
    incomplete: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Shape Core's field descriptions, recursing into section entries.

    Every key Core sends is copied through (``filter``, ``advanced``,
    ``collapsed``, ``default``, ``example``, ``required``, ``selector``), so no
    accepted-value rule is re-implemented here. Translations are added on top,
    and a section keeps its nested ``fields`` processed the same way. Core puts
    section field translations under the service's flat ``fields.<name>``
    namespace and the section's own name under ``sections.<name>``.

    ``example`` has its own translated key (``fields.<field>.example``), which
    the frontend prefers over the services.yaml value, so it is read the same
    way as the name and description; the key is omitted when neither exists.

    A payload shape that is not a mapping is discarded, never fatal. Its dotted
    path is appended to ``incomplete`` so the catalog can label the description
    incomplete instead of silently serving a shorter field list.
    """
    if not isinstance(fields_def, Mapping):
        if incomplete is not None:
            incomplete.append(f"{service_key}.fields" if service_key else "fields")
        return {}

    processed: dict[str, dict[str, Any]] = {}
    for field_name, field_info in fields_def.items():
        if not isinstance(field_info, Mapping):
            if incomplete is not None:
                incomplete.append(f"{service_key}.fields.{field_name}")
            continue

        is_section = isinstance(field_info.get("fields"), Mapping)
        translation_prefix = (
            f"{prefix}.sections.{field_name}"
            if is_section
            else f"{prefix}.fields.{field_name}"
        )
        entry = {
            key: value
            for key, value in field_info.items()
            if not (is_section and key == "fields")
        }
        entry["name"] = _apply_placeholders(
            _translation(translations, f"{translation_prefix}.name")
            or field_info.get("name")
            or field_name.replace("_", " ").title(),
            placeholders,
        )
        entry["description"] = _apply_placeholders(
            _translation(translations, f"{translation_prefix}.description")
            or field_info.get("description")
            or "",
            placeholders,
        )
        translated_example = _translation(translations, f"{translation_prefix}.example")
        if translated_example is not None:
            entry["example"] = _apply_placeholders(translated_example, placeholders)
        elif entry.get("example") is None:
            # Neither a translated nor a Core-supplied example: omit the key
            # rather than advertising an empty one.
            entry.pop("example", None)
        if is_section:
            entry["fields"] = _process_service_fields(
                field_info["fields"],
                translations,
                prefix,
                placeholders,
                service_key=service_key,
                incomplete=incomplete,
            )
        processed[field_name] = entry

    return processed
