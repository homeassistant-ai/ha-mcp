"""
Update management tools for Home Assistant MCP server.

This module provides tools for listing available updates, getting release notes,
and retrieving system version information.
"""

import logging
from typing import Annotated, Any, NoReturn

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..errors import ErrorCode, create_error_response
from .coercion import JSON_STRING_COERCION
from .core_release_analysis import core_release_analysis
from .helpers import (
    core_error_message,
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    ws_failure_code,
)
from .tool_hints import write_hints
from .update_entities import read_update_rows
from .update_release_notes import release_notes_fields

logger = logging.getLogger(__name__)

# Batch-install category split, mirroring the HA 2026.7 Updates page: the
# "Update all" button never covers core/OS/supervisor — those are applied
# individually, on purpose.
_INSTALL_ALL_CATEGORIES = ("addons", "hacs", "devices", "other")
_PROTECTED_UPDATE_CATEGORIES = ("core", "os", "supervisor")
_CATEGORY_ORDER = (*_PROTECTED_UPDATE_CATEGORIES, *_INSTALL_ALL_CATEGORIES)


# Core's refusals of an update service, answered per item.
_CORE_REFUSALS = frozenset({"home_assistant_error", "service_validation_error"})


def _pending(row: dict[str, Any]) -> bool:
    """An offered update that was not skipped."""
    return row["update_available"] and row.get("skipped_version") is None


