"""
Configuration management tools for Home Assistant helpers.

This module provides tools for listing, creating, updating, and removing
Home Assistant helper entities (input_button, input_boolean, input_select,
input_number, input_text, input_datetime, counter, timer, schedule).
"""

import logging
from typing import Annotated, Any, Literal

from pydantic import Field

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
from .config_entry_backup import helper_backup_id
from .config_helpers.core_payload import storage_fields
from .config_helpers.create import _execute_create_simple_helper
from .config_helpers.describe import describe_helper_response
from .config_helpers.flow import _handle_flow_helper, _handle_set_config_subentry
from .config_helpers.listing import (
    _component_covers,
    _paginate_helpers_response,
    _raise_all_requires_component,
    _raise_flow_requires_component,
    _shape_component_helpers_response,
    raise_if_helper_flows_degraded,
    shape_all_helpers_response,
)
from .config_helpers.registry import (
    _check_name_collision,
    _enrich_helpers_with_current_registry,
    _flatten_helper_list_result,
    validate_registry_ids,
)
from .config_helpers.schemas import (
    SIMPLE_CONFIG_KEYS_DESCRIPTION,
    SIMPLE_HELPER_TYPES,
    StorageHelperType,
)
from .config_helpers.typed_config import _core_schema_context
from .config_helpers.update import _execute_update_simple_helper
from .config_helpers.validation import (
    _reject_flow_params_on_storage_helper,
    _validate_set_helper_action,
)
from .config_write_helpers import (
    augment_error_dict_with_skill_content,
    augment_tool_error_with_skill_content,
)
from .helper_flows import helper_flow_types, require_helper_type
from .helpers import (
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
            StorageHelperType | Literal["all"] | str,
            Field(
                description=(
                    "Helper type to list: a storage type (input_boolean, "
                    "counter, timer, ...), a helper flow Home Assistant lists "
                    "(template, group, utility_meter, ...), or 'all' for every "
                    "type in one call."
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
                    "accepts in config for helper_type, as Home Assistant "
                    "reports them (the form the HA UI shows)."
                )
            ),
        ] = False,
        menu_choice: Annotated[
            str | None,
            Field(
                description=(
                    "describe only: sub-type of a menu-based helper "
                    "(template, group), e.g. 'sensor'. A choice that finishes "
                    "the flow saves the helper: flow_type create_entry."
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
        set. Each record carries the configuration for its helper:
        id (immutable storage key), entity_id (current — address the helper by
        this, where available), name (current display name), original_name
        (creation-time name), icon, type-specific settings, and area and label
        assignments. A flow helper record carries entry_id and options instead of
        id; a custom helper integration's options are withheld, marked
        `options_withheld`.

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

        Listing flow types requires the ha_mcp_tools custom component (>= 1.1.0);
        without it, such a listing returns a COMPONENT_NOT_INSTALLED error.

        With helper_type="all", each record carries its own ``helper_type``.
        This mode is component-only (there is no single built-in command that
        lists all types): without the ha_mcp_tools component it returns a
        COMPONENT_NOT_INSTALLED error rather than a partial or empty list.

        describe=True returns each field's name, type, required flag, options,
        default and (with helper_id) current value, the later_forms of a
        helper with several forms, or the menu_options a menu-based helper
        needs a menu_choice from. Call it before ha_config_set_helper.

        EXAMPLES:
        - List all counters: ha_config_list_helpers("counter")
        - List every helper type at once: ha_config_list_helpers("all")
        - Next page: ha_config_list_helpers("input_boolean", offset=100)
        - Fields for a template sensor:
          ha_config_list_helpers("template", describe=True, menu_choice="sensor")

        For detailed helper documentation, use ha_get_skill_guide.
        """
        await require_helper_type(self._client, helper_type, "all")
        if describe or menu_choice is not None or helper_id is not None:
            return await describe_helper_response(
                self._client, helper_type, menu_choice, helper_id, describe=describe
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
        is_flow = helper_type not in SIMPLE_HELPER_TYPES

        try:
            # Prefer the custom component's in-process listing when it advertises
            # the capability: one WS round-trip that joins the entity registry, so
            # each record carries the current entity_id + display name (the #1794
            # stale-id fix, shipped additively) instead of only the storage id.
            # Fall back cleanly for storage types when the component is absent,
            # downlevel, or errors — the taxonomy lives in
            # ``_list_helpers_via_component``.
            # Inside the ``try`` so an error the fallback re-raises reaches the
            # structured-error handler below.
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
        - ``HomeAssistantConnectionError`` (pooled-WS drop or a failed (re)connect
          in ``get_websocket_client()``) or ``HomeAssistantAuthError`` (token rejected):
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
            # (re)connect (HomeAssistantConnectionError), a rejected token
            # (HomeAssistantAuthError) or any other error. A storage type still
            # attempts the legacy
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
            # The component did not authoritatively enumerate this type (person
            # and tag, whose <type>/list adds more than the stored items; or an
            # older component sent no covered_types). Don't trust a partial/empty list: storage
            # types fall back to the legacy path silently; a flow type raises,
            # naming the failed loader read when the component reports one, else
            # the component-required error, rather than emptying out.
            if is_flow:
                raise_if_helper_flows_degraded(result, [helper_type])
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
        listing — a failed component command resolves to the same hard error the
        caller raises (mirroring the flow-helper taxonomy); a listing that misses
        a helper flow Core lists raises in ``shape_all_helpers_response``.
        ``unknown_command`` additionally invalidates the now-stale positive caps.
        """
        try:
            raw = await self._send_component_all_helpers()
        except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
            if is_unknown_command(exc):
                invalidate_caps(self._client)
            logger.warning("ha_mcp_tools/helpers_list (all) failed: %r", exc)
            return None
        return await shape_all_helpers_response(
            raw.get("result") or {},
            self._legacy_helper_list,
            flow_types=await helper_flow_types(self._client, refresh=True),
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
        id_fn=helper_backup_id,
        # Any other flow call can save, since Core runs the step when a flow
        # opens and on a re-read; an abort cannot.
        skip_fn=lambda kw: bool(kw.get("abort")),
    )
    @log_tool_usage
    async def ha_config_set_helper(
        self,
        helper_type: Annotated[
            StorageHelperType | Literal["config_subentry"] | str,
            Field(description="Type of helper to create or update."),
        ],
        name: Annotated[
            str | None,
            Field(
                description=(
                    "Display name. Home Assistant decides which SIMPLE helper "
                    "types need one to be created; optional on an update. "
                    "Creating a FLOW helper, it answers the "
                    "form's `name` field when the form has one and config "
                    "sets none; a FLOW update does not use it (rename the "
                    "entity with ha_set_entity). Not used by config_subentry."
                ),
                default=None,
            ),
        ] = None,
        helper_id: Annotated[
            str | None,
            Field(
                description=(
                    "The helper's storage id ('my_button'), not the name of its "
                    "entity, or its full entity_id ('input_button.my_button'); "
                    "for a FLOW helper its config entry id. Omit to create a "
                    "new helper."
                ),
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
                description="Material Design Icon (e.g., 'mdi:bell'); '' or ' ' clears it, except a zone's stored icon, which Home Assistant keeps on update as the zone dialog does: it can be changed but not removed. A storage helper's icon goes into its stored config when Home Assistant's schema for the type has an icon field, as the helper's own dialog stores it, and the entity registry is left alone; otherwise (person, tag, or another type without the ha_mcp_tools component) it goes into the entity registry. An icon override in the entity registry is set or cleared with ha_set_entity. Restoring a storage helper's automatic backup also puts back its registry icon, unless the backup predates registry icons being recorded; a flow helper's backup holds no icon, so its restore does not set one",
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
        category: Annotated[
            str | None,
            Field(
                description="Category ID for this helper (ha_config_get_category(scope='helpers') lists them, ha_config_set_category() creates one); '' or ' ' clears it.",
                default=None,
            ),
        ] = None,
        config: Annotated[
            dict[str, Any] | list[dict[str, Any]] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Type-specific fields. On update it is a patch: a field "
                    "you omit keeps its current value, and a SIMPLE type's "
                    "field set to null is removed. SIMPLE types take "
                    f"these keys: {SIMPLE_CONFIG_KEYS_DESCRIPTION} "
                    "FLOW types and config_subentry: the answers to the form "
                    "the flow is at, under the form's own field names (a "
                    "section's fields nested under the section's name). On an "
                    "update a field you omit is sent as the form shows it "
                    "(its `current` value); a field set to null is left "
                    "empty. Home Assistant validates the answer. A list "
                    "answers several forms: one object per form, in the "
                    "order Home Assistant asks for them."
                ),
                default=None,
            ),
        ] = None,
        menu_choice: Annotated[
            str | None,
            Field(
                description=(
                    "FLOW types and config_subentry: the option to pick at a "
                    "menu step (template and group open with one)."
                ),
                default=None,
            ),
        ] = None,
        flow_id: Annotated[
            str | None,
            Field(
                description=(
                    "Continue an open flow: the flow_id a previous call "
                    "returned, with the same helper_type and helper_id (or "
                    "entry_id, subentry_type and subentry_id)."
                ),
                default=None,
            ),
        ] = None,
        abort: Annotated[
            bool,
            Field(
                description="With flow_id: end the open flow without saving.",
                default=False,
            ),
        ] = False,
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
        """Create or update Home Assistant helpers: storage types, every helper
        flow HA lists, and config subentries.

        MUST call ha_get_skill_guide OR refer to your locally installed skills first.
        ``helper-selection.md`` ships under ``skill_content`` by default.

        SIMPLE types, written in one call: input_boolean, input_button,
        input_select, input_number, input_text, input_datetime, counter, timer, schedule,
        zone, person, tag.

        FLOW types: every helper flow Home Assistant lists, read from it at
        call time — template, group, utility_meter, derivative, min_max,
        threshold, integration, statistics, trend, ... and custom helper
        integrations. Creating one runs its config flow; `helper_id` (the
        config entry id) runs its options flow instead.
        Do not create `otp` here although Home Assistant lists it: the user sets
        it up in the HA UI, since its secret is a credential they enroll in an
        authenticator app.

        CONFIG_SUBENTRY type: pass `entry_id` and `subentry_type`; with
        `subentry_id` the subentry's reconfigure flow runs, without it its
        create flow.

        Flows, one step per call, as the HA UI runs them: a call answers the
        step the flow is at and returns Home Assistant's next answer. A form
        comes back with its fields (type, default, current value, Home
        Assistant's help text) and flow_id; a menu with menu_options. Answer
        with flow_id and config (a form) or menu_choice (a menu); flow_id alone
        returns the step again, and flow_id with abort=True ends the flow.
        Without flow_id a new flow starts: menu_choice answers its opening
        menu and config the form after it, so a one-form helper is created or
        updated in one call; without config the call returns the form, and
        its later_forms when the helper has more than one (statistics,
        history_stats, generic_thermostat, ...). With a config list the call
        answers form after form and returns at the first one Home Assistant
        rejects, or once the answers run out. A rejected form is an error
        with Core's text, and the flow stays open to correct it. Nothing is
        saved until the flow finishes.

        WHEN NOT TO USE:
        - An integration that is not a helper: ha_set_integration.
        - Removing a helper: ha_remove_helpers_integrations.
        - Renaming a flow helper's entity, or its registry settings alone:
          ha_set_entity.

        Behavior:
        - A SIMPLE update preserves type-specific fields not re-passed (a rename
          never wipes initial/icon/etc.).
        - Omitted `action`: update when `helper_id` (for a config subentry,
          `subentry_id`) is given, else create.
        - area_id, labels, category and icon are applied to a flow helper's
          entities by the call that finishes the flow.
        - A progress step (Home Assistant working in the background) is waited
          out for up to 120 s. When Home Assistant does not answer a submit,
          the error carries status applied_but_unverified: check the helper
          before retrying.

        EXAMPLES:
        - input_number: ha_config_set_helper(helper_type="input_number", name="Target", config={"min": 0, "max": 100, "step": 5})
        - template sensor: ha_config_set_helper(helper_type="template", menu_choice="sensor", config={"name": "Room Temp", "state": "{{ states('sensor.x')|float }}", "unit_of_measurement": "°C"})
        - group: ha_config_set_helper(helper_type="group", menu_choice="light", config={"name": "Kitchen Lights", "entities": ["light.a", "light.b"]})
        - see a form first: ha_config_set_helper(helper_type="statistics"), then answer it: ha_config_set_helper(helper_type="statistics", flow_id="...", config={...})
        - several forms in one call: ha_config_set_helper(helper_type="statistics", config=[{"name": "Temp Mean", "entity_id": "sensor.temp"}, {"state_characteristic": "mean"}, {"sampling_size": 20}])
        - update one option: ha_config_set_helper(helper_type="group", helper_id="01HXYZ...", config={"hide_members": true})
        - config subentry: ha_config_set_helper(helper_type="config_subentry", entry_id="01HXYZ...", subentry_type="conversation", config={"name": "Local agent", "model": "gemma3:27b"})
        """
        try:
            area_id = clear_or_keep(area_id, "area_id")
            icon = clear_or_keep(icon, "icon")
            category = clear_or_keep(category, "category")
            menu_choice, flow_id = menu_choice or None, flow_id or None
            if helper_type == "config_subentry":
                return await _handle_set_config_subentry(
                    self._client,
                    action,
                    entry_id,
                    subentry_type,
                    subentry_id,
                    config,
                    MandatoryBPS,
                    flow_id=flow_id,
                    menu_choice=menu_choice,
                    abort=abort,
                    show_advanced_options=show_advanced_options,
                )

            await require_helper_type(self._client, helper_type)
            action = _validate_set_helper_action(action, helper_id, helper_type)  # type: ignore[assignment]
            flow_args = {
                "flow_id": flow_id,
                "menu_choice": menu_choice,
                "abort": abort or None,
            }
            async with _core_schema_context(self._client, helper_type, action):
                if helper_type not in SIMPLE_HELPER_TYPES:
                    return await _handle_flow_helper(
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
                        action=action,  # type: ignore[arg-type]
                        MandatoryBPS=MandatoryBPS,
                        flow_id=flow_id,
                        menu_choice=menu_choice,
                        abort=abort,
                    )

                _reject_flow_params_on_storage_helper(helper_type, flow_args)
                # Home Assistant validates the fields itself (#2632).
                name, icon, fields = storage_fields(helper_type, config, name, icon)
                # Bug 12: detect name collision before sending so the caller isn't silently given a duplicate.
                unchecked = None
                if action == "create":
                    unchecked = await _check_name_collision(
                        self._client, helper_type, name
                    )

                try:
                    labels = parse_string_list_param(labels, "labels")
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

                if action == "create":
                    created = await _execute_create_simple_helper(
                        self._client,
                        helper_type,
                        name,
                        icon,
                        area_id,
                        labels,
                        category,
                        wait,
                        MandatoryBPS,
                        fields,
                    )
                    if unchecked:
                        created.setdefault("warnings", []).append(unchecked)
                    return created

                if action != "update":
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.INTERNAL_ERROR,
                            f"Unexpected action: {action}",
                        )
                    )

                # helper_id is guaranteed non-None by _validate_set_helper_action for update
                hid: str = helper_id  # type: ignore[assignment]
                # A bare id is a storage id, never turned into an entity_id.
                return await _execute_update_simple_helper(
                    self._client,
                    helper_type,
                    hid,
                    hid,
                    name,
                    icon,
                    area_id,
                    labels,
                    category,
                    wait,
                    MandatoryBPS,
                    fields,
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
