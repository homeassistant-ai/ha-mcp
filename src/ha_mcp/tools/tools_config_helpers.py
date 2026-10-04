"""
Configuration management tools for Home Assistant helpers.

This module provides tools for listing, creating, updating, and removing
Home Assistant helper entities (input_button, input_boolean, input_select,
input_number, input_text, input_datetime, counter, timer, schedule).
"""

import logging
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response
from ..strict_bps import BestPracticeKeyParam
from .auto_backup import with_auto_backup
from .coercion import JSON_STRING_COERCION, parse_string_list_param
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .config_entry_flow import (
    FLOW_HELPER_TYPES,
    SUPPORTED_HELPERS,
)
from .config_helpers.create import _execute_create_simple_helper
from .config_helpers.describe import describe_helper_response
from .config_helpers.flow import _handle_flow_helper, _handle_set_config_subentry
from .config_helpers.listing import (
    _component_covers,
    _paginate_helpers_response,
    _raise_all_requires_component,
    _raise_flow_requires_component,
    _shape_component_helpers_response,
    shape_all_helpers_response,
)
from .config_helpers.registry import (
    _check_name_collision,
    _enrich_helpers_with_current_registry,
    _flatten_helper_list_result,
    validate_registry_ids,
)
from .config_helpers.schemas import (
    _SIMPLE_CONFIG_KEYS_DESCRIPTION,
    _attach_helper_skill,
)
from .config_helpers.typed_config import _core_schema_context, _prepare_typed_params
from .config_helpers.update import _execute_update_simple_helper
from .config_helpers.validation import (
    _validate_pre_dispatch_params,
    _validate_set_helper_action,
)
from .config_write_helpers import (
    augment_error_dict_with_skill_content,
    augment_tool_error_with_skill_content,
)
from .helpers import (
    HIDDEN_PARAM,
    clear_or_keep,
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .tool_hints import read_only_hints, write_hints

logger = logging.getLogger(__name__)


class HelperConfigTools:
    """Encapsulates helper configuration tools for ha_config_list_helpers and ha_config_set_helper."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_config_list_helpers",
        tags={"Helper Entities"},
        annotations=read_only_hints("List Helpers", open_world=False),
    )
    @log_tool_usage
    async def ha_config_list_helpers(
        self,
        helper_type: Annotated[
            Literal[
                "input_button",
                "input_boolean",
                "input_select",
                "input_number",
                "input_text",
                "input_datetime",
                "counter",
                "timer",
                "schedule",
                "zone",
                "person",
                "tag",
                "all",
            ]
            | SUPPORTED_HELPERS,
            Field(
                description=(
                    "Helper type to list. Storage types are listed on all "
                    "installs; flow-based types require the ha_mcp_tools "
                    "custom component. Pass 'all' to list every helper type in "
                    "one call (also requires the ha_mcp_tools component)."
                )
            ),
        ],
        limit: Annotated[
            int,
            Field(
                default=100,
                ge=1,
                le=500,
                description="Max helpers to return per page",
            ),
        ] = 100,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of helpers to skip for pagination",
            ),
        ] = 0,
        describe: Annotated[
            bool,
            Field(
                description=(
                    "Instead of listing, return the fields ha_config_set_helper "
                    "accepts in config for helper_type, read live from Home "
                    "Assistant (the same form the HA UI shows)."
                )
            ),
        ] = False,
        menu_choice: Annotated[
            str | None,
            Field(
                description=(
                    "describe only: sub-type of a menu-based helper "
                    "(template, group), e.g. 'sensor'."
                )
            ),
        ] = None,
        helper_id: Annotated[
            str | None,
            Field(
                description=(
                    "describe only: an existing helper (config entry id for "
                    "flow helpers, entity_id or id otherwise); each field then "
                    "carries its current value."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """List Home Assistant helpers of a specific type with their configurations.

        Returns one page of helpers; `total_count` and `has_more` report the full
        set. Each record carries the complete configuration for its helper:
        id (immutable storage key), entity_id (current — address the helper by
        this, where available), name (current display name), original_name
        (creation-time name), icon, type-specific settings, and area and label
        assignments.

        For a helper renamed in the UI, id/original_name keep the storage values while
        entity_id/name reflect the current entity registry (entity_id is the identifier
        ha_config_set_helper resolves against, so prefer it over id for a renamed helper).
        entity_id/original_name are present only for storage-collection helpers matched in
        the entity registry — types with no backing entity (e.g. tag), and every record when
        the registry read degrades, carry only id/name (a warning flags the degraded case).

        Storage types list what HA's ``{type}/list`` command returns: the
        storage-backed helpers (created via UI/API), not the YAML-defined ones.
        ``person`` is the exception — HA lists its YAML-configured persons
        alongside the storage ones, so both appear here.

        Flow-based types (template / group / utility_meter / derivative / etc.)
        require the ha_mcp_tools custom component (>= 1.1.0) and are served only
        through it. Requesting a flow type without the component returns a
        COMPONENT_NOT_INSTALLED error.

        With helper_type="all", each record carries its own ``helper_type``.
        This mode is component-only (there is no single built-in command that
        lists all types): without the ha_mcp_tools component it returns a
        COMPONENT_NOT_INSTALLED error rather than a partial or empty list.

        With describe=True, nothing is listed: the result is the field list
        for helper_type (name, type, required, options, defaults), taken from
        the running HA instance rather than from documentation. Call it before
        ha_config_set_helper to learn the config keys for a type.

        EXAMPLES:
        - List all counters: ha_config_list_helpers("counter")
        - List every helper type at once: ha_config_list_helpers("all")
        - Next page: ha_config_list_helpers("input_boolean", offset=100)
        - Fields for a template sensor:
          ha_config_list_helpers("template", describe=True, menu_choice="sensor")

        For detailed helper documentation, use ha_get_skill_guide.
        """
        if describe:
            return await describe_helper_response(
                self._client, helper_type, menu_choice, helper_id
            )
        # All-types mode: one merged component listing across every helper type.
        # No legacy equivalent exists (no single WS command enumerates all
        # types), so it is component-only — see ``_list_all_helpers``.
        if helper_type == "all":
            return _paginate_helpers_response(
                await self._list_all_helpers(), offset, limit
            )

        # Flow-based helper types have no ``{type}/list`` command, so only the
        # component's ``helpers_list`` can enumerate them: they are served
        # exclusively through the component path (never the legacy body, never a
        # silent empty). Storage/collection types keep the legacy fallback.
        is_flow = helper_type in FLOW_HELPER_TYPES

        # Prefer the custom component's in-process listing when it advertises
        # the capability: one WS round-trip that joins the entity registry, so
        # each record carries the current entity_id + display name (the #1794
        # stale-id fix, shipped additively) instead of only the storage id.
        # Fall back cleanly for storage types when the component is absent,
        # downlevel, or errors — the taxonomy lives in
        # ``_list_helpers_via_component``. The legacy body below is untouched.
        caps = await get_component_caps(self._client)
        if component_supports(caps, "helpers_list"):
            component_response = await self._list_helpers_via_component(
                helper_type, is_flow=is_flow
            )
            if component_response is not None:
                return _paginate_helpers_response(component_response, offset, limit)
        if is_flow:
            # No usable component path and the legacy body cannot serve flow
            # helpers: hard error rather than an empty or misleading list.
            _raise_flow_requires_component(helper_type)
        try:
            result = await self._client.send_websocket_message(
                {"type": f"{helper_type}/list"}
            )
            if result.get("success"):
                # Flatten first: person/list returns {"storage": [...],
                # "config": [...]} rather than a flat list, so a raw
                # result["result"] would be a dict here — breaking both the
                # count and the registry enrichment below (which iterates
                # records). _flatten_helper_list_result normalises both shapes.
                items = _flatten_helper_list_result(result)
                warnings = await _enrich_helpers_with_current_registry(
                    self._client, helper_type, items
                )
                response: dict[str, Any] = {
                    "success": True,
                    "helper_type": helper_type,
                    "count": len(items),
                    "helpers": items,
                    "message": f"Found {len(items)} {helper_type} helper(s)",
                }
                if warnings:
                    response["warnings"] = warnings
                return _paginate_helpers_response(response, offset, limit)
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to list helpers: {result.get('error', 'Unknown error')}",
                    context={"helper_type": helper_type},
                )
            )
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error listing helpers: {e}")
            exception_to_structured_error(
                e,
                context={"helper_type": helper_type},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                    "Use ha_search(domain_filter='input_*') as alternative",
                ],
            )
            return (
                None  # exception_to_structured_error always raises; explicit for CodeQL
            )
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    async def _list_helpers_via_component(
        self, helper_type: str, *, is_flow: bool
    ) -> dict[str, Any] | None:
        """Serve ha_config_list_helpers from the component; ``None`` ⇒ legacy.

        Error taxonomy (component_api design § 4), with a flow-helper override
        (a flow type has no legacy path, so any component failure is fatal):

        - ``unknown_command`` (the cached positive caps went stale after a
          component downgrade): invalidate the caps. For a storage type, return
          ``None`` so the caller falls through to the byte-identical legacy
          body, **silently**. For a flow type, raise the component-required
          error.
        - any other ``HomeAssistantCommandError`` (a component handler bug) or a
          ``HomeAssistantCommandTimeout`` (the component WS list timed out):
          for a storage type, serve the correct result from the legacy WS list,
          append a ``warnings[]`` entry, and ``log.warning``. For a flow type,
          raise the component-required error — no legacy fallback exists.
        - ``HomeAssistantConnectionError`` (pooled-WS drop) or the plain
          ``Exception`` ``get_websocket_client()`` raises on a failed (re)connect:
          for a storage type, the legacy ``{helper_type}/list`` body is attempted,
          with a ``warnings[]`` entry + ``log.warning``. It rides the
          ``send_websocket_message`` bridge over the SAME pooled connection
          (``WebSocketManager`` keys one client per url/token/verify_ssl), so it
          recovers a component-side fault but not a dead socket — there the bridge
          raises in turn (#1947) and the failure surfaces instead of being served
          as a short list. For a flow type — which has no legacy body — the
          transport failure re-raises to the tool's structured-error handler.

        On a successful response the type must also be in the component's
        ``covered_types`` (see :func:`_component_covers`): a type the response
        could not authoritatively enumerate (``tag``, or any type on an older
        component with no ``covered_types``) is handled like a component miss —
        storage falls back to legacy silently, flow raises.
        """
        try:
            raw = await self._send_component_helpers_list(helper_type, is_flow=is_flow)
        except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
            unknown = is_unknown_command(exc)
            if unknown:
                invalidate_caps(self._client)
            if is_flow:
                logger.warning(
                    "ha_mcp_tools/helpers_list failed for flow type %r: %r",
                    helper_type,
                    exc,
                )
                _raise_flow_requires_component(helper_type)
            if unknown:
                return None
            legacy = await self._legacy_helper_list(helper_type)
            legacy.setdefault("warnings", []).append(
                f"component helpers_list path failed ({exc}); served via legacy path"
            )
            logger.warning(
                "ha_mcp_tools/helpers_list failed; fell back to legacy: %r", exc
            )
            return legacy
        except Exception as exc:
            # Transport/establishment failure — a pooled-WS drop or a failed
            # (re)connect, both HomeAssistantConnectionError. A storage type
            # still attempts the legacy
            # `{helper_type}/list` body: it rides the same pooled connection, so it
            # recovers a component-side fault but not a dead socket, where the
            # bridge raises in turn (#1947). A flow type has NO legacy body, so its
            # transport failure re-raises to the tool's structured-error handler.
            if is_flow:
                raise
            legacy = await self._legacy_helper_list(helper_type)
            legacy.setdefault("warnings", []).append(
                f"component helpers_list connection error ({exc}); "
                "served via legacy path"
            )
            logger.warning(
                "ha_mcp_tools/helpers_list connection error; fell back to legacy: %r",
                exc,
            )
            return legacy
        result = raw.get("result") or {}
        if not _component_covers(result, helper_type):
            # The component did not authoritatively enumerate this type (tag has
            # no state entity for the from-states scan; or an older component
            # sent no covered_types). Don't trust a partial/empty list: storage
            # types fall back to the legacy path silently; a flow type (always
            # covered when include_flow_helpers=True) raises the same
            # component-required error rather than emptying out.
            if is_flow:
                _raise_flow_requires_component(helper_type)
            return None
        return _shape_component_helpers_response(helper_type, result)

    async def _send_component_helpers_list(
        self, helper_type: str, *, is_flow: bool
    ) -> dict[str, Any]:
        """Send one ``ha_mcp_tools/helpers_list`` command over the per-client WS.

        Requests only the single ``helper_type`` and sets
        ``include_flow_helpers`` to match its universe — ``True`` for a
        flow-based type (so the component returns its config-entry-backed
        record), ``False`` for a storage/collection type.
        """
        ws = await get_websocket_client(
            url=self._client.base_url,
            token=self._client.token,
            verify_ssl=getattr(self._client, "verify_ssl", None),
        )
        return await ws.send_command(
            "ha_mcp_tools/helpers_list",
            helper_types=[helper_type],
            include_flow_helpers=is_flow,
        )

    async def _legacy_helper_list(self, helper_type: str) -> dict[str, Any]:
        """Legacy ``{helper_type}/list`` success envelope, for the § 4 #3 fallback.

        A copy of the tool's inline legacy success path, kept separate rather
        than extracted from that body. It flattens and joins the entity registry
        (issue #1945) exactly like the inline path, so a renamed helper served on
        this fallback carries its current entity_id/name; the caller additionally
        appends a ``warnings[]`` entry flagging that the component path was used.
        """
        result = await self._client.send_websocket_message(
            {"type": f"{helper_type}/list"}
        )
        if not result.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to list helpers: {result.get('error', 'Unknown error')}",
                    context={"helper_type": helper_type},
                )
            )
        # Flatten like the inline body: person/list returns {"storage": [...],
        # "config": [...]} rather than a flat list, so a raw result["result"]
        # would be a dict here — breaking count, the pagination slice and the
        # all-types merge, which all expect a list of records.
        items = _flatten_helper_list_result(result)
        # Join the entity registry like the inline body (issue #1945): without
        # this a renamed helper served on the component-error fallback keeps its
        # stale storage id/name, the same #1794 staleness the inline path fixes.
        enrich_warnings = await _enrich_helpers_with_current_registry(
            self._client, helper_type, items
        )
        response: dict[str, Any] = {
            "success": True,
            "helper_type": helper_type,
            "count": len(items),
            "helpers": items,
            "message": f"Found {len(items)} {helper_type} helper(s)",
        }
        if enrich_warnings:
            response["warnings"] = enrich_warnings
        return response

    async def _list_all_helpers(self) -> dict[str, Any]:
        """Serve ``helper_type="all"``: one merged component listing, or a hard error.

        All-types mode has no legacy equivalent, so it is component-only
        (mirroring the flow-helper precedent). When the component advertises
        ``helpers_list`` it serves the merged listing; otherwise — or if the
        component call fails — this raises COMPONENT_NOT_INSTALLED via
        :func:`_raise_all_requires_component` rather than returning an empty list.
        A raw transport failure (e.g. the WS cannot connect right after an HA
        restart) becomes the structured error the rest of the tool uses — never
        an unclassified exception.
        """
        response: dict[str, Any] | None = None
        try:
            caps = await get_component_caps(self._client)
            if component_supports(caps, "helpers_list"):
                response = await self._all_helpers_via_component()
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={"helper_type": "all"},
                suggestions=[
                    "Home Assistant may be restarting or unreachable — retry shortly",
                ],
            )
        if response is None:
            _raise_all_requires_component()
        return response

    async def _all_helpers_via_component(self) -> dict[str, Any] | None:
        """Serve all-types from the component; ``None`` ⇒ raise component-required.

        There is no legacy all-types path, so — unlike single-type storage
        listing — every component failure resolves to the same hard error the
        caller raises (mirroring the flow-helper taxonomy). ``unknown_command``
        additionally invalidates the now-stale positive caps.
        """
        try:
            raw = await self._send_component_all_helpers()
        except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
            if is_unknown_command(exc):
                invalidate_caps(self._client)
            logger.warning("ha_mcp_tools/helpers_list (all) failed: %r", exc)
            return None
        return await shape_all_helpers_response(
            raw.get("result") or {}, self._legacy_helper_list
        )

    async def _send_component_all_helpers(self) -> dict[str, Any]:
        """Send one all-types ``ha_mcp_tools/helpers_list`` command (no type filter).

        Omitting ``helper_types`` makes the component return every storage +
        flow helper it can enumerate in a single round-trip
        (``type_filter=None`` component-side).
        """
        ws = await get_websocket_client(
            url=self._client.base_url,
            token=self._client.token,
            verify_ssl=getattr(self._client, "verify_ssl", None),
        )
        return await ws.send_command(
            "ha_mcp_tools/helpers_list",
            include_flow_helpers=True,
        )

    @tool(
        name="ha_config_set_helper",
        tags={"Helper Entities"},
        annotations=write_hints(
            "Create or Update Helper",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )
    @with_auto_backup(
        domain_fn=lambda kw: f"helper_{kw.get('helper_type', 'unknown')}",
        id_fn=lambda kw: str(
            kw.get("helper_id") or kw.get("entry_id") or kw.get("subentry_id") or ""
        ),
    )
    @log_tool_usage
    async def ha_config_set_helper(
        self,
        helper_type: Annotated[
            Literal[
                "counter",
                "config_subentry",
                "derivative",
                "filter",
                "generic_hygrostat",
                "generic_thermostat",
                "group",
                "history_stats",
                "input_boolean",
                "input_button",
                "input_datetime",
                "input_number",
                "input_select",
                "input_text",
                "integration",
                "min_max",
                "mold_indicator",
                "person",
                "random",
                "schedule",
                "statistics",
                "switch_as_x",
                "tag",
                "template",
                "threshold",
                "timer",
                "tod",
                "trend",
                "utility_meter",
                "zone",
            ],
            Field(description="Type of helper entity to create or update"),
        ],
        name: Annotated[
            str | None,
            Field(
                description=(
                    "Display name for simple/flow helper creation. Optional on helper update. "
                    "Ignored for helper_type='config_subentry', which uses "
                    "entry_id/subentry_type/subentry_id instead. For flow-based "
                    "helper updates (template, group, utility_meter, ...), this is "
                    "ignored because options flows don't expose renaming; change the "
                    "resulting entity's display name with ha_set_entity(name=...)."
                ),
                default=None,
            ),
        ] = None,
        helper_id: Annotated[
            str | None,
            Field(
                description="Bare ID ('my_button') or full entity ID ('input_button.my_button'). Omit to create a new helper.",
                default=None,
            ),
        ] = None,
        entry_id: Annotated[
            str | None,
            Field(
                description=(
                    "Parent config entry ID when helper_type='config_subentry'. "
                    "Use ha_get_integration() to find entry IDs."
                ),
                default=None,
            ),
        ] = None,
        subentry_type: Annotated[
            str | None,
            Field(
                description=(
                    "Integration-defined subentry type when "
                    "helper_type='config_subentry'."
                ),
                default=None,
            ),
        ] = None,
        subentry_id: Annotated[
            str | None,
            Field(
                description=(
                    "Existing config subentry ID to reconfigure when "
                    "helper_type='config_subentry'."
                ),
                default=None,
            ),
        ] = None,
        show_advanced_options: Annotated[
            bool,
            Field(
                description=(
                    "When helper_type='config_subentry', ask older Home "
                    "Assistant versions to expose advanced flow options. No-op "
                    "on HA 2026.6+; pending removal before HA 2027.6."
                ),
                default=False,
            ),
        ] = False,
        icon: Annotated[
            str | None,
            Field(
                description="Material Design Icon (e.g., 'mdi:bell'); '' or ' ' clears it",
                default=None,
            ),
        ] = None,
        area_id: Annotated[
            str | None,
            Field(
                description="Area ID for the helper; '' or ' ' clears it", default=None
            ),
        ] = None,
        labels: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(description="Labels to categorize the helper", default=None),
        ] = None,
        # Type fields for SIMPLE helpers, also accepted inside `config`; hidden from
        # the schema so only `config` documents them.
        min_value: Annotated[
            float | None,
            HIDDEN_PARAM,
            Field(
                default=None,
                validation_alias=AliasChoices("min_value", "min", "minimum"),
            ),
        ] = None,
        max_value: Annotated[
            float | None,
            HIDDEN_PARAM,
            Field(
                default=None,
                validation_alias=AliasChoices("max_value", "max", "maximum"),
            ),
        ] = None,
        step: Annotated[float | None, HIDDEN_PARAM] = None,
        unit_of_measurement: Annotated[
            str | None,
            HIDDEN_PARAM,
            Field(
                default=None,
                validation_alias=AliasChoices("unit_of_measurement", "unit"),
            ),
        ] = None,
        options: Annotated[
            str | list[str] | None, HIDDEN_PARAM, JSON_STRING_COERCION
        ] = None,
        initial: Annotated[str | bool | int | float | None, HIDDEN_PARAM] = None,
        mode: Annotated[str | None, HIDDEN_PARAM] = None,
        has_date: Annotated[bool | None, HIDDEN_PARAM] = None,
        has_time: Annotated[bool | None, HIDDEN_PARAM] = None,
        restore: Annotated[bool | None, HIDDEN_PARAM] = None,
        duration: Annotated[str | int | float | None, HIDDEN_PARAM] = None,
        monday: Annotated[
            list[dict[str, Any]] | None, HIDDEN_PARAM, JSON_STRING_COERCION
        ] = None,
        tuesday: Annotated[
            list[dict[str, Any]] | None, HIDDEN_PARAM, JSON_STRING_COERCION
        ] = None,
        wednesday: Annotated[
            list[dict[str, Any]] | None, HIDDEN_PARAM, JSON_STRING_COERCION
        ] = None,
        thursday: Annotated[
            list[dict[str, Any]] | None, HIDDEN_PARAM, JSON_STRING_COERCION
        ] = None,
        friday: Annotated[
            list[dict[str, Any]] | None, HIDDEN_PARAM, JSON_STRING_COERCION
        ] = None,
        saturday: Annotated[
            list[dict[str, Any]] | None, HIDDEN_PARAM, JSON_STRING_COERCION
        ] = None,
        sunday: Annotated[
            list[dict[str, Any]] | None, HIDDEN_PARAM, JSON_STRING_COERCION
        ] = None,
        latitude: Annotated[float | None, HIDDEN_PARAM] = None,
        longitude: Annotated[float | None, HIDDEN_PARAM] = None,
        radius: Annotated[float | None, HIDDEN_PARAM] = None,
        passive: Annotated[bool | None, HIDDEN_PARAM] = None,
        user_id: Annotated[str | None, HIDDEN_PARAM] = None,
        device_trackers: Annotated[
            list[str] | None, HIDDEN_PARAM, JSON_STRING_COERCION
        ] = None,
        picture: Annotated[str | None, HIDDEN_PARAM] = None,
        tag_id: Annotated[str | None, HIDDEN_PARAM] = None,
        description: Annotated[str | None, HIDDEN_PARAM] = None,
        pattern: Annotated[str | None, HIDDEN_PARAM] = None,
        category: Annotated[
            str | None,
            Field(
                description="Category ID for this helper (ha_config_get_category(scope='helpers') lists them, ha_config_set_category() creates one); '' or ' ' clears it.",
                default=None,
            ),
        ] = None,
        config: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Type-specific fields. On update it is a patch: a field "
                    "you omit keeps its current value. SIMPLE types take "
                    f"these keys: {_SIMPLE_CONFIG_KEYS_DESCRIPTION} "
                    "FLOW types and config_subentry take the flow's fields; a "
                    "field set to null is cleared where the schema allows "
                    "that field to be empty. A field two "
                    "steps declare gets your one value both times; pass "
                    "step_values={'<step_id>': {'<field>': <value>}} to give "
                    "a step its own value, or to leave it out of that step; a "
                    "LIST of those objects supplies one per encounter when the "
                    "flow presents a step more than once."
                ),
                default=None,
            ),
        ] = None,
        wait: Annotated[
            bool,
            Field(
                description="Wait for helper entity to be queryable before returning. Set to False "
                "for bulk operations.",
                default=True,
            ),
        ] = True,
        action: Annotated[
            Literal["create", "update"] | None,
            Field(
                description=(
                    "Explicit intent: 'create' a new helper or 'update' an existing one. "
                    "Pass it so a helper_id typo fails as 'helper not found' instead of "
                    "creating a helper."
                ),
                default=None,
            ),
        ] = None,
        MandatoryBPS: Annotated[
            bool,
            Field(default=True),
        ] = True,
        # BestPracticeKey (#1779): consumed by StrictBpsMiddleware, never read
        # here — see strict_bps.py for the declaration contract.
        BestPracticeKey: BestPracticeKeyParam = None,
    ) -> dict[str, Any]:
        """Create or update Home Assistant helper entities and config subentries
        (30 types, unified interface).

        MUST call ha_get_skill_guide OR refer to your locally installed skills first.
        ``helper-selection.md`` ships under ``skill_content`` by default.

        SIMPLE types (pass `config` dict): input_boolean, input_button,
        input_select, input_number, input_text, input_datetime, counter, timer, schedule,
        zone, person, tag. Create requires `name`; update requires `helper_id`.

        FLOW types (pass `config` dict, Config Entry Flow API): template, group,
        utility_meter, derivative, min_max, threshold, integration, statistics, trend,
        random, filter, tod, generic_thermostat, switch_as_x, generic_hygrostat,
        history_stats, mold_indicator. Create requires `name`; for updates pass the
        existing entry_id as `helper_id` (options flows reject the `name` key).
        `otp` is a helper in the HA UI but not offered here — its flow needs a live
        TOTP code; create it with ha_set_integration(domain="otp").

        CONFIG_SUBENTRY type (Config Subentry Flow API): pass `entry_id`,
        `subentry_type` and `config`; pass `subentry_id` to reconfigure an existing
        subentry, omit it to create one.

        Behavior:
        - UPDATE preserves type-specific fields not re-passed (a rename never wipes
          initial/icon/etc.); flow-helper and config subentry updates behave the same
          way (see `config`).
        - Omitted `action` falls back to the `helper_id`-presence discriminator
          (SIMPLE/FLOW) or the `subentry_id`-presence discriminator (config
          subentries).
        - For flow-based helpers, config keys not declared by any step's data_schema
          are silently ignored by HA. Validation errors carry the helper's
          `data_schema` (and `menu_options` for menu-rooted helpers like
          `template`/`group` when no sub-type is chosen yet), so a follow-up call
          can self-correct without a separate schema-discovery round-trip.
        - Flows that present more than one menu (e.g. an MQTT device subentry
          reconfigure looping through its summary menu) take `next_step_id` as a
          LIST of successive selections, consumed one per menu encounter.

        EXAMPLES:
        - input_number: ha_config_set_helper(helper_type="input_number", name="Target", config={"min_value": 0, "max_value": 100, "step": 5})
        - template sensor: ha_config_set_helper(helper_type="template", name="Room Temp", config={"next_step_id": "sensor", "state": "{{ states('sensor.x')|float }}", "unit_of_measurement": "°C"})
        - group: ha_config_set_helper(helper_type="group", name="Kitchen Lights", config={"group_type": "light", "entities": ["light.a", "light.b"]})
        - config subentry: ha_config_set_helper(helper_type="config_subentry", entry_id="01HXYZ...", subentry_type="conversation", config={"name": "Local agent", "model": "gemma3:27b"})
        """
        try:
            area_id = clear_or_keep(area_id, "area_id")
            icon = clear_or_keep(icon, "icon")
            category = clear_or_keep(category, "category")
            if helper_type == "config_subentry":
                return await _handle_set_config_subentry(
                    self._client,
                    action,
                    entry_id,
                    subentry_type,
                    subentry_id,
                    show_advanced_options,
                    config,
                    MandatoryBPS,
                )

            action = await _validate_set_helper_action(
                self._client, action, helper_id, helper_type
            )  # type: ignore[assignment]
            async with _core_schema_context(self._client, helper_type, action):
                type_kw: dict[str, Any] = {
                    "options": options,
                    "initial": initial,
                    "min_value": min_value,
                    "max_value": max_value,
                    "step": step,
                    "unit_of_measurement": unit_of_measurement,
                    "mode": mode,
                    "has_date": has_date,
                    "has_time": has_time,
                    "restore": restore,
                    "duration": duration,
                    "monday": monday,
                    "tuesday": tuesday,
                    "wednesday": wednesday,
                    "thursday": thursday,
                    "friday": friday,
                    "saturday": saturday,
                    "sunday": sunday,
                    "latitude": latitude,
                    "longitude": longitude,
                    "radius": radius,
                    "passive": passive,
                    "user_id": user_id,
                    "device_trackers": device_trackers,
                    "picture": picture,
                    "tag_id": tag_id,
                    "description": description,
                    "pattern": pattern,
                }
                name, icon, type_kw = _prepare_typed_params(
                    helper_type, config, name, icon, type_kw
                )

                # Bug 12: detect name collision before sending so the caller isn't silently given a duplicate.
                if action == "create":
                    await _check_name_collision(self._client, helper_type, name)

                if helper_type in FLOW_HELPER_TYPES:
                    flow_response = await _handle_flow_helper(
                        self._client,
                        helper_type,
                        name,
                        helper_id,
                        config,
                        area_id,
                        labels,
                        category,
                        wait,
                        icon=icon,
                        action=action,
                    )
                    _attach_helper_skill(flow_response, MandatoryBPS)
                    return flow_response

                try:
                    labels = parse_string_list_param(labels, "labels")
                    type_kw["options"] = parse_string_list_param(
                        type_kw["options"], "options"
                    )
                except ValueError as e:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            f"Invalid list parameter: {e}",
                        )
                    )

                # Bug 16 (issue #1150): validate area_id / labels / category exist.
                await validate_registry_ids(
                    self._client,
                    area_id,
                    labels,
                    {"helpers": category},
                    fail_closed=True,
                )

                # Bug 13/17 (issue #1150): pre-validate per-type schema constraints.
                _validate_pre_dispatch_params(
                    helper_type,
                    type_kw["min_value"],
                    type_kw["max_value"],
                    type_kw["step"],
                    type_kw["options"],
                    type_kw["monday"],
                    type_kw["tuesday"],
                    type_kw["wednesday"],
                    type_kw["thursday"],
                    type_kw["friday"],
                    type_kw["saturday"],
                    type_kw["sunday"],
                )

                if action == "create":
                    return await _execute_create_simple_helper(
                        self._client,
                        helper_type,
                        name,
                        icon,
                        area_id,
                        labels,
                        category,
                        wait,
                        MandatoryBPS,
                        **type_kw,
                    )

                if action != "update":
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.INTERNAL_ERROR,
                            f"Unexpected action: {action}",
                        )
                    )

                # helper_id is guaranteed non-None by _validate_set_helper_action for update
                hid: str = helper_id  # type: ignore[assignment]
                entity_id = (
                    hid if hid.startswith(helper_type) else f"{helper_type}.{hid}"
                )
                return await _execute_update_simple_helper(
                    self._client,
                    helper_type,
                    entity_id,
                    hid,
                    name,
                    icon,
                    area_id,
                    labels,
                    category,
                    wait,
                    MandatoryBPS,
                    **type_kw,
                )

        except ToolError as te:
            raise augment_tool_error_with_skill_content(te, bp_warnings=None) from None
        except Exception as e:  # noqa: BLE001
            error = exception_to_structured_error(
                e,
                context={"action": action, "helper_type": helper_type},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify helper_id exists for update operations",
                    "Ensure required parameters are provided for the helper type",
                ],
                raise_error=False,
            )
            augment_error_dict_with_skill_content(error, bp_warnings=None)
            raise_tool_error(error)


def register_config_helper_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant helper configuration tools."""
    from ..transforms.component_helpers import ComponentHelperSchemaTransform

    register_tool_methods(mcp, HelperConfigTools(client))
    # Advertises Core's helper fields when the component serves helper writes.
    mcp.add_transform(ComponentHelperSchemaTransform(client))
