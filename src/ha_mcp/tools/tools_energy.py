"""Energy preferences through native Core APIs and optional Core schema validation.

Core owns payload validation, defaults and saved state. HA-MCP owns mutation
previews, duplicate protection and optimistic locking. Semantic validation is
post-save because energy/validate only examines persisted preferences.
"""

import json
import logging
from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..errors import ErrorCode, create_error_response
from ..utils.config_hash import compute_config_hash
from .coercion import JSON_STRING_COERCION
from .core_contract import command_payload, core_contract, validate_energy_proposal
from .energy_statistics import (
    _compute_per_key_hashes,
    get_energy_prefs,
    include_energy_statistics,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .tool_hints import write_hints

logger = logging.getLogger(__name__)


def _flatten_validation_errors(raw: Any) -> list[dict[str, Any]]:
    """Add paths for readability while preserving native issue objects as details."""
    def walk(value: Any, path: str) -> list[dict[str, Any]]:
        if not value:
            return []
        if isinstance(value, list):
            return [issue for index, item in enumerate(value) for issue in walk(item, f"{path}[{index}]")]
        if isinstance(value, dict) and not any(key in value for key in ("type", "message", "issue_type")):
            return [issue for key, item in value.items() for issue in walk(item, f"{path}.{key}" if path else key)]
        return [{"path": path, "message": str(value), "details": value}]

    return walk(raw, "")


class EnergyTools:
    """Energy Dashboard preference management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_manage_energy_prefs",
        tags={"Energy"},
        annotations=write_hints(
            "Manage Energy Dashboard Preferences",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )
    @log_tool_usage
    async def ha_manage_energy_prefs(
        self,
        mode: Annotated[
            Literal["get", "set", "add_device", "remove_device", "add_source"],
            Field(
                description=(
                    "Operation mode. Primitives: 'get' reads the current prefs; "
                    "'set' writes a full prefs payload."
                )
            ),
        ],
        config: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Full prefs payload for mode='set'. Must contain the "
                    "top-level keys you intend to replace: 'energy_sources', "
                    "'device_consumption', 'device_consumption_water'. Any "
                    "omitted key is preserved. "
                    "Call with mode='get' first, mutate the returned config, "
                    "then pass the whole object back. Ignored by convenience "
                    "modes."
                ),
                default=None,
            ),
        ] = None,
        config_hash: Annotated[
            str | dict[str, str] | None,
            Field(
                description=(
                    "Hash from a previous mode='get' call. REQUIRED for mode='set' unless "
                    "dry_run=True. Two forms: str (full-blob lock) or dict (per-key lock, "
                    "taken from the config_hash_per_key field of mode='get'). Pass the dict"
                    " form as a native object, NOT a JSON-encoded string — a stringified "
                    "dict is treated as a full-blob token and will report RESOURCE_LOCKED; "
                    "clients that can only send strings should use the str full-blob form. "
                    "Ignored by convenience modes."
                ),
                default=None,
            ),
        ] = None,
        dry_run: Annotated[
            bool,
            Field(
                description=(
                    "If True, preview without saving. With the component, validates "
                    "the complete proposal through Core's registered save schema. "
                    "Without that capability, explicitly reports proposal validation "
                    "unavailable. Semantic energy/validate checks persisted state only."
                ),
                default=False,
            ),
        ] = False,
        stat_consumption: Annotated[
            str | None,
            Field(
                description=(
                    "Statistic entity_id for mode='add_device' / "
                    "'remove_device' (e.g. 'sensor.fridge_energy'). "
                    "Required for those modes; ignored otherwise."
                ),
                default=None,
            ),
        ] = None,
        name: Annotated[
            str | None,
            Field(
                description=("Display name for mode='add_device'; ignored otherwise."),
                default=None,
            ),
        ] = None,
        included_in_stat: Annotated[
            str | None,
            Field(
                description=(
                    "'Parent' statistic for mode='add_device'. Set this to a statistic that"
                    " already INCLUDES this device's consumption (e.g., a whole-home or "
                    "circuit-level meter that this device feeds into). The Energy Dashboard"
                    " will subtract this device's reading from the parent so the parent's "
                    "contribution is not double-counted. Ignored otherwise."
                ),
                default=None,
            ),
        ] = None,
        water: Annotated[
            bool,
            Field(
                description=(
                    "If True, mode='add_device' / 'remove_device' targets "
                    "'device_consumption_water' instead of 'device_consumption'."
                ),
                default=False,
            ),
        ] = False,
        source: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Single native energy_sources entry for mode='add_source'. "
                    "Use mode='get', include_schema=True to inspect the running "
                    "Core's fields and requirements; dry_run=True validates without saving."
                ),
                default=None,
            ),
        ] = None,
        include_schema: Annotated[
            bool, Field(description="With mode='get', describe the running Core's save schema. Unavailable without the component.")
        ] = False,
        include_statistics: Annotated[
            bool,
            Field(
                description="With mode='get', include native recorder metadata and resolved output units for all configured statistic references. Ignored for other modes."
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Manage the Home Assistant Energy Dashboard preferences: grid / solar /
        battery / gas / water energy sources, device consumption sensors for
        electricity and water, and cost tariffs.

        WHEN TO USE:
        - mode='get' / 'set': inspect or replace the full Energy Dashboard
          config. Use 'set' for bulk edits or anything touching multiple
          top-level keys at once.
        - mode='add_device' / 'remove_device': add or remove a single
          device-consumption entry. The tool performs a fresh read-modify-write
          internally; the caller does NOT manage config_hash.
        - mode='add_source': append a single entry to ``energy_sources``.
          Same optimistic read-modify-write semantics.

        RELATED TOOLS: Use ha_get_history(source="statistics") with the returned
        statistic IDs for consumption, totals, and trends. Metadata comes directly
        from the running Core, including stored and display units.

        WHEN NOT TO USE:
        - To create the underlying statistics themselves — they must already
          exist as HA entities before being referenced here; create them via
          the relevant integration's config flow first.

        CAVEATS:
        - ``energy/save_prefs`` has per-key FULL-REPLACE semantics. Passing
          ``{"device_consumption": [<one entry>]}`` deletes every other device
          the user had configured — silently, with no error. mode='set'
          requires a fresh ``config_hash`` for optimistic locking; convenience
          modes hide this entirely.
        - The per-key ``config_hash`` form lets an agent submit only the
          top-level key it wants to change: ``config`` keys must equal the dict
          keys, and a per-key submission still fully replaces
          that key's value. A mismatch on any locked key returns
          ``RESOURCE_LOCKED`` with the offending keys in ``mismatched_keys``.
        - ``dry_run=True`` skips the hash check entirely for both forms.
        - Writes need an administrator token; Home Assistant rejects the save
          otherwise.
        - After a successful write, the tool calls ``energy/validate`` and
          returns residual issues (missing stats, unit mismatches) as
          ``post_save_validation_errors``; the save persists regardless —
          correct the config and write again if needed.
        - 'add_source' rejects duplicates by ``(type, stat_energy_from)`` for
          solar/battery/gas/water; grid entries are appended without a duplicate
          check (multiple grid variants are legitimate), so the caller
          de-duplicates grid sources.
        """
        if mode == "get":
            result = await self._get_prefs()
            if include_schema:
                result["core_contract"] = await core_contract(self._client, "energy/save_prefs")
            return (
                await include_energy_statistics(self._client, result)
                if include_statistics
                else result
            )

        if mode == "add_device":
            return await self._add_device(
                stat_consumption=stat_consumption,
                name=name,
                included_in_stat=included_in_stat,
                water=water,
                dry_run=dry_run,
            )

        if mode == "remove_device":
            return await self._remove_device(
                stat_consumption=stat_consumption,
                water=water,
                dry_run=dry_run,
            )

        if mode == "add_source":
            return await self._add_source(source=source, dry_run=dry_run)

        # mode == "set"
        if config is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "'config' is required when mode='set'",
                    context={"mode": mode},
                    suggestions=[
                        "Call ha_manage_energy_prefs(mode='get') first, mutate the returned config, pass it back",
                    ],
                )
            )

        if dry_run:
            return await self._dry_run(config)

        if config_hash is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "'config_hash' is required when mode='set' and dry_run=False",
                    context={"mode": mode},
                    suggestions=[
                        "Call ha_manage_energy_prefs(mode='get') to obtain a fresh config_hash",
                        "Or call again with dry_run=True to validate without a hash",
                    ],
                )
            )

        return await self._set_prefs(config, config_hash)

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------

    async def _get_prefs(self) -> dict[str, Any]:
        return await get_energy_prefs(self._client)

    async def _dry_run(self, config: dict[str, Any]) -> dict[str, Any]:
        """Preview the actual native schema and label persisted-state checks separately."""
        validation = await validate_energy_proposal(self._client, config)
        current_errors, failure, native_validation = await self._post_save_validate()
        result: dict[str, Any] = {
            "success": True,
            "mode": "set",
            "dry_run": True,
            "proposal_validation": validation,
            "current_state_validation_errors": current_errors,
            "current_state_validation": native_validation,
            "message": "No preferences saved. Semantic checks describe the current persisted state only.",
        }
        if validation["status"] != "validated":
            result["partial"] = True
            result["warnings"] = [validation["reason"]]
        if failure:
            result["partial"] = True
            result.setdefault("warnings", []).append(f"Current-state validation failed: {failure}")
        return result

    async def _set_prefs(
        self,
        config: dict[str, Any],
        config_hash: str | dict[str, str],
        *,
        current_prefs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Hash-check, submit to Core, then report authoritative saved state.

        Core validates every submitted entry. Existing siblings are never checked
        against a narrower local schema. The convenience path supplies its fresh
        snapshot; the per-key form locks exactly the submitted keys.
        """
        try:
            command_payload("energy/save_prefs", config)
            current_prefs = await self._resolve_current_prefs(current_prefs)

            self._check_config_hash(config, config_hash, current_prefs)

            # 3. Save
            save_payload = command_payload("energy/save_prefs", config)

            save_result = await self._client.send_websocket_message(save_payload)
            if not save_result.get("success"):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to save energy prefs: {save_result.get('error', 'Unknown error')}",
                        context={"mode": "set"},
                        suggestions=[
                            "Verify the token has admin privileges (energy/save_prefs is admin-only)",
                            "Check config shape against the energy/get_prefs response",
                        ],
                    )
                )

            # 4. Post-save validation against the newly-persisted state
            (
                post_save_errors,
                post_save_validate_error,
                native_validation,
            ) = await self._post_save_validate()

            # Core returns its full normalized preferences, including defaults
            # and coercions. Never predict the persisted state from the request.
            new_prefs = save_result.get("result")
            if not isinstance(new_prefs, dict):
                new_prefs = (await self._get_prefs())["config"]
            new_hash = compute_config_hash(new_prefs)

            response: dict[str, Any] = {
                "success": True,
                "mode": "set",
                "post_save_validation": native_validation,
                "config": new_prefs,
                "config_hash": new_hash,
                "config_hash_per_key": _compute_per_key_hashes(new_prefs),
                "message": "Energy prefs updated.",
            }
            if post_save_errors:
                response["post_save_validation_errors"] = post_save_errors
                response.setdefault("warnings", []).append(
                    f"Save succeeded, but the persisted config has "
                    f"{len(post_save_errors)} validation error(s). Review "
                    "and re-write if any relate to this change."
                )
            elif post_save_validate_error is not None:
                response["partial"] = True
                response.setdefault("warnings", []).append(
                    f"Save succeeded, but post-save energy/validate "
                    f"failed: {post_save_validate_error}. The persisted "
                    "config has not been re-validated."
                )
            return response

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("Error setting energy prefs", exc_info=True)
            exception_to_structured_error(
                e,
                context={"mode": "set"},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify token has admin privileges",
                    "Re-read prefs and retry with a fresh config_hash",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    async def _resolve_current_prefs(
        self, current_prefs: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Return ``current_prefs`` if supplied, else fetch a fresh snapshot.

        Extracted from ``_set_prefs`` step 2 (snapshot acquisition).
        Convenience modes pass their already-fetched snapshot in to skip
        the re-read; external mode='set' callers fall through to a fresh
        read here. Maps "No prefs" (never configured) to the empty default
        so the hash-check works on fresh installations too.
        """
        if current_prefs is not None:
            return current_prefs

        config: dict[str, Any] = (await self._get_prefs())["config"]
        return config

    @staticmethod
    def _check_config_hash(
        config: dict[str, Any],
        config_hash: str | dict[str, str],
        current_prefs: dict[str, Any],
    ) -> None:
        """Verify ``config_hash`` against ``current_prefs``; raises
        ``ToolError`` on mismatch or malformed per-key input.

        Extracted from ``_set_prefs`` step 2 (hash check). Handles both
        hash forms: a ``dict[str, str]`` (per-key lock) and a plain
        ``str`` (full-blob lock). See the ``_set_prefs`` docstring, and the
        ``ha_manage_energy_prefs`` tool docstring, for the full agent-facing
        contract this enforces.
        """
        if isinstance(config_hash, dict):
            submitted_keys = set(config)
            hashed_keys = set(config_hash)
            if not submitted_keys:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_FAILED,
                        "'config' must include at least one top-level "
                        "key when using per-key config_hash",
                        context={"mode": "set"},
                        suggestions=[
                            "Include the top-level key(s) you want to "
                            "save in 'config' alongside their per-key "
                            "hashes in 'config_hash'",
                        ],
                    )
                )
            if submitted_keys != hashed_keys:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_FAILED,
                        "Per-key config_hash keys must match the "
                        "top-level keys submitted in 'config'",
                        context={
                            "mode": "set",
                            "submitted_keys": sorted(submitted_keys),
                            "hashed_keys": sorted(hashed_keys),
                            "missing_in_hash": sorted(submitted_keys - hashed_keys),
                            "extra_in_hash": sorted(hashed_keys - submitted_keys),
                        },
                        suggestions=[
                            "Pass exactly one config_hash_per_key entry per top-level key in 'config'",
                            "Use the str form of config_hash to lock the full prefs blob instead",
                        ],
                    )
                )

            mismatched_keys = [
                key
                for key in sorted(submitted_keys)
                if config_hash[key]
                != (compute_config_hash({key: current_prefs[key]}) if key in current_prefs else None)
            ]
            if mismatched_keys:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_LOCKED,
                        "Energy prefs modified since last read on "
                        f"top-level key(s): {', '.join(mismatched_keys)}"
                        " (conflict)",
                        context={
                            "mode": "set",
                            "mismatched_keys": mismatched_keys,
                        },
                        suggestions=[
                            "Call ha_manage_energy_prefs(mode='get') again",
                            "Re-apply your changes to the fresh config",
                            "Pass the new config_hash_per_key back in",
                        ],
                    )
                )
        else:
            current_hash = compute_config_hash(current_prefs)
            if current_hash != config_hash:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_LOCKED,
                        "Energy prefs modified since last read (conflict)",
                        context={"mode": "set"},
                        suggestions=[
                            "Call ha_manage_energy_prefs(mode='get') again",
                            "Re-apply your changes to the fresh config",
                            "Pass the new config_hash back in",
                        ],
                    )
                )

    async def _post_save_validate(self) -> tuple[list[dict[str, Any]], str | None, Any]:
        """Call ``energy/validate`` after a save and return
        ``(errors, failure_message)``.

        Extracted from ``_set_prefs`` step 4 (post-save validation). A
        post-save validate failure is non-fatal — the save itself already
        succeeded — so failures are captured as a message rather than
        raised. Returns a populated (possibly empty on success) error list
        and ``None`` on a successful validate call, or an empty error list
        and a failure message when the validate call itself fails
        (transport/timeout, etc.).
        """
        post_save_errors: list[dict[str, Any]] = []
        native_validation: Any = None
        post_save_validate_error: str | None = None
        try:
            validate_result = await self._client.send_websocket_message(
                {"type": "energy/validate"}
            )
            if validate_result.get("success"):
                native_validation = validate_result.get("result")
                post_save_errors = _flatten_validation_errors(native_validation)
            else:
                post_save_validate_error = (
                    validate_result.get("error") or "unknown error"
                )
                logger.warning(
                    f"energy/validate (post-save) failed: {post_save_validate_error}"
                )
        except Exception as e:  # noqa: BLE001
            # Post-save validate failure is non-fatal — the save itself
            # succeeded. Log and continue.
            logger.warning("Post-save energy/validate failed", exc_info=True)
            post_save_validate_error = str(e)
        return post_save_errors, post_save_validate_error, native_validation

    # ------------------------------------------------------------------
    # Convenience modes — atomic read-modify-write (no caller hash)
    # ------------------------------------------------------------------

    async def _add_device(
        self,
        *,
        stat_consumption: str | None,
        name: str | None,
        included_in_stat: str | None,
        water: bool,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Add a device-consumption entry using a fresh snapshot.

        Reads current prefs, checks for duplicate ``stat_consumption`` in the
        target list, appends the new entry, and writes back with the freshly
        captured ``config_hash``. On hash conflict (concurrent modification),
        retries once before failing.
        """
        if stat_consumption is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "'stat_consumption' is required when mode='add_device'",
                    context={"mode": "add_device"},
                    suggestions=[
                        "Pass stat_consumption='sensor.<your_device_energy>'",
                    ],
                )
            )
        # Empty/whitespace stat_consumption would write a ``{"stat_consumption": ""}``
        # entry to energy prefs storage — a phantom row keyed on an empty
        # sensor reference. Same multi-modal-destructive class as
        # ``ha_manage_app`` slug guard.
        validate_identifier_not_empty(
            stat_consumption,
            "stat_consumption",
            suggestions=[
                "Pass stat_consumption='sensor.<your_device_energy>'",
            ],
            context={"mode": "add_device"},
        )

        target_key = "device_consumption_water" if water else "device_consumption"

        new_entry: dict[str, Any] = {"stat_consumption": stat_consumption}
        if name is not None:
            new_entry["name"] = name
        if included_in_stat is not None:
            new_entry["included_in_stat"] = included_in_stat

        return await self._mutate_atomic(
            mode="add_device",
            target_key=target_key,
            mutator=lambda existing: self._append_unique_device(
                existing, new_entry, target_key
            ),
            dry_run=dry_run,
            preview_payload={"would_add": new_entry, "target_key": target_key},
        )

    async def _remove_device(
        self,
        *,
        stat_consumption: str | None,
        water: bool,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Remove a device-consumption entry by ``stat_consumption``."""
        if stat_consumption is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "'stat_consumption' is required when mode='remove_device'",
                    context={"mode": "remove_device"},
                    suggestions=[
                        "Pass stat_consumption='sensor.<existing_device_energy>'",
                    ],
                )
            )
        # Empty/whitespace stat_consumption would search the prefs storage for
        # an empty match (always missing) and surface as a misleading
        # "Device with stat_consumption='' not found".
        validate_identifier_not_empty(
            stat_consumption,
            "stat_consumption",
            suggestions=[
                "Pass stat_consumption='sensor.<existing_device_energy>'",
            ],
            context={"mode": "remove_device"},
        )

        target_key = "device_consumption_water" if water else "device_consumption"

        return await self._mutate_atomic(
            mode="remove_device",
            target_key=target_key,
            mutator=lambda existing: self._remove_device_by_stat(
                existing, stat_consumption, target_key
            ),
            dry_run=dry_run,
            preview_payload={
                "would_remove": {"stat_consumption": stat_consumption},
                "target_key": target_key,
            },
        )

    async def _add_source(
        self,
        *,
        source: dict[str, Any] | None,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Append a native source payload, keeping the wrapper's duplicate guard."""
        if source is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "'source' is required when mode='add_source'",
                    context={"mode": "add_source"},
                    suggestions=[
                        "Pass source={'type': 'grid'|'solar'|'battery'|'gas'|'water', ...}",
                    ],
                )
            )

        return await self._mutate_atomic(
            mode="add_source",
            target_key="energy_sources",
            mutator=lambda existing: self._append_unique_source(existing, source),
            dry_run=dry_run,
            preview_payload={"would_add": source, "target_key": "energy_sources"},
        )

    @staticmethod
    def _append_unique_device(
        existing: list[dict[str, Any]],
        new_entry: dict[str, Any],
        target_key: str,
    ) -> list[dict[str, Any]]:
        """Append ``new_entry`` to ``existing`` if its ``stat_consumption`` is
        not already present. Raises ToolError(RESOURCE_ALREADY_EXISTS) on
        duplicate."""
        stat = new_entry["stat_consumption"]
        for entry in existing:
            if entry.get("stat_consumption") == stat:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_ALREADY_EXISTS,
                        f"Device with stat_consumption='{stat}' already in {target_key}",
                        context={
                            "mode": "add_device",
                            "stat_consumption": stat,
                            "target_key": target_key,
                        },
                        suggestions=[
                            "Use mode='get' to inspect the current entries",
                            "Use mode='remove_device' first if you want to replace it",
                        ],
                    )
                )
        return existing + [new_entry]

    @staticmethod
    def _append_unique_source(
        existing: list[dict[str, Any]],
        new_source: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Append ``new_source`` to ``existing`` with type-aware duplicate
        detection.

        Solar/battery/gas/water entries are keyed on
        ``(type, stat_energy_from)`` — duplicates raise
        ``RESOURCE_ALREADY_EXISTS``. Grid entries are appended without a
        duplicate check (multiple grid variants are legitimate, and grid
        does not have a single canonical uniqueness key — see
        ``_add_source`` docstring for rationale). The post-save
        ``energy/validate`` call still runs on the full payload as a
        backstop for whatever HA Core flags.
        """
        source_type = new_source.get("type")
        if source_type != "grid" and "stat_energy_from" in new_source:
            stat = new_source.get("stat_energy_from")
            for entry in existing:
                if (
                    entry.get("type") == source_type
                    and entry.get("stat_energy_from") == stat
                ):
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.RESOURCE_ALREADY_EXISTS,
                            f"Source of type='{source_type}' with "
                            f"stat_energy_from='{stat}' already in energy_sources",
                            context={
                                "mode": "add_source",
                                "type": source_type,
                                "stat_energy_from": stat,
                                "target_key": "energy_sources",
                            },
                            suggestions=[
                                "Use mode='get' to inspect the current sources",
                                "Use mode='set' to replace the existing entry",
                            ],
                        )
                    )
        return existing + [new_source]

    @staticmethod
    def _remove_device_by_stat(
        existing: list[dict[str, Any]],
        stat_consumption: str,
        target_key: str,
    ) -> list[dict[str, Any]]:
        """Return ``existing`` minus the entry whose ``stat_consumption``
        matches. Raises ToolError(RESOURCE_NOT_FOUND) if no match."""
        kept = [e for e in existing if e.get("stat_consumption") != stat_consumption]
        if len(kept) == len(existing):
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"No device with stat_consumption='{stat_consumption}' in {target_key}",
                    context={
                        "mode": "remove_device",
                        "stat_consumption": stat_consumption,
                        "target_key": target_key,
                    },
                    suggestions=[
                        "Use mode='get' to inspect the current entries",
                        "Check water=True/False targets the right list",
                    ],
                )
            )
        return kept

    # Keys overridden on the convenience-mode response envelope. Anything
    # else returned by ``_set_prefs`` (post_save_validation_errors, warning,
    # partial, plus any future additions) passes through.
    _CONVENIENCE_RESPONSE_OVERRIDES = frozenset(
        {"success", "mode", "config_hash", "target_key", "new_count", "message"}
    )

    async def _mutate_atomic_preview(
        self,
        *,
        mode: str,
        target_key: str,
        mutator: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
        preview_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Preview the mutation and validate the whole resulting native payload."""
        current = await self._get_prefs()
        current_config: dict[str, Any] = current["config"]
        existing_list = list(current_config.get(target_key, []))
        new_list = mutator(existing_list)

        preview = await self._dry_run({target_key: new_list})

        return {
            **preview,
            "mode": mode,
            "dry_run": True,
            **preview_payload,
            "current_count": len(existing_list),
            "new_count": len(new_list),
        }

    @staticmethod
    def _is_hash_conflict(exc: ToolError) -> bool:
        """Return True if ``exc`` is a ``_set_prefs`` ``ToolError`` raised
        for ``RESOURCE_LOCKED`` (hash mismatch / concurrent modification).

        ``raise_tool_error`` serialises the structured error as JSON in the
        exception message, so we parse rather than substring-match.
        """
        try:
            parsed = json.loads(str(exc))
        except (json.JSONDecodeError, TypeError, ValueError):
            return False
        if not isinstance(parsed, dict):
            return False
        err_dict = parsed.get("error")
        if not isinstance(err_dict, dict):
            return False
        return bool(err_dict.get("code") == ErrorCode.RESOURCE_LOCKED.value)

    async def _mutate_atomic(
        self,
        *,
        mode: str,
        target_key: str,
        mutator: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
        dry_run: bool,
        preview_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Retry one detected snapshot conflict; Core has no conditional-save API.

        Dry runs validate the whole mutated proposal without saving. This is
        optimistic locking, not a transaction against concurrent native writers.
        """
        try:
            if dry_run:
                return await self._mutate_atomic_preview(
                    mode=mode,
                    target_key=target_key,
                    mutator=mutator,
                    preview_payload=preview_payload,
                )

            max_attempts = 2
            for attempt in range(max_attempts):
                current = await self._get_prefs()
                current_config = current["config"]
                current_hash: str = current["config_hash"]

                existing_list = list(current_config.get(target_key, []))
                new_list = mutator(existing_list)

                partial_config = {target_key: new_list}
                try:
                    set_result = await self._set_prefs(
                        partial_config,
                        current_hash,
                        current_prefs=current_config,
                    )
                except ToolError as exc:
                    # _set_prefs raises ToolError(RESOURCE_LOCKED) on hash mismatch.
                    # Retry once with a fresh read in case of a benign race.
                    if self._is_hash_conflict(exc) and attempt + 1 < max_attempts:
                        logger.warning(
                            f"{mode} on {target_key}: hash conflict on attempt "
                            f"{attempt + 1}, retrying"
                        )
                        continue
                    raise

                return {
                    "success": True,
                    "mode": mode,
                    "config_hash": set_result["config_hash"],
                    "target_key": target_key,
                    "new_count": len(new_list),
                    "message": set_result.get("message", f"{mode} succeeded."),
                    **{
                        k: v
                        for k, v in set_result.items()
                        if k not in self._CONVENIENCE_RESPONSE_OVERRIDES
                    },
                }

            # Unreachable as long as every iteration either returns or raises:
            # the only ``continue`` is gated on ``attempt + 1 < max_attempts``,
            # which is False on the final iteration — so the bare ``raise``
            # in the except block always fires there. Surface as an actionable
            # structured error rather than a bare AssertionError that would
            # otherwise fall through to ``except Exception`` and lose context.
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"_mutate_atomic({mode}, {target_key}): retry loop exited "
                    "without a return or raise",
                    context={"mode": mode, "target_key": target_key},
                    suggestions=[
                        "This indicates a bug in the optimistic-concurrency "
                        "loop logic — please file an issue with the mode and "
                        "target_key from the context.",
                    ],
                )
            )

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error in {mode} on {target_key}: {e}")
            exception_to_structured_error(
                e,
                context={"mode": mode, "target_key": target_key},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable


def register_energy_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant energy preference management tools."""
    register_tool_methods(mcp, EnergyTools(client))
