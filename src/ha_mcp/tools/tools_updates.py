"""``ha_manage_updates``: Home Assistant's update entities as the Updates page
and the update dialog act on them, Supervisor's release channel, and Repairs
issues (ignore, un-ignore, fix)."""

import asyncio
import logging
from typing import Annotated, Any, NoReturn

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..client.exceptions import (
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
    HomeAssistantError,
)
from ..errors import ErrorCode, create_error_response
from .coercion import JSON_STRING_COERCION
from .component_api import UNKNOWN_COMMAND_CODE
from .core_release_analysis import core_release_analysis
from .helpers import (
    core_error_message,
    exception_to_structured_error,
    log_tool_usage,
    raise_core_refusal,
    raise_tool_error,
    register_tool_methods,
    ws_failure_code,
)
from .tool_hints import write_hints
from .update_channel import set_supervisor_channel, supervisor_channel
from .update_entities import mark_server_app, read_update_rows
from .update_release_notes import release_notes_fields
from .update_repairs import fix_repair, set_repairs_ignored

logger = logging.getLogger(__name__)

# The 2026.11 Updates page gives each integration its own update button and
# Core, OS and Supervisor none: each of those is installed from its own
# update dialog, so a batch install never covers them.
_INSTALL_ALL_CATEGORIES = ("addons", "hacs", "devices", "other")
_PROTECTED_UPDATE_CATEGORIES = ("core", "os", "supervisor")
_CATEGORY_ORDER = (*_PROTECTED_UPDATE_CATEGORIES, *_INSTALL_ALL_CATEGORIES)
_ACTIONS = (
    "list",
    "get",
    "refresh",
    "install",
    "skip",
    "clear_skipped",
    "set_channel",
    "ignore_repair",
    "unignore_repair",
    "fix_repair",
)


# Core's refusals of an update service, answered per item.
_CORE_REFUSALS = frozenset({"home_assistant_error", "service_validation_error"})
# The update dialog's backup toggle starts at Home Assistant's own
# backup-before-update setting for these categories, and off for the rest.
_BACKUP_OPTIONS = {
    "core": "core_backup_before_update",
    "os": "core_backup_before_update",
    "addons": "add_on_backup_before_update",
}
# How long the frontend's "Check for updates" (checkForEntityUpdates in
# src/data/update.ts) waits after the same service call: Supervisor's update
# entities check only after their coordinator's debounce, and nothing reports
# when they are done.
_SUPERVISOR_CHECK_WAIT = 15


def _pending(row: dict[str, Any]) -> bool:
    """An offered update that was not skipped."""
    return row["update_available"] and row.get("skipped_version") is None


def _server_restart_warning(row: dict[str, Any]) -> str:
    return (
        f"Installing {row['entity_id']} restarts this MCP server, which runs in "
        "that app, so the install call ends without an answer. Reconnect, then "
        "read the update again with action='get'."
    )


def _features(row: dict[str, Any]) -> list[str]:
    return row["features"] or []


def _follows_backup_setting(row: dict[str, Any]) -> bool:
    return row["category"] in _BACKUP_OPTIONS and "backup" in _features(row)


def _backup_for(
    row: dict[str, Any], backup: bool | None, settings: dict[str, Any]
) -> bool:
    """Whether the install asks ``row`` for a backup."""
    if row["features"] is None:
        return backup is True
    if "backup" not in row["features"]:
        return False
    if backup is not None:
        return backup
    option = _BACKUP_OPTIONS.get(row["category"])
    return option is not None and settings.get(option) is True


def _offers_install(row: dict[str, Any]) -> bool:
    """The dialog's install button is enabled (the frontend's
    ``updateButtonIsDisabled``): an update is pending, or the offered version
    is the skipped one."""
    latest = row.get("latest_version")
    return bool(row["update_available"]) or (
        latest is not None and row.get("skipped_version") == latest
    )


