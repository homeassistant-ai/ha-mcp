"""
Energy Dashboard preference management tools for Home Assistant.

This module provides a single tool to read and write Home Assistant's Energy
Dashboard configuration through the ``energy/get_prefs`` / ``energy/save_prefs``
WebSocket commands. The underlying API has destructive full-replace semantics
per top-level key (``energy_sources``, ``device_consumption``,
``device_consumption_water``) — sending a key with a partial list silently
deletes everything else the user had configured. Optimistic locking via
``config_hash`` prevents concurrent-modification data loss; a local shape
check catches the most common agent-side errors; and a server-side
``energy/validate`` call after every write surfaces residual issues
(missing stats, wrong unit classes, etc.) in the response.

Note: ``energy/validate`` in Home Assistant Core takes no payload — it
validates the currently-persisted config. Pre-write validation of an
unsubmitted payload is therefore not possible; this tool validates the
post-save state instead.

Note: On a fresh Home Assistant instance that has never had the Energy
Dashboard configured, ``energy/get_prefs`` returns
``ERR_NOT_FOUND "No prefs"`` rather than an empty default. The tool
transparently maps that case to the documented default preferences
structure (all three top-level keys present, empty lists) so agents
get uniform behavior on fresh and configured instances alike.
"""

import json
import logging
from collections.abc import Callable
from typing import Annotated, Any, Literal, cast

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from ..utils.config_hash import compute_config_hash
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)

logger = logging.getLogger(__name__)


# Top-level keys in the energy prefs payload. Each is an independent
# full-replace slot in ``energy/save_prefs``. ``_PrefsKey`` is the
# corresponding ``Literal`` alias so MCP-wire callers (Pydantic-validated)
# get typo-rejection at the boundary; runtime guards in `_set_prefs` cover
# the unit-test path that bypasses Pydantic.
_PrefsKey = Literal[
    "energy_sources", "device_consumption", "device_consumption_water"
]
_PREFS_TOP_LEVEL_KEYS: tuple[_PrefsKey, ...] = (
    "energy_sources",
    "device_consumption",
    "device_consumption_water",
)


def _default_prefs() -> dict[str, Any]:
    """Return the default empty prefs structure used by HA Core.

    Mirrors ``EnergyManager.default_preferences()`` in
    ``homeassistant/components/energy/data.py``. A Home Assistant instance
    that has never had the Energy Dashboard configured returns
    ``ERR_NOT_FOUND "No prefs"`` from ``energy/get_prefs``; this helper
    provides the canonical empty structure so the tool can transparently
    treat the two cases (never-configured vs. configured-but-empty) the
    same way.
    """
    return {
        "energy_sources": [],
        "device_consumption": [],
        "device_consumption_water": [],
    }


def _compute_per_key_hashes(prefs: dict[str, Any]) -> dict[_PrefsKey, str]:
    """Per-top-level-key hashes for partial-update optimistic locking.

    Each top-level key is wrapped in its own single-key dict before hashing,
    so the per-key hash captures both the key name and its value — an agent
    cannot accidentally use, say, an ``energy_sources`` hash to authorise a
    ``device_consumption`` write. ``prefs.get(key, [])`` mirrors the
    "missing top-level key = empty list" semantics codified by
    ``_default_prefs``.
    """
    return {
        key: compute_config_hash({key: prefs.get(key, [])})
        for key in _PREFS_TOP_LEVEL_KEYS
    }


def _is_no_prefs_error(error_msg: str) -> bool:
    """Return True if an error string from send_websocket_message indicates
    ``ERR_NOT_FOUND "No prefs"`` from HA Core's energy/get_prefs handler.

    HA Core wraps the error as ``f"Command failed: {message}"``; the
    underlying sentinel we key on is the literal ``"No prefs"`` message
    emitted by ``ws_get_prefs`` when ``manager.data is None``.
    """
    return error_msg.endswith("No prefs")