class UpdateTools:
    """Update management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _list_updates(self, include_skipped: bool) -> dict[str, Any]:
        """Internal helper to list all update entities."""
        rows, warnings = await read_update_rows(self._client)
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
                    "design (mirrors the HA Updates page, which never bundles "
                    "core/OS/supervisor into 'Update all') — target those "
                    "individually via entity_ids"
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

    async def _call_update_service(
        self,
        action: str,
        targets: list[dict[str, Any]],
        backup: bool,
        version: str | None,
    ) -> tuple[list[dict[str, Any]], int]:
        """One ``update.<action>`` over WebSocket per target; Core's refusal is
        that item's error. A transport failure propagates and ends the batch."""
        results: list[dict[str, Any]] = []
        for row in targets:
            item: dict[str, Any] = {"entity_id": row["entity_id"]}
            data: dict[str, Any] = {}
            if action == "install":
                features = row["features"]
                item["backup"] = backup and (features is None or "backup" in features)
                if item["backup"]:
                    data["backup"] = True
                if version is not None:
                    data["version"] = version
            reply = await self._client.send_websocket_message(
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
        return results, sum(1 for r in results if r["success"])

    async def _execute_batch_action(
        self,
        action: str,
        entity_ids: list[str] | str | None,
        categories: list[str] | str | None,
        backup: bool,
        version: str | None,
    ) -> dict[str, Any]:
        """Resolve targets, apply the update service, and build the batch response."""
        ids, cats = self._validate_batch(action, entity_ids, categories, version)
        rows, warnings = await read_update_rows(self._client)
        by_id = {row["entity_id"]: row for row in rows}

        results: list[dict[str, Any]] = []
        if cats:
            targets = [
                row
                for cat in cats
                for row in rows
                if row["category"] == cat
                and _pending(row)
                and row["can_install"] is not False
            ]
        else:
            targets = [by_id[eid] for eid in ids if eid in by_id]
            # Core's service call no-ops silently on an entity it does not
            # have, which would read as success.
            results = [
                create_error_response(
                    ErrorCode.ENTITY_NOT_FOUND,
                    f"Update entity not found: {eid}",
                    context={"entity_id": eid},
                    suggestions=["Use ha_manage_updates() to list update entities"],
                )
                for eid in ids
                if eid not in by_id
            ]
        requested = len(targets) + len(results)

        call_results, succeeded = await self._call_update_service(
            action, targets, backup, version
        )
        results.extend(call_results)

        failed = requested - succeeded
        response: dict[str, Any] = {
            # Aggregate convention (matches ha_bulk-style tools): True only
            # when nothing failed — zero requested counts as success.
            "success": failed == 0,
            "action": action,
            "requested": requested,
            "succeeded": succeeded,
            "failed": failed,
            "results": results,
        }
        if not requested:
            response["note"] = "No matching pending updates to act on."
        elif action == "install" and succeeded:
            response["note"] = (
                "Confirm per entity with ha_manage_updates(action='get') that "
                f"installed_version reached {version or 'latest_version'}."
            )
        if warnings:
            response["warnings"] = warnings
        return response

    async def _known_repair_keys(self) -> set[tuple[str, str]] | None:
        """(domain, issue_id) of every listed Repairs issue, ignored ones included.

        None when the list cannot be read; the caller then lets Home Assistant
        judge each issue itself.
        """
        result = await self._client.send_websocket_message(
            {"type": "repairs/list_issues"}
        )
        if not result.get("success"):
            return None
        issues = (result.get("result") or {}).get("issues") or []
        return {
            (issue["domain"], issue["issue_id"])
            for issue in issues
            if isinstance(issue, dict)
            and isinstance(issue.get("domain"), str)
            and isinstance(issue.get("issue_id"), str)
        }

    async def _set_repairs_ignored(
        self, action: str, repairs: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        """Ignore or un-ignore Repairs issues, collecting per-item results."""
        items = repairs or []
        if not items or not all(
            isinstance(r, dict)
            and isinstance(r.get("domain"), str)
            and isinstance(r.get("issue_id"), str)
            for r in items
        ):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"'{action}' requires repairs: a list of "
                    "{'domain': ..., 'issue_id': ...} objects.",
                    suggestions=[
                        "Read domain and issue_id from ha_get_overview's repairs "
                        "(include_dismissed_repairs=True lists ignored ones too)",
                    ],
                    context={"action": action},
                )
            )
        ignore = action == "ignore_repair"
        # list_issues omits inactive issues, which the registry keeps (ignored
        # ones included) across a restart; un-ignoring those must still reach
        # Home Assistant, so only ignore is checked against the active list.
        known = await self._known_repair_keys() if ignore else None
        results: list[dict[str, Any]] = []
        succeeded = 0
        for item in items:
            domain, issue_id = item["domain"], item["issue_id"]
            context = {"domain": domain, "issue_id": issue_id}
            if known is not None and (domain, issue_id) not in known:
                results.append(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        f"Repairs issue not found: {domain}/{issue_id}",
                        context=context,
                        suggestions=["Use ha_get_overview() to list Repairs issues"],
                    )
                )
                continue
            reply = await self._client.send_websocket_message(
                {
                    "type": "repairs/ignore_issue",
                    "domain": domain,
                    "issue_id": issue_id,
                    "ignore": ignore,
                }
            )
            if reply.get("success"):
                results.append({"success": True, **context, "ignored": ignore})
                succeeded += 1
                continue
            results.append(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    str(reply.get("error") or "Home Assistant rejected the request"),
                    context=context,
                )
            )
        failed = len(items) - succeeded
        return {
            "success": failed == 0,
            "action": action,
            "requested": len(items),
            "succeeded": succeeded,
            "failed": failed,
            "results": results,
        }

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
                "(details/release notes for one update), 'install' (apply "
                "pending updates), 'skip' (hide the offered version), "
                "'clear_skipped' (re-offer a skipped version), 'ignore_repair' "
                "(dismiss Repairs issues, as the Repairs UI's Ignore does), or "
                "'unignore_repair' (show them again).",
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
            bool,
            Field(
                description="For install: back up first. Sent only to updates whose "
                "features include 'backup' (apps (add-ons)); each result item says "
                "whether it was sent. Without the component it is sent as asked "
                "and Home Assistant refuses it where unsupported.",
                default=False,
            ),
        ] = False,
        version: Annotated[
            str | None,
            Field(
                description="For install with entity_ids: the version to install "
                "instead of latest_version. Home Assistant refuses it for an "
                "update without the 'specific_version' feature.",
                default=None,
            ),
        ] = None,
        repairs: Annotated[
            list[dict[str, Any]] | None,
            JSON_STRING_COERCION,
            Field(
                description="For ignore_repair / unignore_repair: the Repairs "
                "issues to act on, as [{'domain': ..., 'issue_id': ...}] taken "
                "from ha_get_overview's repairs.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Manage Home Assistant updates (list, details, install, skip) and Repairs issues (ignore).

        Covers Core, OS, supervisor, apps (add-ons), device firmware, and
        Marketplace update entities, plus the Repairs issues in Settings > System > Repairs.
        Repairs are listed by ha_get_overview (not here); pass their domain and
        issue_id to 'ignore_repair' / 'unignore_repair'. In Read Only Mode the
        read actions ('list', 'get') stay available; write actions are blocked.

        An update carries Home Assistant's attributes (update_percentage shows
        an install's progress) plus platform, category, features (Home
        Assistant's feature names, such as 'install', 'backup',
        'specific_version', 'release_notes') and can_install. Without the
        ha_mcp_tools component, features and can_install are None and warnings
        says so; Home Assistant then refuses what an update does not support.

        'install' blocks until each update finishes (minutes). One that outlasts
        the request timeout ends the call with a connection timeout while it
        keeps running, and a Core update restarts Home Assistant; neither means
        failure, but later batch targets were not started. Confirm per entity
        with action='get'; finished updates drop out of 'list', which also
        returns ha_mcp_update — this MCP server's own update status {current,
        latest, update_available; python_required + upgrade_command if it needs another Python}.

        EXAMPLES:
        - Pre-update analysis: ha_manage_updates(action="get", entity_ids=["update.home_assistant_core_update"], include_release_notes=True)
        - Update everything pending in a category: ha_manage_updates(action="install", categories=["addons", "hacs"])
        - Dismiss a repair: ha_manage_updates(action="ignore_repair", repairs=[{"domain": "sun", "issue_id": "abc"}])
        """
        try:
            if action == "list":
                return await self._list_updates(bool(include_skipped))

            if action == "get":
                return await self._handle_get_action(
                    entity_ids, bool(include_release_notes)
                )

            if action in ("ignore_repair", "unignore_repair"):
                return await self._set_repairs_ignored(action, repairs)

            if action not in ("install", "skip", "clear_skipped"):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid action '{action}'. Must be 'list', 'get', "
                        "'install', 'skip', 'clear_skipped', 'ignore_repair', or "
                        "'unignore_repair'.",
                        context={"action": action},
                    )
                )

            return await self._execute_batch_action(
                action, entity_ids, categories, bool(backup), version
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
