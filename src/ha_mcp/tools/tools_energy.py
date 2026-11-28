"""Energy preferences through native Core APIs and optional Core schema validation.

Core owns payload validation, defaults and saved state. HA-MCP owns previews
and optimistic locking. Semantic validation is post-save because
energy/validate only examines persisted preferences.
"""

import logging
from collections import Counter
from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..errors import ErrorCode, create_error_response
from ..utils.config_hash import compute_config_hash
from .coercion import JSON_STRING_COERCION
from .core_contract import (
    command_payload,
    include_core_contract,
    validate_energy_proposal,
)
from .energy_preferences import (
    change_energy_suggestions,
    compute_per_key_hashes,
    get_energy_prefs,
    include_energy_statistics,
    include_energy_suggestions,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_core_refusal,
    raise_tool_error,
    register_tool_methods,
)
from .tool_hints import write_hints

logger = logging.getLogger(__name__)


def _device_uses(entries: list[Any], field: str) -> Counter[Any]:
    return Counter(
        entry[field]
        for entry in entries
        if isinstance(entry, dict)
        and "stat_consumption" in entry
        and isinstance(entry.get(field), str)  # Core refuses anything else
    )


def _new_device_repeats(config: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Meters or power sensors a submitted device list uses twice where the
    saved list did not. An HA-MCP policy: Core saves such a list, but the
    Energy panel's device editors leave out what another device of the list
    already uses, so the panel never builds one."""
    return [
        f"{key}: {value}"
        for key, entries in config.items()
        if isinstance(entries, list)
        for field in ("stat_consumption", "stat_rate")
        for value, n in _device_uses(entries, field).items()
        if n > 1 and n > _device_uses(current.get(key) or [], field)[value]
    ]


def _flatten_validation_errors(raw: Any) -> list[dict[str, Any]]:
    """Add paths for readability while preserving native issue objects as details."""

    def walk(value: Any, path: str) -> list[dict[str, Any]]:
        if not value:
            return []
        if isinstance(value, list):
            return [
                issue
                for index, item in enumerate(value)
                for issue in walk(item, f"{path}[{index}]")
            ]
        if isinstance(value, dict) and not any(
            key in value for key in ("type", "message", "issue_type")
        ):
            return [
                issue
                for key, item in value.items()
                for issue in walk(item, f"{path}.{key}" if path else key)
            ]
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
            Literal["get", "set", "dismiss_suggestions", "restore_suggestions"],
            Field(
                description=(
                    "'get' reads the preferences; 'set' saves the top-level "
                    "keys passed in config; 'dismiss_suggestions' / "
                    "'restore_suggestions' hide or bring back suggested meters "
                    "(statistic_ids) for the token's Home Assistant user, as "
                    "Settings > Energy does."
                )
            ),
        ],
        config: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Top-level keys to save for mode='set', each with its whole "
                    "new value: a list from mode='get' with entries added, "
                    "changed or removed. Keys left out are kept as they are."
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
                    "taken from the config_hash_per_key field of mode='get', with exactly "
                    "the keys in config). Pass the dict form as a native object, NOT a "
                    "JSON-encoded string — a stringified dict is treated as a full-blob "
                    "token and will report RESOURCE_LOCKED; clients that can only send "
                    "strings should use the str full-blob form."
                ),
                default=None,
            ),
        ] = None,
        dry_run: Annotated[
            bool,
            Field(
                description=(
                    "With mode='set', preview without saving. With the component, validates "
                    "the complete proposal through Core's registered save schema. "
                    "Without that capability, explicitly reports proposal validation "
                    "unavailable. Semantic energy/validate checks persisted state only. "
                    "The suggestion modes have no preview."
                ),
                default=False,
            ),
        ] = False,
        include_schema: Annotated[
            bool,
            Field(
                description="With mode='get', describe the running Core's save schema. Unavailable without the component."
            ),
        ] = False,
        include_statistics: Annotated[
            bool,
            Field(
                description="With mode='get', include native recorder metadata and resolved output units for all configured statistic references. Ignored for other modes."
            ),
        ] = False,
        include_suggestions: Annotated[
            bool,
            Field(
                description=(
                    "With mode='get', include the energy, gas and water meters "
                    "Settings > Energy suggests adding (energy_suggestions): each "
                    "statisticId with the slots it can fill, the statistics this "
                    "user dismissed, and editors, which names the source or device "
                    "kind and field each slot fills. Computed by the installed "
                    "frontend through the component; Home Assistant 2026.12 and later."
                )
            ),
        ] = False,
        statistic_ids: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Statistic IDs for mode='dismiss_suggestions' or "
                    "'restore_suggestions'. Needs the component and Home "
                    "Assistant 2026.12 or later."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Manage the Home Assistant Energy dashboard preferences: its energy
        sources (grid, solar, battery, gas, water, home) and its device
        consumption lists.

        WHEN NOT TO USE:
        - To create the statistics themselves: integrations and sensors
          provide them, and they must exist before they are referenced here.

        WHEN TO USE:
        - To add, change or remove one source or device: read its list with
          mode='get', edit it, and send that key back with its
          config_hash_per_key entry, as Settings > Energy does. A key with no
          entry there (nothing saved yet) is sent with the full config_hash.
        - To add a meter that is not in the dashboard yet: mode='get' with
          include_suggestions=True names the slots each suggested meter fills.

        RELATED TOOLS: Use ha_get_history(source="statistics") with the returned
        statistic IDs for consumption, totals, and trends. Metadata comes directly
        from the running Core, including stored and display units.

        CAVEATS:
        - Core replaces each submitted key whole: a list sent with one entry
          removes every other entry of that key.
        - A config_hash that no longer matches is refused with RESOURCE_LOCKED
          (the offending keys in mismatched_keys) and nothing is saved.
        - Writes need an administrator token; Home Assistant rejects the save
          otherwise.
        - After a save, the tool calls ``energy/validate`` and returns its
          issues (missing statistics, unit mismatches) as
          ``post_save_validation_errors``; the save stands regardless.

        EXAMPLES:
        - ha_manage_energy_prefs(mode="get", include_schema=True)
        - Add a gas device after a get: ha_manage_energy_prefs(mode="set", config={"device_consumption_gas": [<existing entries>, {"stat_consumption": "sensor.boiler_gas"}]}, config_hash={"device_consumption_gas": "<its config_hash_per_key value>"})
        """
        if mode == "get":
            result = await get_energy_prefs(self._client)
            if include_schema:
                await include_core_contract(self._client, result, "energy/save_prefs")
            if include_suggestions:
                await include_energy_suggestions(self._client, result)
            return (
                await include_energy_statistics(self._client, result)
                if include_statistics
                else result
            )

        if mode in ("dismiss_suggestions", "restore_suggestions"):
            if dry_run:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"mode='{mode}' has no preview; dry_run applies to mode='set'",
                        context={"mode": mode},
                    )
                )
            if not statistic_ids:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_MISSING_PARAMETER,
                        f"'statistic_ids' is required when mode='{mode}'",
                        context={"mode": mode},
                        suggestions=[
                            "Take statisticId values from ha_manage_energy_prefs(mode='get', include_suggestions=True)",
                        ],
                    )
                )
            return await change_energy_suggestions(
                self._client,
                "dismiss" if mode == "dismiss_suggestions" else "restore",
                statistic_ids,
            )

        if config is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "'config' is required when mode='set'",
                    context={"mode": mode},
                    suggestions=[
                        "Call ha_manage_energy_prefs(mode='get') first, edit the lists you change, pass those keys back",
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
            result.setdefault("warnings", []).append(
                f"Current-state validation failed: {failure}"
            )
        return result

    async def _set_prefs(
        self,
        config: dict[str, Any],
        config_hash: str | dict[str, str],
    ) -> dict[str, Any]:
        """Hash-check, submit to Core, then report authoritative saved state.

        Core validates every submitted entry. The ``config_hash`` from an
        earlier read is checked against the current prefs; the per-key form
        locks exactly the submitted keys.
        """
        try:
            save_payload = command_payload("energy/save_prefs", config)
            current_prefs: dict[str, Any] = (await get_energy_prefs(self._client))[
                "config"
            ]
            self._check_config_hash(config, config_hash, current_prefs)
            if repeats := _new_device_repeats(config, current_prefs):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "A device list would use the same meter or power sensor "
                        f"twice: {', '.join(repeats)}",
                        context={"mode": "set", "repeats": repeats},
                        suggestions=[
                            "Each device of a list has its own meter and power sensor, as in Settings > Energy",
                        ],
                    )
                )

            save_result = await self._client.send_websocket_message(save_payload)
            if not save_result.get("success"):
                raise_core_refusal(
                    "Failed to save energy prefs", save_result, context={"mode": "set"}
                )

            (
                post_save_errors,
                post_save_validate_error,
                native_validation,
            ) = await self._post_save_validate()

            # Core returns its full normalized preferences, including defaults
            # and coercions. Never predict the persisted state from the request.
            new_prefs = save_result.get("result")
            if not isinstance(new_prefs, dict):
                new_prefs = (await get_energy_prefs(self._client))["config"]

            response: dict[str, Any] = {
                "success": True,
                "mode": "set",
                "post_save_validation": native_validation,
                "config": new_prefs,
                "config_hash": compute_config_hash(new_prefs),
                "config_hash_per_key": compute_per_key_hashes(new_prefs),
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
        except Exception as e:
            logger.error("Error setting energy prefs", exc_info=True)
            exception_to_structured_error(
                e,
                context={"mode": "set"},
                suggestions=[
                    "Check Home Assistant connection",
                    "Re-read prefs and retry with a fresh config_hash",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    @staticmethod
    def _check_config_hash(
        config: dict[str, Any],
        config_hash: str | dict[str, str],
        current_prefs: dict[str, Any],
    ) -> None:
        """Verify ``config_hash`` against ``current_prefs``; raises
        ``ToolError`` on mismatch or malformed per-key input.

        A ``dict[str, str]`` locks exactly the submitted keys; a plain ``str``
        locks the full prefs blob.
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
                != (
                    compute_config_hash({key: current_prefs[key]})
                    if key in current_prefs
                    else None
                )
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
        """Call ``energy/validate`` and return ``(errors, failure, native result)``.

        A failed validate is non-fatal — a save it follows already succeeded —
        so the failure comes back as a message rather than raised.
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
        except Exception as e:
            # Post-save validate failure is non-fatal — the save itself
            # succeeded. Log and continue.
            logger.warning("Post-save energy/validate failed", exc_info=True)
            post_save_validate_error = str(e)
        return post_save_errors, post_save_validate_error, native_validation


def register_energy_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant energy preference management tools."""
    register_tool_methods(mcp, EnergyTools(client))