def _flatten_validation_errors(raw: Any) -> list[dict[str, str]]:
    """Convert the raw ``energy/validate`` response into a flat error list.

    The raw response mirrors the prefs structure: a dict with the three
    top-level keys, each mapping to a list of per-entry error lists (empty
    inner list = that entry is valid). This function walks that structure and
    returns a flat list of ``{"path", "message"}`` dicts, suitable for agent
    consumption.

    A successful validation returns an empty list.
    """
    if not isinstance(raw, dict):
        return []

    errors: list[dict[str, str]] = []
    for key in _PREFS_TOP_LEVEL_KEYS:
        entries = raw.get(key, [])
        if not isinstance(entries, list):
            continue
        for idx, entry_errors in enumerate(entries):
            if not entry_errors:
                continue
            if isinstance(entry_errors, list):
                errors.extend(
                    {"path": f"{key}[{idx}]", "message": str(msg)}
                    for msg in entry_errors
                )
            elif isinstance(entry_errors, dict):
                for field, msgs in entry_errors.items():
                    msg_list = msgs if isinstance(msgs, list) else [msgs]
                    errors.extend(
                        {"path": f"{key}[{idx}].{field}", "message": str(msg)}
                        for msg in msg_list
                    )
    return errors


def _shape_check(
    config: dict[str, Any],
    validate_only: dict[str, set[int]] | None = None,
) -> list[dict[str, str]]:
    """Validate config shape locally before sending to the server.

    Validates that top-level keys have the expected list-of-dicts shape and
    that required identifying fields are present. Does NOT validate semantic
    correctness (stat IDs existing, units matching, etc.) — that's surfaced
    by the post-save server-side ``energy/validate`` call.

    ``validate_only`` scopes the per-entry check. ``None`` (default) validates
    every entry under every present top-level key — the original contract.
    A dict scopes the check to the listed keys and, within each, only the
    listed indices: top-level keys absent from the dict are skipped entirely,
    indices outside each key's set are skipped per-entry. The "must be a
    list" structural check still fires for any present-and-listed key with a
    non-list value, so ``validate_only`` cannot be used to bypass structural
    sanity. A dict with an empty set for a key (``{key: set()}``) skips the
    per-entry pass for that key while preserving the structural check —
    this is what convenience-mode write paths pass for remove operations
    (no new entries to validate, but the list shape is still checked). An
    empty dict (``{}``) skips all keys entirely. See issue #1086 for the
    asymmetric over-validation problem this addresses.
    """
    errors: list[dict[str, str]] = []

    if not isinstance(config, dict):
        return [{"path": "config", "message": "must be a dict"}]

    for key in _PREFS_TOP_LEVEL_KEYS:
        if key not in config:
            continue
        if validate_only is not None and key not in validate_only:
            continue
        value = config[key]
        if not isinstance(value, list):
            errors.append({"path": key, "message": "must be a list"})
            continue
        allowed_indices: set[int] | None = (
            validate_only[key] if validate_only is not None else None
        )
        for idx, entry in enumerate(value):
            if allowed_indices is not None and idx not in allowed_indices:
                continue
            if not isinstance(entry, dict):
                errors.append(
                    {
                        "path": f"{key}[{idx}]",
                        "message": "entry must be a dict",
                    }
                )
                continue
            if key == "energy_sources":
                valid_types = {"grid", "solar", "battery", "gas"}
                requires_stat_from = {"solar", "battery", "gas"}
                entry_type = entry.get("type")
                if entry_type is None:
                    errors.append(
                        {
                            "path": f"{key}[{idx}]",
                            "message": "energy_sources entries require 'type' (grid|solar|battery|gas)",
                        }
                    )
                elif entry_type not in valid_types:
                    errors.append(
                        {
                            "path": f"{key}[{idx}].type",
                            "message": f"invalid type '{entry_type}' (must be one of grid|solar|battery|gas)",
                        }
                    )
                elif (
                    entry_type in requires_stat_from and "stat_energy_from" not in entry
                ):
                    errors.append(
                        {
                            "path": f"{key}[{idx}]",
                            "message": f"{entry_type} entries require 'stat_energy_from'",
                        }
                    )
            if key == "device_consumption" and "stat_consumption" not in entry:
                errors.append(
                    {
                        "path": f"{key}[{idx}]",
                        "message": "device_consumption entries require 'stat_consumption'",
                    }
                )
            if key == "device_consumption_water" and "stat_consumption" not in entry:
                errors.append(
                    {
                        "path": f"{key}[{idx}]",
                        "message": "device_consumption_water entries require 'stat_consumption'",
                    }
                )

    return errors


