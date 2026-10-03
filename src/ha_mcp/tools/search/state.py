"""Entity state retrieval for ``ha_get_state``.

Bulk-result helpers and the single, bulk and component state methods.
"""

import asyncio
import logging
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ...client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ...client.websocket_client import get_websocket_client
from ...errors import create_validation_error
from ..component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from ..helpers import (
    exception_to_structured_error,
    raise_tool_error,
)
from ..util_helpers import (
    add_timezone_metadata,
)
from ..util_helpers import (
    project_entity_record as _project_entity,
)
from .base import SearchToolsBase

logger = logging.getLogger(__name__)


def _missing_entity_exc(entity_id: str) -> HomeAssistantAPIError:
    """A synthetic 404 for an id the component's ``states`` read reports absent.

    Classifying a component-reported miss through the same
    ``exception_to_structured_error`` path the legacy per-id REST 404 uses makes
    the missing-id error byte-identical on both backends (ENTITY_NOT_FOUND with
    the entity_id context; the response-level ``ha_search()`` suggestion still
    fires), without the server issuing a REST call it just avoided.
    """
    return HomeAssistantAPIError(
        f"API error: 404 - Entity {entity_id} not found", status_code=404
    )


def _accumulate_state_results(
    unique_ids: list[str],
    results: list[dict[str, Any]],
    parsed_fields: list[str] | None,
    parsed_attribute_keys: list[str] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Process asyncio.gather results into (states dict, errors list, attr_warnings list)."""
    states: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    attr_warns: list[str] = []
    for eid, result in zip(unique_ids, results, strict=True):
        if result.get("success") is True and "state" in result:
            state_record, attr_warn = _project_entity(
                result["state"], parsed_fields, parsed_attribute_keys
            )
            states[eid] = state_record
            if attr_warn and attr_warn not in attr_warns:
                attr_warns.append(attr_warn)
        else:
            error_detail = result.get("error")
            if error_detail is None:
                error_detail = {"code": "INTERNAL_ERROR", "message": "Unknown error"}
            errors.append(
                {
                    "entity_id": result.get("entity_id", eid),
                    "error": error_detail,
                }
            )
    return states, errors, attr_warns


def _build_bulk_states_response(
    states: dict[str, Any],
    errors: list[dict[str, Any]],
    attr_warns: list[str],
    attribute_keys_no_effect: bool,
) -> dict[str, Any]:
    """Build the bulk-state response dict from accumulated states, errors, and warnings."""
    response: dict[str, Any] = {
        "success": len(states) > 0,
        "count": len(states),
        "states": states,
    }
    if attribute_keys_no_effect:
        response.setdefault("warnings", []).append(
            "attribute_keys was ignored because 'attributes' is not in "
            "fields=. Add 'attributes' to fields= (or omit fields=) to "
            "apply attribute_keys."
        )
    for _w in attr_warns:
        response.setdefault("warnings", []).append(_w)
    if errors:
        response["errors"] = errors
        response["error_count"] = len(errors)
        response["suggestions"] = [
            "Use ha_search() to find correct entity IDs for failed lookups",
            "Verify entities exist in Home Assistant",
        ]
        if states:
            response["partial"] = True
    return response


class StateMixin(SearchToolsBase):
    """Fetches entity states for ``ha_get_state``."""

    async def _get_single_entity_state(
        self,
        entity_id: str,
        parsed_fields: list[str] | None,
        parsed_attribute_keys: list[str] | None,
        attribute_keys_no_effect: bool,
    ) -> dict[str, Any]:
        """Fetch and return state for a single entity ID."""
        try:
            result = await self._get_one_state(entity_id)
            entity_record, attr_warn = _project_entity(
                result, parsed_fields, parsed_attribute_keys
            )
            # Always wrap (include_metadata=True); callers and tests rely on
            # the ``result["data"]`` envelope even when fields= is active.
            wrapped = await add_timezone_metadata(self._client, entity_record)
            # ``attribute_keys`` was specified but ``attributes`` is not in the
            # projected ``fields=`` set. Attach the warning at the outer wrapper
            # level (sibling of ``data``/``metadata``) — the FIELDS PROJECTION
            # contract: ``fields=`` filters the keys of the returned record;
            # ``warnings`` is not a record key.
            if attribute_keys_no_effect:
                wrapped.setdefault("warnings", []).append(
                    "attribute_keys was ignored because 'attributes' is not in "
                    "fields=. Add 'attributes' to fields= (or omit fields=) to "
                    "apply attribute_keys."
                )
            if attr_warn:
                wrapped.setdefault("warnings", []).append(attr_warn)
            return wrapped
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={"entity_id": entity_id},
                suggestions=[
                    f"Verify entity '{entity_id}' exists in Home Assistant",
                    "Check Home Assistant connection",
                    "Use ha_search() to find correct entity IDs",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    async def _get_bulk_entity_states(
        self,
        entity_ids: list[str],
        parsed_fields: list[str] | None,
        parsed_attribute_keys: list[str] | None,
        attribute_keys_no_effect: bool,
    ) -> dict[str, Any]:
        """Fetch states for multiple entity IDs in parallel."""
        MAX_ENTITIES = 100

        if not isinstance(entity_ids, list) or not entity_ids:
            raise_tool_error(
                create_validation_error(
                    "entity_id must be a non-empty string or list of entity ID strings",
                    parameter="entity_id",
                )
            )

        if not all(isinstance(eid, str) for eid in entity_ids):
            raise_tool_error(
                create_validation_error(
                    "All entity_id values must be strings",
                    parameter="entity_id",
                )
            )

        if len(entity_ids) > MAX_ENTITIES:
            raise_tool_error(
                create_validation_error(
                    f"Too many entity IDs: {len(entity_ids)} exceeds maximum of {MAX_ENTITIES}",
                    parameter="entity_id",
                )
            )

        # Deduplicate while preserving order
        unique_ids = list(dict.fromkeys(entity_ids))
        if len(unique_ids) < len(entity_ids):
            logger.debug(
                f"Deduplicated entity_ids: {len(entity_ids)} -> {len(unique_ids)}"
            )

        try:
            results = await self._resolve_bulk_state_results(unique_ids)
            states, errors, attr_warns = _accumulate_state_results(
                unique_ids, results, parsed_fields, parsed_attribute_keys
            )
            response = _build_bulk_states_response(
                states, errors, attr_warns, attribute_keys_no_effect
            )
            return await add_timezone_metadata(self._client, response)

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error getting bulk states: {e}", exc_info=True)
            exception_to_structured_error(
                e,
                context={"entity_ids": entity_ids},
            )
            return None  # unreachable: exception_to_structured_error always raises

    async def _fetch_single_state(self, eid: str) -> dict[str, Any]:
        """Fetch state for one entity; returns structured error dict on failure."""
        try:
            state = await self._client.get_entity_state(eid)
            return {"success": True, "entity_id": eid, "state": state}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to fetch state for '{eid}': {e}")
            # ast-grep-ignore — batch item failure, aggregated via asyncio.gather
            return exception_to_structured_error(
                e,
                context={"entity_id": eid},
                raise_error=False,
            )

    async def _get_one_state(self, entity_id: str) -> dict[str, Any]:
        """Return one entity's raw state dict — component bulk-read or legacy REST.

        The same fetch primitive the bulk path uses, so single- and bulk-mode
        ``ha_get_state`` share one code path. When the component serves it, an id
        it authoritatively reports absent raises the same 404 the legacy REST read
        would, so the caller's exception handler produces the identical
        single-entity ENTITY_NOT_FOUND (with its ``ha_search()`` suggestion).
        """
        component = await self._fetch_states_via_component([entity_id])
        if component is None:
            legacy_state: dict[str, Any] = await self._client.get_entity_state(
                entity_id
            )
            return legacy_state
        states = component.get("states") or {}
        if entity_id in states:
            record: dict[str, Any] = states[entity_id]
            return record
        raise _missing_entity_exc(entity_id)

    async def _resolve_bulk_state_results(
        self, unique_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Per-id fetch results for a bulk read — component bulk-read or legacy REST.

        Returns one entry per id in ``_fetch_single_state`` shape (a hit dict or a
        structured error). The component path resolves every id in ONE
        ``ha_mcp_tools/states`` frame instead of up to 100 REST GETs; a missing id
        is mapped to the same 404-classified error the legacy per-id path yields,
        so ``_accumulate_state_results`` and the response-level ``ha_search()``
        suggestion behave identically on both backends.
        """
        component = await self._fetch_states_via_component(unique_ids)
        if component is None:
            return list(
                await asyncio.gather(
                    *(self._fetch_single_state(eid) for eid in unique_ids)
                )
            )
        states = component.get("states") or {}
        return [self._component_state_result(eid, states) for eid in unique_ids]

    def _component_state_result(
        self, entity_id: str, states: dict[str, Any]
    ) -> dict[str, Any]:
        """One found/missing per-id result from the component's ``states`` map."""
        if entity_id in states:
            return {"success": True, "entity_id": entity_id, "state": states[entity_id]}
        # ast-grep-ignore — batch item failure, mapped to the legacy 404 shape
        return exception_to_structured_error(
            _missing_entity_exc(entity_id),
            context={"entity_id": entity_id},
            raise_error=False,
        )

    async def _fetch_states_via_component(
        self, entity_ids: list[str]
    ) -> dict[str, Any] | None:
        """One ``ha_mcp_tools/states`` bulk read; ``None`` ⇒ use the legacy REST path.

        Returns the component's ``{states, missing}`` payload (found ids mapped to
        their ``State.as_dict()`` body) or ``None`` when the component lacks the
        ``states`` capability, was downgraded (``unknown_command`` → invalidate the
        cached caps), or errored (logged). Falls back **silently** — unlike
        ``ha_search`` / ``ha_get_zone`` which append a ``warnings[]`` entry —
        because ``ha_get_state``'s single- and bulk-mode responses do not share one
        warnings channel; the ``log.warning`` preserves operator visibility and the
        legacy REST path returns the byte-identical correct data either way.
        ``ha_get_state``'s legacy path is a REST ``get_entity_state`` read on a
        SEPARATE transport, so a WS transport/connect failure
        (``HomeAssistantConnectionError``, covering both a pooled-WS drop and a
        failed connect) is caught here and falls back to REST — an install whose REST API
        still works keeps getting its state instead of a spurious connection error.
        If REST is also down, the legacy path raises the same connection error
        itself. (``ha_search`` / ``ha_get_overview`` likewise fall back on a
        transport failure — no component fetch helper propagates one.)
        """
        caps = await get_component_caps(self._client)
        if not component_supports(caps, "states"):
            return None
        try:
            raw = await self._send_component_states(entity_ids)
        except (
            HomeAssistantCommandError,
            HomeAssistantCommandTimeout,
            HomeAssistantConnectionError,
        ) as exc:
            if is_unknown_command(exc):
                invalidate_caps(self._client)
            else:
                logger.warning(
                    "ha_mcp_tools/states failed; fell back to legacy: %r", exc
                )
            return None
        except Exception as exc:  # noqa: BLE001
            # Anything the tuple above doesn't cover → legacy REST, so an
            # install whose REST API still works keeps answering.
            logger.warning(
                "ha_mcp_tools/states connection error; fell back to legacy: %r", exc
            )
            return None
        result = raw.get("result") or {}
        if not isinstance(result.get("states"), dict):
            return None
        return result

    async def _send_component_states(self, entity_ids: list[str]) -> dict[str, Any]:
        """Send one ``ha_mcp_tools/states`` command over the per-client WebSocket."""
        ws = await get_websocket_client(
            url=self._client.base_url,
            token=self._client.token,
            verify_ssl=getattr(self._client, "verify_ssl", None),
        )
        return await ws.send_command("ha_mcp_tools/states", entity_ids=entity_ids)