def _version_for(row: dict[str, Any], version: str | None) -> str | None:
    """The version the install asks for: the caller's, else the one the row
    offered when the update installs a chosen version and the dialog offers the
    install, as the dialog does; otherwise none, and Core decides."""
    if version is not None or "specific_version" not in _features(row):
        return version
    latest: str | None = row.get("latest_version")
    return latest if _offers_install(row) else None


def _install_request(
    row: dict[str, Any],
    backup: bool | None,
    version: str | None,
    settings: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The result item and the service data of the install of ``row``."""
    item: dict[str, Any] = {"entity_id": row["entity_id"]}
    data: dict[str, Any] = {}
    item["backup"] = _backup_for(row, backup, settings)
    if item["backup"]:
        data["backup"] = True
    if (sent := _version_for(row, version)) is not None:
        data["version"] = item["version"] = sent
    return item, data


def _resolve_targets(
    rows: list[dict[str, Any]], ids: list[str], cats: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The rows a batch acts on, and the not-found item of each id without one."""
    if cats:
        targets = [
            row
            for cat in cats
            for row in rows
            if row["category"] == cat
            and _pending(row)
            and row["can_install"] is not False
        ]
        return targets, []
    by_id = {row["entity_id"]: row for row in rows}
    # Core's service call no-ops silently on an entity it does not have,
    # which would read as success.
    missing = [
        create_error_response(
            ErrorCode.ENTITY_NOT_FOUND,
            f"Update entity not found: {eid}",
            context={"entity_id": eid},
            suggestions=["Use ha_manage_updates() to list update entities"],
        )
        for eid in ids
        if eid not in by_id
    ]
    return [by_id[eid] for eid in ids if eid in by_id], missing


def _unanswered_install(
    item: dict[str, Any], error: HomeAssistantConnectionError
) -> dict[str, Any]:
    """The item of an install Home Assistant was sent and did not answer: not
    a failure. After a timeout it runs on; after a dropped connection (a Core
    or OS update restarts Home Assistant) its outcome is unknown."""
    if error.timed_out:
        message = (
            "Home Assistant was sent this install and had not finished it "
            "within the request timeout; the install keeps running, and a Core "
            "or OS update restarts Home Assistant. Read the update again with "
            "action='get'; do not send the install again."
        )
    else:
        message = (
            "Home Assistant was sent this install and the connection dropped "
            "before it answered, so its outcome is unknown. Read the update "
            "with action='get': it is running if in_progress is true, done if "
            "installed_version changed, and otherwise it did not run and can "
            "be sent again."
        )
    return {"success": True, "partial": True, **item, "message": message}


def _unconfirmed_installs(
    results: list[dict[str, Any]], targets: list[dict[str, Any]]
) -> list[str]:
    """The installed updates whose readback does not show the asked version."""
    offered = {row["entity_id"]: row.get("latest_version") for row in targets}
    return [
        item["entity_id"]
        for item in results
        if item["success"]
        and (
            "installed_version" not in item
            or item["installed_version"]
            != item.get("version", offered.get(item["entity_id"]))
        )
    ]


class UpdateTools:
    """Update management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _mark_server_app(
        self, rows: list[dict[str, Any]], warnings: list[str]
    ) -> None:
        """Mark the row of the app this server runs in; warn when it is pending."""
        own = await mark_server_app(self._client, rows)
        if own is not None and _pending(own):
            warnings.append(_server_restart_warning(own))

    async def _send_server_app_last(
        self, targets: list[dict[str, Any]], warnings: list[str]
    ) -> None:
        """Move the app this server runs in to the end of an install, so its
        restart cuts off no other install, and warn."""
        own = await mark_server_app(self._client, targets)
        if own is not None:
            targets.remove(own)
            targets.append(own)
            warnings.append(_server_restart_warning(own))

    async def _list_updates(self, include_skipped: bool) -> dict[str, Any]:
        """Internal helper to list all update entities."""
        rows, warnings = await read_update_rows(self._client)
        await self._mark_server_app(rows, warnings)
        skipped_updates = [r for r in rows if r.get("skipped_version") is not None]
        available_updates = [r for r in rows if _pending(r)]

        all_updates = available_updates.copy()
        if include_skipped:
            all_updates.extend(skipped_updates)

        categories = {
            category: matching
            for category in _CATEGORY_ORDER
            if (matching := [u for u in all_updates if u["category"] == category])
        }

        result: dict[str, Any] = {
            "success": True,
            "updates_available": len(available_updates),
            "skipped_count": len(skipped_updates),
            "updates": all_updates,
            "categories": categories,
            "include_skipped": include_skipped,
        }
        if warnings:
            result["warnings"] = warnings
        if any(row["platform"] == "hassio" for row in rows) and (
            channel := await supervisor_channel(self._client)
        ):
            result["channel"] = channel

        # Surface the MCP server's OWN update status alongside HA's updates so the
        # model is more likely to mention "you're on X, but Y is out" — this tool
        # is HA-centric, but spreading the ha-mcp self-update signal across the
        # status surfaces raises the odds an AI relays it. ``get_update_field`` is
        # best-effort and never raises; see ha_mcp.update_check for details.
        from ..update_check import get_update_field

        mcp_update = await get_update_field()
        if mcp_update is not None:
            result["ha_mcp_update"] = mcp_update

        return result

    async def _get_update_details(
        self, entity_id: str, include_release_notes: bool = False
    ) -> dict[str, Any]:
        """Internal helper to get details for a specific update entity."""
        if not entity_id.startswith("update."):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Invalid entity_id format. Must start with 'update.'",
                    context={"entity_id": entity_id},
                )
            )

        [row], warnings = await read_update_rows(self._client, entity_id)
        await self._mark_server_app([row], warnings)
        result: dict[str, Any] = {"success": True, **row}
        if warnings:
            result["warnings"] = warnings

        # The row carries every attribute Core wrote, under Core's own keys.
        is_core = row["category"] == "core"
        result.update(
            await release_notes_fields(self._client, entity_id, row, is_core=is_core)
        )
        installed = row.get("installed_version")
        target = row.get("latest_version")
        if include_release_notes and is_core and installed and target:
            analysis = await core_release_analysis(self._client, installed, target)
            warnings = [*result.get("warnings", []), *analysis.pop("warnings")]
            result.update(analysis)
            if warnings:
                result["warnings"] = warnings

        return result

    def _validate_batch(
        self,
        action: str,
        entity_ids: list[str] | str | None,
        categories: list[str] | str | None,
        version: str | None,
    ) -> tuple[list[str], list[str]]:
        """Validate the batch parameters; returns the (entity_ids, categories)."""
        ids = [entity_ids] if isinstance(entity_ids, str) else list(entity_ids or [])
        cats = [categories] if isinstance(categories, str) else list(categories or [])

        if action != "install" and (not ids or cats):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"'{action}' requires entity_ids; categories is install-only.",
                )
            )
        if action == "install" and bool(ids) == bool(cats):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "'install' requires exactly one of entity_ids or categories.",
                )
            )
        if version is not None and (action != "install" or cats):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "version is install-only and needs entity_ids, not categories.",
                    context={"version": version},
                )
            )

        invalid = [e for e in ids if not e.startswith("update.")]
        if invalid:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "entity_ids must be 'update.' entities.",
                    context={"invalid_entity_ids": invalid},
                )
            )

        if not cats:
            return list(dict.fromkeys(ids)), []

        protected = sorted(set(cats) & set(_PROTECTED_UPDATE_CATEGORIES))
        unknown = sorted(
            set(cats) - set(_INSTALL_ALL_CATEGORIES) - set(_PROTECTED_UPDATE_CATEGORIES)
        )
        if protected or unknown:
            problems = []
            if protected:
                problems.append(
                    f"categories {protected} are excluded from batch install by "
                    "design (the HA Updates page gives Core, OS and Supervisor "
                    "no update button; each is installed from its own update "
                    "dialog) — target those individually via entity_ids"
                )
            if unknown:
                problems.append(f"unknown categories {unknown}")
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "; ".join(problems),
                    context={"allowed_categories": list(_INSTALL_ALL_CATEGORIES)},
                )
            )
        return [], list(dict.fromkeys(cats))

    async def _handle_get_action(
        self,
        entity_ids: list[str] | str | None,
        include_release_notes: bool,
    ) -> dict[str, Any]:
        """Validate the 'get' action's single-entity requirement and fetch details."""
        ids = [entity_ids] if isinstance(entity_ids, str) else list(entity_ids or [])
        if len(ids) != 1:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "'get' requires exactly one entity in entity_ids.",
                    context={"entity_ids": ids},
                )
            )
        return await self._get_update_details(ids[0], bool(include_release_notes))

    async def _backup_settings(
        self, targets: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], list[str]]:
        """Home Assistant's backup-before-update settings, read once when a
        target follows one; none, with a warning, when they cannot be read."""
        if not any(_follows_backup_setting(row) for row in targets):
            return {}, []
        reply = await self._client.send_websocket_message(
            {"type": "hassio/update/config/info"},
            expected_error_codes=frozenset({UNKNOWN_COMMAND_CODE}),
        )
        if reply.get("success"):
            return reply.get("result") or {}, []
        return {}, [
            "Home Assistant's backup-before-update setting could not be read "
            f"({core_error_message(reply.get('error', ''))}), so no backup was "
            "asked for."
        ]

    async def _send_update_call(
        self, action: str, row: dict[str, Any], data: dict[str, Any]
    ) -> dict[str, Any]:
        reply: dict[str, Any] = await self._client.send_websocket_message(
            {
                "type": "call_service",
                "domain": "update",
                "service": action,
                "target": {"entity_id": row["entity_id"]},
                "service_data": data,
                # Core answers an install once it has finished, so it gets
                # the client's request timeout rather than a fast command's.
                "_wait_timeout": self._client.timeout,
            },
            expected_error_codes=_CORE_REFUSALS,
        )
        return reply

    async def _call_update_service(
        self,
        action: str,
        targets: list[dict[str, Any]],
        backup: bool | None,
        version: str | None,
        settings: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """One ``update.<action>`` over WebSocket per target; Core's refusal is
        that item's error, and a restored target is refused unsent. An install
        that was sent and got no answer ends the batch as a partial item; any
        other transport failure propagates."""
        results: list[dict[str, Any]] = []
        for row in targets:
            item: dict[str, Any] = {"entity_id": row["entity_id"]}
            if row.get("restored"):
                # Core's service skips an entity whose integration is not
                # loaded and answers success.
                results.append(
                    create_error_response(
                        ErrorCode.ENTITY_UNAVAILABLE,
                        f"{row['entity_id']} is restored: its integration is not "
                        "loaded, so Home Assistant would skip it without acting.",
                        context=item,
                        suggestions=[
                            "Check its integration under Settings > Devices & services"
                        ],
                    )
                )
                continue
            data: dict[str, Any] = {}
            if action == "install":
                item, data = _install_request(row, backup, version, settings)
            try:
                reply = await self._send_update_call(action, row, data)
            except HomeAssistantCommandNotSent:
                raise
            except HomeAssistantConnectionError as e:
                if action != "install":
                    raise
                results.append(_unanswered_install(item, e))
                break
            if reply.get("success"):
                results.append({"success": True, **item})
                continue
            results.append(
                create_error_response(
                    ws_failure_code(reply),
                    core_error_message(reply.get("error", "")),
                    context={**item, "ha_error_code": reply.get("error_code")},
                )
            )
        return results

    async def _execute_batch_action(
        self,
        action: str,
        entity_ids: list[str] | str | None,
        categories: list[str] | str | None,
        backup: bool | None,
        version: str | None,
    ) -> dict[str, Any]:
        """Resolve targets, apply the update service, and build the batch response."""
        ids, cats = self._validate_batch(action, entity_ids, categories, version)
        rows, warnings = await read_update_rows(self._client)
        targets, results = _resolve_targets(rows, ids, cats)
        requested = len(targets) + len(results)

        settings: dict[str, Any] = {}
        if action == "install":
            await self._send_server_app_last(targets, warnings)
            if backup is None:
                settings, unread = await self._backup_settings(targets)
                warnings.extend(unread)
        call_results = await self._call_update_service(
            action, targets, backup, version, settings
        )
        results.extend(call_results)
        if any(r["success"] for r in call_results):
            warnings.extend(await self._read_back(action, call_results))

        failed = sum(1 for r in results if not r["success"])
        response: dict[str, Any] = {
            # Aggregate convention (matches ha_bulk-style tools): True only
            # when nothing failed — zero requested counts as success.
            "success": failed == 0,
            "action": action,
            "requested": requested,
            "succeeded": sum(1 for r in results if r["success"] and "partial" not in r),
            "failed": failed,
            "results": results,
        }
        if any("partial" in r for r in call_results):
            response["partial"] = True
            response["not_started"] = [
                row["entity_id"] for row in targets[len(call_results) :]
            ]
        if not requested:
            response["note"] = "No matching pending updates to act on."
        elif action == "install" and (
            unconfirmed := _unconfirmed_installs(call_results, targets)
        ):
            response["note"] = (
                "Confirm with ha_manage_updates(action='get') that "
                "installed_version reaches the version asked for: "
                f"{', '.join(unconfirmed)}."
            )
        if warnings:
            response["warnings"] = warnings
        return response

    async def _read_back(self, action: str, results: list[dict[str, Any]]) -> list[str]:
        """Add the fresh row to each successful item; the warning when the rows
        could not be read again."""
        try:
            rows, _ = await read_update_rows(self._client)
        except HomeAssistantError as e:
            return [
                f"The updates could not be read back after {action} ({e}); "
                "each result is what Home Assistant answered to the call."
            ]
        fresh = {row["entity_id"]: row for row in rows}
        for item in results:
            if item["success"] and (row := fresh.get(item["entity_id"])):
                item.update({**row, **item})
        return []

    async def _refresh_updates(self, include_skipped: bool) -> dict[str, Any]:
        """Ask every integration to check for updates now, then list them."""
        rows, _ = await read_update_rows(self._client)
        asked = [row for row in rows if not row.get("restored")]
        entity_ids = [row["entity_id"] for row in asked]
        if entity_ids:
            reply = await self._client.send_websocket_message(
                {
                    "type": "call_service",
                    "domain": "homeassistant",
                    "service": "update_entity",
                    "target": {"entity_id": entity_ids},
                    # Core answers once each entity has run its update; an
                    # entity whose coordinator debounces it checks later.
                    "_wait_timeout": self._client.timeout,
                },
                expected_error_codes=_CORE_REFUSALS,
            )
            if not reply.get("success"):
                raise_core_refusal("Home Assistant did not check for updates", reply)
            if any(row["platform"] == "hassio" for row in asked):
                await asyncio.sleep(_SUPERVISOR_CHECK_WAIT)
        result = await self._list_updates(include_skipped)
        result["refreshed"] = len(entity_ids)
        if not entity_ids:
            result["note"] = "Home Assistant has no update entities to check."
        return result

    async def _set_channel(self, channel: str | None) -> dict[str, Any]:
        """Set Supervisor's release channel and return the one it reports."""
        return await set_supervisor_channel(self._client, channel)

    def _handle_update_error(self, e: Exception) -> NoReturn:
        """Map a failure to a structured tool error; always raises."""
        logger.error(f"Failed to manage updates: {e}")
        exception_to_structured_error(
            e,
            suggestions=[
                "Check Home Assistant connection",
                "Use ha_manage_updates() to inspect available updates",
            ],
        )

    @tool(
        name="ha_manage_updates",
        tags={"System"},
        annotations=write_hints(
            "Manage Updates", destructive=True, idempotent=False, open_world=True
        ),
    )
    @log_tool_usage
    async def ha_manage_updates(
        self,
        action: Annotated[
            str,
            Field(
                description="'list' (all pending updates, default), 'get' "
                "(details/release notes for one update), 'refresh' (have every "
                "integration check for updates now, as the Updates page's "
                "'Check for updates' does, then list; about 15 seconds on a "
                "Supervisor install), 'install' (apply "
                "pending updates), 'skip' (hide the offered version), "
                "'clear_skipped' (re-offer a skipped version), 'set_channel' "
                "(Supervisor's release channel), 'ignore_repair' (dismiss "
                "Repairs issues, as the Repairs UI's Ignore does), "
                "'unignore_repair' (show them again), or 'fix_repair' (run a "
                "Repairs issue's fix flow, one step per call).",
                default="list",
            ),
        ] = "list",
        entity_ids: Annotated[
            list[str] | str | None,
            JSON_STRING_COERCION,
            Field(
                description="Update entity_id(s) to act on. 'get' takes exactly "
                "one; skip/clear_skipped require at least one; for install, "
                "mutually exclusive with categories.",
                default=None,
            ),
        ] = None,
        categories: Annotated[
            list[str] | str | None,
            JSON_STRING_COERCION,
            Field(
                description="For install: apply every pending update in these categories ('addons',"
                " 'hacs' (Marketplace repositories), 'devices', 'other'). As with the HA 'Update all' button, "
                "core/os/supervisor are excluded (target those individually via "
                "entity_ids) and skipped updates are never included, nor are "
                "updates whose can_install is false.",
                default=None,
            ),
        ] = None,
        include_skipped: Annotated[
            bool,
            Field(
                description="For list: include updates that have been skipped.",
                default=False,
            ),
        ] = False,
        include_release_notes: Annotated[
            bool,
            Field(
                description="For get on the Core update: read the breaking changes of "
                "every stable release after installed_version up to latest_version "
                "(monthly release posts and patch releases). Adds breaking_changes "
                "(entries with their integration domains and whether one is "
                "installed; versions_checked; versions_unverified, the releases "
                "that could not be read, for which no entries does not mean none; "
                "repairs, the Repairs issues Home Assistant gives a breaking "
                "version), multi_version_release_notes and installed_integrations.",
                default=False,
            ),
        ] = False,
        backup: Annotated[
            bool | None,
            Field(
                description="For install: back up first. Omitted, each update "
                "follows Home Assistant's own backup-before-update setting (one "
                "for Core and OS updates, one for app (add-on) updates), as the "
                "update dialog does. Sent only to updates whose features include "
                "'backup'; each result item says whether it was sent. Without "
                "the component it is sent only when given, and Home Assistant "
                "refuses it where unsupported.",
                default=None,
            ),
        ] = None,
        version: Annotated[
            str | None,
            Field(
                description="For install with entity_ids: the version to install "
                "instead of latest_version. Home Assistant refuses it for an "
                "update without the 'specific_version' feature. Omitted, such an "
                "update that is pending, or whose latest_version is the skipped "
                "one, is asked for the latest_version this call read, as the "
                "update dialog does.",
                default=None,
            ),
        ] = None,
        channel: Annotated[
            str | None,
            Field(
                description="For set_channel: the release channel Supervisor "
                "follows, such as 'stable' or 'beta'; Supervisor validates it. "
                "'list' returns the current one as channel where a Supervisor "
                "answers.",
                default=None,
            ),
        ] = None,
        repairs: Annotated[
            list[dict[str, Any]] | None,
            JSON_STRING_COERCION,
            Field(
                description="The Repairs issues to act on, as [{'domain': ..., "
                "'issue_id': ...}] taken from ha_get_overview's repairs. "
                "fix_repair takes exactly one.",
                default=None,
            ),
        ] = None,
        flow_id: Annotated[
            str | None,
            Field(
                description="For fix_repair: continue an open fix flow, the "
                "flow_id a previous call returned, with the same repairs item. "
                "With config or menu_choice it answers the step; alone it "
                "returns the step again.",
                default=None,
            ),
        ] = None,
        config: Annotated[
            dict[str, Any] | list[dict[str, Any]] | None,
            JSON_STRING_COERCION,
            Field(
                description="For fix_repair: the values for the form the flow is "
                "at ({} confirms a form without fields); a list answers that "
                "form and the ones after it, one object per form in order. "
                "Without flow_id it answers the new flow's first form. Home "
                "Assistant validates each answer.",
                default=None,
            ),
        ] = None,
        menu_choice: Annotated[
            str | None,
            Field(
                description="For fix_repair: the option to pick at a menu step. "
                "Refused when the step is not a menu or does not offer it.",
                default=None,
            ),
        ] = None,
        abort: Annotated[
            bool,
            Field(
                description="For fix_repair with flow_id: end the open fix flow "
                "without fixing; the issue stays.",
                default=False,
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Manage Home Assistant updates (list, get, refresh, install, skip, clear_skipped, set_channel) and Repairs issues (ignore, unignore, fix).

        Covers Core, OS, Supervisor, apps (add-ons), device firmware and
        Marketplace update entities, plus the issues under Settings > System >
        Repairs. Do not use it to find Repairs issues (ha_get_overview lists
        them) or to manage an app beyond its update (ha_manage_app).

        An update row is Home Assistant's state with all its attributes
        (update_percentage shows an install's progress) plus platform,
        category, features (Home Assistant's feature names) and can_install.
        Without the ha_mcp_tools component, features and can_install are None
        and warnings says so; Home Assistant then refuses what an update does
        not support. In Read Only Mode only 'list' and 'get' run.

        Supervisor's updates check a few seconds after Home Assistant asks
        them, and nothing reports when they are done, so 'refresh' reads the
        list after a fixed wait, as the Updates page does.

        'install' blocks until each update finishes, which takes minutes, and
        a Core or OS update restarts Home Assistant. An install that comes
        back without Home Assistant's answer is a partial item whose message
        says whether it keeps running or must be read again before a resend,
        and not_started lists the batch targets that were not sent. An update
        whose integration is not loaded (restored: true) is listed but not
        written to. When this server runs as an app, its own
        app's update (runs_this_server true) is sent last: installing it
        restarts this server, so that call ends without an answer; reconnect
        and read it with action='get'. Finished updates drop out of 'list',
        which also returns ha_mcp_update, this MCP server's own update status.

        A fix flow runs as the Repairs dialog runs it, until the result says
        the issue is fixed or the flow aborted.

        EXAMPLES:
        - Pre-update analysis: ha_manage_updates(action="get", entity_ids=["update.home_assistant_core_update"], include_release_notes=True)
        - Update everything pending in a category: ha_manage_updates(action="install", categories=["addons", "hacs"])
        - Dismiss a repair: ha_manage_updates(action="ignore_repair", repairs=[{"domain": "sun", "issue_id": "abc"}])
        - Fix a repair: ha_manage_updates(action="fix_repair", repairs=[{"domain": "sun", "issue_id": "abc"}]), then the same call with flow_id and config={}
        """
        try:
            if action == "list":
                return await self._list_updates(bool(include_skipped))

            if action == "get":
                return await self._handle_get_action(
                    entity_ids, bool(include_release_notes)
                )

            if action == "refresh":
                return await self._refresh_updates(bool(include_skipped))

            if action == "set_channel":
                return await self._set_channel(channel)

            if action in ("ignore_repair", "unignore_repair"):
                return await set_repairs_ignored(self._client, action, repairs)

            if action == "fix_repair":
                return await fix_repair(
                    self._client,
                    repairs,
                    flow_id=flow_id,
                    config=config,
                    menu_choice=menu_choice,
                    abort=bool(abort),
                )

            if action not in ("install", "skip", "clear_skipped"):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid action '{action}'. Must be one of: "
                        f"{', '.join(_ACTIONS)}.",
                        context={"action": action},
                    )
                )

            return await self._execute_batch_action(
                action, entity_ids, categories, backup, version
            )

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            self._handle_update_error(e)
            return (
                None  # exception_to_structured_error always raises; explicit for CodeQL
            )


def register_update_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant update management tools."""
    register_tool_methods(mcp, UpdateTools(client))