def _appended_tail_indices(existing: list[Any], new: list[Any]) -> set[int]:
    """Return the indices in ``new`` that lie past the end of ``existing``.

    Per issue #1086, this builds the ``validate_only`` index set scoped to the
    appended tail of an append-only / shrink-only mutation, so pre-existing
    HA-validated siblings are not re-checked on every add/remove:

    - Append-only mutators (``_add_*``): returns indices of the new entries.
    - Shrink-only mutators (``_remove_*``): returns an empty set — nothing
      new to validate, and the surviving entries already passed HA validation.

    Refuses in-place mutators where ``len(new) == len(existing)`` but the
    contents differ — the appended-tail formula would yield an empty index
    set on a list whose entries actually changed, silently bypassing
    per-entry validation. Add explicit handling (e.g. an
    ``_indices_of_modified_entries`` helper) before introducing such a
    mutator; do not extend this one.
    """
    if len(new) == len(existing) and new != existing:
        raise_tool_error(
            create_error_response(
                ErrorCode.INTERNAL_ERROR,
                "_appended_tail_indices: in-place mutation detected "
                "(same length, different content) — the appended-tail "
                "validation heuristic only covers append-only / shrink-only "
                "mutators. Add explicit per-entry validation handling for "
                "in-place mutators before reusing this helper.",
                context={
                    "existing_len": len(existing),
                    "new_len": len(new),
                },
                suggestions=[
                    "If introducing a _replace_* / _update_* mutator, "
                    "compute the indices of modified entries explicitly "
                    "and pass those to _shape_check via validate_only.",
                ],
            )
        )
    return set(range(len(existing), len(new)))


class EnergyTools:
    """Energy Dashboard preference management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_manage_energy_prefs",
        tags={"Energy"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": False,
            "title": "Manage Energy Dashboard Preferences",
        },
    )
    @log_tool_usage
    async def ha_manage_energy_prefs(
        self,
        mode: Annotated[
            Literal["get", "set", "add_device", "remove_device", "add_source"],
            Field(
                description=(
                    "Operation mode. Primitives: 'get' reads the current prefs; "
                    "'set' writes a full prefs payload (per-top-level-key "
                    "full-replace). Convenience modes: 'add_device' / "
                    "'remove_device' / 'add_source' perform a single read-"
                    "modify-write atomically — no config_hash from the caller, "
                    "the tool fetches it fresh internally."
                )
            ),
        ],
        config: Annotated[
            dict[str, Any] | None,
            Field(
                description=(
                    "Full prefs payload for mode='set'. Must contain the "
                    "top-level keys you intend to replace: 'energy_sources', "
                    "'device_consumption', 'device_consumption_water'. Any "
                    "top-level key present in this payload REPLACES the "
                    "existing list entirely; any omitted key is preserved. "
                    "Call with mode='get' first, mutate the returned config, "
                    "then pass the whole object back. Ignored by convenience "
                    "modes."
                ),
                default=None,
            ),
        ] = None,
        config_hash: Annotated[
            str | dict[_PrefsKey, str] | None,
            Field(
                description=(
                    "Hash from a previous mode='get' call. REQUIRED for "
                    "mode='set' unless dry_run=True. Two forms: str (full-"
                    "blob lock) or dict (per-key lock, taken from the "
                    "config_hash_per_key field of mode='get'). See the tool "
                    "docstring for fail-closed semantics. Ignored by "
                    "convenience modes."
                ),
                default=None,
            ),
        ] = None,
        dry_run: Annotated[
            bool,
            Field(
                description=(
                    "If True, no write is performed. For mode='set': runs a "
                    "local shape check on the proposed config AND calls the "
                    "server's energy/validate against the CURRENT persisted "
                    "state (Home Assistant's validate endpoint cannot validate "
                    "an unsubmitted payload). For convenience modes: simulates "
                    "the mutation against a fresh read and reports what would "
                    "change without writing — but still raises "
                    "RESOURCE_ALREADY_EXISTS (duplicate add_device, or duplicate "
                    "add_source for solar/battery/gas), RESOURCE_NOT_FOUND "
                    "(missing remove_device), or VALIDATION_FAILED (post-mutator "
                    "shape error) when the proposed mutation is not applicable. "
                    "Default False."
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
                description=(
                    "Optional display name for mode='add_device'. Only used "
                    "when adding a new device entry; ignored otherwise."
                ),
                default=None,
            ),
        ] = None,
        included_in_stat: Annotated[
            str | None,
            Field(
                description=(
                    "Optional 'parent' statistic for mode='add_device'. Set "
                    "this to a statistic that already INCLUDES this device's "
                    "consumption (e.g., a whole-home or circuit-level meter "
                    "that this device feeds into). The Energy Dashboard will "
                    "subtract this device's reading from the parent so the "
                    "parent's contribution is not double-counted. Ignored "
                    "otherwise."
                ),
                default=None,
            ),
        ] = None,
        water: Annotated[
            bool,
            Field(
                description=(
                    "If True, mode='add_device' / 'remove_device' targets "
                    "'device_consumption_water' instead of 'device_consumption'. "
                    "Default False."
                ),
                default=False,
            ),
        ] = False,
        source: Annotated[
            dict[str, Any] | None,
            Field(
                description=(
                    "Single energy_sources entry for mode='add_source'. Must "
                    "contain 'type' (one of grid|solar|battery|gas) and the "
                    "type-specific required fields (e.g. solar/battery/gas "
                    "require 'stat_energy_from'). Note: HA Core's voluptuous "
                    "schema for grid sources requires the full field set "
                    "(cost_adjustment_day, stat_energy_to, stat_cost, "
                    "entity_energy_price, number_energy_price, "
                    "entity_energy_price_export, number_energy_price_export, "
                    "stat_compensation) — the local shape check is narrower, "
                    "so a minimal {'type': 'grid'} passes locally but "
                    "surfaces in post_save_validation_errors after writing. "
                    "Pass the unused fields as None to satisfy the server. "
                    "Required for mode='add_source'; ignored otherwise."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Manage the Home Assistant Energy Dashboard preferences.

        The Energy Dashboard configuration (grid/solar/battery/gas sources,
        individual device consumption sensors, cost tariffs, water) is stored
        in ``.storage/energy`` and not otherwise reachable via REST, services,
        or helper flows — this tool is the only way for agents to inspect or
        modify it.

        WHEN TO USE:
        - mode='get' / 'set': inspect or replace the full Energy Dashboard
          config. Use 'set' for bulk edits or anything touching multiple
          top-level keys at once.
        - mode='add_device' / 'remove_device': add or remove a single
          device-consumption entry. The tool performs a fresh read-modify-write
          internally; the caller does NOT manage config_hash. Use ``water=True``
          to target the water meter list instead of electricity.
        - mode='add_source': append a single entry to ``energy_sources`` (grid,
          solar, battery, or gas). Same atomic read-modify-write semantics.

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
        - ``config_hash`` accepts both a single ``str`` (full-blob lock) and
          a ``dict[_PrefsKey, str]`` keyed by top-level keys (per-key lock,
          taken from the ``config_hash_per_key`` field of the mode='get'
          response). The per-key form lets an agent submit only the top-
          level key it wants to change — set-equality between ``config``
          keys and dict keys is enforced, and any key outside the canonical
          set (typo, etc.) on either side is rejected with
          ``VALIDATION_FAILED`` rather than silently dropped (so an empty
          submission cannot succeed as a no-op). A per-key submission
          still fully replaces that key's value as the save endpoint
          requires. Mismatch on any locked key returns ``RESOURCE_LOCKED``
          with the offending keys in the response's top-level
          ``mismatched_keys`` (``create_error_response`` flattens the
          ``context`` dict onto the response root).
        - ``dry_run=True`` skips the hash check entirely for both forms;
          the per-key form is therefore silently accepted on dry runs even
          if its keys would mismatch the current state.
        - A local shape check runs before every write; malformed payloads
          are rejected with a ``shape_errors`` list.
        - After a successful write, the tool calls ``energy/validate`` and
          returns any residual issues as ``post_save_validation_errors`` in
          the response. These reflect semantic problems (missing stats, unit
          mismatches) that shape checks can't catch; the save persists
          regardless — correct the config and write again if needed.
        - The underlying save endpoint is admin-only. Non-admin tokens will
          receive an authorization error from Home Assistant.
        - Convenience modes are NOT idempotent: 'add_device' on an existing
          ``stat_consumption`` returns RESOURCE_ALREADY_EXISTS; 'remove_device'
          on a missing entry returns RESOURCE_NOT_FOUND. 'add_source' rejects
          duplicates by ``(type, stat_energy_from)`` for solar/battery/gas
          (RESOURCE_ALREADY_EXISTS); grid entries are appended without a
          duplicate check (multiple grid variants are legitimate, and grid
          has no single canonical uniqueness key) — the caller is responsible
          for de-duplicating grid sources.
        - Convenience modes do NOT bypass the local shape check on dry_run:
          ``dry_run=True`` still raises ``RESOURCE_ALREADY_EXISTS``
          (duplicate add_device / add_source), ``RESOURCE_NOT_FOUND``
          (missing remove_device), or ``VALIDATION_FAILED`` (post-mutator
          shape error) when the proposed mutation is not applicable. The
          mutator and shape check both run before the dry-run short-circuit.
        """
        if mode == "get":
            return await self._get_prefs()

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
        """Fetch current prefs and return them with a config_hash.

        On a Home Assistant instance that has never had the Energy Dashboard
        configured, ``energy/get_prefs`` returns ``ERR_NOT_FOUND "No prefs"``
        rather than an empty default. This method maps that case to the
        documented default preferences structure so the tool works uniformly
        on fresh installations.
        """
        try:
            result = await self._client.send_websocket_message(
                {
                    "type": "energy/get_prefs",
                }
            )

            if not result.get("success"):
                error_msg = str(result.get("error", ""))
                if _is_no_prefs_error(error_msg):
                    prefs = _default_prefs()
                    return {
                        "success": True,
                        "mode": "get",
                        "config": prefs,
                        "config_hash": compute_config_hash(prefs),
                        "config_hash_per_key": _compute_per_key_hashes(prefs),
                        "note": (
                            "Energy Dashboard has never been configured on "
                            "this instance; returning empty default."
                        ),
                    }
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to get energy prefs: {result.get('error', 'Unknown error')}",
                        context={"mode": "get"},
                    )
                )

            prefs = result.get("result") or _default_prefs()
            return {
                "success": True,
                "mode": "get",
                "config": prefs,
                "config_hash": compute_config_hash(prefs),
                "config_hash_per_key": _compute_per_key_hashes(prefs),
            }

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error getting energy prefs: {e}")
            exception_to_structured_error(
                e,
                context={"mode": "get"},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                ],
            )

    async def _dry_run(self, config: dict[str, Any]) -> dict[str, Any]:
        """Shape-check the proposed config and fetch current-state validate.

        Returns both error lists clearly labelled so agents can distinguish
        problems they're about to introduce (shape_errors) from pre-existing
        issues in the persisted state (current_state_validation_errors).
        """
        try:
            shape_errors = _shape_check(config)

            validate_result = await self._client.send_websocket_message(
                {
                    "type": "energy/validate",
                }
            )
            validate_warning: str | None = None
            if validate_result.get("success"):
                current_state_errors = _flatten_validation_errors(
                    validate_result.get("result", {})
                )
            else:
                validate_error = validate_result.get("error") or "unknown error"
                logger.warning(
                    f"energy/validate (current state) failed: {validate_error}"
                )
                current_state_errors = []
                validate_warning = (
                    f"energy/validate failed: {validate_error} — "
                    "current-state validation skipped"
                )

            response: dict[str, Any] = {
                "success": len(shape_errors) == 0,
                "mode": "set",
                "dry_run": True,
                "shape_errors": shape_errors,
                "current_state_validation_errors": current_state_errors,
                "message": (
                    "Shape OK. Note: HA's energy/validate cannot validate an "
                    "unsubmitted payload — current_state_validation_errors "
                    "reflects the CURRENT persisted config, not your proposal. "
                    "Semantic issues in the proposed config (missing stats, "
                    "wrong units) will surface in post_save_validation_errors "
                    "after an actual mode='set' write."
                    if not shape_errors
                    else f"{len(shape_errors)} shape error(s) — fix before writing."
                ),
            }
            if validate_warning is not None:
                response["partial"] = True
                response["warning"] = validate_warning
            return response

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error in energy prefs dry_run: {e}")
            exception_to_structured_error(
                e,
                context={"mode": "set", "dry_run": True},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify config shape matches energy/get_prefs response",
                ],
            )

    async def _set_prefs(
        self,
        config: dict[str, Any],
        config_hash: str | dict[_PrefsKey, str],
        *,
        current_prefs: dict[str, Any] | None = None,
        validate_only: dict[str, set[int]] | None = None,
    ) -> dict[str, Any]:
        """Shape-check → hash-check → save → post-save validate.

        Shape errors and hash mismatch fail closed. Post-save validation
        errors are reported in the response as a non-fatal warning; the
        save already succeeded.

        ``config_hash`` accepts two forms. A ``str`` locks against the
        full prefs blob (the original optimistic-locking contract). A
        ``dict[_PrefsKey, str]`` keyed by top-level keys locks each
        submitted key individually — set-equality between ``config`` and
        dict keys is enforced, and unknown keys on either side are
        rejected (``VALIDATION_FAILED``) so an empty submission cannot
        coincide as a no-op success. See the tool docstring for the full
        agent-facing contract.

        ``current_prefs`` is an optional caller-supplied snapshot. When
        provided, the internal re-read is skipped — the convenience-mode
        path uses this to avoid a second ``energy/get_prefs`` round trip
        per attempt (the snapshot was already fetched by ``_mutate_atomic``).
        Convenience modes always pass a ``str`` hash; the dict form is
        only reachable via direct mode='set' callers. The hash check still
        runs against the provided snapshot as a defensive guard.

        ``validate_only`` is forwarded to ``_shape_check`` and lets a caller
        scope the per-entry check to specific top-level keys / indices.
        Convenience-mode writes pass the appended tail indices so
        pre-existing (HA-validated) entries are not re-validated against the
        local schema — see issue #1086.
        """
        try:
            # 1. Shape check (fast local, fail closed)
            shape_errors = _shape_check(config, validate_only=validate_only)
            if shape_errors:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_FAILED,
                        f"Config shape invalid: {len(shape_errors)} error(s)",
                        context={
                            "mode": "set",
                            "shape_errors": shape_errors,
                        },
                        suggestions=[
                            "Fix the listed errors and retry",
                            "Call with dry_run=True to re-check without writing",
                        ],
                    )
                )

            # 2. Snapshot acquisition. Convenience modes pass their
            # already-fetched snapshot in to skip the re-read; external
            # mode='set' callers fall through to a fresh read here. Map
            # "No prefs" (never configured) to empty default so the
            # hash-check works on fresh installations too.
            if current_prefs is None:
                current_result = await self._client.send_websocket_message(
                    {
                        "type": "energy/get_prefs",
                    }
                )
                if current_result.get("success"):
                    current_prefs = current_result.get("result") or _default_prefs()
                else:
                    error = current_result.get("error") or "Unknown error"
                    if _is_no_prefs_error(str(error)):
                        current_prefs = _default_prefs()
                    else:
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.SERVICE_CALL_FAILED,
                                f"Failed to re-read prefs for hash check: {error}",
                                context={"mode": "set"},
                            )
                        )
                        # unreachable; appeases type checkers
                        current_prefs = {}

            if isinstance(config_hash, dict):
                # Per-key form: validate (and hence allow saving) only the
                # top-level keys whose hashes were supplied. Fail-closed on
                # unknown keys (no silent-drop) so a typo in both 'config'
                # and 'config_hash' cannot coincide as an empty no-op
                # success at the save endpoint.
                _valid = set(_PREFS_TOP_LEVEL_KEYS)
                invalid_config_keys = sorted(set(config) - _valid)
                invalid_hash_keys = sorted(set(config_hash) - _valid)
                if invalid_config_keys or invalid_hash_keys:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_FAILED,
                            "Unknown top-level key(s) in 'config' or "
                            "'config_hash' (per-key form)",
                            context={
                                "mode": "set",
                                "invalid_config_keys": invalid_config_keys,
                                "invalid_hash_keys": invalid_hash_keys,
                                "valid_keys": list(_PREFS_TOP_LEVEL_KEYS),
                            },
                            suggestions=[
                                "Use only 'energy_sources', "
                                "'device_consumption', or "
                                "'device_consumption_water'",
                            ],
                        )
                    )

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
                                "missing_in_hash": sorted(
                                    submitted_keys - hashed_keys
                                ),
                                "extra_in_hash": sorted(
                                    hashed_keys - submitted_keys
                                ),
                            },
                            suggestions=[
                                "Pass exactly one config_hash_per_key entry "
                                "per top-level key in 'config'",
                                "Use the str form of config_hash to lock "
                                "the full prefs blob instead",
                            ],
                        )
                    )

                # ``submitted_keys`` was validated against
                # ``_PREFS_TOP_LEVEL_KEYS`` above, so each ``key`` is in
                # fact a ``_PrefsKey``. Mypy can't narrow ``str`` from
                # ``sorted(set[str])`` automatically, hence the explicit
                # ``cast`` at the dict subscript.
                mismatched_keys = [
                    key
                    for key in sorted(submitted_keys)
                    if config_hash[cast(_PrefsKey, key)]
                    != compute_config_hash({key: current_prefs.get(key, [])})
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

            # 3. Save
            save_payload: dict[str, Any] = {"type": "energy/save_prefs"}
            for key in _PREFS_TOP_LEVEL_KEYS:
                if key in config:
                    save_payload[key] = config[key]

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
            post_save_errors: list[dict[str, str]] = []
            post_save_validate_error: str | None = None
            try:
                validate_result = await self._client.send_websocket_message(
                    {
                        "type": "energy/validate",
                    }
                )
                if validate_result.get("success"):
                    post_save_errors = _flatten_validation_errors(
                        validate_result.get("result", {})
                    )
                else:
                    post_save_validate_error = (
                        validate_result.get("error") or "unknown error"
                    )
                    logger.warning(
                        f"energy/validate (post-save) failed: {post_save_validate_error}"
                    )
            except Exception as e:
                # Post-save validate failure is non-fatal — the save itself
                # succeeded. Log and continue.
                logger.warning(f"Post-save energy/validate failed: {e}")
                post_save_validate_error = str(e)

            # 5. Compute new hash from the effective new state (current
            # merged with the submitted keys; save_prefs does not echo it
            # back).
            new_prefs = {**current_prefs}
            for key in _PREFS_TOP_LEVEL_KEYS:
                if key in config:
                    new_prefs[key] = config[key]
            new_hash = compute_config_hash(new_prefs)

            response: dict[str, Any] = {
                "success": True,
                "mode": "set",
                "config_hash": new_hash,
                "config_hash_per_key": _compute_per_key_hashes(new_prefs),
                "message": "Energy prefs updated.",
            }
            if post_save_errors:
                response["post_save_validation_errors"] = post_save_errors
                response["warning"] = (
                    f"Save succeeded, but the persisted config has "
                    f"{len(post_save_errors)} validation error(s). Review "
                    "and re-write if any relate to this change."
                )
            elif post_save_validate_error is not None:
                response["partial"] = True
                response["warning"] = (
                    f"Save succeeded, but post-save energy/validate "
                    f"failed: {post_save_validate_error}. The persisted "
                    "config has not been re-validated."
                )
            return response

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error setting energy prefs: {e}")
            exception_to_structured_error(
                e,
                context={"mode": "set"},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify token has admin privileges",
                    "Re-read prefs and retry with a fresh config_hash",
                ],
            )

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
        """Atomically add a device-consumption entry.

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
        """Atomically remove a device-consumption entry by ``stat_consumption``."""
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
        """Atomically append an entry to ``energy_sources``.

        The ``source`` dict is wrapped into a synthetic single-entry config
        for ``_shape_check`` reuse, which validates the type-specific
        required fields (e.g. ``stat_energy_from`` for solar/battery/gas).

        Duplicate semantics are asymmetric to ``_add_device`` because
        ``energy_sources`` does not expose a single uniqueness key across
        types: solar/battery/gas are keyed on ``stat_energy_from``, but
        ``grid`` entries can legitimately have multiple variants
        (different tariffs, multiple meters) where ``stat_energy_from``
        alone does not identify duplicates. We therefore reject duplicates
        by ``(type, stat_energy_from)`` for solar/battery/gas only and
        leave grid de-duplication to the caller.
        """
        if source is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "'source' is required when mode='add_source'",
                    context={"mode": "add_source"},
                    suggestions=[
                        "Pass source={'type': 'grid'|'solar'|'battery'|'gas', ...}",
                    ],
                )
            )

        # Reuse _shape_check by wrapping the single entry in the expected
        # top-level-list shape.
        wrapped = {"energy_sources": [source]}
        shape_errors = _shape_check(wrapped)
        if shape_errors:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_FAILED,
                    f"Source shape invalid: {len(shape_errors)} error(s)",
                    context={
                        "mode": "add_source",
                        "shape_errors": shape_errors,
                    },
                    suggestions=[
                        "Fix the listed errors and retry",
                        "solar/battery/gas need 'stat_energy_from'; grid needs "
                        "only 'type' for the local check, but HA Core's voluptuous "
                        "schema requires the full grid field set "
                        "(cost_adjustment_day, stat_energy_to, stat_cost, "
                        "entity_energy_price, number_energy_price, "
                        "entity_energy_price_export, number_energy_price_export, "
                        "stat_compensation) — pass them as None when unused or "
                        "the post-save validate will surface them.",
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

        Solar/battery/gas entries are keyed on
        ``(type, stat_energy_from)`` — duplicates raise
        ``RESOURCE_ALREADY_EXISTS``. Grid entries are appended without a
        duplicate check (multiple grid variants are legitimate, and grid
        does not have a single canonical uniqueness key — see
        ``_add_source`` docstring for rationale). The post-save
        ``energy/validate`` call still runs on the full payload as a
        backstop for whatever HA Core flags.
        """
        source_type = new_source.get("type")
        if source_type in {"solar", "battery", "gas"}:
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

    async def _mutate_atomic(
        self,
        *,
        mode: str,
        target_key: str,
        mutator: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
        dry_run: bool,
        preview_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Run convenience-mode read-modify-write with dry-run backstop and hash-conflict retry.

        Atomicity is with respect to the *entire* prefs snapshot, not just
        ``target_key``: ``_set_prefs`` validates the full ``config_hash``, so
        this helper retries on any concurrent modification — even one that
        touched an unrelated top-level key.

        Performs at most two attempts: on RESOURCE_LOCKED from ``_set_prefs``
        (concurrent modification between read and write), retries once with
        a fresh read. Other errors propagate immediately.

        For ``dry_run``: runs the mutator against a fresh read (so duplicate /
        not-found errors surface), shape-checks the resulting list as a
        backstop matching the real-run path, then returns ``preview_payload``
        plus the new shape — without writing. Short-circuits before the retry
        loop since dry_run never writes.

        The convenience path threads the freshly-fetched snapshot into
        ``_set_prefs`` so the inner ``energy/get_prefs`` re-read is skipped
        — halving the read cost on the happy path.
        """
        try:
            if dry_run:
                current = await self._get_prefs()
                current_config: dict[str, Any] = current["config"]
                existing_list = list(current_config.get(target_key, []))
                new_list = mutator(existing_list)

                # Backstop shape-check, mirroring the real-run path through
                # ``_set_prefs`` — keeps dry_run/real-run shape-equivalent if
                # the entry-construction logic ever changes. See
                # ``_appended_tail_indices`` for the validate_only contract.
                appended_indices = _appended_tail_indices(existing_list, new_list)
                shape_errors = _shape_check(
                    {target_key: new_list},
                    validate_only={target_key: appended_indices},
                )
                if shape_errors:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_FAILED,
                            f"Resulting {target_key} shape invalid: "
                            f"{len(shape_errors)} error(s)",
                            context={
                                "mode": mode,
                                "target_key": target_key,
                                "shape_errors": shape_errors,
                            },
                        )
                    )

                return {
                    "success": True,
                    "mode": mode,
                    "dry_run": True,
                    **preview_payload,
                    "current_count": len(existing_list),
                    "new_count": len(new_list),
                }

            max_attempts = 2
            for attempt in range(max_attempts):
                current = await self._get_prefs()
                current_config = current["config"]
                current_hash: str = current["config_hash"]

                existing_list = list(current_config.get(target_key, []))
                new_list = mutator(existing_list)

                partial_config = {target_key: new_list}
                # Per issue #1086: validate only the appended tail so a
                # pre-existing HA-validated entry cannot block an unrelated
                # add/remove. See ``_appended_tail_indices`` for the
                # validate_only contract.
                appended_indices = _appended_tail_indices(existing_list, new_list)
                try:
                    set_result = await self._set_prefs(
                        partial_config,
                        current_hash,
                        current_prefs=current_config,
                        validate_only={target_key: appended_indices},
                    )
                except ToolError as exc:
                    # _set_prefs raises ToolError(RESOURCE_LOCKED) on hash mismatch.
                    # Retry once with a fresh read in case of a benign race.
                    # raise_tool_error serialises the structured error as JSON in
                    # the exception message, so we parse rather than substring-match.
                    err_code: str | None = None
                    try:
                        parsed = json.loads(str(exc))
                    except (json.JSONDecodeError, TypeError, ValueError):
                        parsed = None
                    if isinstance(parsed, dict):
                        err_dict = parsed.get("error")
                        if isinstance(err_dict, dict):
                            err_code = err_dict.get("code")
                    if (
                        err_code == ErrorCode.RESOURCE_LOCKED.value
                        and attempt + 1 < max_attempts
                    ):
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
        except Exception as e:
            logger.error(f"Error in {mode} on {target_key}: {e}")
            exception_to_structured_error(
                e,
                context={"mode": mode, "target_key": target_key},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                ],
            )


def register_energy_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant energy preference management tools."""
    register_tool_methods(mcp, EnergyTools(client))
